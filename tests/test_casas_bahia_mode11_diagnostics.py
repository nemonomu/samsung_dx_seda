"""Mode 1-1 automatic listing evidence: offline, projected, no browser hooks."""

from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from seda.casas_bahia import listing_auto_diagnostics as auto
from seda.casas_bahia import listing_diagnostic_evidence as evidence
from seda.casas_bahia import search_api


CANARY = "MODE11_SYNTHETIC_PRIVATE_CANARY"


class Mode11ProjectionTests(unittest.TestCase):
    def test_mode_one_one_is_exact_string_and_legacy_modes_stay_numeric(self):
        self.assertEqual(evidence.project_event("run_start", {"mode": "1-1"})["mode"], "1-1")
        for mode in (1, 2, 3, 4, "1", "2", "3", "4"):
            with self.subTest(mode=mode):
                self.assertEqual(evidence.project_event("run_start", {"mode": mode})["mode"], int(mode))

    def test_invalid_modes_are_not_coerced_to_one_one_or_exposed(self):
        for mode in ("1.1", 1.1, "01-1", "1-1 " + CANARY, CANARY, True, [], {}):
            with self.subTest(kind=type(mode).__name__):
                actual = evidence.project_event("run_start", {"mode": mode})
                self.assertNotIn("mode", actual)
                self.assertNotIn(CANARY, json.dumps(actual))

    def test_method_counts_and_fixed_price_error_survive_two_projections(self):
        actual = evidence.project_event("page_end", {
            "actual_method": "api_partner_url_first", "trace": [{
                "method": "api_partner_url_first", "status_code": 200,
                "products": 20, "price_count": 15, "price_pending_products": 5,
                "seller_pending_products": 2, "price_error": "price_identity_mismatch",
                "headers": {"Authorization": CANARY}, "raw_response": CANARY,
            }],
        })
        self.assertEqual(actual["actual_method"], "api_partner_url_first")
        trace = actual["trace"][0]
        self.assertEqual(trace["method"], "api_partner_url_first")
        self.assertEqual(trace["price_count"], 15)
        self.assertEqual(trace["price_pending_products"], 5)
        self.assertEqual(trace["seller_pending_products"], 2)
        self.assertEqual(trace["price_error"], "price_identity_mismatch")
        self.assertEqual(evidence.project_event("page_end", actual), actual)
        self.assertNotIn(CANARY, json.dumps(actual))

    def test_price_count_rejects_non_numeric_or_unbounded_data(self):
        for value in (True, -1, float("nan"), float("inf"), 10**20, CANARY):
            actual = evidence.project_event("page_end", {"trace": [{"price_count": value}]})
            self.assertNotIn("price_count", actual["trace"][0])

    def test_fixed_rest_errors_survive_while_arbitrary_suffixes_are_rejected(self):
        errors = ("search_http_not_200", "non_json_response", "invalid_json", "invalid_product_payload",
                  "rest_listing_request_failed", "rest_listing_failed", "invalid_price_result", "invalid_price_status")
        for error in errors:
            with self.subTest(error=error):
                actual = evidence.project_event("page_end", {
                    "error": error, "trace": [{"error": error, "price_error": error}]})
                self.assertEqual(actual["error"], error)
                self.assertEqual(actual["trace"][0]["error"], error)
                self.assertEqual(actual["trace"][0]["price_error"], error)
                self.assertEqual(evidence.safe_error(error + "_" + CANARY), "other_error")


