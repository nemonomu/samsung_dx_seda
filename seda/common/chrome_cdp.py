import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


def ensure_playwright_temp_dir():
    override = os.getenv("SEDA_PLAYWRIGHT_TEMP_DIR", "").strip() or os.getenv("SEDA_TEMP_DIR", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override))
    for key in ("TEMP", "TMP", "TMPDIR"):
        value = os.getenv(key, "").strip()
        if value:
            candidates.append(Path(value))
    candidates.append(Path(__file__).resolve().parents[1] / "data" / "_tmp" / "playwright")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if candidate.exists():
            value = str(candidate)
            os.environ["TEMP"] = value
            os.environ["TMP"] = value
            os.environ["TMPDIR"] = value
            return value
    raise RuntimeError("Could not create a writable temp directory for Playwright.")


def ensure_chrome_cdp(cdp_url, timeout_seconds=20, auto_start=True):
    if _is_cdp_ready(cdp_url):
        return {"ready": True, "started": False, "url": cdp_url}
    if not auto_start:
        raise RuntimeError(f"Chrome CDP is not reachable: {cdp_url}")

    parsed = urlparse(cdp_url)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"Cannot auto-start non-local Chrome CDP target: {cdp_url}")

    chrome = _chrome_path()
    port = parsed.port or 9222
    user_data_dir = Path(os.getenv("SEDA_CDP_USER_DATA_DIR", r"C:\chrome-cdp-seda"))
    user_data_dir.mkdir(parents=True, exist_ok=True)
    _start_chrome(chrome, port, user_data_dir)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _is_cdp_ready(cdp_url):
            return {
                "ready": True,
                "started": True,
                "url": cdp_url,
                "chrome": str(chrome),
                "user_data_dir": str(user_data_dir),
            }
        time.sleep(0.5)
    raise RuntimeError(f"Chrome CDP did not become ready within {timeout_seconds}s: {cdp_url}")


async def close_chrome_cdp(cdp_url):
    if not _is_cdp_ready(cdp_url):
        return False
    ensure_playwright_temp_dir()
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        await browser.close()
    return True


def _is_cdp_ready(cdp_url):
    url = cdp_url.rstrip("/") + "/json/version"
    try:
        with urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _chrome_path():
    override = os.getenv("SEDA_CHROME_PATH", "").strip()
    candidates = [
        override,
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("google-chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(os.getenv("LOCALAPPDATA", "")) / r"Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    raise RuntimeError("Chrome executable not found. Set SEDA_CHROME_PATH to chrome.exe.")


def _start_chrome(chrome, port, user_data_dir):
    command = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
