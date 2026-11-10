"""Offline Windows CMD contract tests; no project imports or live collection.

Each case copies only the batch under test and supplies a fake ``python.bat``.
The child environment is constructed explicitly, never copied or dumped.
"""

from pathlib import Path
from contextlib import ExitStack, nullcontext
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


BATCH_SOURCE = Path(__file__).resolve().parents[1] / "run_casas_bahia_tv_ref_ldy_full.bat"
WORK_ROOT = None
CMD = Path(r"C:\Windows\System32\cmd.exe")
CAPTURE_FIELDS = (
    "SEDA_POSTAL_CODE",
    "SEDA_CASAS_BAHIA_ZIPCODE",
    "SEDA_CASAS_BAHIA_LISTING_MODE",
    "SEDA_CASAS_BAHIA_SEARCH_RETRIES",
    "SEDA_RUN_DATE",
    "SEDA_FORCE_DATED_RUN_ROOT",
    "SEDA_RUN_ROOT",
    "SEDA_DETAIL_SKIP",
    "SEDA_DETAIL_LIMIT",
    "SEDA_DETAIL_TARGET_CSV",
    "SEDA_DETAIL_OUTPUT_CSV",
    "SEDA_DETAIL_WORKER_ID",
    "SEDA_DETAIL_TRACE",
    "SEDA_DETAIL_TRACE_TAG",
    "SEDA_MAGALU_SHIPPING_BACKFILL_ONLY",
    "SEDA_CASAS_BAHIA_API_ENRICH",
    "SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK",
)
PRECHECK_FILES = (
    "output/seda_final_targets.csv",
    "output/final_output_enriched.csv",
    "detail/trace/subcall_trace.csv",
)