class Mode11AutomaticArchiveTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mode11_diag_")))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(patch.object(auto, "_ACTIVE", None))
        original_constructor = auto.AutomaticListingDiagnostics.__init__

        def constructor(instance, **kwargs):
            kwargs["log_dir"] = self.root / "log"
            original_constructor(instance, **kwargs)

        self.stack.enter_context(patch.object(auto.AutomaticListingDiagnostics, "__init__", constructor))
        self.observer = self.stack.enter_context(patch.object(auto, "_Observer",
            side_effect=AssertionError("REST mode must not install a Chrome observer")))
        self.params = self.stack.enter_context(patch.object(search_api, "_params",
            return_value={"resultsperpage": "24", "sessionid": CANARY}))

    def read_archive(self):
        archives = list((self.root / "log").glob("casas_listing_*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as archive:
            self.assertEqual(set(archive.namelist()), {"REPORT.md", "report.json", "events.jsonl"})
            all_text = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
            self.assertNotIn(CANARY, all_text)
            self.assertNotIn("Authorization", all_text)
            self.assertNotIn("raw_response", all_text)
            return (json.loads(archive.read("report.json")),
                    archive.read("REPORT.md").decode("utf-8"))

    def test_main_retains_product_rows_with_pending_prices_in_automatic_zip(self):
        diagnostic = auto.start_diagnostics("1-1", "main", [1], "TV")
        self.assertIsNotNone(diagnostic)
        self.assertIsNone(diagnostic.observer)
        auto.record_event("page_start", 1)
        auto.record_event("page_end", 1, True, 20, 20, "", [{
            "method": "api_partner_url_first", "status_code": 200,
            "inner_attempts": [{"method": "api_partner_url_first", "status_code": 200,
                "attempt": 1, "products": 20, "rows": 20, "price_count": 16,
                "price_pending_products": 4, "seller_pending_products": 1,
                "price_error": "price_identity_mismatch", "headers": {"Authorization": CANARY},
                "raw_response": CANARY}],
        }])
        auto.record_event("manifest_ready", {"complete": True, "downstream_allowed": True,
            "filtered_unique_count": 20, "raw_url": CANARY})
        auto.finish_diagnostics(diagnostic)
        report, markdown = self.read_archive()
        self.assertEqual(report["run"]["mode"], "1-1")
        self.assertEqual(report["run"]["configured_rest_page_size"], 24)
        self.assertEqual(report["outcome"]["completed_pages"], 1)
        self.assertEqual(report["outcome"]["failed_pages"], 0)
        self.assertEqual(report["outcome"]["filtered_unique_count"], 20)
        page = report["page_outcomes"][0]
        self.assertTrue(page["success"])
        self.assertEqual(page["actual_method"], "api_partner_url_first")
        self.assertEqual(page["rows"], 20)
        self.assertEqual(page["trace"][0]["price_count"], 16)
        self.assertEqual(page["trace"][0]["price_pending_products"], 4)
        self.assertEqual(page["trace"][0]["price_error"], "price_identity_mismatch")
        self.assertFalse(page["fallback_used"])
        self.assertEqual(page["ssr_navigation_attempts"], 0)
        self.assertNotIn("new_api_calls_in_page", page)
        self.assertNotIn("effective_mode3_page_size", report["run"])
        self.assertNotIn("effective_mode4_page_size", report["run"])
        self.assertIn("Listing mode: 1-1", markdown)
        self.assertIn("Effective mode 1-1 page size", markdown)
        self.assertNotIn("Effective mode 3", markdown)
        self.assertIn("diagnostic_started mode=1-1", self.output.getvalue())
        self.assertIn("diagnostic_zip=", self.output.getvalue())
        self.observer.assert_not_called()
        self.params.assert_called_once()

    def test_bsr_real_failure_and_error_sanitization_are_preserved(self):
        diagnostic = auto.start_diagnostics("1-1", "bsr", [6], "REF")
        auto.record_event("page_start", 6)
        auto.record_event("page_end", 6, False, 0, 0, CANARY, [{
            "method": "api_partner_url_first", "status_code": 403,
            "error": "api_http_not_200", "price_error": CANARY,
            "price_count": 0, "raw_response": CANARY,
        }])
        auto.record_event("manifest_ready", {"complete": False, "downstream_allowed": False,
            "minimum_unique_required": 100, "filtered_unique_count": 0})
        auto.finish_diagnostics(diagnostic, "SystemExit")
        report, _ = self.read_archive()
        self.assertEqual(report["run"]["run_id"], "bsr")
        self.assertEqual(report["outcome"]["outcome"], "partial_failed")
        self.assertEqual(report["outcome"]["failed_page_numbers"], [6])
        page = report["page_outcomes"][0]
        self.assertEqual(page["actual_method"], "api_partner_url_first")
        self.assertEqual(page["error"], "other_error")
        self.assertEqual(page["trace"][0]["price_error"], "other_error")
        self.assertEqual(page["trace"][0]["error"], "api_http_not_200")
        self.observer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
