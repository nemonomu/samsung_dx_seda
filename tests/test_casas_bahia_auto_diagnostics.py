"""Automatic ZIP observation and production-path integration, fully offline."""

from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from seda import step01_main_list as listing
from seda.casas_bahia import (browser_api, browser_api_url_first,
                              listing_auto_diagnostics as auto, listing_modes, search_api)


CANARY = "SYNTHETIC_PRIVATE_TEXT_DO_NOT_EXPORT"


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.result = ({"status_code": 200, "ok": True, "private": CANARY}, {"products": []})
        self.fetch = Mock(return_value=self.result)
        self.network = Mock(return_value={"same_document": True})
        self.api = SimpleNamespace(_browser_fetch=self.fetch, _network_evidence=self.network)
        self.parsers = SimpleNamespace(_casas_bahia_is_relevant_product=lambda _: True,
                                       _casas_bahia_sku_status=lambda _: "")
        self.writer = Mock()
        self.observer = auto._Observer(self.api, self.parsers, self.writer)
        self.driver = Mock()

    def call(self):
        self.args = (self.driver, "https://example.invalid/search", {"page": 1, "sessionid": CANARY},
                     "GET", {"Authorization": CANARY}, {"body": CANARY}, 25, {"id": CANARY})
        return self.api._browser_fetch(*self.args)

    def test_exact_passthrough_no_added_driver_or_requests(self):
        self.observer.install()
        self.assertIs(self.call(), self.result)
        self.fetch.assert_called_once_with(*self.args)
        self.assertEqual(self.driver.mock_calls, [])
        self.observer.restore()
        self.assertIs(self.api._browser_fetch, self.fetch)
        self.assertIs(self.api._network_evidence, self.network)

    def test_recording_failure_does_not_change_return_or_retry(self):
        self.writer.emit.side_effect = OSError(CANARY)
        self.observer.install()
        self.assertIs(self.call(), self.result)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.observer.errors, 2)
        self.observer.restore()

    def test_original_baseexception_preserved(self):
        original = KeyboardInterrupt(CANARY)
        self.fetch.side_effect = original
        self.observer.install()
        with self.assertRaises(KeyboardInterrupt) as result:
            self.call()
        self.assertIs(result.exception, original)
        self.observer.restore()

    def test_network_passthrough_and_no_added_log_drain(self):
        self.observer.install()
        args = ([], {}, "https://example.invalid/search?page=2", "GET", None)
        expected = self.network.return_value
        self.assertIs(self.api._network_evidence(*args), expected)
        self.network.assert_called_once_with(*args)
        self.assertEqual(self.driver.mock_calls, [])
        self.observer.restore()


class AutomaticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="auto_zip_test_")))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(patch.object(auto, "_ACTIVE", None))
        self.original_constructor = auto.AutomaticListingDiagnostics.__init__

        def constructor(instance, **kwargs):
            kwargs["log_dir"] = self.root / "log"
            self.original_constructor(instance, **kwargs)

        self.stack.enter_context(patch.object(auto.AutomaticListingDiagnostics, "__init__", constructor))
        self.stack.enter_context(patch.object(browser_api, "_SESSION", None))
        self.stack.enter_context(patch.object(browser_api_url_first, "_SESSION", None))
        self.fetch = browser_api._browser_fetch
        self.network = browser_api._network_evidence
        self.stack.enter_context(patch.object(search_api, "_params", return_value={"resultsperpage": "20", "sessionid": CANARY}))

    def archives(self):
        return list((self.root / "log").glob("casas_listing_*.zip"))

    def report(self):
        with zipfile.ZipFile(self.archives()[-1]) as archive:
            self.assertEqual(set(archive.namelist()), {"REPORT.md", "report.json", "events.jsonl"})
            for name in archive.namelist():
                self.assertNotIn(CANARY, archive.read(name).decode("utf-8"))
            return json.loads(archive.read("report.json"))

    def test_direct_log_zip_created_with_threshold_acceptance_and_failed_pages(self):
        diagnostic = auto.start_diagnostics("3", "main", [1, 18], "TV")
        auto.record_event("page_start", 1)
        auto.record_event("page_end", 1, True, 343, 324, "", [])
        auto.record_event("page_start", 18)
        auto.record_event("page_end", 18, False, 0, 324, "api_http_not_200", [{
            "status_code": 403, "private": CANARY,
            "inner_attempts": [{"stage": "search", "status_code": 403, "error": "api_http_not_200"}],
        }])
        auto.record_event("manifest_ready", {"complete": False, "accepted_with_failures": True,
            "downstream_allowed": True, "minimum_unique_required": 300, "filtered_unique_count": 324,
            "raw_url": CANARY})
        auto.finish_diagnostics(diagnostic)
        self.assertEqual(len(self.archives()), 1)
        report = self.report()
        self.assertEqual(report["outcome"]["outcome"], "accepted_with_failures")
        self.assertEqual(report["outcome"]["filtered_unique_count"], 324)
        self.assertEqual(report["outcome"]["failed_page_numbers"], [18])
        self.assertFalse(report["outcome"]["coverage_complete"])
        self.assertTrue(report["outcome"]["downstream_allowed"])
        self.assertIs(browser_api._browser_fetch, self.fetch)
        self.assertIs(browser_api._network_evidence, self.network)
        self.assertIsNone(auto._ACTIVE)
        self.assertIn("diagnostic_zip=", self.output.getvalue())

    def test_failed_page_retains_no_fake_bootstrap_api_calls(self):
        diagnostic = auto.start_diagnostics("3", "main", [1, 2], "TV")
        browser_api._SESSION = SimpleNamespace(bootstrap_attempted=True, bootstrap_error="document_not_completed")
        auto.record_event("page_start", 2)
        auto.record_event("page_end", 2, False, 0, 0, "document_not_completed", [])
        auto.finish_diagnostics(diagnostic, "SystemExit")
        page = self.report()["page_outcomes"][0]
        self.assertTrue(page["bootstrap_failure_reused"])
        self.assertEqual(page["new_api_calls_in_page"], 0)

    def test_mode_four_hooks_only_new_api_and_zip_keeps_get403_and_ssr_success(self):
        api = browser_api_url_first
        driver = Mock()
        response = ({"status_code": 403, "error": "api_http_not_200", "body": CANARY}, None)
        network_result = {"request_response_verified": False}
        url = "https://example.invalid/search?page=1"
        network_messages = [
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": CANARY, "request": {"url": url, "method": "GET", "headers": {"private": CANARY}}}},
            {"method": "Network.responseReceived", "params": {
                "requestId": CANARY, "response": {"status": 403, "mimeType": "text/html"}}},
        ]

        def fetch(*args):
            self.assertIs(api._network_evidence(network_messages, {}, url, "GET", None), network_result)
            return response

        with patch.object(api, "_browser_fetch", side_effect=fetch) as original_fetch, \
                patch.object(api, "_network_evidence", return_value=network_result) as original_network:
            diagnostic = auto.start_diagnostics("4", "main", [1], "TV")
            self.assertIs(diagnostic.observer.api, api)
            self.assertIs(browser_api._browser_fetch, self.fetch)
            self.assertIs(browser_api._network_evidence, self.network)
            auto.record_event("page_start", 1)
            args = (driver, url, {"page": 1, "resultsperpage": 20, "sessionid": CANARY},
                    "GET", {"private": CANARY}, None, 25, {"private": CANARY})
            self.assertIs(api._browser_fetch(*args), response)
            auto.record_event("page_end", 1, True, 1, 1, "", [
                {"method": "uc_api_url_first", "stage": "search", "status_code": 403, "error": "api_http_not_200"},
                {"method": "browser_ssr", "stage": "ssr_fallback", "navigation_attempt": 1, "status_code": 200},
                {"method": "browser_ssr", "stage": "ssr_fallback", "fallback_used": True,
                 "browser_reused": True, "trigger_status_code": 403, "status_code": 200},
            ])
            auto.record_event("manifest_ready", {"complete": True, "downstream_allowed": True})
            auto.finish_diagnostics(diagnostic)
            self.assertIs(api._browser_fetch, original_fetch)
            self.assertIs(api._network_evidence, original_network)
            original_fetch.assert_called_once_with(*args)
            original_network.assert_called_once()
        self.assertEqual(driver.mock_calls, [])
        report = self.report()
        self.assertEqual(report["run"]["mode"], 4)
        self.assertEqual(report["run"]["effective_mode4_page_size"], 20)
        self.assertNotIn("effective_mode3_page_size", report["run"])
        self.assertEqual(report["first_api_403"]["method"], "GET")
        self.assertEqual(report["first_api_403"]["call_number"], 1)
        page = report["page_outcomes"][0]
        self.assertTrue(page["success"])
        self.assertEqual(page["actual_method"], "uc_api_url_first+browser_ssr")
        self.assertEqual(page["new_api_calls_in_page"], 1)
        self.assertEqual(page["ssr_navigation_attempts"], 1)
        self.assertFalse(page["bootstrap_failure_reused"])
        self.assertIs(browser_api._browser_fetch, self.fetch)
        with zipfile.ZipFile(self.archives()[-1]) as archive:
            events = [json.loads(line) for line in archive.read("events.jsonl").decode("utf-8").splitlines()]
        self.assertEqual(sum(item["event"] == "network" for item in events), 1)

    def test_mode_three_observer_does_not_touch_new_api_or_claim_mode_four_metadata(self):
        original_fetch = browser_api_url_first._browser_fetch
        diagnostic = auto.start_diagnostics("3", "main", [1], "TV")
        self.assertIs(diagnostic.observer.api, browser_api)
        self.assertIs(browser_api_url_first._browser_fetch, original_fetch)
        auto.finish_diagnostics(diagnostic)
        run = self.report()["run"]
        self.assertEqual(run["effective_mode3_page_size"], 20)
        self.assertNotIn("effective_mode4_page_size", run)

    def test_mode_four_archive_failure_restores_only_new_hooks(self):
        original_fetch, original_network = browser_api_url_first._browser_fetch, browser_api_url_first._network_evidence
        diagnostic = auto.start_diagnostics("4", "bsr", [1], "LDY")
        with patch.object(diagnostic.writer, "finish", side_effect=OSError(CANARY)):
            auto.finish_diagnostics(diagnostic)
        self.assertIs(browser_api_url_first._browser_fetch, original_fetch)
        self.assertIs(browser_api_url_first._network_evidence, original_network)
        self.assertIs(browser_api._browser_fetch, self.fetch)
        self.assertIsNone(auto._ACTIVE)

    def test_mode_one_and_two_have_no_api_hooks_or_fake_zero_traffic_claim(self):
        for mode in ("1", "2"):
            diagnostic = auto.start_diagnostics(mode, "bsr", [1], "REF")
            self.assertIsNone(diagnostic.observer)
            self.assertIs(browser_api._browser_fetch, self.fetch)
            auto.record_event("page_start", 1)
            auto.record_event("page_end", 1, True, 20, 20, "", [])
            auto.record_event("manifest_ready", {"complete": True, "downstream_allowed": True})
            auto.finish_diagnostics(diagnostic)
        reports = []
        for path in self.archives():
            with zipfile.ZipFile(path) as archive:
                reports.append(json.loads(archive.read("report.json")))
        self.assertEqual({r["run"]["mode"] for r in reports}, {1, 2})
        self.assertTrue(all("new_api_calls_in_page" not in r["page_outcomes"][0] for r in reports))

    def test_archive_failure_does_not_escape_or_leave_hooks_active(self):
        diagnostic = auto.start_diagnostics("3", "main", [1], "TV")
        with patch.object(diagnostic.writer, "finish", side_effect=OSError(CANARY)):
            auto.finish_diagnostics(diagnostic)
        self.assertIsNone(auto._ACTIVE)
        self.assertIs(browser_api._browser_fetch, self.fetch)
        self.assertIn("diagnostic_zip_failed", self.output.getvalue())
        self.assertNotIn(CANARY, self.output.getvalue())

    def test_archive_and_cleanup_failure_do_not_mask_original_outcome(self):
        diagnostic = auto.start_diagnostics("3", "main", [1], "TV")
        original_close = diagnostic.writer.close
        with patch.object(diagnostic.writer, "finish", side_effect=OSError(CANARY)), \
             patch.object(diagnostic.writer, "close", side_effect=OSError(CANARY)):
            auto.finish_diagnostics(diagnostic, "SystemExit")
        original_close()
        self.assertIs(browser_api._browser_fetch, self.fetch)
        self.assertIsNone(auto._ACTIVE)

    def test_start_interrupt_restores_hooks_and_reraises(self):
        with patch.object(auto.evidence.ReportWriter, "emit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                auto.start_diagnostics("3", "main", [1], "TV")
        self.assertIs(browser_api._browser_fetch, self.fetch)
        self.assertIsNone(auto._ACTIVE)

    def test_unexpected_exception_overrides_accepted_status_in_report(self):
        diagnostic = auto.start_diagnostics("3", "main", [1], "TV")
        auto.record_event("manifest_ready", {"complete": False, "accepted_with_failures": True,
                                              "downstream_allowed": True})
        auto.finish_diagnostics(diagnostic, "OSError")
        self.assertEqual(self.report()["outcome"]["outcome"], "tool_error")

    def test_start_failure_restores_installed_hooks(self):
        with patch.object(auto.evidence.ReportWriter, "emit", side_effect=OSError(CANARY)):
            self.assertIsNone(auto.start_diagnostics("3", "main", [1], "TV"))
        self.assertIs(browser_api._browser_fetch, self.fetch)
        self.assertIsNone(auto._ACTIVE)
        self.assertNotIn(CANARY, self.output.getvalue())

    def test_normal_main_finally_writes_zip_even_when_collection_raises(self):
        def run():
            listing._casas_listing_diagnostic_event("page_start", 1)
            listing._casas_listing_diagnostic_event("page_end", 1, False, 0, 0, "api_http_not_200", [])
            raise SystemExit("expected fixture failure")

        with patch.object(listing, "selected_retailers", return_value=["casas_bahia"]), \
             patch.object(listing, "page_numbers", return_value=[1]), \
             patch.object(listing_modes, "selected_mode", return_value="3"), \
             patch.object(listing_modes, "close_browsers") as close, \
             patch.object(listing, "_main_with_retries", side_effect=run):
            with self.assertRaisesRegex(SystemExit, "expected fixture failure"):
                listing.main()
        close.assert_called_once()
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(self.report()["outcome"]["outcome"], "partial_failed")

    def test_keyboard_interrupt_still_archives_and_restores(self):
        with patch.object(listing, "selected_retailers", return_value=["casas_bahia"]), \
             patch.object(listing, "page_numbers", return_value=[1]), \
             patch.object(listing_modes, "selected_mode", return_value="3"), \
             patch.object(listing_modes, "close_browsers"), \
             patch.object(listing, "_main_with_retries", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                listing.main()
        self.assertEqual(self.report()["outcome"]["outcome"], "interrupted")
        self.assertIs(browser_api._browser_fetch, self.fetch)

    def test_normal_logging_is_not_redirected_by_diagnostics(self):
        diagnostic = auto.start_diagnostics("3", "main", [1], "TV")
        print("ORIGINAL_COLLECTION_PROGRESS")
        auto.finish_diagnostics(diagnostic)
        self.assertIn("ORIGINAL_COLLECTION_PROGRESS", self.output.getvalue())

    def test_diagnostic_exception_does_not_change_page_result(self):
        diagnostic = auto.start_diagnostics("3", "main", [1], "TV")
        with patch.object(diagnostic.writer, "emit", side_effect=OSError(CANARY)):
            auto.record_event("page_end", 1, True, 20, 20, "", [])
        self.assertEqual(diagnostic.errors, 1)
        auto.finish_diagnostics(diagnostic)
        self.assertEqual(self.report()["outcome"]["diagnostic_errors"], 1)

    def test_no_autodiagnostic_on_magalu_path(self):
        with patch.object(listing, "selected_retailers", return_value=["magalu"]), \
             patch.object(listing, "_main_with_retries", return_value="kept") as main, \
             patch.object(auto, "start_diagnostics") as start:
            self.assertEqual(listing.main(), "kept")
        main.assert_called_once()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
