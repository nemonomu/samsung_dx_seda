"""Mode 1 REST collection behavior restored from commit 1290145.

The search parameters, headers, HTML conversion and default price attachment
are unchanged since that revision and are reused without modifying other modes.
Do not add price identity/page validation or a retry ceiling to this path.
The sole security deviation is diagnostic redaction: exceptions/HTTP response
bodies are never included in the returned trace, only safe error categories.
"""

import os
import re
import time
from urllib.parse import urlparse

import requests

from .search_api import SEARCH_URL, _as_next_data_html, _headers, _params, _supported_listing_path


def _safe_price_error(value):
    """Legacy decisions stay unchanged; never persist an exception/body."""
    token = str(value or "").split(":", 1)[0]
    if token in {"empty_price_items", "price_attach_disabled", "invalid_price_json", "Exception", "Timeout"}:
        return token
    if re.fullmatch(r"price_http_[0-9]{3}", token):
        return token
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}(?:Error|Exception|Timeout)", token):
        return token
    return "price_request_failed"


def _attach_prices(products, timeout=None):
    if os.getenv("SEDA_CASAS_BAHIA_ATTACH_PRICES", "1").lower() not in {"1", "true", "yes", "y"}:
        return {"success": False, "prices": {}, "error": "price_attach_disabled"}
    try:
        from .price_api import attach_listing_prices

        return attach_listing_prices(products, timeout=timeout)
    except Exception as exc:
        return {"success": False, "prices": {}, "error": type(exc).__name__}


def fetch_search_listing(url, timeout=None):
    """Historical REST flow: nonempty products suffice, prices are optional."""
    parsed = urlparse(url)
    if "casasbahia.com.br" not in parsed.netloc or not _supported_listing_path(parsed.path):
        return {"success": False, "error": "not_casas_bahia_listing_url", "text": "", "trace": []}

    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    retries = int(os.getenv("SEDA_CASAS_BAHIA_SEARCH_RETRIES", "2"))
    sleep_seconds = float(os.getenv("SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS", "3.0"))
    trace = []
    session = requests.Session()
    params = _params(url)

    for attempt in range(retries + 1):
        if attempt:
            time.sleep(sleep_seconds * attempt)
        try:
            response = session.get(SEARCH_URL, params=params, headers=_headers(url), timeout=timeout)
        except Exception as exc:
            trace.append(
                {
                    "attempt": attempt + 1,
                    "status_code": 0,
                    "error": type(exc).__name__,
                }
            )
            continue

        trace_item = {
            "attempt": attempt + 1,
            "status_code": response.status_code,
            "length": len(response.text or ""),
        }
        trace.append(trace_item)
        if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
            trace_item["error"] = "non_json_or_blocked"
            continue
        try:
            parsed_response = response.json()
        except ValueError:
            trace_item["error"] = "invalid_json"
            continue
        products = parsed_response.get("products") or []
        if products:
            price_result = _attach_prices(products, timeout=timeout)
            trace_item["price_count"] = price_result.get("count", 0)
            if price_result.get("error"):
                trace_item["price_error"] = _safe_price_error(price_result.get("error"))
            return {
                "success": True,
                "text": _as_next_data_html(parsed_response, url),
                "products": len(products),
                "trace": trace,
            }
        trace_item["error"] = "empty_products"

    return {"success": False, "error": "casas_bahia_partner_api_failed", "text": "", "trace": trace}
