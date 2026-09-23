"""Offline mode-3 tests. All network/browser launches are mocked."""

from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import Mock, patch

# Production test runners also set this before importing any repo modules.
os.environ.setdefault("SEDA_ENV_PATH", str(Path(__file__).with_name("DISABLED_ENV_NO_FILE")))

try:
    from seda.casas_bahia import browser_api as api
except ImportError:
    spec = importlib.util.spec_from_file_location("seda.casas_bahia.browser_api", Path(__file__).with_name("browser_api.py"))
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
from seda.casas_bahia import browser_listing, price_api, search_api
from seda.parsers import extract_next_data


URL = "https://www.casasbahia.com.br/tv/b?page=1"
FRAME = {"id": "top", "loaderId": "current"}
SEARCH = "https://api-partner-prd.casasbahia.com.br/api/v3/web/busca?page=1&sortby=maisvendidos"


def product(sku=100, product_id=10, seller=None):
    value = {"id": product_id, "sku": sku, "title": "Smart TV Samsung 55 DU8000", "url": f"/smart-tv/p/{sku}"}
    if seller is not None:
        value["lojista"] = seller
    return value


def offer(sku=100, product_id=10, seller=7, price=900):
    return {"PrecoVenda": {"IdProduto": product_id, "IdSku": sku, "IdLojista": seller,
                           "PrecoDe": 1200, "Preco": price},
            "Disponibilidade": {"IdSku": sku, "IdLojista": seller}}


def event(name, request_id="r1", **kwargs):
    params = {"requestId": request_id, "frameId": FRAME["id"], "loaderId": FRAME["loaderId"]}
    params.update(kwargs)
    return {"method": "Network." + name, "params": params}


def events(url=SEARCH, method="GET", body=None, status=200):
    request = {"url": url, "method": method}
    if body is not None:
        request["postData"] = json.dumps(body)
    return [event("requestWillBeSent", request=request, type="Fetch"),
            event("responseReceived", response={"url": url, "status": status}, type="Fetch"),
            event("loadingFinished")]


def response(data, status=200):
    return ({"status_code": status, "ok": status == 200, "json": True, "elapsed_seconds": 0.1,
             **({"error": "api_http_not_200"} if status != 200 else {})}, data)


class ContextTests(unittest.TestCase):
    def context(self, data=None, url=URL):
        return api._parser_context(data if data is not None else {"products": [product()]}, api.browser_listing._request_identity(url))

    def test_missing_page_is_explicit_request_context_only(self):
        source = {"products": [product()]}
        copied, evidence = self.context(source)
        self.assertNotIn("queries", source)
        self.assertNotIn("idSku", source["products"][0])
        self.assertEqual("1", copied["queries"]["page"])
        self.assertEqual(100, copied["products"][0]["idSku"])
        self.assertFalse(evidence["response_page_present"])
        self.assertIsNone(evidence["reported_page"])
        self.assertEqual("observed_request_only", evidence["page_evidence_source"])
        self.assertTrue(evidence["parser_page_context_injected"])

    def test_response_page_mismatch_rejected(self):
        with self.assertRaisesRegex(browser_listing.EvidenceError, "response_page_mismatch"):
            self.context({"queries": {"page": 2}, "products": [product()]})

    def test_response_page_match_has_server_evidence(self):
        _, evidence = self.context({"queries": {"page": 1}, "products": [product()]})
        self.assertTrue(evidence["response_page_present"])
        self.assertFalse(evidence["parser_page_context_injected"])

    def test_bsr_sort_missing_is_request_context_only(self):
        copied, evidence = self.context(url=URL + "&ordenacao=mais-vendidos")
        self.assertEqual("maisvendidos", copied["queries"]["sortby"])
        self.assertTrue(evidence["parser_sort_context_injected"])

    def test_bsr_sort_explicit_mismatch_rejected(self):
        with self.assertRaisesRegex(browser_listing.EvidenceError, "response_sort_mismatch"):
            self.context({"products": [product()], "queries": {"sortby": "menorpreco"}}, URL + "&ordenacao=mais-vendidos")

    def test_source_alias_conflict_rejected(self):
        with self.assertRaisesRegex(browser_listing.EvidenceError, "conflicting_source_sku_aliases"):
            self.context({"products": [dict(product(), idSku=101)]})

    def test_source_alias_zero_and_bool_rejected(self):
        for bad in (0, True, {}, [], "bad"):
            with self.subTest(bad=bad), self.assertRaisesRegex(browser_listing.EvidenceError, "listing_identity_invalid"):
                self.context({"products": [dict(product(), idSku=bad)]})

    def test_url_sku_mismatch_rejected(self):
        with self.assertRaisesRegex(browser_listing.EvidenceError, "listing_identity_invalid"):
            self.context({"products": [dict(product(), url="/wrong/p/999")]})

    def test_duplicate_sku_rejected(self):
        with self.assertRaisesRegex(browser_listing.EvidenceError, "duplicate_listing_sku"):
            self.context({"products": [product(), product()]})

    def test_non_mapping_queries_has_no_server_echo(self):
        for query in (None, [], "unrelated", 2):
            with self.subTest(query=query):
                _, evidence = self.context({"queries": query, "products": [product()]})
                self.assertFalse(evidence["response_queries_is_mapping"])
                self.assertFalse(evidence["response_page_present"])
                self.assertEqual("observed_request_only", evidence["page_evidence_source"])

    def test_invalid_products_rejected(self):
        for bad in ({"products": []}, {"products": [None]}):
            with self.subTest(bad=bad), self.assertRaises(browser_listing.EvidenceError):
                self.context(bad)


