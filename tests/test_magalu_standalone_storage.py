import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from seda import step12_local_cleanup
from seda.common import orchestrator
from seda.magalu import profile_cleanup


ROOT = Path(__file__).resolve().parents[1]
BAT = "run_magalu_tv_ref_ldy_full.bat"


@unittest.skipUnless(os.name == "nt", "Requires Windows cmd.exe")
class StandaloneBatchStorageTests(unittest.TestCase):
    def _run_batch(self, fail="", overrides=None):
        # Only this copied BAT runs. Local command stubs prevent all collection,
        # networking, environment loading, and real profile/data deletion.
        with tempfile.TemporaryDirectory(prefix="magalu_bat_test_") as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / BAT, root / BAT)
            (root / "powershell.cmd").write_text(
                "@echo off\necho 20260923_120000\nexit /b 0\n", encoding="utf-8"
            )
            (root / "python.cmd").write_text(
                f'@echo off\n"{sys.executable}" -B "%~dp0fake_python.py" %*\n'
                'exit /b %errorlevel%\n', encoding="utf-8"
            )
            (root / "fake_python.py").write_text('''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
module = args[1]
if module.endswith("profile_cleanup"):
    action = args[2]
elif module.endswith("step12_local_cleanup"):
    action = "local"
elif module == "seda.magalu.magalu_orchestrator":
    action = args[args.index("--product-line") + 1]
else:
    raise SystemExit(99)
names = ("SEDA_LOCAL_CLEANUP", "SEDA_LOCAL_RETENTION_DAYS",
         "SEDA_LOCAL_CLEANUP_RETAILER", "SEDA_MAGALU_PROFILE_CLEANUP",
         "SEDA_MAGALU_PROFILE_RETENTION_HOURS", "SEDA_STORAGE_MIN_FREE_GB",
         "SEDA_RUN_ROOT", "SEDA_PRODUCT_LINE")
event = {"action": action, "args": args, "env": {name: os.getenv(name) for name in names}}
with (Path(__file__).parent / "events.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps(event) + "\\n")
print("storage-test:" + action, flush=True)
raise SystemExit(7 if action in os.getenv("TEST_FAIL_ACTION", "").split(",") else 0)
''', encoding="utf-8")
            # Pass only OS settings and controlled test values to subprocesses.
            env = {name: os.environ[name] for name in (
                "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH",
                "PATHEXT", "SystemDrive"
            ) if name in os.environ}
            env.update(overrides or {})
            env["TEST_FAIL_ACTION"] = fail
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", BAT],
                cwd=root, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=45,
            )
            events_file = root / "events.jsonl"
            self.assertTrue(events_file.exists(), result.stdout + result.stderr)
            events = [json.loads(line) for line in events_file.read_text(
                encoding="utf-8").splitlines()]
            log = (root / "seda" / "magalu" / "log" /
                   "magalu_tv_ref_ldy_full_20260923_120000.log").read_text(encoding="utf-8")
            return result, events, log

    def test_success_cleans_once_before_tv_without_post_collection_cleanup(self):
        result, events, log = self._run_batch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([e["action"] for e in events],
                         ["local", "prepare", "capacity", "TV", "REF", "LDY"])
        for event in events[:3]:
            self.assertEqual(event["env"]["SEDA_LOCAL_CLEANUP"], "1")
            self.assertEqual(event["env"]["SEDA_LOCAL_RETENTION_DAYS"], "3")
            self.assertEqual(event["env"]["SEDA_LOCAL_CLEANUP_RETAILER"], "magalu")
            self.assertEqual(event["env"]["SEDA_MAGALU_PROFILE_CLEANUP"], "1")
            self.assertEqual(event["env"]["SEDA_MAGALU_PROFILE_RETENTION_HOURS"], "48")
            self.assertEqual(event["env"]["SEDA_STORAGE_MIN_FREE_GB"], "2")
        for event in events[3:]:
            self.assertIn("--skip-local-cleanup", event["args"])
            self.assertIsNone(event["env"]["SEDA_LOCAL_CLEANUP"])
            self.assertIsNone(event["env"]["SEDA_LOCAL_CLEANUP_RETAILER"])
        for action in ("local", "prepare", "capacity"):
            self.assertIn("storage-test:" + action, log)
        self.assertIn("full run completed", log)

    def test_deletion_failures_do_not_stop_collection(self):
        for failed in ("local", "prepare", "local,prepare"):
            with self.subTest(failed=failed):
                result, events, log = self._run_batch(failed)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([e["action"] for e in events],
                                 ["local", "prepare", "capacity", "TV", "REF", "LDY"])
                self.assertIn("WARNING", log)
                self.assertIn("full run completed", log)

    def test_each_product_failure_stops_later_products_without_deleting(self):
        for index, product in enumerate(("TV", "REF", "LDY")):
            with self.subTest(product=product):
                result, events, log = self._run_batch(product)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([e["action"] for e in events],
                                 ["local", "prepare", "capacity"] + ["TV", "REF", "LDY"][:index+1])
                self.assertIn(f"Magalu {product} full run failed", log)
                self.assertNotIn("full run completed", log)

    def test_capacity_failure_alone_blocks_collection_even_after_cleanup_error(self):
        for fail in ("capacity", "local,prepare,capacity"):
            with self.subTest(fail=fail):
                result, events, log = self._run_batch(fail)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([e["action"] for e in events], ["local", "prepare", "capacity"])
                self.assertIn("collection was not started", log)
                self.assertNotIn("full run completed", log)

    def test_cleanup_warning_does_not_mask_real_collection_failure(self):
        result, events, log = self._run_batch("local,prepare,TV")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([e["action"] for e in events], ["local", "prepare", "capacity", "TV"])
        self.assertIn("Magalu TV full run failed", log)

    def test_preparation_keeps_existing_collection_overrides_local(self):
        result, events, _ = self._run_batch(overrides={
            "SEDA_RUN_ROOT": "custom-run-root", "SEDA_PRODUCT_LINE": "LDY",
            "SEDA_MAGALU_PROFILE_RETENTION_HOURS": "72",
            "SEDA_STORAGE_MIN_FREE_GB": "4",
        })
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(events[0]["env"]["SEDA_RUN_ROOT"])
        self.assertEqual(events[0]["env"]["SEDA_PRODUCT_LINE"], "TV")
        self.assertEqual(events[3]["env"]["SEDA_RUN_ROOT"], "custom-run-root")
        self.assertEqual(events[3]["env"]["SEDA_PRODUCT_LINE"], "LDY")
        self.assertEqual(events[1]["env"]["SEDA_MAGALU_PROFILE_RETENTION_HOURS"], "72")
        self.assertEqual(events[1]["env"]["SEDA_STORAGE_MIN_FREE_GB"], "4")


