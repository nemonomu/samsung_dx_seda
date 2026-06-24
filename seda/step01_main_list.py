import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .parsers import extract_next_data, magalu_next_search_is_null, parse_listing, _magalu_is_relevant_product
from .step00_config import RETAILERS, OUTPUT_COLUMNS, page_url, product_identity, run_root, selected_retailers, write_csv
from .transport import fetch_url, is_blocked_html


def page_numbers(run_id=None):
    is_bsr = (run_id or "").lower() == "bsr"
    prefix = "SEDA_BSR" if is_bsr else "SEDA_MAIN"
    raw = os.getenv(f"{prefix}_PAGE_LIST", os.getenv("SEDA_PAGE_LIST", "")).strip()
    if raw:
        return [int(item.strip()) for item in raw.split(",") if item.strip()]
    start = int(os.getenv(f"{prefix}_START_PAGE", os.getenv("SEDA_START_PAGE", "1")))
    target = unique_target(run_id)
    default_pages = max_pages(is_bsr) if target else _default_pages(is_bsr)
    pages = int(os.getenv(f"{prefix}_PAGES", os.getenv("SEDA_PAGES", default_pages)))
    return list(range(start, start + pages))


def _default_pages(is_bsr):
    retailer = os.getenv("SEDA_ACTIVE_RETAILER", "").strip().lower()
    if retailer == "casas_bahia":
        return "15" if is_bsr else "20"
    return "3" if is_bsr else "9"


def unique_target(run_id=None):
    is_bsr = (run_id or "").lower() == "bsr"
    prefix = "SEDA_BSR" if is_bsr else "SEDA_MAIN"
    raw = os.getenv(f"{prefix}_UNIQUE_TARGET", os.getenv("SEDA_UNIQUE_TARGET", "")).strip()
    if raw:
        return int(raw)
    return 0


def max_pages(is_bsr):
    prefix = "SEDA_BSR" if is_bsr else "SEDA_MAIN"
    default = "40" if is_bsr else "120"
    return int(os.getenv(f"{prefix}_MAX_PAGES", os.getenv("SEDA_MAX_PAGES", default)))


def _should_magalu_browser_fill(retailer_name, method, parsed, html_text="", source_url=""):
    if retailer_name != "Magalu":
        return False
    if os.getenv("SEDA_MAGALU_LISTING_BROWSER_FILL", "1").lower() in {"0", "false", "no", "n"}:
        return False
    if "direct_graphql_search" not in str(method or ""):
        return False
    if html_text and source_url:
        payload_ok, _ = _magalu_browser_fill_payload_ok(source_url, html_text, len(parsed))
        if payload_ok:
            return False
        return True
    minimum = _safe_int(os.getenv("SEDA_MAGALU_LISTING_DIRECT_MIN_PARSED_ROWS", "0"), 0)
    return minimum > 0 and len(parsed) < minimum


def _append_method(current, extra):
    current = str(current or "").strip()
    extra = str(extra or "").strip()
    if not extra:
        return current
    if not current:
        return extra
    parts = current.split("+")
    return current if extra in parts else f"{current}+{extra}"


def _magalu_browser_fill_search_null_retries():
    raw = os.getenv("SEDA_MAGALU_BROWSER_FILL_SEARCH_NULL_RETRIES", "1")
    try:
        return max(0, int(raw))
    except ValueError:
        return 1


def _magalu_requested_page(url):
    query = parse_qs(urlparse(str(url or "")).query)
    raw = (query.get("page") or ["1"])[0]
    return _safe_int(raw, 1)


def _safe_int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _magalu_listing_fail_fast_enabled(retailer_name):
    if retailer_name != "Magalu":
        return False
    return os.getenv("SEDA_MAGALU_LISTING_FAIL_FAST", "0").lower() in {"1", "true", "yes", "y"}


def _magalu_next_search_pagination(html_text):
    data = extract_next_data(html_text)
    props = data.get("props") if isinstance(data, dict) else {}
    page_props = props.get("pageProps") if isinstance(props, dict) else {}
    page_data = page_props.get("data") if isinstance(page_props, dict) else {}
    search = page_data.get("search") if isinstance(page_data, dict) else {}
    if not isinstance(search, dict):
        return {}, 0
    products = search.get("products") or []
    product_count = len(products) if isinstance(products, list) else 0
    pagination = search.get("pagination") or {}
    return pagination if isinstance(pagination, dict) else {}, product_count


def _magalu_next_listing_stats(html_text, parsed_count=0):
    data = extract_next_data(html_text)
    props = data.get("props") if isinstance(data, dict) else {}
    page_props = props.get("pageProps") if isinstance(props, dict) else {}
    page_data = page_props.get("data") if isinstance(page_props, dict) else {}
    search = page_data.get("search") if isinstance(page_data, dict) else {}
    products = search.get("products", []) if isinstance(search, dict) else []
    pagination = search.get("pagination") if isinstance(search, dict) else {}
    if not isinstance(products, list):
        products = []
    kept_count = sum(1 for product in products if isinstance(product, dict) and _magalu_is_relevant_product(product))
    return {
        "raw_products": len(products),
        "kept_products": kept_count,
        "dropped_products": max(0, len(products) - kept_count),
        "parsed_rows": parsed_count,
        "pagination_page": _safe_int((pagination or {}).get("page"), 0) if isinstance(pagination, dict) else 0,
        "pagination_size": _safe_int((pagination or {}).get("size"), 0) if isinstance(pagination, dict) else 0,
    }