class NetworkTests(unittest.TestCase):
    def evidence(self, messages=None, url=SEARCH, method="GET", body=None):
        return api._network_evidence(messages if messages is not None else events(), FRAME, url, method, body)

    def test_probe_accepts_http200_without_loading_finished(self):
        self.assertTrue(self.evidence()["request_response_verified"])
        self.assertTrue(self.evidence(events()[:-1])["request_response_verified"])

    def test_other_page_or_endpoint_rejected(self):
        for bad in (SEARCH.replace("page=1", "page=2"), SEARCH.replace("page=1&", ""),
                    SEARCH.replace("api-partner-prd.casasbahia.com.br", "example.invalid"),
                    SEARCH.replace("/api/v3/web/busca", "/api/v3/web/other")):
            with self.subTest(bad=bad):
                self.assertFalse(self.evidence(events(url=bad))["request_response_verified"])

    def test_probe_does_not_add_sort_or_business_query_matching_gate(self):
        for observed in (SEARCH.replace("maisvendidos", "menorpreco"), SEARCH + "&regionid=other"):
            with self.subTest(observed=observed):
                self.assertTrue(self.evidence(events(url=observed))["request_response_verified"])

    def test_probe_does_not_require_network_frame_or_loader_fields(self):
        for field in ("frameId", "loaderId"):
            value = events()
            value[0]["params"][field] = "other"
            value[1]["params"].pop(field)
            self.assertTrue(self.evidence(value)["request_response_verified"])

    def test_preflight_not_accepted_as_get(self):
        self.assertFalse(self.evidence(events(method="OPTIONS"))["request_response_verified"])

    def test_probe_does_not_add_cache_or_service_worker_rejection(self):
        for field in ("fromDiskCache", "fromServiceWorker"):
            value = events()
            value[1]["params"]["response"][field] = True
            self.assertTrue(self.evidence(value)["request_response_verified"])

    def test_probe_post_evidence_does_not_require_cdp_post_body(self):
        body = {"produtos": [{"idProduto": 10}], "skus": []}
        self.assertTrue(self.evidence(events(method="POST", body=body), method="POST", body=body)["request_response_verified"])
        self.assertTrue(self.evidence(events(method="POST"), method="POST", body=body)["request_response_verified"])

    def test_probe_accepts_multiple_matching_successful_requests(self):
        value = events()
        more = events()
        for item in more:
            item["params"]["requestId"] = "r2"
        self.assertTrue(self.evidence(value + more)["request_response_verified"])

    def test_extra_info_http200_is_matching_response_evidence(self):
        value = [events()[0], event("responseReceivedExtraInfo", statusCode=200)]
        self.assertTrue(self.evidence(value)["request_response_verified"])

    def test_http200_must_belong_to_selected_request_not_options(self):
        for value in ([events()[0], event("responseReceivedExtraInfo", "other", statusCode=200)],
                      [event("requestWillBeSent", request={"url": SEARCH, "method": "OPTIONS"}, type="Fetch"),
                       event("responseReceivedExtraInfo", statusCode=200)],
                      events(status=403), events(status=0)):
            with self.subTest(value=value):
                self.assertFalse(self.evidence(value)["request_response_verified"])

    def test_top_document_navigation_counted(self):
        value = events() + [event("requestWillBeSent", "doc", loaderId="new", type="Document", request={"url": URL, "method": "GET"})]
        self.assertEqual(1, self.evidence(value)["document_requests_observed"])

    def test_evidence_does_not_expose_url_or_headers_or_postdata(self):
        value = events()
        value[0]["params"]["request"]["headers"] = {"private-test": "do-not-output"}
        public = json.dumps(self.evidence(value))
        for forbidden in ("https://", "do-not-output", "headers", "postData"):
            self.assertNotIn(forbidden, public)

    def test_browser_fetch_passes_only_given_browser_headers(self):
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": FRAME}}
        driver.execute_async_script.return_value = {"status": 200, "ok": True, "json": True, "data": {"products": []}, "elapsed_seconds": 0.1}
        messages = events(url=search_api.SEARCH_URL + "?page=1")
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": message})} for message in messages]]
        safe, data = api._browser_fetch(driver, search_api.SEARCH_URL, {"page": 1}, "GET", {"accept": "*/*"}, None, 10, FRAME)
        self.assertNotIn("error", safe)
        self.assertEqual({"products": []}, data)
        self.assertEqual(30, driver.set_script_timeout.call_args.args[0])
        self.assertEqual(25, driver.execute_async_script.call_args.args[-1])
        self.assertIn("credentials: 'same-origin'", driver.execute_async_script.call_args.args[0])

    def test_browser_fetch_probe_does_not_precheck_bootstrap_document(self):
        driver = Mock()
        current = dict(FRAME, loaderId="new-but-stable")
        driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": current}}
        driver.execute_async_script.return_value = {"status": 200, "ok": True, "json": True,
                                                    "data": {"products": []}, "elapsed_seconds": 0.1}
        messages = events(url=search_api.SEARCH_URL + "?page=1")[:-1]
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": item})} for item in messages]]
        safe, _ = api._browser_fetch(driver, search_api.SEARCH_URL, {"page": 1}, "GET", {}, None, 120, FRAME)
        self.assertNotIn("error", safe)
        self.assertEqual(30, driver.set_script_timeout.call_args.args[0])
        self.assertEqual(25, driver.execute_async_script.call_args.args[-1])
        self.assertEqual(2, driver.get_log.call_count)

    def test_browser_fetch_document_request_is_rejected_even_if_frame_stable(self):
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": FRAME}}
        driver.execute_async_script.return_value = {"status": 200, "ok": True, "json": True,
                                                    "data": {"products": []}, "elapsed_seconds": 0.1}
        messages = events(url=search_api.SEARCH_URL + "?page=1") + [event(
            "requestWillBeSent", "doc", type="Document", request={"url": URL, "method": "GET"})]
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": item})} for item in messages]]
        safe, _ = api._browser_fetch(driver, search_api.SEARCH_URL, {"page": 1}, "GET", {}, None, 10, FRAME)
        self.assertEqual("document_navigation_during_api_call", safe["error"])

    def test_browser_fetch_document_change_is_rejected(self):
        driver = Mock()
        driver.execute_cdp_cmd.side_effect = [{"frameTree": {"frame": FRAME}}, {"frameTree": {"frame": dict(FRAME, loaderId="new")}}]
        driver.execute_async_script.return_value = {"status": 403, "ok": False, "json": False}
        driver.get_log.return_value = []
        safe, _ = api._browser_fetch(driver, search_api.SEARCH_URL, {"page": 1}, "GET", {}, None, 10, FRAME)
        self.assertEqual("document_navigation_during_api_call", safe["error"])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.stack = []
        for context in (patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV", "SEDA_CASAS_BAHIA_SEARCH_RETRIES": "2", "SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "0"}),
                        patch("requests.sessions.Session.request", side_effect=AssertionError("Python network forbidden")),
                        patch("sys.stdout", new_callable=io.StringIO),
                        patch.object(api.time, "sleep")):
            self.stack.append(context.start())
            self.addCleanup(context.stop)
        self.browser = Mock()
        self.browser.major = 153
        self.browser.fetch.return_value = {"success": True, "status_code": 200}
        self.browser.driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": FRAME}}
        with patch.object(api, "ProbeBrowserSession", return_value=self.browser):
            self.session = api._APISession()

    def good(self, sku=100, product_id=10, queries=None):
        data = {"products": [product(sku, product_id)]}
        if queries is not None:
            data["queries"] = queries
        return [response(data), response({"Ofertas": [offer(sku, product_id)]})]

    def test_bootstrap_once_then_two_api_calls_per_page(self):
        with patch.object(api, "_browser_fetch", side_effect=self.good() + self.good(200, 20)) as fetch:
            first = self.session.fetch(URL)
            second = self.session.fetch(URL.replace("page=1", "page=2"))
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(1, self.browser.fetch.call_count)
        self.assertEqual(4, fetch.call_count)
        self.assertEqual(["GET", "POST", "GET", "POST"], [call.args[3] for call in fetch.call_args_list])
        self.browser.driver.get.assert_not_called()
        payload = extract_next_data(first["text"])
        result = payload["props"]["pageProps"]["initialState"]["search"]["results"]["products"][0]
        self.assertEqual("7", result["lojista"])
        self.assertEqual(900, result["price"]["currentPrice"])

    def test_probe_limits_ignore_generic_timeouts_and_pin_first_navigation(self):
        with patch.dict(os.environ, {"SEDA_TIMEOUT": "120"}), \
                patch.object(api, "_browser_fetch", side_effect=self.good()) as fetch:
            self.assertTrue(self.session.fetch(URL, timeout=1)["success"])
        self.browser.fetch.assert_called_once_with(URL, timeout=45)
        self.assertEqual([25, 25], [call.args[6] for call in fetch.call_args_list])

    def test_probe_search_params_fixed_and_environment_not_mutated(self):
        overrides = {
            "SEDA_CASAS_BAHIA_RESULTS_PER_PAGE": "99",
            "SEDA_CASAS_BAHIA_VARIANT_CONFIGURATION": "other",
            "SEDA_CASAS_BAHIA_REGION_ID": "999999",
            "SEDA_CASAS_BAHIA_USER_ID": "synthetic-user",
            "SEDA_CASAS_BAHIA_SESSION_ID": "synthetic-old-session",
        }
        with patch.dict(os.environ, overrides), \
                patch.object(api, "_browser_fetch", side_effect=self.good() + self.good(200, 20)) as fetch:
            self.assertTrue(self.session.fetch(URL)["success"])
            self.assertTrue(self.session.fetch(URL.replace("page=1", "page=2"))["success"])
            self.assertEqual(overrides, {name: os.environ[name] for name in overrides})
        first, first_price, second, second_price = [call.args[2] for call in fetch.call_args_list]
        for params in (first, second):
            self.assertEqual("20", str(params["resultsperpage"]))
            self.assertEqual("q2", params["variantconfiguration"])
            self.assertEqual("126000", str(params["regionid"]))
            self.assertEqual("", params["userid"])
            self.assertEqual(str(uuid.UUID(params["sessionid"])), params["sessionid"])
        self.assertEqual(first["sessionid"], second["sessionid"])
        self.assertNotEqual(overrides["SEDA_CASAS_BAHIA_SESSION_ID"], first["sessionid"])
        self.assertEqual(["1", "2"], [str(first["page"]), str(second["page"])])
        self.assertEqual("126000", str(first_price["IdRegiao"]))
        self.assertEqual("126000", str(second_price["IdRegiao"]))

    def test_request_parameter_sources_are_not_mutated(self):
        search_source = {"page": "1", "terms": "tv", "resultsperpage": "88", "regionid": "other"}
        price_source = {"IdRegiao": "other", "composicao": "DescontoFormaPagamento,MelhoresParcelamentos"}
        expected_search, expected_price = deepcopy(search_source), deepcopy(price_source)
        with patch.object(search_api, "_params", return_value=search_source), \
                patch.object(price_api, "_params", return_value=price_source), \
                patch.object(api, "_browser_fetch", side_effect=self.good()):
            self.assertTrue(self.session.fetch(URL)["success"])
        self.assertEqual(expected_search, search_source)
        self.assertEqual(expected_price, price_source)

    def test_fresh_session_id_is_per_browser_instance_not_per_page(self):
        with patch.object(api, "ProbeBrowserSession", return_value=self.browser):
            other = api._APISession()
        with patch.object(api, "_browser_fetch", side_effect=self.good() * 2) as fetch:
            self.assertTrue(self.session.fetch(URL)["success"])
            self.assertTrue(other.fetch(URL)["success"])
        first, second = [fetch.call_args_list[index].args[2]["sessionid"] for index in (0, 2)]
        self.assertNotEqual(first, second)
        self.assertEqual(str(uuid.UUID(first)), first)
        self.assertEqual(str(uuid.UUID(second)), second)

    def test_search_waits_five_seconds_after_bootstrap_and_prior_search(self):
        now = [0.0]
        search_starts, search_finishes, waits = [], [], []
        values = iter(self.good() + self.good(200, 20))

        def wait(seconds):
            waits.append(seconds)
            now[0] += seconds

        def fetch(*args):
            if args[3] == "GET":
                search_starts.append(now[0])
                now[0] += 2
                search_finishes.append(now[0])
            else:
                now[0] += 1
            return next(values)

        with patch.object(api.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(api.time, "sleep", side_effect=wait), \
                patch.object(api, "_browser_fetch", side_effect=fetch):
            self.assertTrue(self.session.fetch(URL)["success"])
            self.assertTrue(self.session.fetch(URL.replace("page=1", "page=2"))["success"])
        self.assertEqual([5.0, 12.0], search_starts)
        self.assertEqual([7.0, 14.0], search_finishes)
        self.assertEqual([5.0, 4.0], [seconds for seconds in waits if seconds > 0])

    def test_long_price_call_already_satisfies_search_interval(self):
        now, search_starts, waits = [0.0], [], []
        values = iter(self.good() + self.good(200, 20))

        def wait(seconds):
            waits.append(seconds)
            now[0] += seconds

        def fetch(*args):
            if args[3] == "GET":
                search_starts.append(now[0])
                now[0] += 1
            else:
                now[0] += 10
            return next(values)

        with patch.object(api.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(api.time, "sleep", side_effect=wait), \
                patch.object(api, "_browser_fetch", side_effect=fetch):
            self.assertTrue(self.session.fetch(URL)["success"])
            self.assertTrue(self.session.fetch(URL.replace("page=1", "page=2"))["success"])
        self.assertEqual([5.0, 16.0], search_starts)
        self.assertEqual([5.0], [seconds for seconds in waits if seconds > 0])

    def test_failed_search_exception_still_sets_next_search_interval(self):
        now, search_starts = [0.0], []
        values = iter(self.good())

        def wait(seconds):
            now[0] += seconds

        def fetch(*args):
            if args[3] == "GET":
                search_starts.append(now[0])
                now[0] += 2
                if len(search_starts) == 1:
                    raise RuntimeError("synthetic failure")
            return next(values)

        with patch.object(api.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(api.time, "sleep", side_effect=wait), \
                patch.object(api, "_browser_fetch", side_effect=fetch):
            self.assertTrue(self.session.fetch(URL)["success"])
        self.assertEqual([5.0, 12.0], search_starts)

    def test_browser_controlled_headers_not_forwarded(self):
        with patch.object(api, "_browser_fetch", side_effect=self.good()) as fetch:
            self.session.fetch(URL)
        for call in fetch.call_args_list:
            for forbidden in ("origin", "referer", "user-agent", "sec-ch-ua", "cookie"):
                self.assertNotIn(forbidden, call.args[4])

    def test_three_non403_failures_no_fallback_and_preserves_session(self):
        with patch.object(api, "_browser_fetch", return_value=response(None, 500)) as fetch:
            result = self.session.fetch(URL)
        self.assertFalse(result["success"])
        self.assertEqual(500, result["status_code"])
        self.assertEqual(3, fetch.call_count)
        self.browser.close.assert_not_called()
        self.assertEqual(1, self.browser.fetch.call_count)

    def test_next_page_can_succeed_after_non403_without_new_navigation(self):
        with patch.object(api, "_browser_fetch", side_effect=[response(None, 500)] * 3 + self.good(200, 20)):
            self.assertFalse(self.session.fetch(URL)["success"])
            self.assertTrue(self.session.fetch(URL.replace("page=1", "page=2"))["success"])
        self.assertEqual(1, self.browser.fetch.call_count)

    def test_retry_recovers_within_three(self):
        with patch.object(api, "_browser_fetch", side_effect=[response(None, 403)] + self.good()) as fetch:
            result = self.session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual(3, fetch.call_count)
        self.assertEqual(2, result["trace"][-1]["attempt"])

    def test_price_failure_retries_search_and_price_bounded(self):
        bad = [self.good()[0], response(None, 403)]
        with patch.object(api, "_browser_fetch", side_effect=bad * 3) as fetch:
            result = self.session.fetch(URL)
        self.assertFalse(result["success"])
        self.assertEqual(6, fetch.call_count)

    def test_wrong_price_identity_is_failure(self):
        values = [self.good()[0], response({"Ofertas": [offer(sku=999)]})]
        with patch.object(api, "_browser_fetch", side_effect=values * 3):
            result = self.session.fetch(URL)
        self.assertEqual("price_or_seller_identity_incomplete", result["error"])

    def test_response_page_mismatch_skips_price(self):
        with patch.object(api, "_browser_fetch", return_value=self.good(queries={"page": 2})[0]) as fetch:
            result = self.session.fetch(URL)
        self.assertEqual("response_page_mismatch", result["error"])
        self.assertTrue(all(call.args[3] == "GET" for call in fetch.call_args_list))

    def test_different_page_identical_sku_set_rejected(self):
        with patch.object(api, "_browser_fetch", side_effect=self.good() * 4):
            self.assertTrue(self.session.fetch(URL)["success"])
            result = self.session.fetch(URL.replace("page=1", "page=2"))
        self.assertEqual("earlier_page_repeated_for_other_page", result["error"])

    def test_same_page_repeat_allowed(self):
        with patch.object(api, "_browser_fetch", side_effect=self.good() * 2):
            self.assertTrue(self.session.fetch(URL)["success"])
            self.assertTrue(self.session.fetch(URL)["success"])

    def test_bsr_request_sort_reaches_api_and_parser(self):
        with patch.object(api, "_browser_fetch", side_effect=self.good()) as fetch:
            result = self.session.fetch(URL + "&ordenacao=mais-vendidos")
        self.assertTrue(result["success"])
        self.assertEqual("maisvendidos", fetch.call_args_list[0].args[2]["sortby"])
        self.assertTrue(result["trace"][-1]["parser_sort_context_injected"])

    def test_non403_bootstrap_failure_not_retried_or_replaced_by_ssr(self):
        self.browser.fetch.return_value = {"success": False, "status_code": 500}
        for _ in range(2):
            with self.assertRaisesRegex(browser_listing.EvidenceError, "initial_page_not_verified"):
                self.session.fetch(URL)
        self.assertEqual(1, self.browser.fetch.call_count)

    def test_bootstrap_public_error_preserves_status_and_safe_reason(self):
        self.browser.fetch.return_value = {"success": False, "status_code": 403, "error": "document_not_200"}
        with patch.object(api, "_SESSION", self.session):
            result = api.fetch_listing(URL)
        self.assertEqual(403, result["status_code"])
        self.assertEqual("document_not_200", result["error"])
        self.assertEqual("bootstrap", result["trace"][0]["stage"])

    def test_bootstrap_error_raw_message_not_exposed(self):
        self.browser.fetch.return_value = {"success": False, "status_code": 200, "error": "private value https://private.invalid"}
        with patch.object(api, "_SESSION", self.session):
            result = api.fetch_listing(URL)
        self.assertEqual("initial_page_not_verified", result["error"])
        self.assertNotIn("private", json.dumps(result))

    def test_exception_message_not_exposed(self):
        with patch.object(api, "_browser_fetch", side_effect=RuntimeError("private-test-value https://secret.invalid/?private=1")):
            result = self.session.fetch(URL)
        public = json.dumps(result)
        self.assertNotIn("private-test-value", public)
        self.assertNotIn("secret.invalid", public)
        self.assertEqual("browser_api_RuntimeError", result["error"])

    def test_max_attempts_hard_capped_and_bad_config_safe(self):
        for value, expected in (("99", 3), ("invalid", 3), ("-99", 1), ("0", 1)):
            with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_SEARCH_RETRIES": value}):
                self.assertEqual(expected, api._attempt_limit())

    def test_close_only_owned_session(self):
        with patch.object(api, "_SESSION", self.session):
            api.close_browser()
            api.close_browser()
            self.browser.close.assert_called_once()

    def test_invalid_url_never_starts_browser(self):
        with patch.object(api, "_SESSION", None), patch.object(api, "_APISession") as factory:
            result = api.fetch_listing("https://example.org/tv/b")
        self.assertFalse(result["success"])
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
