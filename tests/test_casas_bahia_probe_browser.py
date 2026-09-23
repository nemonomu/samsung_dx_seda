"""Offline only: browser, provisioning, copies and recursive cleanup are mocked."""

import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from seda.casas_bahia import browser_probe_session as probe


PARENT = Path(__file__).resolve().parent / "mock_temp_parent"
ROOT = PARENT / (probe._TEMP_PREFIX + "test123")
CHROME = PARENT / "chrome.exe"
DRIVER_NAME = "chromedriver.exe" if os.name == "nt" else "chromedriver"


class FakeOptions:
    def __init__(self):
        self.arguments = []
        self.capabilities = {}
        self.debugger_address = None

    def add_argument(self, value):
        self.arguments.append(value)

    def set_capability(self, key, value):
        self.capabilities[key] = value


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls

        class FakeChrome:
            def __init__(self, **kwargs):
                calls.append(("init", kwargs))
                self.capabilities = {"browserVersion": "153.0.0.0", "chrome": {"chromedriverVersion": "153.0.0.0"}}

            def quit(self):
                calls.append(("quit",))

            def set_script_timeout(self, value):
                calls.append(("script_timeout", value))

            def execute_cdp_cmd(self, method, params):
                calls.append((method, params))

        self.uc = SimpleNamespace(Chrome=FakeChrome, ChromeOptions=FakeOptions,
                                  find_chrome_executable=lambda: str(CHROME))
        patches = [
            patch.dict("sys.modules", {"undetected_chromedriver": self.uc}),
            patch.object(probe.os, "getenv", return_value=None),
            patch.object(probe.Path, "is_file", return_value=True),
            patch.object(probe.browser_listing, "_chrome_major", return_value=153),
            patch.object(probe.tempfile, "gettempdir", return_value=str(PARENT)),
            patch.object(probe.tempfile, "mkdtemp", return_value=str(ROOT)),
            patch.object(probe, "_prepare_driver", return_value=(ROOT / "driver" / DRIVER_NAME, "matching_copy")),
            patch.object(probe.ProbeBrowserSession, "_remove_owned_root"),
        ]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.session = probe.ProbeBrowserSession()
        self.addCleanup(self.session.close)

    def test_exact_probe_options_fresh_profile_and_no_login(self):
        self.session.start()
        kwargs = self.calls[0][1]
        self.assertEqual(list(probe._OPTIONS), kwargs["options"].arguments)
        self.assertEqual({"goog:loggingPrefs": {"performance": "ALL"}}, kwargs["options"].capabilities)
        self.assertEqual(str(ROOT / "chrome_profile"), kwargs["user_data_dir"])
        self.assertEqual(str(ROOT / "driver" / DRIVER_NAME), kwargs["driver_executable_path"])
        self.assertEqual(str(CHROME), kwargs["browser_executable_path"])
        self.assertEqual(153, kwargs["version_main"])
        self.assertEqual(0, kwargs["port"])
        self.assertIs(False, kwargs["user_multi_procs"])
        self.assertIs(False, kwargs["patcher_force_close"])
        self.assertIs(True, kwargs["use_subprocess"])
        self.assertNotIn("headless", kwargs)
        self.mocks[4].assert_called_once_with()
        self.mocks[5].assert_called_once_with(prefix=probe._TEMP_PREFIX, dir=str(PARENT))

    def test_probe_network_and_script_configuration(self):
        self.session.start()
        self.assertEqual([
            ("script_timeout", 30),
            ("Network.enable", {"maxTotalBufferSize": 60000000, "maxResourceBufferSize": 10000000}),
            ("Network.setCacheDisabled", {"cacheDisabled": True}),
            ("Network.setBypassServiceWorker", {"bypass": True}),
        ], self.calls[1:])

    def test_headless_setting_is_not_used_in_probe_mode(self):
        self.mocks[1].side_effect = lambda key, *args: "true" if key == "SEDA_CASAS_BAHIA_BROWSER_HEADLESS" else None
        self.session.start()
        self.assertEqual([(("SEDA_CASAS_BAHIA_CHROME_PATH",), {})], self.mocks[1].call_args_list)

    def test_start_reuses_only_this_session_driver(self):
        self.session.start()
        self.session.start()
        self.assertEqual(1, sum(call[0] == "init" for call in self.calls))

    def test_missing_chrome_does_not_create_temp_tree(self):
        self.mocks[2].return_value = False
        with self.assertRaisesRegex(probe.browser_listing.EvidenceError, "chrome_executable_missing"):
            self.session.start()
        self.mocks[5].assert_not_called()

    def test_existing_browser_attachment_rejected(self):
        options = FakeOptions()
        options.debugger_address = "localhost:12345"
        self.uc.ChromeOptions = lambda: options
        with self.assertRaisesRegex(probe.browser_listing.EvidenceError, "existing_browser_attachment_rejected"):
            self.session.start()
        self.assertFalse(self.calls)
        self.mocks[7].assert_called_once()

    def test_browser_version_mismatch_closes_owned_browser(self):
        original = self.uc.Chrome.__init__

        def mismatch(driver, **kwargs):
            original(driver, **kwargs)
            driver.capabilities["browserVersion"] = "152.0.0.0"

        self.uc.Chrome.__init__ = mismatch
        with self.assertRaisesRegex(probe.browser_listing.EvidenceError, "browser_driver_version_mismatch"):
            self.session.start()
        self.assertIn(("quit",), self.calls)
        self.assertIsNone(self.session.driver)

    def test_partial_constructor_failure_retains_owned_reference_for_quit(self):
        def fail(driver, **kwargs):
            self.calls.append(("partial_init",))
            raise RuntimeError("test")

        self.uc.Chrome.__init__ = fail
        with self.assertRaises(RuntimeError):
            self.session.start()
        self.assertEqual([("partial_init",), ("quit",)], self.calls)

    def test_fetch_pins_bootstrap_timeouts(self):
        with patch.object(probe.browser_listing._BrowserSession, "fetch", return_value={"success": True}) as fetch:
            self.assertEqual({"success": True}, self.session.fetch("https://example.invalid/", timeout=1))
        fetch.assert_called_once_with("https://example.invalid/", timeout=45, wait_seconds=30)


