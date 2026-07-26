import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from seda import step12_local_cleanup


class LocalCleanupTests(unittest.TestCase):
    def _old(self, path, days=45):
        timestamp = time.time() - (days * 24 * 60 * 60)
        os.utime(path, (timestamp, timestamp))

    def _mtime(self, path, when):
        timestamp = when.timestamp()
        os.utime(path, (timestamp, timestamp))

    def test_trace_cleanup_defaults_to_three_days_without_full_run_cleanup(self):
        fixed_now = datetime(2026, 7, 26, 12, 0, 0)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260726"
            expired = base / "magalu" / "tv" / "20260722"
            casas_expired = base / "casas_bahia" / "ref" / "20260722"
            fresh_bundle = base / "magalu" / "ref" / "20260721"
            recent_date = base / "magalu" / "ldy" / "20260725"
            for run in (current, expired, casas_expired, fresh_bundle, recent_date):
                (run / "detail" / "trace").mkdir(parents=True)
                (run / "output").mkdir()

            current_trace = current / "detail" / "trace" / "subcall_trace.csv"
            current_trace.write_text("current", encoding="utf-8")
            self._mtime(current_trace, fixed_now - timedelta(days=10))

            expired_files = [
                expired / "detail" / "trace" / "subcall_trace.csv",
                expired / "detail" / "trace" / "subcall_trace_run_w0.csv",
                expired / "detail" / "trace" / ".subcall_trace.csv.123.456.tmp",
                casas_expired / "detail" / "trace" / "magalu_review_page_trace.csv",
            ]
            for path in expired_files:
                path.write_text("expired", encoding="utf-8")
                self._mtime(path, fixed_now - timedelta(days=3, seconds=1))
            self._mtime(expired_files[-1], fixed_now - timedelta(days=3))

            unrelated_trace = expired / "detail" / "trace" / "notes.csv"
            unrelated_trace.write_text("keep", encoding="utf-8")
            self._mtime(unrelated_trace, fixed_now - timedelta(days=10))
            outside_trace = expired / "output" / "subcall_trace.csv"
            outside_trace.write_text("keep", encoding="utf-8")
            self._mtime(outside_trace, fixed_now - timedelta(days=10))

            old_canonical = fresh_bundle / "detail" / "trace" / "subcall_trace.csv"
            fresh_worker = fresh_bundle / "detail" / "trace" / "subcall_trace_run_w1.csv"
            old_canonical.write_text("old canonical", encoding="utf-8")
            fresh_worker.write_text("fresh worker", encoding="utf-8")
            self._mtime(old_canonical, fixed_now - timedelta(days=10))
            self._mtime(fresh_worker, fixed_now - timedelta(days=2))

            recent_old_trace = recent_date / "detail" / "trace" / "subcall_trace.csv"
            recent_old_trace.write_text("recent dated run", encoding="utf-8")
            self._mtime(recent_old_trace, fixed_now - timedelta(days=10))

            env = {"SEDA_LOCAL_CLEANUP": "0"}
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(
                step12_local_cleanup, "run_root", return_value=current
            ), patch.object(
                step12_local_cleanup, "datetime", FixedDateTime
            ):
                step12_local_cleanup.main()

            self.assertTrue(current_trace.is_file())
            for path in expired_files:
                self.assertFalse(path.exists(), path)
            self.assertTrue(unrelated_trace.is_file())
            self.assertTrue(outside_trace.is_file())
            self.assertTrue(old_canonical.is_file())
            self.assertTrue(fresh_worker.is_file())
            self.assertTrue(recent_old_trace.is_file())
            self.assertTrue(expired.is_dir())
            self.assertTrue(casas_expired.is_dir())

            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            trace_cleanup = manifest["detail_trace_cleanup"]
            self.assertIs(trace_cleanup["enabled"], True)
            self.assertEqual(trace_cleanup["retention_days"], 3)
            self.assertEqual(
                set(trace_cleanup["deleted_files"]),
                {str(path.resolve()) for path in expired_files},
            )
            self.assertGreater(trace_cleanup["deleted_bytes"], 0)
            self.assertEqual(manifest["deleted"], [])
            self.assertEqual(
                manifest["run_cleanup_skip_reason"],
                "SEDA_LOCAL_CLEANUP is disabled.",
            )

    def test_invalid_trace_retention_rejects_all_deletion_before_start(self):
        fixed_now = datetime(2026, 7, 26, 12, 0, 0)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260726"
            expired = base / "magalu" / "tv" / "20260501"
            current.mkdir(parents=True)
            trace = expired / "detail" / "trace" / "subcall_trace.csv"
            trace.parent.mkdir(parents=True)
            trace.write_text("keep", encoding="utf-8")
            self._mtime(trace, fixed_now - timedelta(days=60))
            self._mtime(expired, fixed_now - timedelta(days=60))

            env = {
                "SEDA_LOCAL_CLEANUP": "1",
                "SEDA_LOCAL_RETENTION_DAYS": "30",
                "SEDA_DETAIL_TRACE_CLEANUP": "1",
                "SEDA_DETAIL_TRACE_RETENTION_DAYS": "-1",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(
                step12_local_cleanup, "run_root", return_value=current
            ), patch.object(
                step12_local_cleanup, "datetime", FixedDateTime
            ):
                with self.assertRaisesRegex(ValueError, "SEDA_DETAIL_TRACE_RETENTION_DAYS"):
                    step12_local_cleanup.main()

            self.assertTrue(trace.is_file())
            self.assertTrue(expired.is_dir())
            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertIs(manifest["success"], False)
            self.assertEqual(manifest["deleted"], [])

    def test_trace_cleanup_preserves_partial_progress_when_unlink_fails(self):
        fixed_now = datetime(2026, 7, 26, 12, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260726"
            old_run = base / "magalu" / "tv" / "20260720"
            current.mkdir(parents=True)
            trace_dir = old_run / "detail" / "trace"
            trace_dir.mkdir(parents=True)
            first = trace_dir / "subcall_trace_a.csv"
            locked = trace_dir / "subcall_trace_z.csv"
            first.write_text("first", encoding="utf-8")
            locked.write_text("locked", encoding="utf-8")
            for path in (first, locked):
                self._mtime(path, fixed_now - timedelta(days=10))
            original_iterdir = Path.iterdir
            original_unlink = Path.unlink

            def ordered_iterdir(path):
                children = list(original_iterdir(path))
                return iter(sorted(children)) if path == trace_dir else iter(children)

            def fail_locked(path, *args, **kwargs):
                if path == locked:
                    raise PermissionError("locked trace")
                return original_unlink(path, *args, **kwargs)

            result = {"deleted_files": [], "deleted_bytes": 0}
            with patch.object(Path, "iterdir", ordered_iterdir), patch.object(
                Path, "unlink", fail_locked
            ):
                step12_local_cleanup._cleanup_detail_trace_files(
                    [old_run],
                    current=current,
                    base=base,
                    retention_days=3,
                    now=fixed_now,
                    result=result,
                )

            self.assertFalse(first.exists())
            self.assertTrue(locked.is_file())
            self.assertEqual(result["deleted_files"], [str(first.resolve())])
            self.assertEqual(result["deleted_bytes"], len("first"))
            self.assertEqual(result["errors"][0]["path"], str(locked))
            self.assertIn("PermissionError:locked trace", result["errors"][0]["error"])

    def test_trace_cleanup_file_error_does_not_fail_main(self):
        fixed_now = datetime(2026, 7, 26, 12, 0, 0)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260726"
            old_run = base / "magalu" / "tv" / "20260720"
            current.mkdir(parents=True)
            locked = old_run / "detail" / "trace" / "subcall_trace.csv"
            locked.parent.mkdir(parents=True)
            locked.write_text("locked", encoding="utf-8")
            self._mtime(locked, fixed_now - timedelta(days=10))
            original_unlink = Path.unlink

            def fail_locked(path, *args, **kwargs):
                if path == locked:
                    raise PermissionError("locked trace")
                return original_unlink(path, *args, **kwargs)

            env = {
                "SEDA_LOCAL_CLEANUP": "0",
                "SEDA_DETAIL_TRACE_CLEANUP": "1",
                "SEDA_DETAIL_TRACE_RETENTION_DAYS": "3",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(
                step12_local_cleanup, "run_root", return_value=current
            ), patch.object(
                step12_local_cleanup, "datetime", FixedDateTime
            ), patch.object(Path, "unlink", fail_locked):
                step12_local_cleanup.main()

            self.assertTrue(locked.is_file())
            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertIs(manifest["success"], True)
            self.assertEqual(manifest["detail_trace_cleanup"]["deleted_files"], [])
            self.assertEqual(
                manifest["detail_trace_cleanup"]["errors"][0]["path"],
                str(locked),
            )

    def test_full_run_cleanup_and_trace_cleanup_do_not_double_count(self):
        fixed_now = datetime(2026, 7, 26, 12, 0, 0)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260726"
            deleted_run = base / "magalu" / "tv" / "20260501"
            trace_only_run = base / "casas_bahia" / "ref" / "20260720"
            current.mkdir(parents=True)
            deleted_run_trace = deleted_run / "detail" / "trace" / "subcall_trace.csv"
            trace_only = trace_only_run / "detail" / "trace" / "subcall_trace.csv"
            for path in (deleted_run_trace, trace_only):
                path.parent.mkdir(parents=True)
                path.write_text("old", encoding="utf-8")
                self._mtime(path, fixed_now - timedelta(days=60))
            self._mtime(deleted_run, fixed_now - timedelta(days=60))
            self._mtime(trace_only_run, fixed_now - timedelta(days=1))
            env = {
                "SEDA_LOCAL_CLEANUP": "1",
                "SEDA_LOCAL_RETENTION_DAYS": "30",
                "SEDA_DETAIL_TRACE_CLEANUP": "1",
                "SEDA_DETAIL_TRACE_RETENTION_DAYS": "3",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(
                step12_local_cleanup, "run_root", return_value=current
            ), patch.object(
                step12_local_cleanup, "datetime", FixedDateTime
            ):
                step12_local_cleanup.main()

            self.assertFalse(deleted_run.exists())
            self.assertTrue(trace_only_run.is_dir())
            self.assertFalse(trace_only.exists())
            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["deleted"], [str(deleted_run.resolve())])
            self.assertEqual(
                manifest["detail_trace_cleanup"]["deleted_files"],
                [str(trace_only.resolve())],
            )

    def test_trace_cleanup_includes_exact_date_and_mtime_boundary(self):
        fixed_now = datetime(2026, 7, 26, 12, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260726"
            boundary_run = base / "magalu" / "tv" / "20260723"
            current.mkdir(parents=True)
            trace = boundary_run / "detail" / "trace" / "subcall_trace.csv"
            trace.parent.mkdir(parents=True)
            trace.write_text("boundary", encoding="utf-8")
            self._mtime(trace, fixed_now - timedelta(days=3))

            result = step12_local_cleanup._cleanup_detail_trace_files(
                [boundary_run],
                current=current,
                base=base,
                retention_days=3,
                now=fixed_now,
            )

            self.assertFalse(trace.exists())
            self.assertEqual(result["deleted_files"], [str(trace.resolve())])

    def test_detail_trace_filename_allowlist_rejects_near_misses(self):
        accepted = (
            "subcall_trace.csv",
            "subcall_trace_run_w0.csv",
            "magalu_review_page_trace.csv",
            ".subcall_trace.csv.123.456.tmp",
            ".magalu_review_page_trace_run_w1.csv.123.456.tmp",
        )
        rejected = (
            "notes.csv",
            "subcall_trace.csv.bak",
            "subcall_trace",
            ".subcall_trace.csv.tmp",
            ".subcall_trace.csv.123.456.tmp.bak",
        )
        for name in accepted:
            with self.subTest(name=name):
                self.assertTrue(step12_local_cleanup._is_detail_trace_file(name))
        for name in rejected:
            with self.subTest(name=name):
                self.assertFalse(step12_local_cleanup._is_detail_trace_file(name))

    def test_both_cleanup_modes_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "external" / "20260726"
            current.mkdir(parents=True)
            trace = current / "detail" / "trace" / "subcall_trace.csv"
            trace.parent.mkdir(parents=True)
            trace.write_text("keep", encoding="utf-8")

            env = {
                "SEDA_LOCAL_CLEANUP": "0",
                "SEDA_DETAIL_TRACE_CLEANUP": "0",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "run_root", return_value=current
            ):
                step12_local_cleanup.main()

            self.assertTrue(trace.is_file())
            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertIs(manifest["detail_trace_cleanup"]["enabled"], False)
            self.assertIn("disabled", manifest["skip_reason"])

    def test_nested_cleanup_deletes_only_expired_run_leaves(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260719"
            current.mkdir(parents=True)
            (current / "current.csv").write_text("current", encoding="utf-8")

            old_same_retailer = base / "magalu" / "tv" / "20260501"
            old_other_retailer = base / "casas_bahia" / "ref" / "20260502"
            old_legacy = base / "20260503"
            old_product_line_legacy = base / "ldy" / "20260504"
            unrelated = base / "network_capture"
            unrelated_dated = unrelated / "20260505"
            for path in (
                old_same_retailer,
                old_other_retailer,
                old_legacy,
                old_product_line_legacy,
                unrelated_dated,
            ):
                path.mkdir(parents=True)
                (path / "payload.txt").write_text("fixture", encoding="utf-8")
                self._old(path / "payload.txt")
                self._old(path)

            # An old parent mtime must never make the current nested run a
            # deletion candidate. Only the dated run leaves are removable.
            self._old(base / "magalu")
            self._old(base / "casas_bahia")
            self._old(current)

            env = {"SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_RETENTION_DAYS": "30"}
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(step12_local_cleanup, "run_root", return_value=current):
                step12_local_cleanup.main()

            self.assertTrue((current / "current.csv").is_file())
            self.assertTrue((current / "cleanup" / "manifest_local_cleanup.json").is_file())
            self.assertTrue((base / "magalu").is_dir())
            self.assertTrue((base / "casas_bahia").is_dir())
            self.assertTrue((unrelated_dated / "payload.txt").is_file())
            self.assertFalse(old_same_retailer.exists())
            self.assertFalse(old_other_retailer.exists())
            self.assertFalse(old_legacy.exists())
            self.assertFalse(old_product_line_legacy.exists())

            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(manifest["deleted"]),
                {
                    str(old_same_retailer),
                    str(old_other_retailer),
                    str(old_legacy),
                    str(old_product_line_legacy),
                },
            )

    def test_cleanup_protects_descendants_of_an_explicit_current_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv"
            nested_run = current / "20260501"
            nested_run.mkdir(parents=True)
            (nested_run / "payload.txt").write_text("current subtree", encoding="utf-8")
            protected_trace = nested_run / "detail" / "trace" / "subcall_trace.csv"
            protected_trace.parent.mkdir(parents=True)
            protected_trace.write_text("current trace", encoding="utf-8")
            self._old(protected_trace)
            self._old(nested_run)

            env = {"SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_RETENTION_DAYS": "30"}
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(step12_local_cleanup, "run_root", return_value=current):
                step12_local_cleanup.main()

            self.assertTrue((nested_run / "payload.txt").is_file())
            self.assertTrue(protected_trace.is_file())

    def test_negative_retention_is_rejected_before_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "data"
            current = base / "magalu" / "tv" / "20260719"
            expired = base / "casas_bahia" / "ref" / "20260501"
            current.mkdir(parents=True)
            expired.mkdir(parents=True)
            (expired / "payload.txt").write_text("keep", encoding="utf-8")
            self._old(expired)

            env = {"SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_RETENTION_DAYS": "-1"}
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(step12_local_cleanup, "run_root", return_value=current):
                with self.assertRaisesRegex(ValueError, "zero or greater"):
                    step12_local_cleanup.main()

            self.assertTrue((expired / "payload.txt").is_file())
            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertIs(manifest["success"], False)
            self.assertIn("ValueError", manifest["error"])

    def test_external_current_root_does_not_clean_default_data_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "data"
            current = root / "external" / "magalu" / "tv" / "20260719"
            expired = base / "magalu" / "tv" / "20260501"
            current.mkdir(parents=True)
            expired.mkdir(parents=True)
            (expired / "payload.txt").write_text("keep", encoding="utf-8")
            trace = expired / "detail" / "trace" / "subcall_trace.csv"
            trace.parent.mkdir(parents=True)
            trace.write_text("keep trace", encoding="utf-8")
            self._old(trace)
            self._old(expired)

            env = {"SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_RETENTION_DAYS": "30"}
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(step12_local_cleanup, "run_root", return_value=current):
                step12_local_cleanup.main()

            self.assertTrue((expired / "payload.txt").is_file())
            self.assertTrue(trace.is_file())
            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["skip_reason"], "current_run_outside_default_cleanup_base")
            self.assertEqual(manifest["detail_trace_cleanup"]["deleted_files"], [])

    @unittest.skipUnless(os.name == "nt" and hasattr(Path, "is_junction"), "Windows junction test")
    def test_junction_inside_run_fails_before_external_content_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "data"
            run = base / "magalu" / "tv" / "20260501"
            outside = root / "outside"
            run.mkdir(parents=True)
            outside.mkdir()
            (run / "ordinary.txt").write_text("keep until safe rejection", encoding="utf-8")
            (outside / "keep.txt").write_text("outside", encoding="utf-8")
            junction = run / "linked"
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                check=True,
                capture_output=True,
                text=True,
            )

            with self.assertRaisesRegex(RuntimeError, "cleanup_reparse_point"):
                step12_local_cleanup._remove_run_directory(run, base)

            self.assertTrue((outside / "keep.txt").is_file())
            self.assertTrue((run / "ordinary.txt").is_file())
            junction.rmdir()

    @unittest.skipUnless(os.name == "nt" and hasattr(Path, "is_junction"), "Windows junction test")
    def test_junction_retailer_parent_is_not_followed_during_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "data"
            outside = root / "outside"
            (outside / "tv" / "20260501").mkdir(parents=True)
            base.mkdir()
            retailer_junction = base / "magalu"
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(retailer_junction), str(outside)],
                check=True,
                capture_output=True,
                text=True,
            )

            skipped = []
            self.assertEqual(step12_local_cleanup._run_directories(base, skipped), [])
            self.assertEqual(skipped, [str(retailer_junction)])
            self.assertTrue((outside / "tv" / "20260501").is_dir())
            retailer_junction.rmdir()

    @unittest.skipUnless(os.name == "nt" and hasattr(Path, "is_junction"), "Windows junction test")
    def test_trace_cleanup_does_not_follow_trace_directory_junction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "data"
            current = base / "magalu" / "tv" / "20260726"
            old_run = base / "magalu" / "tv" / "20260501"
            outside = root / "outside"
            current.mkdir(parents=True)
            (old_run / "detail").mkdir(parents=True)
            outside.mkdir()
            outside_trace = outside / "subcall_trace.csv"
            outside_trace.write_text("outside", encoding="utf-8")
            self._old(outside_trace)
            trace_junction = old_run / "detail" / "trace"
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(trace_junction), str(outside)],
                check=True,
                capture_output=True,
                text=True,
            )

            env = {
                "SEDA_LOCAL_CLEANUP": "0",
                "SEDA_DETAIL_TRACE_CLEANUP": "1",
                "SEDA_DETAIL_TRACE_RETENTION_DAYS": "3",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(step12_local_cleanup, "run_root", return_value=current):
                step12_local_cleanup.main()

            self.assertTrue(outside_trace.is_file())
            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertIn(str(trace_junction), manifest["skipped_reparse"])
            trace_junction.rmdir()


if __name__ == "__main__":
    unittest.main()
