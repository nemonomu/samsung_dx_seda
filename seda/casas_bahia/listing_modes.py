"""Explicit Casas listing transports; never changes the detail fetch mode."""

import os
from urllib.parse import parse_qs, urlsplit


DEFAULT_MODE = "1"
MODES = {
    "1": {"label": "rest_api", "fetch_mode": "casas_listing_rest"},
    "1-1": {"label": "rest_api_url_first", "fetch_mode": "casas_listing_rest_url_first"},
    "2": {"label": "hybrid", "fetch_mode": "casas_listing_hybrid"},
    "3": {"label": "uc_api", "fetch_mode": "casas_listing_uc_api"},
    "4": {"label": "uc_api_url_first", "fetch_mode": "casas_listing_uc_api_url_first"},
}


def selected_mode(value=None):
    selected = str(value if value is not None else os.getenv("SEDA_CASAS_BAHIA_LISTING_MODE", DEFAULT_MODE)).strip()
    if selected not in MODES:
        raise ValueError("invalid_casas_listing_mode_expected_1_1dash1_2_3_4")
    return selected


def fetch_mode(value=None):
    return MODES[selected_mode(value)]["fetch_mode"]


def fetch_listing(url, timeout=None, mode=None):
    selected = selected_mode(mode)
    if selected == "1-1":
        from .rest_url_first import fetch_listing as fetch_rest_url_first

        return fetch_rest_url_first(url, timeout=timeout)
    if selected == "2":
        from .listing_hybrid import fetch_listing as fetch_hybrid

        return fetch_hybrid(url, timeout=timeout)
    if selected == "3":
        from .browser_api import fetch_listing as fetch_browser_api

        return fetch_browser_api(url, timeout=timeout)
    if selected == "4":
        from .browser_api_url_first import fetch_listing as fetch_url_first

        return fetch_url_first(url, timeout=timeout)
    return _fetch_rest(url, timeout)


def _fetch_rest(url, timeout):
    from . import rest_legacy
    from .listing_hybrid import _safe_error, _safe_trace

    page = (parse_qs(urlsplit(url).query).get("page") or ["1"])[0]
    print(f"[seda] Casas Bahia mode=1 rest_api page={page} start policy=pre_20260922", flush=True)
    try:
        result = rest_legacy.fetch_search_listing(url, timeout=timeout)
    except Exception as exc:
        result = {"success": False, "text": "", "error": type(exc).__name__, "trace": []}
    trace = _safe_trace(result.get("trace"))
    status = next((int(row["status_code"]) for row in reversed(trace) if row.get("status_code")), 0)
    # The 1290145 REST transport accepted the returned listing body. Do not
    # add price, SKU, page, relevance or duplicate checks to restored Mode 1.
    success = bool(result.get("text"))
    error = "" if success else _safe_error(result.get("error"))
    if success:
        print(f"[seda] Casas Bahia mode=1 rest_api page={page} OK attempts={len(trace)}", flush=True)
    else:
        print(f"[seda] Casas Bahia mode=1 rest_api page={page} FAILED status={status} attempts={len(trace)} error={error}", flush=True)
    return {"success": success, "text": result.get("text", "") if success else "", "status_code": 200 if success else status,
            "method": "api_partner", "error": "" if success else error or "rest_listing_failed", "trace": trace}


def close_browsers():
    from . import browser_api, browser_listing, browser_api_url_first, browser_listing_url_first
    from . import rest_url_first

    rest_url_first.clear_state()

    for client in (browser_api, browser_listing, browser_api_url_first, browser_listing_url_first):
        try:
            client.close_browser()
        except Exception as exc:
            # Cleanup must not hide a collection error or prevent the other close.
            print(f"[seda] Casas Bahia listing cleanup_failed type={type(exc).__name__}", flush=True)
