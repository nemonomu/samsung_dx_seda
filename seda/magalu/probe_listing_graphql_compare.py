import argparse
import csv
import json
import os
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from seda.magalu import search_api
from seda.parsers import magalu_next_search_is_null, parse_listing
from seda.step00_config import DEFAULT_RUNS_BASE, RETAILERS, page_url, run_date, run_root


CSV_COLUMNS = [
    "run_id",
    "product_line",
    "page",
    "url",
    "page_size",
    "direct_status",
    "direct_products",
    "direct_parsed_rows",
    "direct_pagination_page",
    "direct_pagination_pages",
    "direct_pagination_records",
    "direct_error",
    "browser_status",
    "browser_products",
    "browser_parsed_rows",
    "browser_pagination_page",
    "browser_pagination_pages",
    "browser_pagination_records",
    "browser_error",
    "browser_ready_success",
    "browser_ready_state",
    "browser_ready_url",
    "browser_ready_error",
    "browser_html_length",
    "browser_html_parsed_rows",
    "browser_html_search_null",
    "product_count_delta",
    "parsed_count_delta",
    "html_vs_direct_parsed_delta",
    "browser_only_ids",
    "direct_only_ids",
]


def main():
    args = parse_args()
    os.environ["SEDA_PRODUCT_LINE"] = args.product_line.strip().upper()
    if args.postal_code:
        os.environ["SEDA_POSTAL_CODE"] = args.postal_code.strip()

    pages = _parse_pages(args.pages)
    config = RETAILERS["magalu"]
    output_dir = _output_dir(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"listing_graphql_compare_{args.run_id}_{args.product_line.upper()}_{stamp}.json"
    csv_path = output_dir / f"listing_graphql_compare_{args.run_id}_{args.product_line.upper()}_{stamp}.csv"

    results = []
    session = requests.Session()
    browser_available = True

    for page in pages:
        url = page_url(config, page, run_id=args.run_id)
        payload = search_api._payload(url, args.page_size)
        direct = _fetch_direct(session, url, payload, args.timeout)

        browser = {
            "success": False,
            "error": "browser_disabled",
            "status_code": 0,
            "search": {},
            "raw_text": "",
            "warmup_html": "",
        }
        if not args.no_browser and browser_available:
            if args.restart_browser_per_page:
                _close_browser_page()
            browser = _fetch_browser(url, payload, args.timeout, args.browser_ready_timeout, args.browser_settle_seconds)
            if browser.get("error", "").startswith("browser_import_"):
                browser_available = False
            if args.restart_browser_per_page:
                _close_browser_page()

        result = _summarize(args.run_id, args.product_line, page, url, args.page_size, direct, browser)
        results.append(result)
        _write_outputs(json_path, csv_path, results)
        print(
            "[magalu_listing_compare] "
            f"page={page} direct={result['direct_products']}/{result['direct_parsed_rows']} "
            f"browser_gql={result['browser_products']}/{result['browser_parsed_rows']} "
            f"browser_html={result['browser_html_parsed_rows']} "
            f"delta_gql={result['product_count_delta']}/{result['parsed_count_delta']} "
            f"delta_html={result['html_vs_direct_parsed_delta']} "
            f"errors={result['direct_error'] or '-'}|{result['browser_error'] or '-'}",
            flush=True,
        )

    _close_browser_page()

    print(f"[magalu_listing_compare] wrote {json_path}")
    print(f"[magalu_listing_compare] wrote {csv_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compare Magalu listing direct GraphQL vs browser-context GraphQL for selected pages."
    )
    parser.add_argument("--product-line", default=os.getenv("SEDA_PRODUCT_LINE", "TV"), choices=["TV", "REF", "LDY"])
    parser.add_argument("--run-id", default="main", choices=["main", "bsr"])
    parser.add_argument("--pages", default="1,2,3", help="Comma-separated page numbers, e.g. 1,2,3.")
    parser.add_argument("--page-size", type=int, default=int(os.getenv("SEDA_MAGALU_SEARCH_PAGE_SIZE", "60")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_TIMEOUT", "60")))
    parser.add_argument(
        "--browser-warmup-seconds",
        type=float,
        default=float(os.getenv("SEDA_MAGALU_SEARCH_BROWSER_WARMUP_SECONDS", "3")),
        help="Backward-compatible alias for --browser-settle-seconds when the latter is not set.",
    )
    parser.add_argument(
        "--browser-ready-timeout",
        type=float,
        default=float(os.getenv("SEDA_MAGALU_PROBE_READY_TIMEOUT", "20")),
        help="Seconds to wait for the opened Magalu page to become browser-fetch ready.",
    )
    parser.add_argument(
        "--browser-settle-seconds",
        type=float,
        default=None,
        help="Extra seconds to wait after readyState/fetch readiness. Defaults to --browser-warmup-seconds.",
    )
    parser.add_argument("--postal-code", default=os.getenv("SEDA_POSTAL_CODE", "01001-001"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-browser", action="store_true", help="Only run direct GraphQL.")
    parser.add_argument(
        "--reuse-browser",
        dest="restart_browser_per_page",
        action="store_false",
        default=os.getenv("SEDA_MAGALU_PROBE_RESTART_BROWSER_PER_PAGE", "1").lower()
        not in {"0", "false", "no", "n"},
        help="Reuse the same browser session across pages. Default restarts the browser page per page.",
    )
    args = parser.parse_args()
    if args.browser_settle_seconds is None:
        args.browser_settle_seconds = args.browser_warmup_seconds
    return args


def _parse_pages(value):
    pages = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        page = int(item)
        if page < 1:
            raise SystemExit(f"Invalid page number: {page}")
        pages.append(page)
    return pages or [1, 2, 3]


def _output_dir(override):
    if override:
        return Path(override)
    root = run_root()
    root_parts = [part.lower() for part in root.parts]
    if "magalu" in root_parts:
        return root / run_date() / "diagnostics"
    return DEFAULT_RUNS_BASE / "magalu" / run_date() / "diagnostics"


def _fetch_direct(session, url, payload, timeout):
    started = time.perf_counter()
    try:
        response = session.post(
            search_api.GRAPHQL_URL,
            json=payload,
            headers=search_api._headers(url),
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "success": False,
            "status_code": 0,
            "elapsed": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
            "search": {},
            "raw_text": "",
        }
    raw_text = response.text or ""
    parsed = _parse_json(raw_text)
    search = ((parsed.get("data") or {}).get("search") or {}) if isinstance(parsed, dict) else {}
    error = ""
    if response.status_code != 200:
        error = f"http_{response.status_code}"
    elif not isinstance(parsed, dict):
        error = "invalid_json"
    elif parsed.get("errors"):
        error = "graphql_errors"
    elif not isinstance(search, dict):
        error = "missing_search"
    return {
        "success": not error,
        "status_code": response.status_code,
        "elapsed": round(time.perf_counter() - started, 3),
        "error": error,
        "search": search if isinstance(search, dict) else {},
        "raw_text": raw_text[:1000],
    }


def _fetch_browser(url, payload, timeout, ready_timeout, settle_seconds):
    started = time.perf_counter()
    try:
        from seda.magalu.browser_session import get_page, graphql_post
    except Exception as exc:
        return {
            "success": False,
            "status_code": 0,
            "elapsed": round(time.perf_counter() - started, 3),
            "error": f"browser_import_{type(exc).__name__}: {exc}",
            "search": {},
            "raw_text": "",
        }
    ready = {}
    navigation_error = ""
    try:
        page = get_page()
        try:
            page.get(url)
        except Exception as exc:
            navigation_error = f"{type(exc).__name__}: {exc}"
        ready = _wait_browser_ready(page, url, ready_timeout, settle_seconds)
        if not ready.get("success"):
            error = ready.get("error") or "page_not_ready"
            if navigation_error:
                error = f"{error}; navigation={navigation_error}"
            return {
                "success": False,
                "status_code": 0,
                "elapsed": round(time.perf_counter() - started, 3),
                "error": error,
                "search": {},
                "raw_text": "",
                "ready_trace": ready.get("trace") or [],
                "ready": ready,
                "warmup_trace": ready.get("trace") or [],
                "warmup_html": "",
                "warmup_success": False,
            }
        result = graphql_post(payload, timeout=timeout)
    except Exception as exc:
        return {
            "success": False,
            "status_code": 0,
            "elapsed": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
            "search": {},
            "raw_text": "",
            "ready_trace": ready.get("trace") if isinstance(ready, dict) else [],
            "ready": ready if isinstance(ready, dict) else {},
        }
    data = result.get("data") or {}
    search = ((data.get("data") or {}).get("search") or {}) if isinstance(data, dict) else {}
    error = result.get("error", "")
    if not error and not isinstance(search, dict):
        error = "missing_search"
    return {
        "success": not error,
        "status_code": result.get("status_code", 0),
        "elapsed": round(time.perf_counter() - started, 3),
        "error": error,
        "search": search if isinstance(search, dict) else {},
        "raw_text": str(result.get("text") or "")[:1000],
        "ready_trace": ready.get("trace") or [],
        "ready": ready,
        "warmup_trace": ready.get("trace") or [],
        "warmup_html": "",
        "warmup_success": bool(ready.get("success")),
    }


def _wait_browser_ready(page, expected_url, timeout_seconds, settle_seconds):
    timeout_seconds = max(float(timeout_seconds or 0), 1.0)
    settle_seconds = max(float(settle_seconds or 0), 0.0)
    deadline = time.perf_counter() + timeout_seconds
    trace = []
    last_state = {}
    last_error = ""

    while time.perf_counter() < deadline:
        state = _browser_state(page)
        last_state = state
        if state.get("error"):
            last_error = state["error"]
        trace.append(state)

        if _state_is_blocked(state):
            return {
                "success": False,
                "error": "blocked_page",
                "trace": trace,
                "state": state,
                "ready_state": state.get("readyState", ""),
                "url": state.get("href", ""),
            }

        if _state_is_ready(state, expected_url):
            if settle_seconds > 0:
                time.sleep(settle_seconds)
                state = _browser_state(page)
                trace.append({**state, "phase": "settle"})
                if _state_is_blocked(state):
                    return {
                        "success": False,
                        "error": "blocked_page_after_settle",
                        "trace": trace,
                        "state": state,
                        "ready_state": state.get("readyState", ""),
                        "url": state.get("href", ""),
                    }
                if not _state_is_ready(state, expected_url):
                    last_state = state
                    time.sleep(0.25)
                    continue
            return {
                "success": True,
                "error": "",
                "trace": trace,
                "state": state,
                "ready_state": state.get("readyState", ""),
                "url": state.get("href", ""),
            }

        time.sleep(0.25)

    return {
        "success": False,
        "error": f"page_not_ready:{last_error or _not_ready_reason(last_state, expected_url)}",
        "trace": trace,
        "state": last_state,
        "ready_state": last_state.get("readyState", ""),
        "url": last_state.get("href", ""),
    }


def _browser_state(page):
    script = """
return JSON.stringify({
  href: String(location.href || ''),
  readyState: String(document.readyState || ''),
  fetchType: String(typeof window.fetch),
  title: String(document.title || ''),
  bodyText: String((document.body && document.body.innerText) || '').slice(0, 800)
});
"""
    try:
        raw = page.run_js(script, timeout=2) or "{}"
        state = json.loads(raw) if isinstance(raw, str) else raw
        return state if isinstance(state, dict) else {"error": "invalid_state"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "href": str(getattr(page, "url", "") or "")}


def _state_is_ready(state, expected_url):
    if not isinstance(state, dict):
        return False
    if state.get("fetchType") != "function":
        return False
    if state.get("readyState") not in {"interactive", "complete"}:
        return False
    return _same_listing_path(state.get("href", ""), expected_url)


def _state_is_blocked(state):
    text = _ascii_lower(" ".join(str(state.get(key, "")) for key in ("title", "bodyText", "href")))
    markers = (
        "nao e possivel acessar a pagina",
        "nao e possivel acessar",
        "erro 403",
        "access denied",
        "akamai",
        "captcha",
    )
    return any(marker in text for marker in markers)


def _same_listing_path(actual_url, expected_url):
    actual = urlparse(str(actual_url or ""))
    expected = urlparse(str(expected_url or ""))
    if not actual.netloc.endswith("magazineluiza.com.br"):
        return False
    return actual.path.rstrip("/") == expected.path.rstrip("/")


def _not_ready_reason(state, expected_url):
    if not state:
        return "empty_state"
    if state.get("error"):
        return state["error"]
    parts = []
    if state.get("readyState") not in {"interactive", "complete"}:
        parts.append(f"readyState={state.get('readyState', '')}")
    if state.get("fetchType") != "function":
        parts.append(f"fetchType={state.get('fetchType', '')}")
    if not _same_listing_path(state.get("href", ""), expected_url):
        parts.append(f"url={state.get('href', '')}")
    return ",".join(parts) or "unknown"


def _ascii_lower(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def _summarize(run_id, product_line, page, url, page_size, direct, browser):
    direct_search = direct.get("search") or {}
    browser_search = browser.get("search") or {}
    direct_products = direct_search.get("products") or []
    browser_products = browser_search.get("products") or []
    direct_rows = _parse_rows(direct_search, url, run_id)
    browser_rows = _parse_rows(browser_search, url, run_id)
    browser_html = browser.get("warmup_html") or ""
    browser_html_rows = _parse_html_rows(browser_html, url, run_id)
    direct_ids = _product_ids(direct_products)
    browser_ids = _product_ids(browser_products)
    direct_set = set(direct_ids)
    browser_set = set(browser_ids)
    return {
        "run_id": run_id,
        "product_line": product_line.upper(),
        "page": page,
        "url": url,
        "page_size": page_size,
        "direct": _details(direct, direct_search, direct_products, direct_rows, direct_ids),
        "browser": _details(browser, browser_search, browser_products, browser_rows, browser_ids),
        "direct_status": direct.get("status_code", 0),
        "direct_products": len(direct_products),
        "direct_parsed_rows": len(direct_rows),
        "direct_pagination_page": _pagination_value(direct_search, "page"),
        "direct_pagination_pages": _pagination_value(direct_search, "pages"),
        "direct_pagination_records": _pagination_value(direct_search, "records"),
        "direct_error": direct.get("error", ""),
        "browser_status": browser.get("status_code", 0),
        "browser_products": len(browser_products),
        "browser_parsed_rows": len(browser_rows),
        "browser_pagination_page": _pagination_value(browser_search, "page"),
        "browser_pagination_pages": _pagination_value(browser_search, "pages"),
        "browser_pagination_records": _pagination_value(browser_search, "records"),
        "browser_error": browser.get("error", ""),
        "browser_ready_success": int(bool((browser.get("ready") or {}).get("success"))),
        "browser_ready_state": (browser.get("ready") or {}).get("ready_state", ""),
        "browser_ready_url": (browser.get("ready") or {}).get("url", ""),
        "browser_ready_error": (browser.get("ready") or {}).get("error", ""),
        "browser_html_length": len(browser_html),
        "browser_html_parsed_rows": len(browser_html_rows),
        "browser_html_search_null": int(bool(browser_html and magalu_next_search_is_null(browser_html))),
        "product_count_delta": len(browser_products) - len(direct_products),
        "parsed_count_delta": len(browser_rows) - len(direct_rows),
        "html_vs_direct_parsed_delta": len(browser_html_rows) - len(direct_rows),
        "browser_only_ids": ",".join([item for item in browser_ids if item not in direct_set][:20]),
        "direct_only_ids": ",".join([item for item in direct_ids if item not in browser_set][:20]),
    }


def _details(result, search, products, rows, ids):
    return {
        "status_code": result.get("status_code", 0),
        "elapsed": result.get("elapsed", 0),
        "error": result.get("error", ""),
        "products": len(products),
        "parsed_rows": len(rows),
        "pagination": search.get("pagination") or {},
        "first_ids": ids[:10],
        "first_titles": [str(item.get("title") or "")[:120] for item in products[:5] if isinstance(item, dict)],
        "raw_preview": result.get("raw_text", ""),
        "warmup_trace": result.get("warmup_trace", []),
        "warmup_success": result.get("warmup_success", False),
        "ready": result.get("ready", {}),
        "ready_trace": result.get("ready_trace", []),
    }


def _parse_rows(search, url, run_id):
    if not search:
        return []
    html_text = search_api._as_next_data_html(search, url)
    return parse_listing(html_text, "Magalu", RETAILERS["magalu"].base_url, url, run_id=run_id)


def _parse_html_rows(html_text, url, run_id):
    if not html_text:
        return []
    return parse_listing(html_text, "Magalu", RETAILERS["magalu"].base_url, url, run_id=run_id)


def _product_ids(products):
    values = []
    for item in products:
        if not isinstance(item, dict):
            continue
        identity = item.get("id") or item.get("variationId") or item.get("reference") or item.get("url") or item.get("path")
        values.append(str(identity or ""))
    return [value for value in values if value]


def _pagination_value(search, key):
    pagination = search.get("pagination") if isinstance(search, dict) else {}
    return (pagination or {}).get(key, "")


def _parse_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})


def _write_outputs(json_path, csv_path, rows):
    json_path.write_text(json.dumps({"results": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(csv_path, rows)


def _close_browser_page():
    try:
        from seda.magalu.browser_session import close_page

        close_page(force=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
