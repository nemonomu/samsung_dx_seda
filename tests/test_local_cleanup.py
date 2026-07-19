import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from seda import step12_local_cleanup


class LocalCleanupTests(unittest.TestCase):
    def _old(self, path, days=45):
        timestamp = time.time() - (days * 24 * 60 * 60)
        os.utime(path, (timestamp, timestamp))

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
            self._old(nested_run)

            env = {"SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_RETENTION_DAYS": "30"}
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(step12_local_cleanup, "run_root", return_value=current):
                step12_local_cleanup.main()

            self.assertTrue((nested_run / "payload.txt").is_file())

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
            self._old(expired)

            env = {"SEDA_LOCAL_CLEANUP": "1", "SEDA_LOCAL_RETENTION_DAYS": "30"}
            with patch.dict(os.environ, env, clear=True), patch.object(
                step12_local_cleanup, "DEFAULT_RUNS_BASE", base
            ), patch.object(step12_local_cleanup, "run_root", return_value=current):
                step12_local_cleanup.main()

            self.assertTrue((expired / "payload.txt").is_file())
            manifest = json.loads(
                (current / "cleanup" / "manifest_local_cleanup.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["skip_reason"], "current_run_outside_default_cleanup_base")

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


if __name__ == "__main__":
    unittest.main()
