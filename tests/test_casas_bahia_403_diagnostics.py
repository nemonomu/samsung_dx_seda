"""Pure, offline evidence tests for same-browser 403 SSR recovery."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import zipfile

from seda.casas_bahia import listing_auto_diagnostics as auto
from seda.casas_bahia import listing_diagnostic_evidence as evidence


CANARY = "SYNTHETIC_PRIVATE_CONTEXT_MUST_NOT_APPEAR"


class SafeWriter:
    def __init__(self):
        self.records = []

    def emit(self, event, payload):
        self.records.append((event, evidence.project_event(event, payload)))


def fallback_trace(*, success=True, pending=False, navigation_count=1):
    trace = [
        {"method": "uc_api", "stage": "search", "attempt": attempt, "status_code": 403,
         "error": "api_http_not_200", "requestId": CANARY}
        for attempt in range(1, 4)
    ]
    trace.extend({"method": "browser_ssr", "navigation_attempt": attempt, "status_code": 200 if success else 403,
                  "diagnostics": {"selected_document_loading_finished": True, "ready_state": "complete"},
                  "url": CANARY} for attempt in range(1, navigation_count + 1))
    trace.append({"method": "browser_ssr", "stage": "ssr_fallback", "status_code": 200 if success else 403,
                  "trigger_status_code": 403, "fallback_used": True, "browser_reused": True,
                  "recovery_pending": pending, "products": 20 if success else 0, "parsed_rows": 19 if success else 0,
                  "error": "" if success else "document_not_200", "body": CANARY})
    return trace


class RecoveryEvidenceTests(unittest.TestCase):
    def test_hybrid_validation_errors_remain_precise_and_allowlisted(self):
        for error in ("ssr_fallback_failed", "empty_products", "missing_product_identity",
                      "duplicate_product_identity", "price_identity_mismatch", "missing_price",
                      "no_relevant_parsed_products", "parsed_identity_or_price_mismatch", "invalid_listing_payload"):
            self.assertEqual(error, evidence.safe_error(error))
        self.assertEqual("other_error", evidence.safe_error(CANARY))

    def diagnostic(self, *, session=None):
        value = auto.AutomaticListingDiagnostics(mode="3", run_id="main", pages=[1, 2], product_line="TV")
        value.writer = SafeWriter()
        if session is None:
            session = SimpleNamespace(bootstrap_attempted=True, bootstrap_error="", ssr_recovery_required=False)
        value.observer = SimpleNamespace(page=0, call_number=0, errors=0, api=SimpleNamespace(_SESSION=session))
        return value

    def test_existing_browser_facts_are_boolean_only_without_calls(self):
        driver = Mock()
        session = SimpleNamespace(browser=SimpleNamespace(driver=driver), bootstrap_attempted=True,
                                  bootstrap_error="", ssr_recovery_required=False, private=CANARY)
        result = auto._session_evidence(session)
        self.assertEqual(result, {"browser_session_reused": True, "bootstrap_previously_completed": True})
        self.assertEqual(driver.mock_calls, [])
        self.assertNotIn(CANARY, json.dumps(result))

    def test_no_browser_and_invalid_document_are_not_reported_validated(self):
        self.assertEqual(auto._session_evidence(None),
                         {"browser_session_reused": False, "bootstrap_previously_completed": False})
        for error, pending in (("document_not_200", False), ("", True)):
            session = SimpleNamespace(browser=SimpleNamespace(driver=object()), bootstrap_attempted=True,
                                      bootstrap_error=error, ssr_recovery_required=pending)
            result = auto._session_evidence(session)
            self.assertTrue(result["browser_session_reused"])
            self.assertFalse(result["bootstrap_previously_completed"])

    def test_run_start_reuse_flags_survive_projection(self):
        source = {"mode": 3, "run_id": "bsr", "product_line": "TV", "browser_session_reused": True,
                  "bootstrap_previously_completed": True, "profile": CANARY}
        result = evidence.project_event("run_start", source)
        self.assertTrue(result["browser_session_reused"])
        self.assertTrue(result["bootstrap_previously_completed"])
        self.assertNotIn(CANARY, json.dumps(result))

    def test_success_trace_keeps_original_403_and_actual_ssr_method(self):
        diagnostic = self.diagnostic()
        diagnostic.page_start(1)
        diagnostic.observer.call_number = 3
        diagnostic.page_end(1, True, 19, 19, "", [{"method": "uc_api+browser_ssr", "inner_attempts": fallback_trace()}])
        event, page = diagnostic.writer.records[-1]
        self.assertEqual(event, "page_end")
        self.assertEqual(page["actual_method"], "uc_api+browser_ssr")
        self.assertTrue(page["fallback_used"])
        self.assertEqual(page["new_api_calls_in_page"], 3)
        self.assertEqual(page["ssr_navigation_attempts"], 1)
        self.assertEqual(page["status_code"], 200)
        self.assertEqual(page["rows"], 19)
        self.assertEqual([item["status_code"] for item in page["trace"][:3]], [403, 403, 403])
        self.assertEqual(page["trace"][-1]["stage"], "ssr_fallback")
        self.assertEqual(page["trace"][-1]["trigger_status_code"], 403)
        self.assertFalse(page["bootstrap_failure_reused"])
        self.assertNotIn(CANARY, json.dumps(page))

    def test_pending_ssr_recovery_does_not_claim_cached_bootstrap_replay(self):
        session = SimpleNamespace(bootstrap_attempted=True, bootstrap_error="document_not_200", ssr_recovery_required=True)
        diagnostic = self.diagnostic(session=session)
        diagnostic.page_start(2)
        diagnostic.page_end(2, False, 0, 0, "document_not_200",
                            [{"inner_attempts": fallback_trace(success=False, pending=True, navigation_count=2)[3:]}])
        page = diagnostic.writer.records[-1][1]
        self.assertFalse(page["bootstrap_failure_reused"])
        self.assertTrue(page["recovery_pending"])
        self.assertEqual(page["new_api_calls_in_page"], 0)
        self.assertEqual(page["ssr_navigation_attempts"], 2)
        self.assertEqual(page["status_code"], 403)
        self.assertFalse(page["success"])

    def test_new_fallback_clears_earlier_reuse_label(self):
        session = SimpleNamespace(bootstrap_attempted=True, bootstrap_error="document_not_200", ssr_recovery_required=False)
        diagnostic = self.diagnostic(session=session)
        diagnostic.page_start(2)
        self.assertTrue(diagnostic.bootstrap_reused)
        diagnostic.page_end(2, True, 19, 19, "", [{"inner_attempts": fallback_trace()[3:]}])
        self.assertFalse(diagnostic.writer.records[-1][1]["bootstrap_failure_reused"])

    def test_real_cached_failure_without_navigation_keeps_reuse_label(self):
        session = SimpleNamespace(bootstrap_attempted=True, bootstrap_error="document_not_completed", ssr_recovery_required=False)
        diagnostic = self.diagnostic(session=session)
        diagnostic.page_start(2)
        diagnostic.page_end(2, False, 0, 0, "document_not_completed", [{"inner_attempts": [
            {"method": "uc_api", "stage": "bootstrap", "status_code": 200, "error": "document_not_completed"}]}])
        page = diagnostic.writer.records[-1][1]
        self.assertTrue(page["bootstrap_failure_reused"])
        self.assertFalse(page["fallback_used"])
        self.assertEqual(page["ssr_navigation_attempts"], 0)

    def test_http200_with_failed_ssr_validation_remains_failure(self):
        diagnostic = self.diagnostic()
        diagnostic.page_start(1)
        trace = fallback_trace()
        trace[-1]["error"] = "requested_sort_mismatch"
        diagnostic.page_end(1, False, 0, 0, "requested_sort_mismatch", [{"inner_attempts": trace}])
        page = diagnostic.writer.records[-1][1]
        self.assertEqual(page["status_code"], 200)
        self.assertFalse(page["success"])
        self.assertEqual(page["error"], "requested_sort_mismatch")

    def test_forged_extra_fallback_strings_are_dropped(self):
        result = evidence.project_event("page_end", {"actual_method": CANARY, "fallback_used": CANARY,
            "browser_reused": CANARY, "recovery_pending": CANARY, "ssr_navigation_attempts": CANARY,
            "trace": [{"stage": CANARY, "method": CANARY, "trigger_status_code": CANARY,
                       "navigation_attempt": CANARY, "error": CANARY}]})
        self.assertEqual(result["actual_method"], "other")
        self.assertNotIn("fallback_used", result)
        self.assertNotIn(CANARY, json.dumps(result))

    def test_zip_report_exposes_recovery_without_raw_payload(self):
        with tempfile.TemporaryDirectory(prefix="casas_recovery_evidence_") as temporary:
            directory = Path(temporary)
            writer = evidence.ReportWriter(directory / "events")
            writer.emit("run_start", {"mode": 3, "run_id": "bsr", "product_line": "TV",
                                       "browser_session_reused": True, "bootstrap_previously_completed": True})
            diagnostic = self.diagnostic()
            diagnostic.page_start(1)
            diagnostic.page_end(1, True, 19, 19, "", [{"inner_attempts": fallback_trace()}])
            writer.emit("page_end", diagnostic.writer.records[-1][1])
            paths = writer.finish(archive_dir=directory / "log")
            with zipfile.ZipFile(paths["share_zip"]) as archive:
                report = json.loads(archive.read("report.json"))
                self.assertTrue(report["run"]["browser_session_reused"])
                self.assertEqual(report["page_outcomes"][0]["actual_method"], "uc_api+browser_ssr")
                markdown = archive.read("REPORT.md").decode("utf-8")
                self.assertIn("uc_api+browser_ssr", markdown)
                self.assertIn("Existing browser reused at run start: True", markdown)
                for name in archive.namelist():
                    self.assertNotIn(CANARY, archive.read(name).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
