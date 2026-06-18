import html
import json
import os
import time
import unicodedata
import uuid
from urllib.parse import parse_qs, unquote, urlparse

import requests

from seda.step00_config import casas_bahia_listing_slugs, casas_bahia_search_term


SEARCH_URL = "https://api-partner-prd.casasbahia.com.br/api/v3/web/busca"


def fetch_search_listing(url, timeout=None):
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
                    "error": f"{type(exc).__name__}: {exc}",
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
                trace_item["price_error"] = price_result.get("error")
            return {
                "success": True,
                "text": _as_next_data_html(parsed_response, url),
                "products": len(products),
                "trace": trace,
            }
        trace_item["error"] = "empty_products"

    return {"success": False, "error": "casas_bahia_partner_api_failed", "text": "", "trace": trace}


def _params(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    page = _first(query, "page") or "1"
    sort = _first(query, "ordenacao") or _first(query, "sortby")
    params = {
        "resultsperpage": os.getenv("SEDA_CASAS_BAHIA_RESULTS_PER_PAGE", "20"),
        "apikey": "casasbahia",
        "page": page,
        "variantconfiguration": os.getenv("SEDA_CASAS_BAHIA_VARIANT_CONFIGURATION", "q2"),
        "regionid": os.getenv("SEDA_CASAS_BAHIA_REGION_ID", "126000"),
        "multiselection": "true",
        "partnerkey": "elastic",
        "terms": casas_bahia_search_term(),
        "sessionid": os.getenv("SEDA_CASAS_BAHIA_SESSION_ID", "89ec6f5d-3c85-40ba-af9c-9bf91e8af2b0")
        or str(uuid.uuid4()),
        "userid": os.getenv("SEDA_CASAS_BAHIA_USER_ID", ""),
        "wps": "true",
        "device": "desktop",
    }
    if sort:
        params["sortby"] = sort.replace("-", "")
    return params


def _supported_listing_path(path):
    normalized = _normalize_path(path)
    allowed = casas_bahia_listing_slugs()
    return any(f"{_normalize_path(slug)}/b" in normalized for slug in allowed)


def _normalize_path(value):
    text = unquote(str(value or "")).lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.strip("/")


def _attach_prices(products, timeout=None):
    if os.getenv("SEDA_CASAS_BAHIA_ATTACH_PRICES", "1").lower() not in {"1", "true", "yes", "y"}:
        return {"success": False, "prices": {}, "error": "price_attach_disabled"}
    try:
        from .price_api import attach_listing_prices

        return attach_listing_prices(products, timeout=timeout)
    except Exception as exc:
        return {"success": False, "prices": {}, "error": f"{type(exc).__name__}: {exc}"}


def _first(query, key):
    values = query.get(key) or []
    return values[0] if values else ""


def _headers(url):
    return {
        "accept": "*/*",
        "accept-language": os.getenv("SEDA_CASAS_BAHIA_ACCEPT_LANGUAGE", "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"),
        "cache-control": "no-cache",
        "origin": "https://www.casasbahia.com.br",
        "pragma": "no-cache",
        "referer": "https://www.casasbahia.com.br/",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "x-origem": "vv-categoria-frontend",
        "xaplication": "vv-categoria-frontend",
    }


def _as_next_data_html(search, url):
    term = casas_bahia_search_term()
    data = {
        "props": {
            "pageProps": {
                "initialState": {
                    "search": {
                        "query": search.get("queries") or {},
                        "searchTerm": term,
                        "results": {"products": search.get("products") or []},
                    }
                }
            }
        },
        "page": urlparse(url).path or "/tv/b",
        "query": {"source_url": url},
    }
    raw = json.dumps(data, ensure_ascii=False)
    return '<script id="__NEXT_DATA__" type="application/json">' + html.escape(raw) + "</script>"
