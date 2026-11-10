"""Offline status checks: persisted DB receipts survive notification failures."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch


_DISABLED_ENV = Path(__file__).with_name("STATUS_TEST_ENV_NOT_PRESENT")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
with patch.dict(os.environ, {"SEDA_ENV_PATH": str(_DISABLED_ENV)}):
    from seda import step10_status_check as status_step


class CasasStatusNotificationTests(unittest.TestCase):
    def run_status(self, *, email="sent", inserted=2, db_success=True,
                   rows=2, tracking="complete", retailer="casas_bahia"):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / "db").mkdir()
            receipt = root / "db" / "manifest_db_load.json"
            original = json.dumps({"success": db_success, "inserted": inserted,
                                   "table": "fixture_table", "mode": "append"})
            receipt.write_text(original, encoding="utf-8")
            stack.enter_context(patch.dict(os.environ, {
                "SEDA_ENV_PATH": str(_DISABLED_ENV), "SEDA_ACTIVE_RETAILER": retailer,
                "SEDA_PRODUCT_LINE": "TV",
            }, clear=True))
            stack.enter_context(patch.object(status_step, "run_root", return_value=root))
            stack.enter_context(patch.object(status_step, "csv_count", return_value=rows))
            stack.enter_context(patch.object(status_step, "summarize_usage", return_value={
                "tracking_status": tracking, "http_calls": 0,
            }))
            send = stack.enter_context(patch.object(status_step, "_send_email"))
            if isinstance(email, Exception):
                send.side_effect = email
            else:
                send.return_value = email
            output = stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
            code = 0
            try:
                status_step.main()
            except SystemExit as exc:
                code = exc.code
            result = json.loads((root / "status" / "status_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt.read_text(encoding="utf-8"), original)
            self.assertTrue((root / "status" / "email_report.txt").is_file())
            send.assert_called_once()
            return code, result, output.getvalue()

    def test_smtp_disconnect_is_warning_after_successful_db_load(self):
        code, result, output = self.run_status(
            email=smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
        self.assertEqual(code, 0)
        self.assertTrue(result["data_success"])
        self.assertTrue(result["success"])
        self.assertTrue(result["email_warning"])
        self.assertIn("SMTPServerDisconnected", result["email_status"])
        self.assertIn("WARNING Casas Bahia email notification not sent", output)

    def test_sent_email_has_no_warning(self):
        code, result, output = self.run_status()
        self.assertEqual(code, 0)
        self.assertTrue(result["success"])
        self.assertFalse(result["email_warning"])
        self.assertNotIn("WARNING", output)

    def test_notification_configuration_does_not_invalidate_data(self):
        for email in ("disabled", "dry_run", "missing:SEDA_EMAIL_TO"):
            with self.subTest(email=email):
                code, result, _ = self.run_status(email=email)
                self.assertEqual(code, 0)
                self.assertTrue(result["success"])
                self.assertTrue(result["email_warning"])
                self.assertEqual(result["email_status"], email)

    def test_real_data_or_tracking_failures_remain_failures(self):
        for failure in ({"db_success": False}, {"inserted": 1},
                        {"rows": 0, "inserted": 0}, {"tracking": "error"}):
            for email in ("sent", smtplib.SMTPServerDisconnected("fixture disconnect")):
                with self.subTest(failure=failure, email=type(email).__name__):
                    code, result, _ = self.run_status(email=email, **failure)
                    self.assertEqual(code, 1)
                    self.assertFalse(result["success"])

    def test_magalu_notification_policy_is_unchanged(self):
        code, result, _ = self.run_status(
            retailer="magalu", email=smtplib.SMTPServerDisconnected("fixture disconnect"))
        self.assertEqual(code, 1)
        self.assertFalse(result["success"])
        self.assertNotIn("email_warning", result)


if __name__ == "__main__":
    unittest.main()
