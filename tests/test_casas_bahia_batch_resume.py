"""Offline Windows CMD contract tests; no project imports or live collection.

Each case copies only the batch under test and supplies a fake ``python.bat``.
The child environment is constructed explicitly, never copied or dumped.
"""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


BATCH_SOURCE = Path(__file__).resolve().parents[1] / "run_casas_bahia_tv_ref_ldy_full.bat"
WORK_ROOT = None
CMD = Path(r"C:\Windows\System32\cmd.exe")
CAPTURE_FIELDS = (
    "SEDA_CASAS_BAHIA_LISTING_MODE",
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

    def run_batch(self, args=(), *, runtime=None, fail=None):
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
            child_env[f"HARNESS_{fail}_EXIT"] = "7"
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
        for mode in ("1", "2", "3", "4"):
            with self.subTest(mode=mode):
                result, calls = self.run_batch([mode])
                self.assert_success_chain(result, calls, mode=mode)
                for call in calls:
                    self.assertEqual(call["SEDA_DETAIL_SKIP"], "")
                    self.assertEqual(call["SEDA_DETAIL_TARGET_CSV"], "")
                    self.assertEqual(call["SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK"], "1")

    def test_no_argument_uses_default_mode_one(self):
        self.assert_success_chain(*self.run_batch(), mode="1")

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

    def test_normal_failures_remain_fail_fast(self):
        for index, category in enumerate(("TV", "REF", "LDY"), 1):
            with self.subTest(category=category):
                result, calls = self.run_batch(["4"], fail=category)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), index, result.stdout + result.stderr)

    def test_resume_rejects_legacy_modes_before_any_python(self):
        for mode in ("1", "2", "3"):
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
