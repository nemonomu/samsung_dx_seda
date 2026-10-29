"""Bounded mode-3-only API recovery; one owned browser per worker process."""
import atexit
import os
import threading
import time

_SESSION = None
_LOCK = threading.RLock()


def enabled():
    return os.getenv("SEDA_CASAS_BAHIA_LISTING_MODE", "1").strip() == "3"


class _Session:
    def __init__(self):
        self.browser = None
        self.frame = None
        self.failures = {}
        self.start_error = ""

    def close(self):
        browser, self.browser = self.browser, None
        if browser is not None:
            browser.close()

    def fetch(self, endpoint, params, method, headers, body, label="api"):
        from . import browser_api as api, browser_api_document as document
        from .browser_probe_session import ProbeBrowserSession

        if self.start_error or self.failures.get(label, 0) >= 3:
            return {"success": False, "error": self.start_error or "browser_recovery_circuit_open"}
        try:
            if self.browser is None:
                self.browser = ProbeBrowserSession()
                print("[seda] casas mode=3 recovery stage=browser_start", flush=True)
                self.browser.start()
                prepared = document._navigate(self.browser.driver, document.HOME_URL, None)
                if not prepared.get("success"):
                    self.start_error = "recovery_homepage_failed"
                    return {"success": False, "error": self.start_error}
                self.frame = prepared["document_frame"]
                time.sleep(5)
            result, data = api._browser_fetch(
                self.browser.driver, endpoint, params, method, headers, body, 25, self.frame)
            ok = result.get("status_code") == 200 and not result.get("error") and isinstance(data, dict)
            self.failures[label] = 0 if ok else self.failures.get(label, 0) + 1
            return {"success": ok, "data": data if ok else None,
                    "status_code": result.get("status_code", 0),
                    "error": "" if ok else result.get("error", "browser_api_failed"), "headers": {}}
        except Exception as exc:
            if self.frame is None:
                self.start_error = "recovery_start_" + type(exc).__name__
            self.failures[label] = self.failures.get(label, 0) + 1
            return {"success": False, "error": "recovery_" + type(exc).__name__}


def fetch(endpoint, *, params=None, method="GET", headers=None, body=None, label="api"):
    global _SESSION
    if not enabled():
        return {"success": False, "error": "browser_recovery_disabled"}
    # Callers supply fixed public API endpoints, never credentials or proxy URLs.
    from urllib.parse import urlsplit
    if urlsplit(endpoint).hostname not in {
        "pdp-api.casasbahia.com.br", "recs.casasbahia.com.br",
        "api-destaque-descoberta.casasbahia.com.br",
    }:
        return {"success": False, "error": "unsupported_recovery_endpoint"}
    safe_headers = {key: value for key, value in (headers or {}).items()
                    if key.lower() in {"accept", "content-type", "x-origem", "xaplication"}}
    if method == "POST":
        safe_headers["content-type"] = "application/json"
    with _LOCK:
        if _SESSION is None:
            _SESSION = _Session()
        started = time.monotonic()
        print(f"[seda] casas mode=3 recovery stage={label} start", flush=True)
        result = _SESSION.fetch(endpoint, params or {}, method, safe_headers, body, label)
        result["method"] = "casas_bahia_" + label + "_browser_api"
        print(f"[seda] casas mode=3 recovery stage={label} status={result.get('status_code', 0)} "
              f"success={str(result['success']).lower()} error={result.get('error') or 'none'} "
              f"seconds={time.monotonic() - started:.3f}", flush=True)
        return result


def close_browser():
    global _SESSION
    with _LOCK:
        session, _SESSION = _SESSION, None
        if session is not None:
            session.close()


atexit.register(close_browser)