class DriverTests(unittest.TestCase):
    def make_uc(self):
        records = []

        class Patcher:
            data_path = str(PARENT / "shared_cache")

            def __init__(self, **kwargs):
                records.append((self.data_path, kwargs))
                self.executable_path = str(Path(self.data_path) / ("undetected_" + DRIVER_NAME))

            def auto(self):
                records.append("auto")

        return SimpleNamespace(Patcher=Patcher), records

    def test_matching_copy_never_provisions_or_changes_patcher(self):
        uc, records = self.make_uc()
        with patch.object(probe.Path, "mkdir"), patch.object(probe.Path, "is_file", return_value=True), \
                patch.object(probe, "_driver_major", return_value=153), patch.object(probe.shutil, "copy2") as copy:
            result = probe._prepare_driver(uc, ROOT, 153)
        expected = ROOT / "driver" / DRIVER_NAME
        self.assertEqual((expected, "matching_copy"), result)
        copy.assert_called_once_with(PARENT / "shared_cache" / ("undetected_" + DRIVER_NAME), expected)
        self.assertFalse(records)
        self.assertEqual(str(PARENT / "shared_cache"), uc.Patcher.data_path)

    def test_missing_cached_driver_provisions_only_private_cache(self):
        uc, records = self.make_uc()
        with patch.object(probe.Path, "mkdir"), patch.object(probe.Path, "is_file", return_value=False), \
                patch.object(probe, "_driver_major", return_value=153), patch.object(probe.shutil, "copy2") as copy:
            result = probe._prepare_driver(uc, ROOT, 153)
        self.assertEqual((ROOT / "driver" / DRIVER_NAME, "private_provision"), result)
        self.assertEqual([(str(ROOT / "uc_cache"), {"version_main": 153, "force": False, "user_multi_procs": False}), "auto"], records)
        self.assertEqual(str(PARENT / "shared_cache"), uc.Patcher.data_path)
        copy.assert_called_once_with(ROOT / "uc_cache" / ("undetected_" + DRIVER_NAME), ROOT / "driver" / DRIVER_NAME)

    def test_wrong_cached_major_provisions_matching_version(self):
        uc, records = self.make_uc()
        with patch.object(probe.Path, "mkdir"), patch.object(probe.Path, "is_file", return_value=True), \
                patch.object(probe, "_driver_major", side_effect=[152, 153, 153]), patch.object(probe.shutil, "copy2"):
            self.assertEqual("private_provision", probe._prepare_driver(uc, ROOT, 153)[1])
        self.assertEqual("auto", records[-1])

    def test_provisioned_wrong_major_is_rejected_before_copy(self):
        uc, _ = self.make_uc()
        with patch.object(probe.Path, "mkdir"), patch.object(probe.Path, "is_file", return_value=False), \
                patch.object(probe, "_driver_major", return_value=152), patch.object(probe.shutil, "copy2") as copy:
            with self.assertRaisesRegex(probe.browser_listing.EvidenceError, "isolated_driver_major_mismatch"):
                probe._prepare_driver(uc, ROOT, 153)
        copy.assert_not_called()

    def test_driver_version_command_is_bounded_and_headless(self):
        with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(stdout="ChromeDriver 153.0.1.2")) as run:
            self.assertEqual(153, probe._driver_major(ROOT / DRIVER_NAME))
        args, kwargs = run.call_args
        self.assertEqual([str(ROOT / DRIVER_NAME), "--version"], args[0])
        self.assertEqual(10, kwargs["timeout"])
        self.assertTrue(kwargs["check"])
        if os.name == "nt":
            self.assertEqual(subprocess.CREATE_NO_WINDOW, kwargs["creationflags"])


