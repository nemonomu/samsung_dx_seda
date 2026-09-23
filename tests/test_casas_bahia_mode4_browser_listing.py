import base64
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from seda.casas_bahia import browser_listing_url_first as browser
from seda.parsers import extract_next_data


URL = "https://www.casasbahia.com.br/tv/b?page=1"
PRICE_URL = "https://api.casasbahia.com.br/merchandising/oferta/v1/Preco/Oferta/PrecoVenda/"
FRAME = {"id": "top", "loaderId": "current"}


def product(product_id=10, sku=100, seller=None, title="Smart TV Samsung 55 DU8000"):
    value = {"id": product_id, "idSku": sku, "title": title, "href": f"/smart-tv/p/{sku}", "rating": 4.7}
    if seller is not None:
        value["lojista"] = seller
    return value


def offer(product_id=10, sku=100, seller=7, price=1000):
    return {"PrecoVenda": {"IdProduto": product_id, "IdSku": sku, "IdLojista": seller,
                           "PrecoDe": 1200, "Preco": price},
            "DescontoFormaPagamento": {"PrecoVendaComDesconto": price - 100, "DescricaoDesconto": "No Pix"},
            "Disponibilidade": {"Retira": True}}


def document(products=None, page=1, sort=None):
    query = {"page": page}
    if sort:
        query["sortby"] = sort
    payload = {"props": {"pageProps": {"initialState": {"search": {
        "query": query, "results": {"products": products if products is not None else [product()]}
    }}}}}
    return '<html><body><div id="preserved">other content</div><script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script></body></html>'


def event(method, request_id, **kwargs):
    params = {"requestId": request_id, "frameId": FRAME["id"], "loaderId": FRAME["loaderId"]}
    params.update(kwargs)
    return {"method": "Network." + method, "params": params}


