"""Offline document bootstrap tests: no Chrome, network, env, or DB access."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

_DISABLED_ENV = Path(__file__).with_name("MODE3_PREPARATION_NO_ENV_FILE")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ.setdefault("SEDA_ENV_PATH", str(_DISABLED_ENV))

from seda.casas_bahia import browser_api_document as preparation, browser_listing
from seda.casas_bahia.browser_probe_session import ProbeBrowserSession
from test_casas_bahia_browser_listing import URL, document, product
from test_casas_bahia_mode3_ssr_root_state import root_document


HOME = '<script id="__NEXT_DATA__">{"props":{"pageProps":{}}}</script>'
ENRICH_DOCUMENT = browser_listing._enrich_document


class FakeDriver:
    def __init__(self, routes):
        self.routes = iter(routes)
        self.frame = {"id": "top", "loaderId": "old", "url": "about:blank"}
        self.logs = []
        self.visits = []
        self.bodies = {}
        self.body_reads = []
        self.timeouts = []
        self.change_after_body = False

    def set_page_load_timeout(self, seconds):
        self.timeouts.append(seconds)

    def get(self, url):
        route = next(self.routes)
        self.visits.append(url)
        loader = "document-" + str(len(self.visits))
        self.frame = {"id": "top", "loaderId": loader, "url": route.get("url", url)}
        request_id = loader
        response = {"url": self.frame["url"], "status": route.get("status", 200),
                    "fromDiskCache": route.get("cached", False),
                    "fromServiceWorker": route.get("service_worker", False)}
        event = {"method": "Network.responseReceived", "params": {
            "type": "Document", "requestId": request_id, "response": response,
            "frameId": route.get("frame", "top"), "loaderId": route.get("loader", loader)}}
        events = [event]
        if route.get("completed", True):
            events.append({"method": "Network.loadingFinished", "params": {"requestId": request_id}})
        self.logs = [{"message": json.dumps({"message": event})} for event in events]
        self.bodies[request_id] = route.get("body", document())
        self.change_after_body = route.get("change_after_body", False)
        if route.get("timeout"):
            class TimeoutException(Exception):
                pass
            raise TimeoutException("private exception text")

    def get_log(self, name):
        logs, self.logs = self.logs, []
        return logs

    def execute_cdp_cmd(self, method, params):
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": dict(self.frame)}}
        if method == "Network.getResponseBody":
            request_id = params["requestId"]
            self.body_reads.append(request_id)
            if self.change_after_body:
                self.frame["loaderId"] += "-changed"
            return {"body": self.bodies[request_id]}
        raise AssertionError(method)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        def clock():
            self.now += 10
            return self.now
        contexts = [patch.object(preparation.time, "monotonic", side_effect=clock),
                    patch.object(preparation.time, "sleep"),
                    patch.object(browser_listing, "_enrich_document", side_effect=AssertionError("SSR offers must not gate preparation")),
                    patch("requests.sessions.Session.request", side_effect=AssertionError("no REST"))]
        self.mocks = [context.start() for context in contexts]
        for context in contexts:
            self.addCleanup(context.stop)

    def navigate(self, route):
        driver = FakeDriver([route])
        result = preparation._navigate(driver, URL, browser_listing._request_identity(URL))
        return result, driver

    def test_homepage_then_listing_without_any_price_response(self):
        driver = FakeDriver([{"body": HOME}, {"body": root_document()}])
        browser = ProbeBrowserSession()
        browser.driver = driver
        result = browser.prepare_api(URL)
        self.assertTrue(result["success"])
        self.assertTrue(result["homepage_ready"])
        self.assertEqual(2, result["document_navigations"])
        self.assertEqual([preparation.HOME_URL, URL], driver.visits)
        self.assertEqual([45, 45], driver.timeouts)
        self.mocks[1].assert_called_once_with(5)
        self.assertNotIn("text", result)
        self.assertNotIn("props", json.dumps(result))
        self.assertNotIn("loaderId", json.dumps(result["trace"]))

    def test_legacy_and_root_state_work_for_all_product_lines(self):
        for slug, title in [("tv", "Smart TV Samsung"), ("geladeira", "Geladeira Samsung"),
                            ("maquina-de-lavar", "Maquina de lavar Samsung")]:
            for transform in (lambda x: x, root_document):
                url = "https://www.casasbahia.com.br/" + slug + "/b?page=1"
                raw = transform(document([product(title=title)]))
                self.assertEqual(1, preparation._validate_document(raw, browser_listing._request_identity(url)))

    def test_document_get_timeout_can_still_accept_completed_valid_response(self):
        result, _ = self.navigate({"timeout": True})
        self.assertTrue(result["success"])
        self.assertTrue(result["trace"][0]["navigation_timeout"])
        self.assertNotIn("private", json.dumps(result))

    def test_bad_network_evidence_is_not_admitted(self):
        cases = [({"status": 403}, "document_not_200"),
                 ({"completed": False}, "document_not_completed"),
                 ({"cached": True}, "cached_document_rejected"),
                 ({"service_worker": True}, "cached_document_rejected"),
                 ({"frame": "iframe"}, "missing_current_document"),
                 ({"loader": "old"}, "missing_current_document"),
                 ({"url": URL.replace("page=1", "page=2")}, "requested_page_mismatch")]
        for route, error in cases:
            with self.subTest(error=error, route=route):
                result, driver = self.navigate(route)
                self.assertFalse(result["success"])
                self.assertEqual(error, result["error"])
                self.assertFalse(driver.body_reads)

    def test_error_html_and_navigation_during_body_read_are_not_admitted(self):
        for route in ({"body": "<html>Oops! Something went wrong</html>"},
                      {"change_after_body": True}):
            result, _ = self.navigate(route)
            self.assertFalse(result["success"])

    def test_page_sort_and_product_identity_remain_required(self):
        requested = browser_listing._request_identity(URL)
        cases = [(document(page=2), requested, "requested_page_mismatch"),
                 (document(sort="relevancia"), ("/tv/b", "1", "maisvendidos"), "requested_sort_mismatch"),
                 (document([]), requested, "empty_ssr_products"),
                 (document([product(), product()]), requested, "duplicate_listing_sku"),
                 (document([dict(product(), href="/wrong/p/999")]), requested, "listing_identity_invalid")]
        for raw, request, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(browser_listing.EvidenceError, error):
                preparation._validate_document(root_document(raw), request)

    def test_one_same_browser_403_retry_then_api_preparation_success(self):
        driver = FakeDriver([{"body": HOME}, {"status": 403}, {"body": root_document()}])
        browser = SimpleNamespace(start=Mock(), driver=driver)
        result = preparation.prepare(browser, URL)
        self.assertTrue(result["success"])
        self.assertEqual(3, result["document_navigations"])
        self.assertEqual([preparation.HOME_URL, URL, URL], driver.visits)
        self.assertEqual([200, 403, 200], [t["status_code"] for t in result["trace"]])
        browser.start.assert_called_once()

    def test_homepage_failure_never_opens_listing(self):
        driver = FakeDriver([{"status": 403}])
        result = preparation.prepare(SimpleNamespace(start=Mock(), driver=driver), URL)
        self.assertFalse(result["success"])
        self.assertFalse(result["homepage_ready"])
        self.assertEqual([preparation.HOME_URL], driver.visits)

    def test_target_403_retry_is_bounded_and_non403_is_not_retried(self):
        for status, visits in [(403, 3), (500, 2)]:
            driver = FakeDriver([{"body": HOME}, {"status": status}, {"status": status}])
            result = preparation.prepare(SimpleNamespace(start=Mock(), driver=driver), URL)
            self.assertFalse(result["success"])
            self.assertEqual(visits, len(driver.visits))

    def test_diagnostic_print_failure_does_not_change_success(self):
        driver = FakeDriver([{"body": HOME}, {}])
        with patch("builtins.print", side_effect=OSError("private log error")):
            result = preparation.prepare(SimpleNamespace(start=Mock(), driver=driver), URL)
        self.assertTrue(result["success"])

    def test_main_pages_and_bsr_use_api_without_more_document_navigation(self):
        from seda.casas_bahia import browser_api as api
        from seda.parsers import parse_listing
        from test_casas_bahia_browser_api import product as api_product, offer, response

        # Initial SSR product is deliberately absent from every API response.
        initial = root_document(document([product(product_id=999, sku=999)]))
        driver = FakeDriver([{"body": HOME}, {"body": initial}])
        owned = ProbeBrowserSession()
        owned.driver, owned.major = driver, 156
        replies = []
        for sku, product_id in [(100, 10), (200, 20), (100, 10)]:
            replies.extend([response({"products": [api_product(sku, product_id)]}),
                            response({"Ofertas": [offer(sku, product_id)]})])
        with patch.object(api, "ProbeBrowserSession", return_value=owned):
            session = api._APISession()
        with patch.object(owned, "fetch", side_effect=AssertionError("unexpected SSR navigation")), \
                patch.object(api, "_browser_fetch", side_effect=replies) as fetch, \
                patch.object(browser_listing, "_enrich_document", ENRICH_DOCUMENT), \
                patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            urls = [URL, URL.replace("page=1", "page=2"), URL + "&ordenacao=maisvendidos"]
            for url in urls:
                result = session.fetch(url)
                self.assertTrue(result["success"])
                self.assertEqual("uc_api", result["method"])
                rows = parse_listing(result["text"], "Casas Bahia", "https://www.casasbahia.com.br", url)
                self.assertEqual(1, len(rows))
                self.assertTrue(rows[0]["final_sku_price"])
                self.assertTrue(rows[0]["seller_id"])
                self.assertNotIn("/p/999", rows[0]["product_url"])
            self.assertEqual(["GET", "POST"] * 3, [call.args[3] for call in fetch.call_args_list])
            self.assertEqual("maisvendidos", fetch.call_args_list[4].args[2]["sortby"])
        self.assertEqual([preparation.HOME_URL, URL], driver.visits)


if __name__ == "__main__":
    unittest.main()