@unittest.skipUnless(os.name == "nt" and CMD.is_file(), "requires Windows CMD")
class CasasBatchResumeTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="casas resume ", dir=WORK_ROOT)
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.batch = self.root / BATCH_SOURCE.name
        shutil.copyfile(BATCH_SOURCE, self.batch)
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.capture = self.root / "calls.txt"
        self.tv_root = self.root / "seda" / "data" / "casas_bahia" / "tv" / "20260923"
        for relative in PRECHECK_FILES:
            fixture = self.tv_root / relative
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-only\n", encoding="utf-8")
        stub_lines = ["@echo off", '>> "%HARNESS_CAPTURE%" echo BEGIN',
                      '>> "%HARNESS_CAPTURE%" echo ARGS=%*']
        stub_lines += [f'>> "%HARNESS_CAPTURE%" echo {name}=%{name}%'
                       for name in CAPTURE_FIELDS]
        stub_lines += [
            '>> "%HARNESS_CAPTURE%" echo END',
            'if /i "%~4"=="TV" exit /b %HARNESS_TV_EXIT%',
            'if /i "%~4"=="REF" exit /b %HARNESS_REF_EXIT%',
            'if /i "%~4"=="LDY" exit /b %HARNESS_LDY_EXIT%',
            "exit /b 90",
        ]
        (self.stub_dir / "python.bat").write_text("\n".join(stub_lines) + "\n", encoding="ascii")

    def run_batch(self, args=(), *, runtime=None, fail=None, fail_code="7", status_worker=None):
        # Fixed nonsecret Windows paths; do not copy/read the parent environment.
        child_env = {
            "SystemRoot": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "ComSpec": str(CMD),
            "PATH": str(self.stub_dir) + r";C:\Windows\System32;C:\Windows\System32\WindowsPowerShell\v1.0",
            "PATHEXT": ".BAT;.CMD;.EXE;.COM",
            "TEMP": str(self.root),
            "TMP": str(self.root),
            "SEDA_ENV_PATH": str(self.root / "never-created.env"),
            "SEDA_RUN_LOG_FILE": str(self.root / "batch.log"),
            "HARNESS_CAPTURE": str(self.capture),
            "HARNESS_TV_EXIT": "0",
            "HARNESS_REF_EXIT": "0",
            "HARNESS_LDY_EXIT": "0",
        }
        if runtime:
            self.assertTrue(set(runtime).issubset(CAPTURE_FIELDS))
            child_env.update(runtime)
        if fail:
            self.assertIn(fail, ("TV", "REF", "LDY"))
            child_env[f"HARNESS_{fail}_EXIT"] = fail_code
        if status_worker:
            child_env["HARNESS_REAL_PYTHON"] = sys.executable
            child_env["HARNESS_STATUS_WORKER"] = str(status_worker)
        self.capture.unlink(missing_ok=True)
        # cwd is the fixture root; avoiding a quoted nested absolute command
        # also avoids Windows' different CRT/CMD quote-escaping rules.
        command = "call " + subprocess.list2cmdline([self.batch.name, *args])
        result = subprocess.run(
            [str(CMD), "/d", "/c", command], cwd=self.root, env=child_env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        )
        calls = []
        current = None
        if self.capture.exists():
            for line in self.capture.read_text(encoding="utf-8").splitlines():
                if line == "BEGIN":
                    current = {}
                elif line == "END":
                    self.assertIsNotNone(current)
                    calls.append(current)
                    current = None
                elif current is not None:
                    key, separator, value = line.partition("=")
                    self.assertTrue(separator)
                    current[key] = value
        self.assertIsNone(current, "stub capture must contain complete calls")
        return result, calls

    def assert_success_chain(self, result, calls, *, resume=False, mode="4"):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 3, result.stdout + result.stderr)
        for product, call in zip(("TV", "REF", "LDY"), calls):
            suffix = "--from-step detail_enrichment --skip-local-cleanup" if resume and product == "TV" else "--all"
            self.assertEqual(call["ARGS"],
                             f"-m seda.casas_bahia.casas_bahia_orchestrator --product-line {product} {suffix}")
            self.assertEqual(call["SEDA_CASAS_BAHIA_LISTING_MODE"], mode)

    def test_normal_modes_preserve_three_full_runs(self):
        for mode in ("1", "1-1", "2", "3", "4"):
            with self.subTest(mode=mode):
                result, calls = self.run_batch([mode])
                self.assert_success_chain(result, calls, mode=mode)
                for call in calls:
                    self.assertEqual(call["SEDA_DETAIL_SKIP"], "")
                    self.assertEqual(call["SEDA_DETAIL_TARGET_CSV"], "")
                    self.assertEqual(call["SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK"], "1")

    def test_no_argument_uses_default_mode_three(self):
        self.assert_success_chain(*self.run_batch(), mode="3")

    def test_all_casas_batches_override_inherited_postal_codes(self):
        for name in (BATCH_SOURCE.name, "run_casas_bahia_tv_full.bat",
                     "run_casas_bahia_ref_full.bat", "run_casas_bahia_ldy_full.bat"):
            self.batch = self.root / name
            shutil.copyfile(BATCH_SOURCE.with_name(name), self.batch)
            for inherited in ({}, {"SEDA_POSTAL_CODE": "01001-001",
                                   "SEDA_CASAS_BAHIA_ZIPCODE": "20040020"}):
                with self.subTest(batch=name, inherited=inherited):
                    result, calls = self.run_batch(runtime=inherited)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(calls), 3 if name == BATCH_SOURCE.name else 1)
                    for call in calls:
                        self.assertEqual(call["SEDA_POSTAL_CODE"], "01010-010")
                        self.assertEqual(call["SEDA_CASAS_BAHIA_ZIPCODE"], "01010010")
                    if name == BATCH_SOURCE.name:
                        self.check_postal_requests(calls[0])

    def check_postal_requests(self, call):
        disabled_env = self.root / "never-created.env"
        self.assertFalse(disabled_env.exists())
        with patch.dict(os.environ, {
            "SEDA_ENV_PATH": str(disabled_env),
            "SEDA_POSTAL_CODE": call["SEDA_POSTAL_CODE"],
            "SEDA_CASAS_BAHIA_ZIPCODE": call["SEDA_CASAS_BAHIA_ZIPCODE"],
        }, clear=True), ExitStack() as stack:
            from seda.casas_bahia import detail_api, price_api, freight_cdp_backfill
            self.assertEqual(price_api._params()["cep"], "01010010")
            response = Mock(status_code=200, headers={"content-type": "application/json"})
            response.json.return_value = {"options": []}
            stack.enter_context(patch.object(detail_api, "_headers", return_value={}))
            stack.enter_context(patch.object(detail_api, "throttle"))
            stack.enter_context(patch.object(detail_api, "_freight_transports", return_value=["requests"]))
            request = stack.enter_context(patch.object(detail_api, "_freight_get", return_value=response))
            self.assertTrue(detail_api.fetch_freight("fixture-sku", "fixture-seller")["success"])
            self.assertIn("/zipcode/01010-010/source/CB", request.call_args.args[1])
            stack.enter_context(patch("sys.argv", ["freight_cdp_backfill"]))
            stack.enter_context(patch.object(freight_cdp_backfill, "detail_consumer_guard", return_value=nullcontext()))
            run = stack.enter_context(patch.object(freight_cdp_backfill, "run", new=Mock(return_value={})))
            stack.enter_context(patch.object(freight_cdp_backfill.asyncio, "run", side_effect=lambda result: result))
            stack.enter_context(patch("builtins.print"))
            freight_cdp_backfill.main()
            self.assertEqual(run.call_args.args[0].zipcode, "01010-010")

    def test_real_status_email_failure_continues_all_categories(self):
        # Exercise the actual status module/exit code under CMD. Collection, DB
        # receipts and SMTP are fixtures; no live requests or env files are used.
        stub = self.stub_dir / "python.bat"
        source = stub.read_text(encoding="ascii").split('if /i "%~4"=="TV"')[0]
        source += '"%HARNESS_REAL_PYTHON%" "%HARNESS_STATUS_WORKER%" "%~4"\nexit /b %errorlevel%\n'
        stub.write_text(source, encoding="ascii")
        worker = self.root / "status_worker.py"
        worker.write_text(
            "import json, os, smtplib, sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            f"sys.path.insert(0, {str(BATCH_SOURCE.parent)!r})\n"
            "from seda import step10_status_check as status\n"
            "product = sys.argv[1]\n"
            "root = Path(__file__).parent / product\n"
            "(root / 'db').mkdir(parents=True, exist_ok=True)\n"
            "failed = os.getenv('HARNESS_' + product + '_EXIT') != '0'\n"
            "receipt = {'success': not failed, 'inserted': 2, 'mode': 'append'}\n"
            "(root / 'db' / 'manifest_db_load.json').write_text(json.dumps(receipt), encoding='utf-8')\n"
            "os.environ['SEDA_ACTIVE_RETAILER'] = 'casas_bahia'\n"
            "os.environ['SEDA_PRODUCT_LINE'] = product\n"
            "with patch.object(status, 'run_root', return_value=root), "
            "patch.object(status, 'csv_count', return_value=2), "
            "patch.object(status, 'summarize_usage', return_value={'tracking_status': 'complete', 'http_calls': 0}), "
            "patch.object(status, '_send_email', side_effect=smtplib.SMTPServerDisconnected('fixture disconnect')):\n"
            "    status.main()\n", encoding="utf-8")
        for failed_product in (None, "TV", "REF", "LDY"):
            with self.subTest(failed_product=failed_product):
                result, calls = self.run_batch(status_worker=worker, fail=failed_product)
                self.assertEqual(result.returncode, 1 if failed_product else 0, result.stdout + result.stderr)
                self.assertEqual(len(calls), 3, result.stdout + result.stderr)
                for product in ("TV", "REF", "LDY"):
                    status_path = self.root / product / "status" / "status_summary.json"
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    self.assertEqual(status["success"], product != failed_product)
                    self.assertTrue(status["email_warning"])
                self.assertIn("WARNING Casas Bahia email notification not sent", result.stdout)
                final = "failed" if failed_product else "completed"
                self.assertIn(f"Casas Bahia TV/REF/LDY full run {final}", result.stdout)

    def test_standalone_ldy_defaults_to_mode_three_and_allows_override(self):
        standalone = BATCH_SOURCE.with_name("run_casas_bahia_ldy_full.bat")
        self.batch = self.root / standalone.name
        shutil.copyfile(standalone, self.batch)
        for selected in (None, "1"):
            with self.subTest(selected=selected):
                runtime = {"SEDA_CASAS_BAHIA_LISTING_MODE": selected} if selected else None
                result, calls = self.run_batch(runtime=runtime)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["ARGS"], "-m seda.casas_bahia.casas_bahia_orchestrator --product-line LDY --all")
                self.assertEqual(calls[0]["SEDA_CASAS_BAHIA_LISTING_MODE"], selected or "3")

    def test_mode_one_one_logs_explicit_url_first_rest_label(self):
        result, calls = self.run_batch(["1-1"])
        self.assert_success_chain(result, calls, mode="1-1")
        self.assertIn("listing mode=1-1 (rest_api_url_first)", result.stdout)

    def test_restored_mode_one_preserves_retry_configuration(self):
        for configured in (None, "4"):
            runtime = {} if configured is None else {"SEDA_CASAS_BAHIA_SEARCH_RETRIES": configured}
            result, calls = self.run_batch(["1"], runtime=runtime)
            self.assert_success_chain(result, calls, mode="1")
            self.assertEqual([call["SEDA_CASAS_BAHIA_SEARCH_RETRIES"] for call in calls],
                             [configured or ""] * 3)

    def test_other_modes_keep_retry_ceiling_configuration(self):
        for mode in ("1-1", "2", "3", "4"):
            result, calls = self.run_batch([mode], runtime={"SEDA_CASAS_BAHIA_SEARCH_RETRIES": "4"})
            self.assert_success_chain(result, calls, mode=mode)
            self.assertEqual([call["SEDA_CASAS_BAHIA_SEARCH_RETRIES"] for call in calls], ["2"] * 3)

    def test_valid_resume_then_ref_ldy_full(self):
        result, calls = self.run_batch(["4", "--resume-tv-detail", "20260923", "308"])
        self.assert_success_chain(result, calls, resume=True)
        tv = calls[0]
        self.assertEqual(tv["SEDA_DETAIL_SKIP"], "308")
        self.assertEqual(tv["SEDA_DETAIL_LIMIT"], "0")
        self.assertEqual(tv["SEDA_DETAIL_TARGET_CSV"], str(self.tv_root / PRECHECK_FILES[0]))
        self.assertEqual(tv["SEDA_DETAIL_OUTPUT_CSV"], str(self.tv_root / PRECHECK_FILES[1]))
        self.assertEqual(tv["SEDA_CASAS_BAHIA_API_ENRICH"], "1")
        self.assertEqual(tv["SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK"], "0")
        self.assertEqual(tv["SEDA_DETAIL_TRACE"], "1")
        for call in calls:
            self.assertEqual(call["SEDA_RUN_DATE"], "20260923")
            self.assertEqual(call["SEDA_FORCE_DATED_RUN_ROOT"], "1")
            self.assertEqual(call["SEDA_DETAIL_WORKER_ID"], "")
            self.assertEqual(call["SEDA_DETAIL_TRACE_TAG"], "")
        for call in calls[1:]:
            self.assertIn(call["SEDA_DETAIL_SKIP"], ("", "0"))
            self.assertEqual(call["SEDA_DETAIL_TARGET_CSV"], "")
            self.assertEqual(call["SEDA_DETAIL_OUTPUT_CSV"], "")
            self.assertEqual(call["SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK"], "1")

    def test_resume_failures_stop_at_first_failed_category(self):
        for index, category in enumerate(("TV", "REF", "LDY"), 1):
            with self.subTest(category=category):
                result, calls = self.run_batch(["4", "--resume-tv-detail", "20260923", "308"], fail=category)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), index, result.stdout + result.stderr)

    def test_normal_failures_continue_other_categories_and_report_failure(self):
        for category in ("TV", "REF", "LDY"):
            with self.subTest(category=category):
                result, calls = self.run_batch(["1"], fail=category)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), 3, result.stdout + result.stderr)
                self.assertEqual([call["SEDA_CASAS_BAHIA_LISTING_MODE"] for call in calls], ["1"] * 3)
                self.assertIn(f"Casas Bahia {category} full run failed", result.stdout)
                self.assertIn("Casas Bahia TV/REF/LDY full run failed", result.stdout)

    def test_negative_exit_is_a_failure_and_other_categories_run(self):
        for category in ("TV", "REF", "LDY"):
            with self.subTest(category=category):
                result, calls = self.run_batch(["1"], fail=category, fail_code="-1")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(len(calls), 3)
                self.assertIn(f"Casas Bahia {category} full run failed", result.stdout)

    def test_resume_rejects_legacy_modes_before_any_python(self):
        for mode in ("1", "1-1", "2", "3"):
            with self.subTest(mode=mode):
                result, calls = self.run_batch([mode, "--resume-tv-detail", "20260923", "308"])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(calls, [])

    def test_invalid_mode_rejected_before_any_python(self):
        for mode in ("0", "5", "unknown"):
            with self.subTest(mode=mode):
                result, calls = self.run_batch([mode])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(calls, [])

    def test_resume_requires_date_and_count(self):
        for args in (["4", "--resume-tv-detail"], ["4", "--resume-tv-detail", "20260923"]):
            with self.subTest(args=args):
                result, calls = self.run_batch(args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(calls, [])

    def test_resume_rejects_invalid_calendar_or_date_shape(self):
        for date in ("20260230", "20261301", "2026-09-23", "yesterday"):
            with self.subTest(date=date):
                result, calls = self.run_batch(["4", "--resume-tv-detail", date, "308"])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(calls, [])

    def test_resume_rejects_invalid_count(self):
        for count in ("0", "-1", "three", "3.5"):
            with self.subTest(count=count):
                result, calls = self.run_batch(["4", "--resume-tv-detail", "20260923", count])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(calls, [])

    def test_unknown_resume_switch_rejected(self):
        result, calls = self.run_batch(["4", "--resume-other", "20260923", "308"])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(calls, [])

    def test_missing_preflight_file_stops_before_any_python(self):
        for relative in PRECHECK_FILES:
            with self.subTest(relative=relative):
                path = self.tv_root / relative
                path.unlink()
                try:
                    result, calls = self.run_batch(["4", "--resume-tv-detail", "20260923", "308"])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(calls, [])
                finally:
                    path.write_text("fixture-only\n", encoding="utf-8")

    def test_resume_clears_inherited_tv_overrides_for_later_categories(self):
        inherited = {
            "SEDA_RUN_DATE": "19990101",
            "SEDA_FORCE_DATED_RUN_ROOT": "0",
            "SEDA_RUN_ROOT": str(self.root / "wrong-root"),
            "SEDA_DETAIL_SKIP": "999",
            "SEDA_DETAIL_LIMIT": "1",
            "SEDA_DETAIL_TARGET_CSV": str(self.root / "wrong-target.csv"),
            "SEDA_DETAIL_OUTPUT_CSV": str(self.root / "wrong-output.csv"),
            "SEDA_DETAIL_WORKER_ID": "worker-9",
            "SEDA_DETAIL_TRACE_TAG": "worker-9",
            "SEDA_MAGALU_SHIPPING_BACKFILL_ONLY": "1",
            "SEDA_CASAS_BAHIA_API_ENRICH": "0",
        }
        result, calls = self.run_batch(["4", "--resume-tv-detail", "20260923", "308"], runtime=inherited)
        self.assert_success_chain(result, calls, resume=True)
        for call in calls:
            self.assertEqual(call["SEDA_RUN_DATE"], "20260923")
            self.assertEqual(call["SEDA_FORCE_DATED_RUN_ROOT"], "1")
            self.assertEqual(call["SEDA_DETAIL_WORKER_ID"], "")
            self.assertEqual(call["SEDA_DETAIL_TRACE_TAG"], "")
            self.assertEqual(call["SEDA_MAGALU_SHIPPING_BACKFILL_ONLY"], "0")
            self.assertEqual(call["SEDA_DETAIL_LIMIT"], "0")
        self.assertEqual(calls[0]["SEDA_CASAS_BAHIA_API_ENRICH"], "1")
        for call in calls[1:]:
            self.assertIn(call["SEDA_DETAIL_SKIP"], ("", "0"))
            self.assertEqual(call["SEDA_DETAIL_TARGET_CSV"], "")
            self.assertEqual(call["SEDA_DETAIL_OUTPUT_CSV"], "")
            self.assertNotEqual(call["SEDA_RUN_ROOT"], str(self.tv_root))

    def test_tv_only_zenrows_override_restores_existing_nonsecret_setting(self):
        result, calls = self.run_batch(
            ["4", "--resume-tv-detail", "20260923", "308"],
            runtime={"SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK": "0"},
        )
        self.assert_success_chain(result, calls, resume=True)
        self.assertEqual([call["SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK"] for call in calls], ["0", "0", "0"])


if __name__ == "__main__":
    unittest.main()