def network_events(status=200, old_price=False):
    return [event("responseReceived", "doc", type="Document", response={"url": URL, "status": status}),
            event("loadingFinished", "doc"),
            event("requestWillBeSent", "price", type="Fetch", loaderId="old" if old_price else "current",
                  request={"url": PRICE_URL, "method": "POST"}),
            event("responseReceived", "price", type="Fetch", loaderId="old" if old_price else "current",
                  response={"url": PRICE_URL, "status": 200}),
            event("loadingFinished", "price")]


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def merge(self, raw=None, payloads=None, url=URL):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")):
            selected = [{"Ofertas": [offer()]}] if payloads is None else payloads
            return browser._enrich_document(raw or document(), url, selected)

    def test_preserves_html_and_source_join(self):
        raw = document([product(10, 100), product(20, 200, seller=8)])
        enriched, ids, count = self.merge(raw, [{"Ofertas": [offer(20, 200, 8), offer(10, 100)]}])
        self.assertIn('<div id="preserved">other content</div>', enriched)
        self.assertEqual((["100", "200"], 2), (ids, count))
        products = extract_next_data(enriched)["props"]["pageProps"]["initialState"]["search"]["results"]["products"]
        self.assertEqual(["7", "8"], [p["lojista"] for p in products])
        self.assertEqual(900, products[0]["price"]["currentPrice"])
        self.assertEqual("No Pix", products[0]["price"]["discountDescription"])
        self.assertNotIn("price", product())

    def test_batches_join_by_identity_not_order(self):
        raw = document([product(10, 100), product(20, 200)])
        _, _, count = self.merge(raw, [{"Ofertas": [offer(20, 200)]}, {"Ofertas": [offer()]}])
        self.assertEqual(2, count)

    def test_duplicate_identical_offer_allowed_conflict_is_optional(self):
        self.merge(payloads=[{"Ofertas": [offer(), offer()]}])
        with self.assertRaisesRegex(browser.EvidenceError, "conflicting_price_offers"):
            browser._normalize_offers([{"Ofertas": [offer(), offer(price=1100)]}])
        enriched, _, count = self.merge(payloads=[{"Ofertas": [offer(), offer(price=1100)]}])
        item = extract_next_data(enriched)["props"]["pageProps"]["initialState"]["search"]["results"]["products"][0]
        self.assertEqual(1, count)
        self.assertTrue(item["_casas_listing_price_pending"])

    def test_wrong_product_sku_or_seller_keeps_url_with_pending_price(self):
        for wrong in (offer(product_id=999), offer(sku=999), offer(seller=999)):
            with self.subTest(wrong=wrong):
                enriched, ids, count = self.merge(document([product(seller=7)]), [{"Ofertas": [wrong]}])
                item = extract_next_data(enriched)["props"]["pageProps"]["initialState"]["search"]["results"]["products"][0]
                self.assertEqual((["100"], 1), (ids, count))
                self.assertTrue(item["_casas_listing_price_pending"])

    def test_ambiguous_seller_is_pending(self):
        enriched, _, count = self.merge(payloads=[{"Ofertas": [offer(seller=7), offer(seller=8)]}])
        item = extract_next_data(enriched)["props"]["pageProps"]["initialState"]["search"]["results"]["products"][0]
        self.assertEqual(1, count)
        self.assertTrue(item["_casas_listing_price_pending"])
        self.assertTrue(item["_casas_listing_seller_pending"])

    def test_missing_price_is_pending(self):
        bad = offer()
        bad["PrecoVenda"]["Preco"] = None
        bad["DescontoFormaPagamento"] = {}
        enriched, _, count = self.merge(payloads=[{"Ofertas": [bad]}])
        item = extract_next_data(enriched)["props"]["pageProps"]["initialState"]["search"]["results"]["products"][0]
        self.assertEqual(1, count)
        self.assertTrue(item["_casas_listing_price_pending"])

    def test_page_sort_and_source_identity_rejected(self):
        for raw, url, code in ((document(page=2), URL, "requested_page_mismatch"),
                               (document(sort="relevancia"), URL + "&ordenacao=mais-vendidos", "requested_sort_mismatch"),
                               (document([product(), product()]), URL, "duplicate_listing_sku")):
            with self.subTest(code=code), self.assertRaisesRegex(browser.EvidenceError, code):
                self.merge(raw, url=url)

    def test_url_sku_wins_conflicting_source_alias_without_page_failure(self):
        raw = document([dict(product(), href="/smart-tv/p/999")])
        enriched, ids, count = self.merge(raw, [])
        item = extract_next_data(enriched)["props"]["pageProps"]["initialState"]["search"]["results"]["products"][0]
        self.assertEqual((["999"], 1), (ids, count))
        self.assertEqual("999", item["idSku"])
        self.assertEqual("999", item["sku"])
        self.assertNotIn("id", item)
        self.assertTrue(item["_casas_listing_product_id_quarantined"])
        self.assertTrue(item["_casas_listing_price_pending"])

    def test_sort_normalization(self):
        self.merge(document(sort="maisvendidos"), url=URL + "&ordenacao=mais-vendidos")

    def test_existing_relevance_filter_preserved(self):
        raw = document([product(), product(20, 200, title="Suporte para TV parede")])
        _, ids, count = self.merge(raw, [{"Ofertas": [offer(), offer(20, 200)]}])
        self.assertEqual((2, 1), (len(ids), count))

    def test_script_injection_is_json_escaped(self):
        raw = document([product(title="Smart TV </script> & teste")]).replace("</script> & teste", "\\u003c/script> & teste")
        enriched, _, _ = self.merge(raw)
        self.assertEqual(1, enriched.count("</script>"))
        data = extract_next_data(enriched)
        title = data["props"]["pageProps"]["initialState"]["search"]["results"]["products"][0]["title"]
        self.assertEqual("Smart TV </script> & teste", title)

    def test_invalid_ids_rejected(self):
        for value in (None, True, False, 1.5, 0, -1, "NaN", "١", [], {}):
            with self.subTest(value=value):
                self.assertEqual("", browser._positive_id(value))

    def test_price_coverage_missing_and_seller_mismatch_diagnostics(self):
        raw = document([product(10, 100, seller=7), product(20, 200), product(30, 300)])
        payload = {"Ofertas": [offer(10, 100, seller=8), offer(30, 301, seller=9)],
                   "sensitive_other_field": "must not be returned"}
        coverage = browser._price_coverage(raw, [payload])
        self.assertEqual(3, coverage["source_products"])
        self.assertEqual(2, coverage["price_offer_count"])
        self.assertEqual(3, coverage["missing_price_identity_count"])
        self.assertEqual(1, coverage["seller_mismatch_count"])
        self.assertEqual(["8"], coverage["seller_mismatches"][0]["offered_seller_ids"])
        self.assertEqual({"product_id": "20", "sku_id": "200", "explicit_seller_id": ""},
                         coverage["missing_price_identities"][1])
        self.assertEqual("301", coverage["related_offer_identities"][1]["offer_identities"][0]["sku_id"])
        self.assertNotIn("must not be returned", json.dumps(coverage))
        self.assertNotIn("title", json.dumps(coverage))

    def test_price_coverage_matched_and_ambiguous_counts(self):
        raw = document([product(10, 100), product(20, 200, seller=7)])
        coverage = browser._price_coverage(raw, [{"Ofertas": [offer(), offer(seller=8), offer(20, 200)]}])
        self.assertEqual(1, coverage["matched_source_products"])
        self.assertEqual(1, coverage["ambiguous_identity_count"])
        self.assertEqual(0, coverage["missing_price_identity_count"])


