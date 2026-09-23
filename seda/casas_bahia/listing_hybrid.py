"""REST API + SSR Document parsing hybrid, exclusively for Casas listings."""
from urllib.parse import parse_qs, urlsplit

from . import browser_listing, search_api


def _validation_error(text, url):
    """Validate actual source identities while retaining existing relevance filters."""
    from seda.parsers import extract_next_data, parse_listing, sku_from_url

    try:
        payload = extract_next_data(text)
        search = payload["props"]["pageProps"]["initialState"]["search"]
        products = search["results"]["products"]
        if not isinstance(products, list) or not products:
            return "empty_products"
        requested = parse_qs(urlsplit(url).query).get("page", ["1"])[0]
        actual = (search.get("query") or {}).get("page")
        if actual is not None and str(actual) != str(requested):
            return "requested_page_mismatch"
        requested_sort = browser_listing._sort_value(parse_qs(urlsplit(url).query))
        reported_sort = browser_listing._sort_value(search.get("query") or {})
        if requested_sort and reported_sort and requested_sort != reported_sort:
            return "requested_sort_mismatch"
        identities = set()
        for product in products:
            product_id = str(product.get("id") or "")
            sku_id = sku_from_url(product.get("href") or product.get("url") or "")
            seller = str(product.get("lojista") or product.get("sellerId") or "")
            price = product.get("price") or {}
            if not all(value.isdigit() for value in (product_id, sku_id, seller)):
                return "missing_product_identity"
            identity = (product_id, sku_id, seller)
            if identity in identities:
                return "duplicate_product_identity"
            identities.add(identity)
            if any(str(price.get(key) or "") != value for key, value in (("productId", product_id), ("skuId", sku_id), ("sellerId", seller))):
                return "price_identity_mismatch"
            if not price.get("currentPrice"):
                return "missing_price"
        rows = parse_listing(text, "Casas Bahia", "https://www.casasbahia.com.br", url)
        if not rows:
            return "no_relevant_parsed_products"
        for row in rows:
            identity = (str(row.get("retailer_product_id") or ""), sku_from_url(row.get("product_url") or ""), str(row.get("seller_id") or ""))
            if identity not in identities or not row.get("final_sku_price"):
                return "parsed_identity_or_price_mismatch"
    except (AttributeError, KeyError, TypeError, ValueError):
        return "invalid_listing_payload"
    return ""


def _safe_error(value):
    # Never propagate a requests exception (may contain URL/query/header values).
    import re
    token = str(value or "").split(":", 1)[0]
    return token if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", token) else "listing_request_failed"


def _safe_trace(items):
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        row = {key: item[key] for key in ("attempt", "status_code", "length", "products", "price_count") if isinstance(item.get(key), (int, float))}
        for key in ("error", "price_error"):
            if item.get(key):
                row[key] = _safe_error(item[key])
        cleaned.append(row)
    return cleaned


def fetch_listing(url, timeout=None):
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.casasbahia.com.br", "casasbahia.com.br"} or not search_api._supported_listing_path(parsed.path):
        return {"success": False, "text": "", "status_code": 0, "method": "rest_ssr_hybrid", "trace": [], "error": "not_casas_bahia_listing_url"}
    page = parse_qs(parsed.query).get("page", ["1"])[0]
    print(f"[seda] Casas Bahia page={page} hybrid REST start max_attempts=3", flush=True)
    try:
        rest = search_api.fetch_search_listing(url, timeout=timeout, max_attempts=3, validator=_validation_error)
    except Exception as exc:
        rest = {"success": False, "text": "", "trace": [], "error": type(exc).__name__}
    inner = _safe_trace(rest.get("trace"))
    status = next((int(item["status_code"]) for item in reversed(inner) if item.get("status_code")), 0)
    error = _validation_error(rest.get("text", ""), url) if rest.get("success") else _safe_error(rest.get("error"))
    trace = [{"method": "api_partner", "status_code": status, "error": error, "inner_attempts": inner}]
    if rest.get("success") and not error:
        print(f"[seda] Casas Bahia page={page} hybrid REST OK attempts={len(inner)}", flush=True)
        return {"success": True, "text": rest["text"], "status_code": 200, "method": "api_partner", "trace": trace, "error": ""}
    print(f"[seda] Casas Bahia page={page} hybrid REST FAILED attempts={len(inner)} status={status} error={error}; Chrome SSR fallback start", flush=True)
    try:
        browser = browser_listing.fetch_page(url, timeout=timeout)
    except Exception as exc:
        browser = {"success": False, "text": "", "status_code": 0, "trace": [], "error": type(exc).__name__}
    browser_error = _validation_error(browser.get("text", ""), url) if browser.get("success") else _safe_error(browser.get("error"))
    trace.append({"method": "browser_ssr", "status_code": browser.get("status_code", 0), "error": browser_error, "inner_attempts": browser.get("trace") or []})
    if browser.get("success") and not browser_error:
        print(f"[seda] Casas Bahia page={page} hybrid Chrome SSR OK", flush=True)
        return {"success": True, "text": browser["text"], "status_code": 200, "method": "browser_ssr", "trace": trace, "error": ""}
    print(f"[seda] Casas Bahia page={page} hybrid Chrome SSR FAILED error={browser_error}", flush=True)
    return {"success": False, "text": "", "status_code": browser.get("status_code") or status, "method": "rest_ssr_hybrid", "trace": trace, "error": browser_error or "hybrid_listing_failed"}
