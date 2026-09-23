"""Offline tests using synthetic data only, never a browser or environment file."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

try:
    from seda.casas_bahia import listing_diagnostic_evidence as evidence
except ImportError:
    import listing_diagnostic_evidence as evidence


CANARY = "SYNTHETIC_DO_NOT_EXPORT_THIS_TEXT"


class ProjectionTests(unittest.TestCase):
    def assert_safe(self, value):
        self.assertNotIn(CANARY, json.dumps(value))

    def test_error_allowlist_not_shape(self):
        self.assertEqual(evidence.safe_error("document_not_completed"), "document_not_completed")
        self.assertEqual(evidence.safe_error("browser_api_TimeoutException"), "browser_api_TimeoutException")
        self.assertEqual(evidence.safe_error(CANARY), "other_error")
        self.assertEqual(evidence.safe_error({"error": CANARY}), "other_error")

    def test_request_projection_and_reprojection(self):
        result = evidence.request_summary({"page": "18", "resultsperpage": "20", "regionid": "126000",
            "userid": CANARY, "sessionid": CANARY, "endpoint": CANARY, "sortby": CANARY,
            "variantconfiguration": "q2", "configured_rest_page_size": 24})
        self.assertEqual(result["page"], 18)
        self.assertEqual(result["resultsperpage"], 20)
        self.assertFalse(result["user_id_empty"])
        self.assertEqual(result["sort"], "other")
        self.assertEqual(evidence.request_summary(result), result)
        self.assert_safe(result)

    def test_numeric_boundaries_and_bool_rejected(self):
        self.assertEqual(evidence.request_summary({"page": True, "resultsperpage": -1, "regionid": "9" * 30}), {})
        self.assertEqual(evidence.api_summary({"status_code": 700, "elapsed_seconds": float("nan")}), {})
        self.assertEqual(evidence.api_summary({"elapsed_seconds": 10**1000}), {})

    def test_api_projection_whole_nested_record(self):
        result = evidence.api_summary({"status_code": 403, "ok": False, "json": False,
            "body": CANARY, "error": CANARY, "error_name": CANARY, "elapsed_seconds": 1.5,
            "network": {"request_method": "GET", "requestId": CANARY, "headers": {"Authorization": CANARY},
                "matching_request_count": 2, "response_status_codes": [200, 403, CANARY],
                "matched_http200_methods": ["OPTIONS", CANARY], "same_document": True}})
        self.assertEqual(result["error"], "other_error")
        self.assertEqual(result["network"]["matched_http200_methods"], ["OPTIONS"])
        self.assertEqual(result["network"]["response_status_codes"], [200, 403])
        self.assert_safe(result)

    def test_all_event_schemas_ignore_unknown_strings(self):
        candidate = {key: CANARY for key in ("product_line", "run_id", "python_version", "code_revision", "mode",
            "pages", "page", "method", "call_number", "request", "network", "result", "products", "rows",
            "status_code", "success", "trace", "error", "outcome", "tool_error_type", "authorization", "utc")}
        for event in evidence.EVENTS:
            self.assert_safe(evidence.project_event(event, candidate))
        self.assertIsNone(evidence.project_event(CANARY, candidate))
        self.assertIsNone(evidence.project_event({}, candidate))

    def test_known_trace_context_kept_unknown_rejected(self):
        result = evidence.project_event("page_end", {"page": 2, "status_code": 200, "success": False,
            "error": "document_not_completed", "trace": [{"stage": "bootstrap", "error": CANARY,
                "ready_state": "interactive", "document_network_error": CANARY, "browser_prepare_seconds": 3,
                "diagnostics": {"navigation_timeout": True, "body": CANARY}, "requestId": CANARY}]})
        self.assertEqual(result["trace"][0]["stage"], "bootstrap")
        self.assertEqual(result["trace"][0]["ready_state"], "interactive")
        self.assertEqual(result["trace"][0]["diagnostics"], {"navigation_timeout": True})
        self.assert_safe(result)

    def test_arrays_and_nested_trace_are_bounded(self):
        result = evidence.api_summary({"network": {"response_status_codes": [200] * 3000}})
        self.assertEqual(len(result["network"]["response_status_codes"]), evidence.MAX_ITEMS)
        cycle = {"stage": "bootstrap"}
        cycle["diagnostics"] = cycle
        result = evidence.project_event("page_end", {"trace": [cycle] * 100})
        self.assertEqual(len(result["trace"]), 20)
        self.assertLess(len(json.dumps(result)), 4000)

    def test_product_identity_and_filter_counts(self):
        result = evidence.product_summary({"products": [
            {"id": "10", "sku": "20", "sellerId": "30", "title": CANARY, "url": CANARY, "isSponsored": True},
            {"id": "11", "idSku": "21", "lojista": "31", "sponsored": CANARY},
        ]}, lambda item: item.get("id") == "10", lambda item: item.get("isSponsored") is True)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["sponsored"], 1)
        self.assertEqual(result["filtered"], 1)
        self.assertEqual(result["items"][1]["sku_id"], 21)
        self.assertTrue(result["items"][1]["has_sponsored"])
        self.assert_safe(result)

    def test_product_limits_and_predicate_errors(self):
        def broken(_):
            raise RuntimeError(CANARY)
        result = evidence.product_summary({"products": [{"id": CANARY}] * 2100}, broken, lambda _: False)
        self.assertEqual(result["returned"], 2100)
        self.assertEqual(result["items_truncated"], 100)
        self.assertEqual(result["predicate_errors"], 2000)
        self.assert_safe(result)

    def test_network_disambiguates_preflight_and_get(self):
        url = "https://example.invalid/search?page=18&sessionid=" + CANARY
        messages = [
            {"method": "Network.requestWillBeSent", "params": {"requestId": "pre", "request": {"url": url, "method": "OPTIONS", "headers": {"Authorization": CANARY}}}},
            {"method": "Network.requestWillBeSent", "params": {"requestId": "get", "request": {"url": url, "method": "GET"}}},
            {"method": "Network.responseReceivedExtraInfo", "params": {"requestId": "pre", "statusCode": 200}},
            {"method": "Network.responseReceivedExtraInfo", "params": {"requestId": "get", "statusCode": 403,
                "headers": {"Retry-After": "60", "Server": "AkamaiGHost", "Set-Cookie": CANARY}}},
            {"method": "Network.responseReceived", "params": {"requestId": "get", "response": {"status": 403,
                "mimeType": "text/html", "protocol": "h2", "headers": {"Server": CANARY}}}},
            {"method": "Network.loadingFailed", "params": {"requestId": "get", "canceled": True, "errorText": "net::ERR_FAILED"}},
            {"method": "Network.loadingFinished", "params": {"requestId": "pre"}},
        ]
        result = evidence.network_summary(messages, url, "GET")
        self.assertEqual(result["methods"]["GET"]["status_counts"], {"403": 1})
        self.assertEqual(result["methods"]["OPTIONS"]["status_counts"], {"200": 1})
        self.assertEqual(result["methods"]["GET"]["retry_after_seconds"], [60])
        self.assertEqual(result["methods"]["GET"]["loading_failed"], 1)
        self.assert_safe(result)

    def test_network_correlation_handles_earlier_extra_info(self):
        url = "https://example.invalid/search?page=2"
        messages = [
            {"method": "Network.responseReceivedExtraInfo", "params": {"requestId": "get", "statusCode": 403}},
            {"method": "Network.requestWillBeSent", "params": {"requestId": "get", "request": {"url": url, "method": "GET"}}},
            {"method": "Network.requestWillBeSent", "params": {"requestId": "different", "request": {"url": url.replace("page=2", "page=3"), "method": "GET"}}},
            {"method": "Network.responseReceived", "params": {"requestId": "different", "response": {"status": 200}}},
        ]
        result = evidence.network_summary(messages, url, "GET")
        self.assertEqual(result["methods"]["GET"]["status_counts"], {"403": 1})
        self.assertEqual(result["methods"]["GET"]["requests"], 1)

    def test_integral_float_cdp_status_and_actual_sponsorship_flags(self):
        url = "https://example.invalid/search?page=2"
        messages = [
            {"method": "Network.requestWillBeSent", "params": {"requestId": "get", "request": {"url": url, "method": "GET"}}},
            {"method": "Network.responseReceived", "params": {"requestId": "get", "response": {"status": 403.0}}},
        ]
        result = evidence.network_summary(messages, url, "GET")
        self.assertEqual(result["methods"]["GET"]["status_counts"], {"403": 1})
        self.assertEqual(evidence.api_summary({"status_code": 200.0})["status_code"], 200)
        self.assertNotIn("status_code", evidence.api_summary({"status_code": 200.1}))
        products = evidence.product_summary({"products": [{"advertasingEvents": CANARY,
            "advertisingEvents": CANARY, "tagName": "Produto patrocinado"}]}, lambda _: True, lambda _: True)
        self.assertTrue(products["items"][0]["has_advertasing_events"])
        self.assertTrue(products["items"][0]["has_advertising_events"])
        self.assertTrue(products["items"][0]["tag_name_sponsored"])
        self.assert_safe(products)

    def test_bootstrap_reuse_and_final_tool_state_preserved(self):
        page = evidence.project_event("page_end", {"page": 2, "new_api_calls_in_page": 0,
            "bootstrap_failure_reused": True, "trace": [{"chrome_major": 153, "document_navigations": 1,
                "parser_sku_aliases_added": 0, "parser_sort_context_injected": False,
                "sort_evidence_source": "observed_request_only"}]})
        self.assertEqual(page["new_api_calls_in_page"], 0)
        self.assertTrue(page["bootstrap_failure_reused"])
        self.assertEqual(page["trace"][0]["chrome_major"], 153)
        ending = evidence.project_event("run_end", {"attempted_pages": 2, "diagnostic_errors": 0, "tool_error_type": "none"})
        self.assertEqual(ending["tool_error_type"], "none")
        self.assertEqual(ending["attempted_pages"], 2)

    def test_actual_modes_and_partial_coverage_policy(self):
        for mode in (1, 2, 3):
            self.assertEqual(evidence.project_event("run_start", {"mode": mode})["mode"], mode)
        self.assertNotIn("mode", evidence.project_event("run_start", {"mode": 5}))
        ending = evidence.project_event("run_end", {"outcome": "accepted_with_failures", "filtered_unique_count": 343,
            "required_unique": 300, "downstream_allowed": True, "coverage_complete": False,
            "accepted_with_failures": True, "failed_page_numbers": [20, 18, 19, 18, 0, 1001, CANARY]})
        self.assertEqual(ending["outcome"], "accepted_with_failures")
        self.assertEqual(ending["failed_page_numbers"], [18, 19, 20])
        self.assertFalse(ending["coverage_complete"])
        self.assertTrue(ending["downstream_allowed"])
        self.assertEqual(ending["filtered_unique_count"], 343)
        self.assertEqual(evidence.project_event("page_end", {"filtered_unique_count": 100})["filtered_unique_count"], 100)
        self.assert_safe(ending)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="casas_evidence_test_")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_stream_flush_exclusive_and_safe_zip(self):
        writer = evidence.ReportWriter(self.path)
        writer.emit("run_start", {"product_line": "TV", "run_id": "main", "pages": 20,
            "configured_rest_page_size": 24, "effective_mode3_page_size": 20, "raw": CANARY})
        writer.emit("api_end", {"page": 18, "method": "GET", "call_number": 37, "elapsed_seconds": 3,
            "result": {"status_code": 403, "error": "api_http_not_200", "raw": CANARY}})
        writer.emit("page_end", {"page": 18, "success": False, "status_code": 403, "rows": 0, "error": "api_http_not_200"})
        self.assertGreater((self.path / "events.jsonl").stat().st_size, 0)
        with self.assertRaises(FileExistsError):
            evidence.ReportWriter(self.path)
        # An unrelated file must never be included or opened by reporting.
        (self.path / "DO_NOT_SHARE.txt").write_text(CANARY, encoding="utf-8")
        paths = writer.finish()
        with zipfile.ZipFile(paths["share_zip"]) as archive:
            self.assertEqual(set(archive.namelist()), {"events.jsonl", "REPORT.md", "report.json"})
            for name in archive.namelist():
                self.assertNotIn(CANARY, archive.read(name).decode("utf-8"))
        report = json.loads(Path(paths["report_json"]).read_text(encoding="utf-8"))
        self.assertEqual(report["first_api_403"]["call_number"], 37)
        self.assertEqual(report["outcome"]["outcome"], "interrupted")
        self.assertEqual(report["run"]["configured_rest_page_size"], 24)

    def test_offline_reprojects_tampered_fields_and_partial_tail(self):
        event = {"event": "page_end", "payload": {"page": 1, "success": False, "status_code": 403,
            "error": CANARY, "trace": [{"error": CANARY, "headers": CANARY}]}, "utc": CANARY, "raw": CANARY}
        (self.path / "events.jsonl").write_bytes((json.dumps(event) + '\n{"event":').encode("utf-8"))
        paths = evidence.build_report(self.path)
        report = json.loads(Path(paths["report_json"]).read_text(encoding="utf-8"))
        self.assertTrue(report["partial_tail"])
        self.assertEqual(report["ignored_lines"], 1)
        with zipfile.ZipFile(paths["share_zip"]) as archive:
            for name in archive.namelist():
                self.assertNotIn(CANARY, archive.read(name).decode("utf-8"))
        self.assertIn(CANARY, (self.path / "events.jsonl").read_text(encoding="utf-8"))

    def test_repeat_reports_do_not_overwrite(self):
        writer = evidence.ReportWriter(self.path)
        first = writer.finish()
        second = evidence.build_report(self.path)
        self.assertNotEqual(first["share_zip"], second["share_zip"])
        self.assertEqual(Path(second["share_zip"]).name, "share_1.zip")

    def test_automatic_archive_written_directly_in_log_directory(self):
        events_dir = self.path / CANARY
        log_dir = self.path / "log"
        writer = evidence.ReportWriter(events_dir)
        writer.emit("run_start", {"mode": 2, "product_line": "TV", "run_id": "main"})
        writer.emit("run_end", {"outcome": "accepted_with_failures", "failed_page_numbers": [18, 19, 20],
            "filtered_unique_count": 343, "required_unique": 300, "downstream_allowed": True,
            "accepted_with_failures": True, "coverage_complete": False})
        result = writer.finish(archive_dir=log_dir)
        archive_path = Path(result["share_zip"])
        self.assertEqual(archive_path.parent, log_dir)
        self.assertRegex(archive_path.name, r"^casas_listing_TV_main_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{32}\.zip$")
        self.assertFalse((events_dir / "share.zip").exists())
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(set(archive.namelist()), {"events.jsonl", "REPORT.md", "report.json"})
            report = json.loads(archive.read("report.json"))
            self.assertEqual(report["scope"], "automatic_listing_run")
            self.assertEqual(report["run"]["mode"], 2)
            self.assertEqual(report["outcome"]["failed_page_numbers"], [18, 19, 20])
            markdown = archive.read("REPORT.md").decode("utf-8")
            self.assertIn("Coverage complete: False", markdown)
            for name in archive.namelist():
                self.assertNotIn(CANARY, archive.read(name).decode("utf-8"))
        repeated = evidence.build_report(events_dir, archive_dir=log_dir)
        self.assertNotEqual(result["share_zip"], repeated["share_zip"])
        self.assertTrue(archive_path.exists())

    def test_finish_closes_events_even_if_report_fails(self):
        writer = evidence.ReportWriter(self.path)
        with patch.object(evidence, "build_report", side_effect=OSError("synthetic_failure")):
            with self.assertRaises(OSError):
                writer.finish(archive_dir=self.path / "log")
        self.assertTrue(writer._file.closed)

    def test_invalid_metadata_and_unknown_events_are_dropped(self):
        events = [
            {"event": "page_start", "payload": {"page": 1}, "utc": "2026-99-99T00:00:00Z", "elapsed_seconds": -1},
            {"event": CANARY, "payload": {"secret": CANARY}},
        ]
        (self.path / "events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
        paths = evidence.build_report(self.path)
        with zipfile.ZipFile(paths["share_zip"]) as archive:
            lines = archive.read("events.jsonl").decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn("utc", json.loads(lines[0]))

    def test_oversized_file_rejected(self):
        (self.path / "events.jsonl").write_text("12345", encoding="utf-8")
        with patch.object(evidence, "MAX_FILE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "events_file_rejected"):
                evidence.build_report(self.path)

    def test_oversized_line_rejected(self):
        (self.path / "events.jsonl").write_text("12345", encoding="utf-8")
        with patch.object(evidence, "MAX_LINE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "events_line_too_large"):
                evidence.build_report(self.path)

    def test_symlink_rejected_without_open(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "linked_path_rejected"):
                evidence.build_report(self.path)

    def test_event_count_limit_includes_unknown_events(self):
        (self.path / "events.jsonl").write_text('{"event":"unknown"}\n' * 3, encoding="utf-8")
        with patch.object(evidence, "MAX_EVENTS", 2):
            with self.assertRaisesRegex(ValueError, "too_many_events"):
                evidence.build_report(self.path)


if __name__ == "__main__":
    unittest.main()
