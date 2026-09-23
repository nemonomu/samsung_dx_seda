"""HTTP 403 recovery is same-browser, bounded and source-validated (offline)."""
from copy import deepcopy
import io
import os
import unittest
from unittest.mock import Mock, patch

from seda.casas_bahia import browser_api_url_first as api, browser_listing_url_first as browser_listing, search_api
from test_casas_bahia_mode4_browser_api import URL, FRAME, product, offer, response


def good(sku=100, product_id=10):
    return [response({"products": [product(sku, product_id)]}),
            response({"Ofertas": [offer(sku, product_id)]})]


def ssr(url=URL, sku=100, product_id=10):
    parsed, _ = api._parser_context({"products": [product(sku, product_id)]}, browser_listing._request_identity(url))
    raw = search_api._as_next_data_html(parsed, url)
    html, ids, rows = browser_listing._enrich_document(raw, url, [{"Ofertas": [offer(sku, product_id)]}])
    return {"success": True, "status_code": 200, "text": html, "products": len(ids),
            "trace": [{"method": "browser_ssr", "status_code": 200, "products": len(ids),
                       "parsed_rows": rows, "diagnostics": {"selected_document_loading_finished": True}}]}


def failed_ssr(status=403, error="document_not_200"):
    return {"success": False, "status_code": status, "error": error,
            "trace": [{"method": "browser_ssr", "status_code": status, "error": error}]}