class NetworkEvidenceTests(unittest.TestCase):
    def evidence(self, messages):
        return browser._network_evidence(messages, FRAME, browser._request_identity(URL))

    def test_completed_current_navigation_required(self):
        docs, prices, completed = self.evidence(network_events())
        self.assertEqual(["price"], prices)
        self.assertEqual(1, len(docs))
        self.assertIn("doc", completed)
        self.assertEqual([], self.evidence(network_events(old_price=True))[1])
        self.assertEqual([], self.evidence(network_events()[:-1])[1])

    def test_wrong_frame_and_options_excluded(self):
        messages = network_events()
        messages[2]["params"]["request"]["method"] = "OPTIONS"
        self.assertEqual([], self.evidence(messages)[1])
        messages = network_events()
        messages[3]["params"]["frameId"] = "iframe"
        self.assertEqual([], self.evidence(messages)[1])

    def test_cache_serviceworker_wrong_page_excluded(self):
        for field in ("fromDiskCache", "fromServiceWorker"):
            messages = network_events()
            messages[3]["params"]["response"][field] = True
            self.assertEqual([], self.evidence(messages)[1])
        messages = network_events()
        messages[0]["params"]["response"]["url"] = URL.replace("page=1", "page=2")
        self.assertEqual([], self.evidence(messages)[0])

    def test_base64_response_body(self):
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"body": base64.b64encode(b"body").decode(), "base64Encoded": True}
        self.assertEqual("body", browser._response_body(driver, "id"))