def _format_magalu_listing_stats(stats):
    if not stats:
        return ""
    return (
        f"raw_products={stats.get('raw_products', 0)} "
        f"kept_products={stats.get('kept_products', 0)} "
        f"dropped_products={stats.get('dropped_products', 0)} "
        f"pagination_page={stats.get('pagination_page', 0)} "
        f"pagination_size={stats.get('pagination_size', 0)} "
    )


def _magalu_transport_trace_stats(attempts):
    for attempt in reversed(attempts or []):
        inner_attempts = attempt.get("inner_attempts") if isinstance(attempt, dict) else []
        for inner in reversed(inner_attempts or []):
            if not isinstance(inner, dict):
                continue
            if "cdp_success" not in inner and "fallback_used" not in inner:
                continue
            return {
                "next_data_source": inner.get("source", ""),
                "next_data_length": inner.get("next_data_length", 0),
                "cdp_success": inner.get("cdp_success", 0),
                "cdp_empty": inner.get("cdp_empty", 0),
                "cdp_error": inner.get("cdp_error", ""),
                "fallback_used": inner.get("fallback_used", 0),
                "fallback_success": inner.get("fallback_success", 0),
                "fallback_empty": inner.get("fallback_empty", 0),
                "fallback_error": inner.get("fallback_error", ""),
            }
    return {}


def _format_magalu_transport_trace_stats(stats):
    if not stats:
        return ""
    parts = [
        f"next_source={stats.get('next_data_source', '')}",
        f"next_len={stats.get('next_data_length', 0)}",
        f"cdp_success={stats.get('cdp_success', 0)}",
        f"cdp_empty={stats.get('cdp_empty', 0)}",
        f"fallback_used={stats.get('fallback_used', 0)}",
        f"fallback_success={stats.get('fallback_success', 0)}",
        f"fallback_empty={stats.get('fallback_empty', 0)}",
    ]
    cdp_error = stats.get("cdp_error")
    fallback_error = stats.get("fallback_error")
    if cdp_error:
        parts.append(f"cdp_error={cdp_error}")
    if fallback_error:
        parts.append(f"fallback_error={fallback_error}")
    return " ".join(parts) + " "


def _magalu_browser_fill_payload_ok(url, html_text, parsed_count):
    pagination, product_count = _magalu_next_search_pagination(html_text)
    if not pagination:
        return False, "missing_pagination"
    requested_page = _magalu_requested_page(url)
    payload_page = _safe_int(pagination.get("page"), 0)
    payload_size = _safe_int(pagination.get("size"), 0)
    if payload_page != requested_page:
        return False, f"page_mismatch:{payload_page}!={requested_page}"
    max_page_size = int(os.getenv("SEDA_MAGALU_BROWSER_FILL_MAX_PAGE_SIZE", "100"))
    if payload_size > max_page_size:
        return False, f"page_size_too_large:{payload_size}"
    max_products = int(os.getenv("SEDA_MAGALU_BROWSER_FILL_MAX_PRODUCTS", "120"))
    if product_count > max_products or parsed_count > max_products:
        return False, f"too_many_products:{max(product_count, parsed_count)}"
    return True, ""


def _magalu_browser_fill(url, config, run_id, base_count):
    attempts = []
    max_attempts = _magalu_browser_fill_search_null_retries() + 1
    for attempt in range(1, max_attempts + 1):
        result = fetch_url(url, mode="browser")
        attempts.extend(result.attempts)
        blocked = is_blocked_html(result.text, result.status_code)
        if not result.text or result.error or blocked:
            return [], result, attempts, "blocked_or_error"
        parsed = parse_listing(result.text, config.name, config.base_url, url, run_id=run_id)
        if magalu_next_search_is_null(result.text):
            if attempt < max_attempts:
                print(
                    f"[seda] {run_id} {config.name} browser fill search=null; retry {attempt}/{max_attempts - 1}",
                    flush=True,
                )
                continue
            return parsed, result, attempts, "search_null"
        payload_ok, payload_error = _magalu_browser_fill_payload_ok(url, result.text, len(parsed))
        if not payload_ok:
            return parsed, result, attempts, payload_error
        if len(parsed) <= base_count:
            return parsed, result, attempts, "no_growth"
        return parsed, result, attempts, ""
    return [], None, attempts, "search_null"


