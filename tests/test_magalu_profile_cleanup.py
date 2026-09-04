import os
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from seda.magalu import profile_cleanup


DiskUsage = namedtuple("DiskUsage", "total used free")


class MagaluProfileCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "seda_magalu_profiles"
        self.root.mkdir()
        self.current = (
            self.root
            / "run_magalu_casas_interleaved_tv_ref_ldy_20260902_070000"
        )
        self.env = {
            "SEDA_MAGALU_PROFILE_ROOT": str(self.root),
            "SEDA_MAGALU_BROWSER_PROFILE": str(self.current),
            "SEDA_MAGALU_PROFILE_CLEANUP": "1",
            "SEDA_MAGALU_PROFILE_RETENTION_HOURS": "48",
            "SEDA_STORAGE_MIN_FREE_GB": "2",
            "SEDA_PROFILE_DELETE_ATTEMPTS": "1",
            "SEDA_PROFILE_DELETE_RETRY_SECONDS": "0",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_prepare_deletes_only_expired_managed_profiles(self):
        now = time.time()
        stale = self._profile(
            "run_magalu_casas_interleaved_tv_ref_ldy_20260829_070000",
            age_hours=72,
        )
        stale_worker = self._profile(
            "run_magalu_casas_interleaved_tv_ref_ldy_20260829_070000_w0",
            age_hours=72,
        )
        recent = self._profile(
            "run_magalu_casas_interleaved_tv_ref_ldy_20260901_070000",
            age_hours=24,
        )
        other_series = self._profile(
            "run_magalu_full_20260829_060000",
            age_hours=72,
        )
        current_worker = self._profile(f"{self.current.name}_w0", age_hours=72)
        unrelated = self._profile("keep_me", age_hours=240)

        with patch.dict(os.environ, self.env, clear=False):
            result = profile_cleanup.prepare(
                now=now,
                disk_usage_func=self._disk_usage(10),
            )

        self.assertTrue(result["success"])
        self.assertFalse(stale.exists())
        self.assertFalse(stale_worker.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(other_series.exists())
        self.assertTrue(current_worker.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(len(result["stale_deleted"]), 2)
        self.assertGreater(result["freed_bytes"], 0)

    def test_finalize_deletes_current_base_and_workers_only(self):
        base = self._profile(self.current.name)
        worker_zero = self._profile(f"{self.current.name}_w0")
        worker_two = self._profile(f"{self.current.name}_w2")
        lookalike = self._profile(f"{self.current.name}_backup")
        other_run = self._profile("run_magalu_full_20260902_060000")

        with patch.dict(os.environ, self.env, clear=False):
            result = profile_cleanup.finalize(
                disk_usage_func=self._disk_usage(10),
            )

        self.assertTrue(result["success"])
        self.assertFalse(base.exists())
        self.assertFalse(worker_zero.exists())
        self.assertFalse(worker_two.exists())
        self.assertTrue(lookalike.exists())
        self.assertTrue(other_run.exists())
        self.assertEqual(len(result["deleted"]), 3)

    def test_prepare_preserves_tombstone_from_other_profile_series(self):
        tombstone_root = self.root / ".seda_profile_cleanup"
        same_series = (
            tombstone_root
            / "run_magalu_casas_interleaved_tv_ref_ldy_20260829_070000"
            ".1.1.deleting"
        )
        other_series = (
            tombstone_root / "run_magalu_full_20260829_070000.1.2.deleting"
        )
        for path in (same_series, other_series):
            path.mkdir(parents=True, exist_ok=True)
            (path / "cache.bin").write_bytes(b"profile-cache")

        with patch.dict(os.environ, self.env, clear=False):
            result = profile_cleanup.prepare(
                disk_usage_func=self._disk_usage(10),
            )

        self.assertTrue(result["success"])
        self.assertFalse(same_series.exists())
        self.assertTrue(other_series.exists())
        self.assertEqual(len(result["tombstones_deleted"]), 1)

    def test_prepare_fails_before_collection_when_free_space_is_low(self):
        with patch.dict(os.environ, self.env, clear=False):
            result = profile_cleanup.prepare(
                disk_usage_func=self._disk_usage(1.9),
            )

        self.assertFalse(result["success"])
        self.assertFalse(result["sufficient"])
        self.assertEqual(result["required_free_bytes"], 2 * 1024**3)

    def test_profile_root_with_unmanaged_name_is_rejected(self):
        unmanaged_root = Path(self.temp_dir.name) / "not_a_profile_root"
        env = dict(self.env)
        env["SEDA_MAGALU_PROFILE_ROOT"] = str(unmanaged_root)
        env["SEDA_MAGALU_BROWSER_PROFILE"] = str(
            unmanaged_root / self.current.name
        )

        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(
                RuntimeError,
                "profile_root_name_not_managed",
            ):
                profile_cleanup.prepare(disk_usage_func=self._disk_usage(10))

    def test_profile_outside_managed_root_is_rejected(self):
        env = dict(self.env)
        env["SEDA_MAGALU_BROWSER_PROFILE"] = str(
            Path(self.temp_dir.name) / "run_magalu_full_20260902_070000"
        )
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(
                RuntimeError,
                "profile_path_outside_managed_root",
            ):
                profile_cleanup.prepare(disk_usage_func=self._disk_usage(10))

    def test_reparse_profile_root_is_rejected(self):
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch(
                "seda.magalu.profile_cleanup._is_reparse_point",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "profile_root_reparse_point",
            ):
                profile_cleanup.prepare(disk_usage_func=self._disk_usage(10))

    def test_finalize_does_not_partially_delete_a_locked_profile(self):
        base = self._profile(self.current.name)
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch(
                "seda.magalu.profile_cleanup.os.replace",
                side_effect=PermissionError("locked"),
            ),
        ):
            result = profile_cleanup.finalize(
                disk_usage_func=self._disk_usage(10),
            )

        self.assertFalse(result["success"])
        self.assertTrue(base.exists())
        self.assertEqual(len(result["cleanup_errors"]), 1)

    def test_old_tombstone_warning_does_not_hide_current_cleanup_success(self):
        base = self._profile(self.current.name)
        tombstone_result = {
            "deleted": [],
            "freed_bytes": 0,
            "errors": [{"path": "old", "error": "locked"}],
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch(
                "seda.magalu.profile_cleanup._cleanup_tombstones",
                return_value=tombstone_result,
            ),
        ):
            result = profile_cleanup.finalize(
                disk_usage_func=self._disk_usage(10),
            )

        self.assertTrue(result["success"])
        self.assertFalse(base.exists())
        self.assertEqual(result["cleanup_warnings"], tombstone_result["errors"])

    def test_integrated_bat_enables_three_day_retention_and_finalization(self):
        bat_path = (
            Path(__file__).resolve().parents[1]
            / "run_magalu_casas_interleaved_tv_ref_ldy_full.bat"
        )
        text = bat_path.read_text(encoding="utf-8")

        self.assertIn("SEDA_LOCAL_CLEANUP=1", text)
        self.assertIn("SEDA_LOCAL_RETENTION_DAYS=3", text)
        self.assertIn("SEDA_MAGALU_PROFILE_RETENTION_HOURS=48", text)
        self.assertIn("SEDA_STORAGE_MIN_FREE_GB=2", text)
        self.assertIn("python -m seda.magalu.step12_local_cleanup", text)
        self.assertIn("python -m seda.magalu.profile_cleanup prepare", text)
        self.assertIn("python -m seda.magalu.profile_cleanup finalize", text)
        self.assertLess(
            text.index("call :prepare_storage"),
            text.index("call :run_magalu TV"),
        )
        self.assertLess(
            text.index("call :run_casas LDY"),
            text.index("python -m seda.magalu.profile_cleanup finalize"),
        )
        main_flow = text[
            text.index('set "SEDA_BATCH_EXIT_CODE=0"') : text.index("\n:run_magalu\n")
        ]
        self.assertNotIn("exit /b 1", main_flow)
        self.assertGreaterEqual(main_flow.count("goto :finish"), 6)

    def _profile(self, name, age_hours=0):
        path = self.root / name
        path.mkdir()
        payload = path / "cache.bin"
        payload.write_bytes(b"profile-cache")
        modified = time.time() - age_hours * 3600
        os.utime(payload, (modified, modified))
        os.utime(path, (modified, modified))
        return path

    @staticmethod
    def _disk_usage(free_gb):
        free = int(free_gb * 1024**3)
        return lambda _path: DiskUsage(20 * 1024**3, 0, free)


if __name__ == "__main__":
    unittest.main()
