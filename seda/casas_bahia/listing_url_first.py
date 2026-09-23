"""Mode 4 URL-first validation and sanitized diagnostic projections."""
import math
from urllib.parse import parse_qs, urlsplit

from . import browser_listing_url_first as browser_listing


def _product_url_sku(product):
    """Accept only positive SKU links on the actual Casas product origin."""
    return browser_listing._product_url_sku(product)


def _validation_error(text, url):
    """Validate product URLs and filtered order, not optional offer metadata."""
    from seda.parsers import (
        _casas_bahia_is_relevant_product, extract_next_data, parse_listing,
    )

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
        identities = []
        seen_skus = set()
        for product in products:
            sku_id = _product_url_sku(product)
            if not sku_id:
                return "missing_product_identity"
            if sku_id in seen_skus:
                return "duplicate_product_identity"
            seen_skus.add(sku_id)
            if _casas_bahia_is_relevant_product(product):
                identities.append(sku_id)
        rows = parse_listing(text, "Casas Bahia", "https://www.casasbahia.com.br", url)
        if not rows:
            return "no_relevant_parsed_products"
        parsed_identities = [
            _product_url_sku({"url": row.get("product_url")})
            for row in rows
        ]
        if parsed_identities != identities:
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
    from .listing_diagnostic_evidence import safe_error

    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        fields = ("attempt", "status_code", "length", "products", "price_count",
                  "price_pending_products", "seller_pending_products")
        row = {key: item[key] for key in fields
               if type(item.get(key)) in (int, float) and 0 <= item[key] <= 10**9 and math.isfinite(item[key])}
        for key in ("error", "price_error"):
            if item.get(key):
                row[key] = safe_error(item[key])
        if isinstance(item.get("price"), dict):
            price = item["price"]
            row["price"] = {}
            status = price.get("status_code")
            if type(status) is int and 0 <= status <= 599:
                row["price"]["status_code"] = status
            if "error" in price:
                row["price"]["error"] = safe_error(price["error"])
        cleaned.append(row)
    return cleaned