def main():
    run_id = os.getenv("SEDA_RUN_ID", "main").strip().lower()
    root = run_root() / run_id
    rows = []
    failures = []
    listing_stats = []
    rank_offsets = {}
    attempted_pages = []
    target = unique_target(run_id)
    no_growth_limit = int(os.getenv("SEDA_UNIQUE_NO_GROWTH_LIMIT", "5"))
    for retailer_key in selected_retailers():
        config = RETAILERS[retailer_key]
        rank_offsets.setdefault(retailer_key, 0)
        unique_seen = set()
        no_growth_pages = 0
        for page in page_numbers(run_id):
            url = page_url(config, page, run_id=run_id)
            attempted_pages.append(page)
            before_unique = len(unique_seen)
            raw_path = root / "raw" / retailer_key / f"page_{page:03d}.html"
            reuse_raw = os.getenv("SEDA_REUSE_RAW", "0").lower() in {"1", "true", "yes", "y"}
            if reuse_raw and raw_path.exists():
                text = raw_path.read_text(encoding="utf-8", errors="ignore")
                method = "raw_reparse"
                attempts = []
                error = ""
            else:
                result = fetch_url(url)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(result.text or result.error, encoding="utf-8", errors="ignore")
                text = result.text
                method = result.method
                attempts = result.attempts
                error = result.error
            if not text or error:
                failure = {
                    "retailer": config.name,
                    "page": page,
                    "url": url,
                    "error": error,
                    "attempts": attempts,
                }
                failures.append(failure)
                if _magalu_listing_fail_fast_enabled(config.name):
                    raise SystemExit(
                        f"[seda] {run_id} {config.name} page={page} listing fetch failed: {error}"
                    )
                continue
            parsed = parse_listing(text, config.name, config.base_url, url, run_id=run_id)
            magalu_stats = _magalu_next_listing_stats(text, len(parsed)) if config.name == "Magalu" else {}
            magalu_trace_stats = _magalu_transport_trace_stats(attempts) if config.name == "Magalu" else {}
            if _should_magalu_browser_fill(config.name, method, parsed, text, url):
                fill_parsed, fill_result, fill_attempts, fill_error = _magalu_browser_fill(
                    url,
                    config,
                    run_id,
                    len(parsed),
                )
                attempts = attempts + fill_attempts
                if fill_error in {"blocked_or_error", "search_null"}:
                    failures.append(
                        {
                            "retailer": config.name,
                            "page": page,
                            "url": url,
                            "error": f"browser_fill_{fill_error}",
                            "attempts": fill_attempts,
                        }
                    )
                if not fill_error and fill_result and len(fill_parsed) > len(parsed):
                    text = fill_result.text
                    method = _append_method(method, fill_result.method)
                    parsed = fill_parsed
                    magalu_stats = _magalu_next_listing_stats(text, len(parsed)) if config.name == "Magalu" else {}
                    magalu_trace_stats = _magalu_transport_trace_stats(attempts) if config.name == "Magalu" else {}
                    raw_path.write_text(text, encoding="utf-8", errors="ignore")
            rank_field = "bsr_rank" if run_id == "bsr" else "main_rank"
            for local_index, item in enumerate(parsed, start=1):
                item["fetch_method"] = method
                item[rank_field] = rank_offsets[retailer_key] + local_index
            rank_offsets[retailer_key] += len(parsed)
            rows.extend(parsed)
            for item in parsed:
                key = product_identity(item)
                if key[1]:
                    unique_seen.add(key)
            unique_count = len(unique_seen)
            if unique_count == before_unique:
                no_growth_pages += 1
            else:
                no_growth_pages = 0
            print(
                f"[seda] {run_id} {config.name} page={page} rows={len(parsed)} "
                f"{_format_magalu_listing_stats(magalu_stats)}"
                f"{_format_magalu_transport_trace_stats(magalu_trace_stats)}"
                f"unique={unique_count} method={method}",
                flush=True,
            )
            if magalu_stats:
                listing_stats.append(
                    {
                        "retailer": config.name,
                        "page": page,
                        "url": url,
                        "method": method,
                        "unique": unique_count,
                        **magalu_stats,
                        **magalu_trace_stats,
                    }
                )
            if target and unique_count >= target:
                print(f"[seda] {run_id} {config.name} reached unique target {target}", flush=True)
                break
            if target and no_growth_pages >= no_growth_limit:
                print(
                    f"[seda] {run_id} {config.name} stopped after {no_growth_pages} pages without unique growth",
                    flush=True,
                )
                break

    parsed_dir = root / "parsed"
    write_csv(parsed_dir / "main_occurrences.csv", rows, columns=OUTPUT_COLUMNS)
    manifest = {
        "run_id": run_id,
        "rows": len(rows),
        "failures": failures,
        "retailers": selected_retailers(),
        "pages": attempted_pages,
        "unique_target": target,
        "fetch_mode": os.getenv("SEDA_FETCH_MODE", "uc_first"),
        "listing_stats": listing_stats,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[seda] wrote {parsed_dir / 'main_occurrences.csv'} rows={len(rows)}")
    if not rows and os.getenv("SEDA_ALLOW_EMPTY_LISTING", "0").lower() not in {"1", "true", "yes", "y"}:
        raise SystemExit(f"[seda] {run_id} listing produced 0 rows; stopping before downstream load")


if __name__ == "__main__":
    main()
