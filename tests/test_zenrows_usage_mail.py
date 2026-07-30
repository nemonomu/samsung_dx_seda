import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, sentinel

from seda import step10_status_check, zenrows_usage
from seda.magalu import zenrows_client


class ZenRowsUsageMailTests(unittest.TestCase):
    def _usage_env(self, root, execution_id="execution_a"):
        return patch.dict(
            os.environ,
            {
                "SEDA_ALLOW_ZENROWS": "1",
                "SEDA_ZENROWS_DRY_RUN": "0",
                "SEDA_RUN_ROOT": str(root),
                "SEDA_ACTIVE_RETAILER": "magalu",
                "SEDA_PRODUCT_LINE": "TV",
                zenrows_usage.EXECUTION_ID_ENV: execution_id,
                zenrows_usage.REQUIRED_ENV: "1",
            },
            clear=True,
        )

    @staticmethod
    def _response(ok=True, status_code=200, text="ok"):
        response = Mock(ok=ok, status_code=status_code, text=text)
        response.headers = {}
        return response

    def test_success_http_error_and_request_exception_each_count_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self._usage_env(root), patch.object(
                zenrows_client,
                "api_key",
                return_value=sentinel.api_key,
            ), patch.object(
                zenrows_client.requests,
                "get",
                side_effect=(
                    self._response(),
                    RuntimeError("transport failed"),
                    self._response(
                        ok=False,
                        status_code=503,
                        text="unavailable",
                    ),
                ),
            ), patch.object(
                zenrows_client.requests,
                "post",
                return_value=self._response(text="{}"),
            ):
                first = zenrows_client.request_url(
                    "https://example.com/one",
                    profile="basic_html",
                )
                second = zenrows_client.request_json(
                    "https://example.com/two",
                    {"operationName": "test"},
                    profile="basic_html",
                )
                third = zenrows_client.request_url(
                    "https://example.com/three",
                    profile="basic_html",
                )
                fourth = zenrows_client.request_url(
                    "https://example.com/four",
                    profile="basic_html",
                )
                summary = zenrows_usage.summarize_usage(root)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertEqual(third.error, "request_error:RuntimeError")
            self.assertEqual(fourth.error, "http_503")
            self.assertEqual(summary["tracking_status"], "complete")
            self.assertEqual(summary["http_calls"], 4)
            self.assertEqual(summary["by_retailer"], {"magalu": 4})
            ledger_text = "\n".join(
                path.read_text(encoding="ascii")
                for path in root.glob("status/zenrows_usage/execution_a/*.jsonl")
            )
            self.assertNotIn("example.com", ledger_text)
            self.assertNotIn("operationName", ledger_text)
            self.assertNotIn("apikey", ledger_text.casefold())

    def test_summary_aggregates_independent_process_shards(self):
        event = {
            "event": "zenrows_http_request_attempt",
            "retailer": "casas_bahia",
            "product_line": "ref",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usage_root = (
                root
                / "status"
                / "zenrows_usage"
                / "execution_a"
            )
            usage_root.mkdir(parents=True)
            for name in ("worker_1.jsonl", "worker_2.jsonl"):
                (usage_root / name).write_text(
                    json.dumps(event) + "\n",
                    encoding="ascii",
                )
            summary = zenrows_usage.summarize_usage(
                root,
                "execution_a",
            )

        self.assertEqual(summary["tracking_status"], "complete")
        self.assertEqual(summary["http_calls"], 2)
        self.assertEqual(summary["by_retailer"], {"casas_bahia": 2})

    def test_preflight_rejections_are_not_http_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self._usage_env(root), patch.object(
                zenrows_client.requests,
                "get",
            ) as request_get:
                unknown = zenrows_client.request_url(
                    "https://example.com",
                    profile="unknown",
                )
                with patch.object(zenrows_client, "enabled", return_value=False):
                    disabled = zenrows_client.request_url(
                        "https://example.com",
                        profile="basic_html",
                    )
                with patch.object(
                    zenrows_client,
                    "enabled",
                    return_value=True,
                ), patch.object(
                    zenrows_client,
                    "dry_run",
                    return_value=True,
                ):
                    dry_run = zenrows_client.request_url(
                        "https://example.com",
                        profile="basic_html",
                    )
                with patch.object(
                    zenrows_client,
                    "enabled",
                    return_value=True,
                ), patch.object(
                    zenrows_client,
                    "dry_run",
                    return_value=False,
                ), patch.object(
                    zenrows_client,
                    "api_key",
                    return_value="",
                ):
                    missing = zenrows_client.request_url(
                        "https://example.com",
                        profile="basic_html",
                    )
                summary = zenrows_usage.summarize_usage(root)

            self.assertTrue(all(
                not result.success
                for result in (unknown, disabled, dry_run, missing)
            ))
            request_get.assert_not_called()
            self.assertEqual(summary["http_calls"], 0)

    def test_required_tracking_failure_prevents_unrecorded_http_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self._usage_env(root), patch.object(
                zenrows_client,
                "api_key",
                return_value=sentinel.api_key,
            ), patch.object(
                zenrows_usage,
                "record_http_request_attempt",
                side_effect=OSError("disk unavailable"),
            ), patch.object(
                zenrows_client.requests,
                "get",
            ) as request_get:
                result = zenrows_client.request_url(
                    "https://example.com",
                    profile="basic_html",
                )

            self.assertFalse(result.success)
            self.assertEqual(result.error, "usage_tracking_error:OSError")
            request_get.assert_not_called()

    def test_optional_manual_tracking_failure_preserves_existing_http_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self._usage_env(root), patch.dict(
                os.environ,
                {zenrows_usage.REQUIRED_ENV: "0"},
                clear=False,
            ), patch.object(
                zenrows_client,
                "api_key",
                return_value=sentinel.api_key,
            ), patch.object(
                zenrows_usage,
                "record_http_request_attempt",
                side_effect=OSError("disk unavailable"),
            ), patch.object(
                zenrows_client.requests,
                "get",
                return_value=self._response(),
            ) as request_get:
                result = zenrows_client.request_url(
                    "https://example.com",
                    profile="basic_html",
                )

            self.assertTrue(result.success)
            request_get.assert_called_once()

    def test_execution_ids_are_isolated_and_invalid_ledger_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self._usage_env(root, "first"):
                zenrows_usage.record_http_request_attempt("GET", "basic_html", "1x")
                first = zenrows_usage.summarize_usage(root)
                os.environ[zenrows_usage.EXECUTION_ID_ENV] = "second"
                zenrows_usage.record_http_request_attempt("GET", "basic_html", "1x")
                zenrows_usage.record_http_request_attempt("POST", "basic_html", "1x")
                second = zenrows_usage.summarize_usage(root)

            self.assertEqual(first["http_calls"], 1)
            self.assertEqual(second["http_calls"], 2)
            first_shard = next(
                root.glob("status/zenrows_usage/first/*.jsonl")
            )
            with first_shard.open("a", encoding="ascii") as handle:
                handle.write("not-json\n")
            damaged = zenrows_usage.summarize_usage(root, "first")
            self.assertEqual(damaged["tracking_status"], "error")
            self.assertEqual(damaged["http_calls"], 1)

    def test_status_summary_and_email_report_include_current_execution_count(self):
        usage = {
            "scope": "current_orchestrator_execution",
            "execution_id": "execution_a",
            "tracking_status": "complete",
            "http_calls": 4,
            "by_retailer": {"casas_bahia": 4},
            "by_product_line": {"tv": 4},
            "error": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_read_json(path):
                if Path(path).name == "manifest_db_load.json":
                    return {
                        "success": True,
                        "inserted": 1,
                        "table": "dx_seda_tv_retail_com",
                    }
                return {}

            with patch.dict(
                os.environ,
                {
                    "SEDA_ACTIVE_RETAILER": "casas_bahia",
                    "SEDA_PRODUCT_LINE": "TV",
                },
                clear=True,
            ), patch.object(
                step10_status_check,
                "run_root",
                return_value=root,
            ), patch.object(
                step10_status_check,
                "csv_count",
                return_value=1,
            ), patch.object(
                step10_status_check,
                "read_json",
                side_effect=fake_read_json,
            ), patch.object(
                step10_status_check,
                "summarize_usage",
                return_value=usage,
            ), patch.object(
                step10_status_check,
                "_send_email",
                return_value="sent",
            ):
                step10_status_check.main()

            status = json.loads(
                (root / "status" / "status_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            body = (root / "status" / "email_report.txt").read_text(
                encoding="utf-8"
            )
            self.assertEqual(status["zenrows_usage"]["http_calls"], 4)
            self.assertIn(
                "- HTTP requests (this execution): 4",
                body,
            )
            self.assertIn("- Tracking status: complete", body)

    def test_email_uses_na_when_tracking_is_unavailable(self):
        status = {
            "data_success": True,
            "final_output_rows": 1,
            "db_inserted_rows": 1,
            "db_load": {"success": True},
            "zenrows_usage": {
                "tracking_status": "error",
                "http_calls": 2,
                "error": "invalid_ledger_json",
            },
        }
        body = step10_status_check._build_email_body(status)
        self.assertIn(
            "- HTTP requests (this execution): N/A",
            body,
        )
        self.assertIn("ZenRows HTTP request tracking is unavailable", body)
        self.assertIn("Status: CHECK NEEDED", body)
        self.assertTrue(
            step10_status_check._email_subject(status).startswith(
                "WARNING "
            )
        )
        self.assertFalse(step10_status_check._report_success(status))

    def test_complete_tracking_with_no_calls_reports_zero(self):
        status = {
            "data_success": True,
            "final_output_rows": 1,
            "db_inserted_rows": 1,
            "db_load": {"success": True},
            "zenrows_usage": {
                "tracking_status": "complete",
                "http_calls": 0,
                "error": "",
            },
        }
        body = step10_status_check._build_email_body(status)
        self.assertIn(
            "- HTTP requests (this execution): 0",
            body,
        )
        self.assertNotIn(
            "ZenRows HTTP request tracking is unavailable",
            body,
        )
        self.assertIn("Status: SUCCESS", body)
        self.assertTrue(step10_status_check._report_success(status))


if __name__ == "__main__":
    unittest.main()