class StartupOnlyPipelineTests(unittest.TestCase):
    def test_skip_flag_removes_only_cleanup_for_each_product(self):
        for product in ("TV", "REF", "LDY"):
            for skip in (False, True):
                with self.subTest(product=product, skip=skip):
                    args = ["test", "--all", "--product-line", product]
                    if skip:
                        args.append("--skip-local-cleanup")
                    with patch.dict(os.environ, {
                        # Even inherited cleanup switches cannot trigger deletion
                        # when this BAT supplies the explicit skip flag.
                        "SEDA_LOCAL_CLEANUP": "1", "SEDA_DETAIL_TRACE_CLEANUP": "1",
                    }, clear=True), patch("sys.argv", args), patch.object(
                        orchestrator, "run_module", return_value=0
                    ) as run, patch.object(orchestrator, "assert_detail_publish_complete"), patch.object(
                        orchestrator, "start_zenrows_usage_execution"
                    ):
                        orchestrator.run_retailer_orchestrator("magalu", "seda.magalu", "test")
                    expected = [step.module for step in orchestrator.steps_for("seda.magalu")
                                if step.number != 0 and (not skip or step.name != "local_cleanup")]
                    self.assertEqual([c.args[0] for c in run.call_args_list], expected)
                    self.assertIn("seda.magalu.step14_db_load", expected)
                    self.assertIn("seda.magalu.step10_status_check", expected)

    def test_capacity_does_not_delete_and_accepts_custom_profile_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "custom_profile" / "not_created_yet"
            with patch.dict(os.environ, {
                "SEDA_MAGALU_BROWSER_PROFILE": str(profile),
                "SEDA_STORAGE_MIN_FREE_GB": "2",
            }, clear=True), patch.object(profile_cleanup, "cleanup_stale_profiles") as delete:
                seen = []
                def disk(path):
                    seen.append(path)
                    return SimpleNamespace(free=2 * 1024**3)
                self.assertTrue(profile_cleanup.capacity(disk_usage_func=disk)["success"])
                self.assertFalse(profile_cleanup.capacity(
                    disk_usage_func=lambda _: SimpleNamespace(free=2 * 1024**3 - 1)
                )["success"])
                self.assertEqual(seen, [root])
                delete.assert_not_called()
                self.assertFalse(profile.parent.exists())

    def test_capacity_still_checked_when_profile_cleanup_is_disabled(self):
        with patch.dict(os.environ, {"SEDA_MAGALU_PROFILE_CLEANUP": "0"}, clear=True), patch.object(
            profile_cleanup, "capacity", return_value={"success": False, "mode": "capacity"}
        ) as check:
            self.assertEqual(profile_cleanup.main(["capacity"]), 1)
            check.assert_called_once_with()


