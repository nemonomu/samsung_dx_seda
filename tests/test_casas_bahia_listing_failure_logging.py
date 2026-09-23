import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from seda import step01_main_list, transport
from seda.casas_bahia import search_api


TV_URL = "https://www.casasbahia.com.br/tv/b?page=1"


class _BlockedResponse:
    status_code = 403
    text = "blocked"
    headers = {"content-type": "text/html"}


class CasasBahiaListingFailureLoggingTests(unittest.TestCase):
    def test_partner_defaults_to_three_total_attempts(self):
        session = mock.Mock()
        session.get.return_value = _BlockedResponse()

        with mock.patch.dict(
            os.environ,
            {"SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "0"},
            clear=True,
        ), mock.patch.object(search_api.requests, "Session", return_value=session):
            result = search_api.fetch_search_listing(TV_URL, timeout=1)

        self.assertFalse(result["success"])
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([item["attempt"] for item in result["trace"]], [1, 2, 3])
        self.assertEqual([item["status_code"] for item in result["trace"]], [403] * 3)

    def test_partner_retries_three_times_without_printing_page_summary(self):
        session = mock.Mock()
        session.get.return_value = _BlockedResponse()
        output = io.StringIO()

        with mock.patch.dict(
            os.environ,
            {
                "SEDA_CASAS_BAHIA_SEARCH_RETRIES": "2",
                "SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "0",
            },
            clear=False,
        ), mock.patch.object(search_api.requests, "Session", return_value=session), redirect_stdout(output):
            result = search_api.fetch_search_listing(TV_URL, timeout=1)

        self.assertFalse(result["success"])
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([item["attempt"] for item in result["trace"]], [1, 2, 3])
        self.assertEqual([item["status_code"] for item in result["trace"]], [403, 403, 403])
        self.assertTrue(all(item["error"] == "non_json_or_blocked" for item in result["trace"]))
        self.assertNotIn("FAILED", output.getvalue())

    def test_transport_preserves_partner_status_and_inner_attempts(self):
        trace = [
            {"attempt": 1, "status_code": 403, "length": 3154, "error": "non_json_or_blocked"},
            {"attempt": 2, "status_code": 403, "length": 3154, "error": "non_json_or_blocked"},
            {"attempt": 3, "status_code": 403, "length": 3154, "error": "non_json_or_blocked"},
        ]
        partner_result = {
            "success": False,
            "error": "casas_bahia_partner_api_failed",
            "text": "",
            "trace": trace,
        }

        with mock.patch.object(search_api, "fetch_search_listing", return_value=partner_result):
            result = transport._fetch_graphql(TV_URL, timeout=1)

        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.attempts, trace)
        self.assertEqual(result.method, "api_partner")

    def test_page_failure_summary_reports_three_403_attempts(self):
        attempts = [
            {
                "method": "api_partner",
                "status_code": 403,
                "error": "casas_bahia_partner_api_failed",
                "inner_attempts": [
                    {"attempt": 1, "status_code": 403},
                    {"attempt": 2, "status_code": 403},
                    {"attempt": 3, "status_code": 403},
                ],
            }
        ]
        output = io.StringIO()

        with redirect_stdout(output):
            step01_main_list._log_listing_page_failure(
                "main",
                "Casas Bahia",
                7,
                "api_partner",
                "casas_bahia_partner_api_failed:details",
                attempts,
            )

        line = output.getvalue().strip()
        self.assertIn("main Casas Bahia page=7 listing fetch FAILED", line)
        self.assertIn("method=api_partner status=403 attempts=3", line)
        self.assertIn("statuses=403x3", line)
        self.assertIn("error=casas_bahia_partner_api_failed", line)

    def test_each_failed_page_is_logged_before_the_next_page_starts(self):
        inner_attempts = [
            {"attempt": 1, "status_code": 403, "length": 3154, "error": "non_json_or_blocked"},
            {"attempt": 2, "status_code": 403, "length": 3154, "error": "non_json_or_blocked"},
            {"attempt": 3, "status_code": 403, "length": 3154, "error": "non_json_or_blocked"},
        ]
        output = io.StringIO()
        calls = []

        def fetch(url, **kwargs):
            page = len(calls) + 1
            if page == 2:
                self.assertIn("page=1 listing fetch FAILED", output.getvalue())
            calls.append(page)
            return transport.FetchResult(
                url=url,
                text="",
                status_code=403,
                method="api_partner",
                error="casas_bahia_partner_api_failed:details",
                attempts=[
                    {
                        "method": "api_partner",
                        "status_code": 403,
                        "error": "casas_bahia_partner_api_failed",
                        "inner_attempts": inner_attempts,
                    }
                ],
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "SEDA_RETAILERS": "casas_bahia",
                "SEDA_ACTIVE_RETAILER": "casas_bahia",
                "SEDA_PRODUCT_LINE": "TV",
                "SEDA_RUN_ROOT": str(root),
                "SEDA_RUN_ID": "main",
                "SEDA_REUSE_RAW": "0",
                "SEDA_ALLOW_EMPTY_LISTING": "0",
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                step01_main_list,
                "page_numbers",
                return_value=[1, 2],
            ), mock.patch.object(
                step01_main_list,
                "unique_target",
                return_value=0,
            ), mock.patch.object(
                step01_main_list,
                "page_url",
                side_effect=lambda config, page, run_id=None: (
                    f"https://www.casasbahia.com.br/tv/b?page={page}"
                ),
            ), mock.patch.object(
                step01_main_list,
                "fetch_url",
                side_effect=fetch,
            ), redirect_stdout(output):
                with self.assertRaisesRegex(SystemExit, "listing produced 0 rows"):
                    step01_main_list._main_once()

            manifest = json.loads(
                (root / "main" / "manifest.json").read_text(encoding="utf-8")
            )

        failure_lines = [
            line for line in output.getvalue().splitlines() if "listing fetch FAILED" in line
        ]
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(failure_lines), 2)
        self.assertIn("page=1", failure_lines[0])
        self.assertIn("page=2", failure_lines[1])
        self.assertTrue(all("status=403 attempts=3 statuses=403x3" in line for line in failure_lines))
        self.assertEqual([item["page"] for item in manifest["failures"]], [1, 2])
        self.assertTrue(all(item["attempts"] for item in manifest["failures"]))


if __name__ == "__main__":
    unittest.main()
