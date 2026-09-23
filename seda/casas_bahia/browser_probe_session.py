"""Mode 3 startup pinned to the successful, logged-out browser experiment.

Only this session's fresh profile, driver copy and private provisioning cache
are owned. Existing Chrome profiles and login state are never used.
"""

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from . import browser_listing


_TEMP_PREFIX = "seda_casas_mode3_"
_OPTIONS = ("--disable-gpu", "--no-sandbox", "--no-first-run", "--no-default-browser-check")


def _driver_major(executable):
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    result = subprocess.run([str(executable), "--version"], capture_output=True,
                            text=True, timeout=10, check=True, **kwargs)
    match = re.search(r"ChromeDriver (\d+)\.", result.stdout)
    if not match:
        raise browser_listing.EvidenceError("isolated_driver_version_unavailable")
    return int(match[1])


def _prepare_driver(uc, root, major):
    """Copy a matching cached binary, or provision only into a private cache."""
    name = "chromedriver.exe" if os.name == "nt" else "chromedriver"
    driver_dir = root / "driver"
    driver_dir.mkdir()
    destination = driver_dir / name
    # This is a single known driver executable, never a browser/profile search.
    source = Path(uc.Patcher.data_path) / ("undetected_" + name)
    if source.is_file():
        try:
            matching = _driver_major(source) == major
        except (OSError, subprocess.SubprocessError, browser_listing.EvidenceError):
            matching = False
        if matching:
            shutil.copy2(source, destination)
            if _driver_major(destination) != major:
                raise browser_listing.EvidenceError("isolated_driver_major_mismatch")
            return destination, "matching_copy"

    class PrivatePatcher(uc.Patcher):
        data_path = str(root / "uc_cache")

    # No mutation of uc.Patcher/data_path globals and no force-kill behavior.
    patcher = PrivatePatcher(version_main=major, force=False, user_multi_procs=False)
    patcher.auto()
    provisioned = Path(patcher.executable_path)
    if provisioned.resolve().parent != (root / "uc_cache").resolve():
        raise browser_listing.EvidenceError("isolated_driver_path_invalid")
    if _driver_major(provisioned) != major:
        raise browser_listing.EvidenceError("isolated_driver_major_mismatch")
    shutil.copy2(provisioned, destination)
    if _driver_major(destination) != major:
        raise browser_listing.EvidenceError("isolated_driver_major_mismatch")
    return destination, "private_provision"


class ProbeBrowserSession(browser_listing._BrowserSession):
    """Fresh headed Chrome; no dependency on a GCP user's prior Chrome login."""

    def __init__(self):
        super().__init__()
        self._owned_root = None
        self._owned_parent = None
        self._cleanup_blocked = False

    def start(self):
        if self.driver is not None:
            return
        import undetected_chromedriver as uc

        executable = os.getenv("SEDA_CASAS_BAHIA_CHROME_PATH") or uc.find_chrome_executable()
        if not executable or not Path(executable).is_file():
            raise browser_listing.EvidenceError("chrome_executable_missing")
        self.major = browser_listing._chrome_major(executable)
        print(f"[seda] casas_bahia mode=3 probe_browser detected_browser_major={self.major}", flush=True)
        self._owned_parent = Path(tempfile.gettempdir()).resolve()
        self._owned_root = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX, dir=str(self._owned_parent))).absolute()
        try:
            private_driver, driver_source = _prepare_driver(uc, self._owned_root, self.major)
            options = uc.ChromeOptions()
            for argument in _OPTIONS:
                options.add_argument(argument)
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            if options.debugger_address:
                raise browser_listing.EvidenceError("existing_browser_attachment_rejected")
            owned_type = browser_listing._owned_chrome_type(uc.Chrome)
            # Keep an owned reference even if UC raises partway through startup.
            driver = owned_type.__new__(owned_type)
            self.driver = driver
            owned_type.__init__(
                driver, options=options, browser_executable_path=str(executable),
                driver_executable_path=str(private_driver),
                user_data_dir=str(self._owned_root / "chrome_profile"),
                version_main=self.major, port=0, user_multi_procs=False,
                patcher_force_close=False, use_subprocess=True,
            )
            browser_major = str(driver.capabilities.get("browserVersion", "")).split(".")[0]
            driver_version = driver.capabilities.get("chrome", {}).get("chromedriverVersion", "")
            if (browser_major != str(self.major)
                    or (driver_version and driver_version.split(".")[0] != str(self.major))):
                raise browser_listing.EvidenceError("browser_driver_version_mismatch")
            driver.set_script_timeout(30)
            driver.execute_cdp_cmd("Network.enable", {"maxTotalBufferSize": 60000000, "maxResourceBufferSize": 10000000})
            driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
            driver.execute_cdp_cmd("Network.setBypassServiceWorker", {"bypass": True})
            print(f"[seda] casas_bahia mode=3 probe_browser ready browser_major={self.major} "
                  f"driver_major={self.major} profile=fresh login_required=false "
                  f"driver_source={driver_source} headed=true", flush=True)
        except Exception:
            self.close()
            raise

    def fetch(self, url, timeout=None):
        # The successful probe used these values, irrespective of global
        # transport timeout, headless or hybrid evidence-wait settings.
        return super().fetch(url, timeout=45, wait_seconds=30)

    def _remove_owned_root(self):
        root, parent = self._owned_root, self._owned_parent
        if root is None or self._cleanup_blocked:
            return
        # Never recursively remove a broad, substituted, linked or external path.
        if (parent is None or root.parent != parent or root.resolve() != root
                or not root.name.startswith(_TEMP_PREFIX) or root.name == _TEMP_PREFIX):
            print("[seda] casas_bahia mode=3 probe_browser cleanup=unsafe_path_retained", flush=True)
            return
        try:
            info = root.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                print("[seda] casas_bahia mode=3 probe_browser cleanup=linked_path_retained", flush=True)
                return
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except OSError:
            print("[seda] casas_bahia mode=3 probe_browser cleanup=owned_temp_retained", flush=True)
            return
        self._owned_root = None
        self._owned_parent = None

    def close(self):
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                # Retain the private tree if closing could not be completed.
                self._cleanup_blocked = True
                print("[seda] casas_bahia mode=3 probe_browser cleanup=close_failed_temp_retained", flush=True)
                return
        self._remove_owned_root()