class StandaloneCleanupScopeTests(unittest.TestCase):
    def _old_file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-test-data", encoding="utf-8")
        old = time.time() - 10 * 86400
        os.utime(path, (old, old))
        os.utime(path.parent, (old, old))

    def test_magalu_scope_cleans_all_products_but_preserves_other_data(self):
        with tempfile.TemporaryDirectory(prefix="magalu_scope_test_") as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260923"
            current.mkdir(parents=True)
            expired = [base / "magalu" / product / "20260901" for product in ("tv", "ref", "ldy")]
            preserved = [base / "casas_bahia" / "tv" / "20260901",
                         base / "tv" / "20260901", base / "20260901", current]
            for run in expired + preserved:
                self._old_file(run / "data.csv")
            recent = base / "magalu" / "tv" / "20260922"
            recent.mkdir(parents=True)
            global_detached = base / ".seda_cleanup" / "20260901.1.1.deleting"
            scoped_detached = base / "magalu" / ".seda_cleanup" / "20260901.1.2.deleting"
            for path in (global_detached, scoped_detached):
                self._old_file(path / "data.csv")
            with patch.dict(os.environ, {
                "SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_RETENTION_DAYS": "3",
                "SEDA_LOCAL_CLEANUP_RETAILER": "magalu",
            }, clear=True), patch.object(step12_local_cleanup, "DEFAULT_RUNS_BASE", base), patch.object(
                step12_local_cleanup, "run_root", return_value=current
            ):
                step12_local_cleanup.main()
            for path in expired + [scoped_detached]:
                self.assertFalse(path.exists(), path)
            for path in preserved + [recent, global_detached]:
                self.assertTrue(path.exists(), path)
            manifest = json.loads((current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["deleted"]), {str(path.resolve()) for path in expired})

    def test_invalid_retailer_rejected_before_deletion(self):
        for retailer in ("../casas_bahia", "unknown", "magalu/casas_bahia"):
            with self.subTest(retailer=retailer), tempfile.TemporaryDirectory() as directory:
                base = Path(directory) / "data"
                current = base / "magalu" / "tv" / "20260923"
                current.mkdir(parents=True)
                old_run = base / "magalu" / "tv" / "20260901"
                self._old_file(old_run / "data.csv")
                with patch.dict(os.environ, {
                    "SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_CLEANUP_RETAILER": retailer,
                }, clear=True), patch.object(step12_local_cleanup, "DEFAULT_RUNS_BASE", base), patch.object(
                    step12_local_cleanup, "run_root", return_value=current
                ):
                    with self.assertRaisesRegex(ValueError, "SEDA_LOCAL_CLEANUP_RETAILER"):
                        step12_local_cleanup.main()
                self.assertTrue(old_run.exists())

    def test_scoped_cleanup_rejects_reparse_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260923"
            current.mkdir(parents=True)
            with patch.dict(os.environ, {
                "SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_CLEANUP_RETAILER": "magalu",
            }, clear=True), patch.object(step12_local_cleanup, "DEFAULT_RUNS_BASE", base), patch.object(
                step12_local_cleanup, "run_root", return_value=current
            ), patch.object(step12_local_cleanup, "_is_reparse_point", side_effect=lambda p: Path(p) == base):
                with self.assertRaisesRegex(RuntimeError, "cleanup_reparse_point"):
                    step12_local_cleanup.main()

    def test_trace_only_cleanup_is_also_restricted_to_magalu(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20990101"
            current.mkdir(parents=True)
            traces = [base / retailer / "tv" / "20200101" / "detail" / "trace" / "subcall_trace.csv"
                      for retailer in ("magalu", "casas_bahia")]
            for trace in traces:
                self._old_file(trace)
            with patch.dict(os.environ, {
                "SEDA_LOCAL_CLEANUP": "0", "SEDA_DETAIL_TRACE_CLEANUP": "1",
                "SEDA_LOCAL_CLEANUP_RETAILER": "magalu",
            }, clear=True), patch.object(step12_local_cleanup, "DEFAULT_RUNS_BASE", base), patch.object(
                step12_local_cleanup, "run_root", return_value=current
            ):
                step12_local_cleanup.main()
            self.assertFalse(traces[0].exists())
            self.assertTrue(traces[1].exists())

    def test_standalone_profile_lifecycle_preserves_integrated_and_recent_profiles(self):
        with tempfile.TemporaryDirectory(prefix="magalu_profiles_test_") as directory:
            root = Path(directory) / "seda_magalu_profiles"
            current = root / "run_magalu_tv_ref_ldy_20260923_120000"
            worker = root / (current.name + "_w0")
            old = root / "run_magalu_tv_ref_ldy_20260901_120000"
            old_worker = root / (old.name + "_w2")
            other = root / "run_magalu_casas_interleaved_tv_ref_ldy_20260901_120000"
            recent = root / "run_magalu_tv_ref_ldy_20260922_120000"
            for path in (current, worker, old, old_worker, other):
                self._old_file(path / "cache.bin")
            recent.mkdir()
            with patch.dict(os.environ, {
                "SEDA_MAGALU_PROFILE_ROOT": str(root),
                "SEDA_MAGALU_BROWSER_PROFILE": str(current),
                "SEDA_MAGALU_PROFILE_RETENTION_HOURS": "48",
                "SEDA_STORAGE_MIN_FREE_GB": "2",
            }, clear=True):
                disk = lambda _: SimpleNamespace(free=5 * 1024**3)
                prepared = profile_cleanup.prepare(disk_usage_func=disk)
                self.assertTrue(prepared["success"])
                self.assertFalse(old.exists())
                self.assertFalse(old_worker.exists())
                for path in (current, worker, other, recent):
                    self.assertTrue(path.exists(), path)
                finalized = profile_cleanup.finalize(disk_usage_func=disk)
                self.assertTrue(finalized["success"])
                self.assertFalse(current.exists())
                self.assertFalse(worker.exists())
                self.assertTrue(other.exists())
                self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
