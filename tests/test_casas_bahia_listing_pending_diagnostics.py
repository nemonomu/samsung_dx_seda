"""Safe source-product pending counters, without new diagnostic requests."""

import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from seda.casas_bahia import listing_diagnostic_evidence as evidence
from seda.casas_bahia import listing_url_first as url_first


class PendingPriceEvidenceTests(unittest.TestCase):
    def test_mode_four_page_size_survives_report_with_mode_four_label_only(self):
        with tempfile.TemporaryDirectory(prefix="mode4_report_", dir=Path(__file__).parent) as directory:
            writer = evidence.ReportWriter(Path(directory) / "events")
            writer.emit("run_start", {"mode": 4, "product_line": "TV", "run_id": "main",
                                      "configured_rest_page_size": 24, "effective_mode4_page_size": 20})
            writer.emit("run_end", {"outcome": "completed"})
            paths = writer.finish()
            with zipfile.ZipFile(paths["share_zip"]) as archive:
                report = json.loads(archive.read("report.json"))
                markdown = archive.read("REPORT.md").decode("utf-8")
        self.assertEqual(report["run"]["effective_mode4_page_size"], 20)
        self.assertNotIn("effective_mode3_page_size", report["run"])
        self.assertIn("Effective mode 4 page size: 20", markdown)
        self.assertIn("effective mode 4 value may differ", markdown)
        self.assertNotIn("mode 3", markdown)

    def test_legacy_modes_keep_original_mode_three_report_field_and_label(self):
        for mode in (1, 2, 3):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="legacy_mode_report_", dir=Path(__file__).parent) as directory:
                writer = evidence.ReportWriter(Path(directory) / "events")
                writer.emit("run_start", {"mode": mode, "product_line": "TV", "run_id": "main",
                                          "configured_rest_page_size": 24, "effective_mode3_page_size": 20})
                writer.emit("run_end", {"outcome": "completed"})
                paths = writer.finish()
                with zipfile.ZipFile(paths["share_zip"]) as archive:
                    report = json.loads(archive.read("report.json"))
                    markdown = archive.read("REPORT.md").decode("utf-8")
                self.assertEqual(report["run"]["effective_mode3_page_size"], 20)
                self.assertNotIn("effective_mode4_page_size", report["run"])
                self.assertIn("Effective mode 3 page size: 20", markdown)
                self.assertIn("effective mode 3 value may differ", markdown)
                self.assertNotIn("mode 4", markdown)

    def test_mode_four_page_size_rejects_invalid_and_raw_values(self):
        for value in (True, -1, 10001, float("nan"), "RAW_PENDING_CANARY"):
            actual = evidence.project_event("run_start", {"mode": 4, "effective_mode4_page_size": value})
            self.assertNotIn("effective_mode4_page_size", actual)
            self.assertNotIn("RAW_PENDING_CANARY", json.dumps(actual))

    def test_mode_four_and_its_methods_are_explicit_allowlisted_values(self):
        self.assertEqual(evidence.project_event("run_start", {"mode": 4})["mode"], 4)
        for method in ("uc_api_url_first", "uc_api_url_first+browser_ssr"):
            actual = evidence.project_event("page_end", {"actual_method": method, "trace": [{"method": method}]})
            self.assertEqual(actual["actual_method"], method)
            self.assertEqual(actual["trace"][0]["method"], method)

    def test_mode_four_keeps_original_price_403_inside_successful_listing_200(self):
        payload = {"props": {"pageProps": {"initialState": {"search": {
            "query": {"page": "1", "strbusca": "tv"}, "searchTerm": "tv",
            "results": {"products": [{"href": "/smart-tv/p/201", "title": "Smart TV Samsung 43 DU7700",
                                      "_casas_listing_url_first": True,
                                      "price": {}, "_casas_listing_price_pending": True,
                                      "_casas_listing_seller_pending": True}]},
        }}}}}
        rest = {"success": True, "text": '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload) + '</script>', "trace": [{
                    "attempt": 1, "status_code": 200, "products": 1,
                    "price_pending_products": 1, "seller_pending_products": 1,
                    "price_error": "price_http_403", "raw_body": "RAW_PENDING_CANARY",
                    "price": {"status_code": 403, "error": "price_http_403", "body": "RAW_PENDING_CANARY"},
                }]}
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            self.assertEqual(url_first._validation_error(rest["text"],
                "https://www.casasbahia.com.br/tv/b?page=1"), "")
        trace = [{"status_code": 200, "inner_attempts": url_first._safe_trace(rest["trace"])}]
        actual = evidence.project_event("page_end", {"page": 1, "success": True, "rows": 1,
                                                      "trace": trace})
        self.assertEqual(actual["trace"][0]["status_code"], 200)
        attempt = actual["trace"][0]["inner_attempts"][0]
        self.assertEqual(attempt["status_code"], 200)
        self.assertEqual(attempt["price"], {"status_code": 403, "error": "price_http_403"})
        self.assertEqual(attempt["price_error"], "price_http_403")
        self.assertEqual(attempt["price_pending_products"], 1)
        self.assertEqual(attempt["seller_pending_products"], 1)
        self.assertEqual(evidence.project_event("page_end", actual), actual)
        self.assertNotIn("RAW_PENDING_CANARY", json.dumps(actual))

    def test_mode_four_trace_suppresses_raw_errors_and_invalid_counts(self):
        for invalid in (True, -1, float("nan"), float("inf"), 10**400, "RAW_PENDING_CANARY"):
            with self.subTest(value_type=type(invalid).__name__):
                result = url_first._safe_trace([{
                    "price_pending_products": invalid, "seller_pending_products": invalid,
                    "error": "RAW_PENDING_CANARY", "price_error": "RAW_PENDING_CANARY",
                    "price": {"status_code": invalid, "error": "RAW_PENDING_CANARY", "body": "RAW_PENDING_CANARY"},
                }])[0]
                self.assertNotIn("price_pending_products", result)
                self.assertNotIn("seller_pending_products", result)
                self.assertNotIn("status_code", result["price"])
                self.assertEqual(result["error"], "other_error")
                self.assertEqual(result["price"], {"error": "other_error"})
                self.assertNotIn("RAW_PENDING_CANARY", json.dumps(result))

    def test_fixed_optional_price_errors_and_only_bounded_http_codes_are_preserved(self):
        codes = ("missing_product_url_identity", "optional_price_diagnostics_failed", "price_attach_disabled",
                 "price_attach_failed", "price_request_failed", "invalid_price_offers", "invalid_price_json",
                 "price_http_100", "price_http_403", "price_http_599")
        for code in codes:
            self.assertEqual(evidence.safe_error(code), code)
        for unsafe in ("price_http_099", "price_http_600", "price_http_403_RAW_PENDING_CANARY", "RAW_PENDING_CANARY"):
            self.assertEqual(evidence.safe_error(unsafe), "other_error")

    def test_inner_attempt_projection_keeps_existing_depth_and_item_limits(self):
        raw = {"status_code": 200, "body": "RAW_PENDING_CANARY"}
        for _ in range(5):
            raw = {"inner_attempts": [raw] * 25}
        actual = evidence.project_event("page_end", {"trace": [raw]})["trace"][0]
        self.assertEqual(len(actual["inner_attempts"]), 20)
        self.assertEqual(len(actual["inner_attempts"][0]["inner_attempts"]), 20)
        self.assertNotIn("inner_attempts", actual["inner_attempts"][0]["inner_attempts"][0])
        self.assertNotIn("RAW_PENDING_CANARY", json.dumps(actual))

    def test_complete_listing_trace_preserves_source_pending_counts_and_original_price_denial(self):
        candidate = {"page": 1, "success": True, "rows": 18, "trace": [{
            "stage": "complete", "status_code": 200, "products": 20,
            "price_pending_products": 5, "seller_pending_products": 2,
            "price_error": "api_http_not_200", "price": {"status_code": 403},
            "source_title": "RAW_PENDING_CANARY", "body": "RAW_PENDING_CANARY",
            "diagnostics": {"price_pending_products": 5, "seller_pending_products": 2,
                            "url": "RAW_PENDING_CANARY"},
        }]}
        actual = evidence.project_event("page_end", candidate)
        item = actual["trace"][0]
        self.assertEqual(item["price_pending_products"], 5)
        self.assertEqual(item["seller_pending_products"], 2)
        self.assertEqual(item["diagnostics"]["price_pending_products"], 5)
        self.assertEqual(item["price_error"], "api_http_not_200")
        self.assertEqual(item["price"]["status_code"], 403)
        self.assertEqual(evidence.project_event("page_end", actual), actual)
        self.assertNotIn("RAW_PENDING_CANARY", json.dumps(actual))

    def test_pending_counters_reject_strings_booleans_and_invalid_numbers(self):
        for invalid in ("RAW_PENDING_CANARY", True, -1, float("nan"), float("inf"), 10**20):
            with self.subTest(value=invalid):
                actual = evidence.project_event("page_end", {"trace": [{
                    "price_pending_products": invalid, "seller_pending_products": invalid,
                    "price_error": "RAW_PENDING_CANARY",
                }]})["trace"][0]
                self.assertNotIn("price_pending_products", actual)
                self.assertNotIn("seller_pending_products", actual)
                self.assertEqual(actual["price_error"], "other_error")

    def test_owned_report_archive_keeps_pending_counts_without_raw_payload(self):
        with tempfile.TemporaryDirectory(prefix="pending_diagnostic_", dir=Path(__file__).parent) as directory:
            writer = evidence.ReportWriter(Path(directory) / "events")
            writer.emit("run_start", {"mode": 3, "product_line": "TV", "run_id": "main", "pages": 1})
            writer.emit("page_end", {"page": 1, "success": True, "rows": 18, "trace": [{
                "stage": "complete", "products": 20, "price_pending_products": 20,
                "seller_pending_products": 3, "price_error": "api_http_not_200",
                "raw_body": "RAW_PENDING_CANARY",
            }]})
            writer.emit("run_end", {"outcome": "completed", "completed_pages": 1})
            paths = writer.finish()
            with zipfile.ZipFile(paths["share_zip"]) as archive:
                report = json.loads(archive.read("report.json"))
                all_text = "".join(archive.read(name).decode("utf-8") for name in archive.namelist())
            self.assertEqual(report["page_outcomes"][0]["trace"][0]["price_pending_products"], 20)
            self.assertNotIn("RAW_PENDING_CANARY", all_text)


if __name__ == "__main__":
    unittest.main()