class FallbackTests(unittest.TestCase):
    def setUp(self):
        for context in (patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV", "SEDA_CASAS_BAHIA_SEARCH_RETRIES": "2",
                                               "SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "0"}),
                        patch("requests.sessions.Session.request", side_effect=AssertionError("no Python HTTP")),
                        patch("sys.stdout", new_callable=io.StringIO), patch.object(api.time, "sleep")):
            context.start()
            self.addCleanup(context.stop)
        self.browser = Mock()
        self.browser.major = 153
        self.browser.driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": FRAME}}
        self.browser.fetch.return_value = {"success": True, "status_code": 200}
        with patch.object(api, "URLFirstProbeBrowserSession", return_value=self.browser):
            self.session = api._APISession()

    def test_three_search_403_then_same_browser_ssr_and_original_trace(self):
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, ssr()]
        with patch.object(api, "_browser_fetch", return_value=response(None, 403)) as fetch, \
                patch.object(api, "URLFirstProbeBrowserSession", side_effect=AssertionError("no new Chrome")):
            result = self.session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual("uc_api_url_first+browser_ssr", result["method"])
        self.assertEqual(3, fetch.call_count)
        self.assertEqual(2, self.browser.fetch.call_count)
        self.browser.close.assert_not_called()
        self.assertEqual([403, 403, 403], [t["status_code"] for t in result["trace"] if t.get("stage") == "search"])
        self.assertEqual(200, result["trace"][-1]["status_code"])
        self.assertTrue(result["trace"][-1]["browser_reused"])
        self.assertEqual({"100"}, self.session.page_ids[("/tv/b", "")]["1"])

    def test_search_recovers_before_limit_without_ssr(self):
        with patch.object(api, "_browser_fetch", side_effect=[response(None, 403)] * 2 + good()):
            result = self.session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual("uc_api_url_first", result["method"])
        self.assertEqual(1, self.browser.fetch.call_count)

    def test_final_non403_does_not_trigger_ssr(self):
        with patch.object(api, "_browser_fetch", side_effect=[response(None, 403), response(None, 403), response(None, 500)]):
            result = self.session.fetch(URL)
        self.assertFalse(result["success"])
        self.assertEqual(500, result["status_code"])
        self.assertEqual(1, self.browser.fetch.call_count)

    def test_price_only_403_preserves_urls_without_extra_navigation(self):
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, ssr()]
        with patch.object(api, "_browser_fetch", side_effect=[good()[0], response(None, 403)] * 3) as fetch:
            result = self.session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual(2, fetch.call_count)
        self.assertEqual("uc_api_url_first", result["method"])
        self.assertEqual(1, self.browser.fetch.call_count)
        self.assertEqual(403, result["trace"][-1]["price"]["status_code"])
        self.assertEqual(1, result["trace"][-1]["price_pending_products"])

    def test_success_refreshes_frame_and_next_page_uses_api(self):
        updated = {"id": "top", "loaderId": "after-ssr"}
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, ssr()]
        with patch.object(api, "_frame", side_effect=[FRAME, updated]), \
                patch.object(api, "_browser_fetch", side_effect=[response(None, 403)] * 3 + good(200, 20)) as fetch:
            self.assertTrue(self.session.fetch(URL)["success"])
            result = self.session.fetch(URL.replace("page=1", "page=2"))
        self.assertTrue(result["success"])
        self.assertEqual(updated, fetch.call_args_list[-1].args[7])
        self.assertEqual(2, self.browser.fetch.call_count)
        self.assertFalse(self.session.ssr_recovery_required)

    def test_bootstrap_403_has_no_api_and_can_recover_same_browser(self):
        self.browser.fetch.side_effect = [failed_ssr(), ssr()]
        with patch.object(api, "_browser_fetch") as fetch:
            result = self.session.fetch(URL)
        fetch.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual("bootstrap", result["trace"][0]["stage"])
        self.assertEqual(403, result["trace"][0]["status_code"])
        self.assertEqual("", self.session.bootstrap_error)
        self.assertTrue(self.session.bootstrap_attempted)

    def test_bootstrap_non403_keeps_existing_failure_policy(self):
        self.browser.fetch.return_value = failed_ssr(200, "document_not_completed")
        with self.assertRaises(api._BootstrapError):
            self.session.fetch(URL)
        self.assertEqual(1, self.browser.fetch.call_count)

    def test_failed_ssr_bounded_then_next_page_recovers_without_api_on_error_document(self):
        next_url = URL.replace("page=1", "page=2")
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, failed_ssr(), failed_ssr(), ssr(next_url, 200, 20)]
        with patch.object(api, "_browser_fetch", return_value=response(None, 403)) as fetch:
            first = self.session.fetch(URL)
            self.assertFalse(first["success"])
            self.assertTrue(self.session.ssr_recovery_required)
            self.assertIsNone(self.session.document_frame)
            second = self.session.fetch(next_url)
        self.assertTrue(second["success"])
        self.assertEqual(3, fetch.call_count)
        self.assertEqual(4, self.browser.fetch.call_count)
        self.assertTrue(second["trace"][-1]["recovery_pending"])
        self.browser.close.assert_not_called()

    def test_ssr_document_recovery_uses_existing_two_navigation_limit(self):
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200},
                                         failed_ssr(200, "document_not_completed"), ssr()]
        with patch.object(api, "_browser_fetch", return_value=response(None, 403)):
            result = self.session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual([1, 2], [t["navigation_attempt"] for t in result["trace"] if "navigation_attempt" in t])

    def test_invalid_ssr_payload_not_accepted_or_used_for_api(self):
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200},
                                         {"success": True, "status_code": 200, "text": "<html>no data</html>"}]
        with patch.object(api, "_browser_fetch", return_value=response(None, 403)):
            result = self.session.fetch(URL)
        self.assertFalse(result["success"])
        self.assertEqual("", result["text"])
        self.assertTrue(self.session.ssr_recovery_required)
        self.assertEqual({}, self.session.page_ids)

    def test_ssr_missing_frame_not_published(self):
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, ssr()]
        with patch.object(api, "_frame", side_effect=[FRAME, {}]), \
                patch.object(api, "_browser_fetch", return_value=response(None, 403)):
            result = self.session.fetch(URL)
        self.assertEqual("bootstrap_frame_missing", result["error"])
        self.assertEqual({}, self.session.page_ids)
        self.assertTrue(self.session.ssr_recovery_required)

    def test_repeated_set_across_api_then_ssr_rejected(self):
        next_url = URL.replace("page=1", "page=2")
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, ssr(next_url)]
        with patch.object(api, "_browser_fetch", side_effect=good() + [response(None, 403)] * 3):
            self.assertTrue(self.session.fetch(URL)["success"])
            result = self.session.fetch(next_url)
        self.assertEqual("earlier_page_repeated_for_other_page", result["error"])
        self.assertNotIn("2", self.session.page_ids[("/tv/b", "")])

    def test_repeated_set_across_ssr_then_api_rejected(self):
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, ssr()]
        with patch.object(api, "_browser_fetch", side_effect=[response(None, 403)] * 3 + good() * 3):
            self.assertTrue(self.session.fetch(URL)["success"])
            result = self.session.fetch(URL.replace("page=1", "page=2"))
        self.assertEqual("earlier_page_repeated_for_other_page", result["error"])

    def test_main_then_bsr_same_ids_allowed_and_sort_passed_without_restarting(self):
        bsr_url = URL + "&ordenacao=mais-vendidos"
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, ssr(bsr_url)]
        with patch.object(api, "_browser_fetch", side_effect=good() + [response(None, 403)] * 3) as fetch:
            self.assertTrue(self.session.fetch(URL)["success"])
            result = self.session.fetch(bsr_url)
        self.assertTrue(result["success"])
        self.assertEqual("maisvendidos", fetch.call_args_list[-1].args[2]["sortby"])
        self.assertEqual({("/tv/b", ""), ("/tv/b", "maisvendidos")}, set(self.session.page_ids))
        self.browser.close.assert_not_called()

    def test_ssr_exception_preserves_prior_trace_without_raw_exception(self):
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200},
                                         failed_ssr(), RuntimeError("private-synthetic-value")]
        with patch.object(api, "_browser_fetch", return_value=response(None, 403)):
            result = self.session.fetch(URL)
        self.assertFalse(result["success"])
        self.assertNotIn("private-synthetic-value", str(result))
        self.assertEqual("browser_api_RuntimeError", result["error"])
        self.assertEqual(1, len([t for t in result["trace"] if t.get("navigation_attempt") == 1]))
        self.assertEqual(3, len([t for t in result["trace"] if t.get("stage") == "search"]))

    def test_reduced_attempt_limit_preserved(self):
        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, ssr()]
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_SEARCH_RETRIES": "0"}), \
                patch.object(api, "_browser_fetch", return_value=response(None, 403)) as fetch:
            result = self.session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual(1, fetch.call_count)

    def test_ssr_summary_is_not_counted_as_extra_network_attempt(self):
        from seda import step01_main_list

        self.browser.fetch.side_effect = [{"success": True, "status_code": 200}, failed_ssr(), failed_ssr()]
        with patch.object(api, "_browser_fetch", return_value=response(None, 403)):
            result = self.session.fetch(URL)
        leaves = step01_main_list._leaf_fetch_attempts([{"inner_attempts": result["trace"]}])
        self.assertEqual(6, len(leaves))  # bootstrap + 3 API attempts + 2 navigations
        self.assertEqual([200, 403, 403, 403, 403, 403], [t["status_code"] for t in leaves])


if __name__ == "__main__":
    unittest.main()