class CleanupTests(unittest.TestCase):
    def session(self):
        session = probe.ProbeBrowserSession()
        session._owned_parent = PARENT
        session._owned_root = ROOT
        return session

    def test_only_exact_owned_temp_tree_removed_after_quit(self):
        session = self.session()
        order = []
        session.driver = SimpleNamespace(quit=lambda: order.append("quit"))
        with patch.object(probe.Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR)), \
                patch.object(probe.shutil, "rmtree", side_effect=lambda path: order.append(path)) as remove:
            session.close()
            session.close()
        self.assertEqual(["quit", ROOT], order)
        remove.assert_called_once_with(ROOT)
        self.assertIsNone(session._owned_root)

    def test_close_failure_never_deletes_even_on_repeat_close(self):
        session = self.session()
        session.driver = SimpleNamespace(quit=Mock(side_effect=RuntimeError("test")))
        with patch.object(probe.shutil, "rmtree") as remove:
            session.close()
            session.close()
        remove.assert_not_called()
        self.assertEqual(ROOT, session._owned_root)

    def test_broad_unrelated_and_external_roots_rejected(self):
        for root in (PARENT, PARENT / "other", PARENT / probe._TEMP_PREFIX,
                     PARENT.parent / (probe._TEMP_PREFIX + "foreign")):
            with self.subTest(root=root):
                session = self.session()
                session._owned_root = root
                with patch.object(probe.shutil, "rmtree") as remove:
                    session.close()
                remove.assert_not_called()

    def test_symlink_or_windows_reparse_root_never_removed(self):
        for info in (SimpleNamespace(st_mode=stat.S_IFLNK),
                     SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            session = self.session()
            with patch.object(probe.Path, "resolve", return_value=ROOT), \
                    patch.object(probe.Path, "lstat", return_value=info), \
                    patch.object(probe.shutil, "rmtree") as remove:
                session.close()
            remove.assert_not_called()

    def test_locked_owned_tree_is_retained_without_cleanup_error(self):
        session = self.session()
        with patch.object(probe.Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR)), \
                patch.object(probe.shutil, "rmtree", side_effect=PermissionError("test")):
            session.close()
        self.assertEqual(ROOT, session._owned_root)


if __name__ == "__main__":
    unittest.main()