class SessionTests(unittest.TestCase):
    def tearDown(self):
        browser.close_browser()

    def test_version_read_targets_selected_executable(self):
        with patch.object(browser.subprocess, "run", return_value=SimpleNamespace(stdout="153.0.8010.53")) as run:
            self.assertEqual(153, browser._chrome_major(r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
        command = run.call_args.args[0]
        self.assertTrue(any("chrome.exe" in part for part in command))
        if os.name == "nt":
            self.assertIn("Get-Item", command[-1])
            self.assertNotIn("--version", command)

    def test_start_matches_installed_major_and_checks_driver(self):
        fake = Mock()
        fake.capabilities = {"browserVersion": "153.0.1.2", "chrome": {"chromedriverVersion": "153.0.1.2"}}
        uc = SimpleNamespace(ChromeOptions=Mock(return_value=Mock()), Chrome=Mock(return_value=fake),
                             find_chrome_executable=lambda: "selected-chrome")
        with patch.dict("sys.modules", {"undetected_chromedriver": uc}), patch.object(Path, "is_file", return_value=True), patch.object(browser, "_chrome_major", return_value=153), patch.object(browser, "_owned_chrome_type", side_effect=lambda base: base):
            session = browser._BrowserSession()
            session.start()
            self.assertEqual(153, uc.Chrome.call_args.kwargs["version_main"])
            self.assertEqual("selected-chrome", uc.Chrome.call_args.kwargs["browser_executable_path"])
            session.close()
        fake.quit.assert_called_once()

    def test_mismatch_not_accepted(self):
        fake = Mock()
        fake.capabilities = {"browserVersion": "153.0.1.2", "chrome": {"chromedriverVersion": "154.0.1.2"}}
        uc = SimpleNamespace(ChromeOptions=Mock(return_value=Mock()), Chrome=Mock(return_value=fake),
                             find_chrome_executable=lambda: "selected-chrome")
        with patch.dict("sys.modules", {"undetected_chromedriver": uc}), patch.object(Path, "is_file", return_value=True), patch.object(browser, "_chrome_major", return_value=153), patch.object(browser, "_owned_chrome_type", side_effect=lambda base: base):
            result = browser.fetch_page(URL)
        self.assertFalse(result["success"])
        self.assertEqual("browser_driver_version_mismatch", result["error"])
        fake.quit.assert_called_once()

    def test_owned_driver_cleanup_is_idempotent(self):
        class FakeChrome:
            def __init__(self):
                self.quit_count = 0

            def quit(self):
                self.quit_count += 1

        OwnedChrome = browser._owned_chrome_type(FakeChrome)
        driver = OwnedChrome()
        driver.quit()
        driver.quit()
        driver.__del__()
        self.assertEqual(1, driver.quit_count)
        self.assertFalse(hasattr(FakeChrome, "__del__"))

    def test_owned_driver_destructor_handles_failed_initialization(self):
        class FakeChrome:
            def quit(self):
                raise OSError("invalid handle")

        driver = browser._owned_chrome_type(FakeChrome)()
        driver.__del__()
        driver.__del__()
        self.assertTrue(driver._casas_listing_quit_started)

    def test_reuses_owned_session_and_redacts_exception(self):
        session = Mock()
        session.fetch.return_value = {"success": True}
        with patch.object(browser, "_BrowserSession", return_value=session) as factory:
            self.assertTrue(browser.fetch_page(URL)["success"])
            self.assertTrue(browser.fetch_page(URL)["success"])
            factory.assert_called_once()
            session.fetch.side_effect = RuntimeError("sensitive exception body")
            result = browser.fetch_page(URL)
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual("browser_RuntimeError", result["error"])
        session.close.assert_called_once()

    def test_recoverable_failure_then_success_uses_two_navigation_traces(self):
        session = Mock()
        failure = {"success": False, "error": "document_not_completed", "trace": [{"status_code": 200, "price_coverage": {"missing_price_identity_count": 1}}]}
        success = {"success": True, "text": "new navigation only", "trace": [{"status_code": 200, "price_coverage": {"missing_price_identity_count": 0}}]}
        session.fetch.side_effect = [failure, success]
        with patch.object(browser, "_BrowserSession", return_value=session), patch.object(browser.time, "sleep") as sleep:
            result = browser.fetch_page(URL)
        self.assertTrue(result["success"])
        self.assertEqual("new navigation only", result["text"])
        self.assertEqual([1, 2], [item["navigation_attempt"] for item in result["trace"]])
        self.assertEqual([1, 0], [item["price_coverage"]["missing_price_identity_count"] for item in result["trace"]])
        self.assertEqual(2, session.fetch.call_count)
        sleep.assert_called_once_with(3)

    def test_two_recoverable_failures_stop_after_two_navigations(self):
        session = Mock()
        session.fetch.return_value = {"success": False, "error": "document_not_200", "trace": [{"status_code": 403}]}
        with patch.object(browser, "_BrowserSession", return_value=session), patch.object(browser.time, "sleep") as sleep:
            result = browser.fetch_page(URL)
        self.assertFalse(result["success"])
        self.assertEqual(2, session.fetch.call_count)
        self.assertEqual([1, 2], [item["navigation_attempt"] for item in result["trace"]])
        sleep.assert_called_once_with(3)

    def test_structural_failures_never_retry(self):
        for error in ("requested_page_mismatch", "requested_sort_mismatch", "missing_product_url_identity", "duplicate_listing_sku"):
            with self.subTest(error=error):
                browser.close_browser()
                session = Mock()
                session.fetch.return_value = {"success": False, "error": error, "trace": [{"status_code": 200}]}
                with patch.object(browser, "_BrowserSession", return_value=session), patch.object(browser.time, "sleep") as sleep:
                    result = browser.fetch_page(URL)
                self.assertFalse(result["success"])
                session.fetch.assert_called_once_with(URL, timeout=None)
                self.assertEqual([1], [item["navigation_attempt"] for item in result["trace"]])
                sleep.assert_not_called()

    def test_invalid_host_rejected_before_browser(self):
        with patch.object(browser, "_BrowserSession") as factory:
            self.assertFalse(browser.fetch_page("https://casasbahia.com.br.evil.example/tv/b")["success"])
        factory.assert_not_called()

    def test_current_completed_document_and_price_success(self):
        session = browser._BrowserSession()
        session.major = 153
        driver = Mock()
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": m})} for m in network_events()]]
        frames = iter([dict(FRAME, loaderId="prior"), FRAME])
        def cdp(command, params):
            if command == "Page.getFrameTree":
                return {"frameTree": {"frame": next(frames)}}
            if command == "Network.getResponseBody":
                return {"body": document() if params["requestId"] == "doc" else json.dumps({"Ofertas": [offer()]})}
            raise AssertionError(command)
        driver.execute_cdp_cmd.side_effect = cdp
        session.driver = driver
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            result = session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual("browser_ssr", result["method"])
        self.assertEqual(1, result["trace"][0]["parsed_rows"])
        self.assertEqual(1, result["trace"][0]["price_coverage"]["matched_source_products"])

    def test_completed_document_without_price_response_is_success(self):
        session = browser._BrowserSession()
        driver = Mock()
        messages = network_events()[:2]
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": m})} for m in messages]]
        frames = iter([dict(FRAME, loaderId="prior"), FRAME])
        def cdp(command, params):
            if command == "Page.getFrameTree":
                return {"frameTree": {"frame": next(frames)}}
            if command == "Network.getResponseBody":
                return {"body": document()}
            raise AssertionError(command)
        driver.execute_cdp_cmd.side_effect = cdp
        session.driver = driver
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            result = session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual(0, result["trace"][0]["price_response_count"])
        self.assertEqual(1, result["trace"][0]["price_pending_products"])
        self.assertEqual(1, result["trace"][0]["seller_pending_products"])
        item = extract_next_data(result["text"])["props"]["pageProps"]["initialState"]["search"]["results"]["products"][0]
        self.assertTrue(item["_casas_listing_price_pending"])

    def test_invalid_optional_price_body_is_ignored(self):
        session = browser._BrowserSession()
        driver = Mock()
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": m})} for m in network_events()]]
        frames = iter([dict(FRAME, loaderId="prior"), FRAME])
        def cdp(command, params):
            if command == "Page.getFrameTree":
                return {"frameTree": {"frame": next(frames)}}
            if command == "Network.getResponseBody":
                return {"body": document() if params["requestId"] == "doc" else "not-json"}
            raise AssertionError(command)
        driver.execute_cdp_cmd.side_effect = cdp
        session.driver = driver
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            result = session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual(1, result["trace"][0]["invalid_price_response_count"])
        self.assertEqual(1, result["trace"][0]["price_pending_products"])

    def test_403_not_success_and_response_not_read(self):
        session = browser._BrowserSession()
        driver = Mock()
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": m})} for m in network_events(status=403)]]
        driver.execute_cdp_cmd.side_effect = [{"frameTree": {"frame": dict(FRAME, loaderId="prior")}},
                                             {"frameTree": {"frame": FRAME}}]
        session.driver = driver
        with patch.object(browser.time, "monotonic", side_effect=[0, 31]):
            result = session.fetch(URL)
        self.assertFalse(result["success"])
        self.assertEqual("document_not_200", result["error"])
        self.assertEqual(403, result["status_code"])
        self.assertFalse(any(call.args[0] == "Network.getResponseBody" for call in driver.execute_cdp_cmd.call_args_list))

    def test_same_previous_loader_is_not_new_navigation(self):
        session = browser._BrowserSession()
        driver = Mock()
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": m})} for m in network_events()]]
        driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": FRAME}}
        session.driver = driver
        with patch.object(browser.time, "monotonic", side_effect=[0, 31]):
            result = session.fetch(URL)
        self.assertFalse(result["success"])
        self.assertEqual("new_document_navigation_not_observed", result["error"])


if __name__ == "__main__":
    unittest.main()
