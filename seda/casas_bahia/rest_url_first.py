"""Mode 1-1: direct REST listing, with URL-first optional quote admission.

Modes 1/2/3/4 are not changed. Search page identity/order remain mandatory;
missing or conflicting prices cannot discard otherwise valid product URLs.
Only the existing public search and price APIs are used, never Chrome/ZenRows.
"""

from copy import deepcopy
import math
import os
import re
import threading
import time
from urllib.parse import urlsplit

import requests

from . import browser_listing_url_first as identity
from . import price_api, search_api
from .listing_url_first import _validation_error


METHOD = "api_partner_url_first"
_LOCK = threading.RLock()
_PAGE_IDS = {}


def clear_state():
    """Reset accepted-page identity evidence when the listing stage finishes."""
    with _LOCK:
        _PAGE_IDS.clear()


def _attempt_limit():
    try:
        return min(3, max(1, int(os.getenv("SEDA_CASAS_BAHIA_SEARCH_RETRIES", "2")) + 1))
    except (TypeError, ValueError):
        return 3


def _number(value, default, *, minimum=0):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= minimum else default
    except (TypeError, ValueError):
        return default


def _request_identity(url):
    parsed = urlsplit(url)
    if (parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)
            or not search_api._supported_listing_path(parsed.path)):
        raise identity.EvidenceError("not_casas_bahia_listing_url")
    return identity._request_identity(url)


def _quarantine_inline_product_conflict(product):
    """Do not retain an inline quote whose availability names another product.

    Shared URL-first helpers already check SKU/seller aliases. This additional
    REST-mode guard covers the availability product alias, without changing
    mode 4 or removing the listing's valid product identity/URL.
    """
    price = product.get("price")
    if not isinstance(price, dict):
        return
    availability = price.get("availability")
    if not isinstance(availability, dict):
        return
    raw_product = availability.get("IdProduto")
    if raw_product in (None, ""):
        return
    availability_product = identity._positive_id(raw_product)
    expected = {value for value in (identity._positive_id(product.get("id")),
                                    identity._positive_id(price.get("productId"))) if value}
    if not availability_product or any(value != availability_product for value in expected):
        product["price"] = {}
        product["_casas_listing_price_pending"] = True


def _parser_context(data, requested):
    """Preserve explicit response page/sort; inject only absent parser context."""
    if not isinstance(data, dict):
        raise identity.EvidenceError("invalid_product_payload")
    page, sort = requested[1:]
    query = data.get("queries")
    is_mapping = isinstance(query, dict)
    query = deepcopy(query) if is_mapping else {}
    raw_page = query.get("page")
    has_page = raw_page not in (None, "")
    if has_page and identity._positive_id(raw_page) != page:
        raise identity.EvidenceError("response_page_mismatch")
    sorts = [str(query[key]).replace("-", "").lower()
             for key in ("ordenacao", "sortby", "sortBy") if query.get(key) not in (None, "")]
    if sort and sorts and any(value != sort for value in sorts):
        raise identity.EvidenceError("response_sort_mismatch")
    copied = deepcopy(data)
    copied["queries"] = query
    query["page"] = page
    if sort and not sorts:
        query["sortby"] = sort
    products = copied.get("products")
    if not isinstance(products, list) or not products:
        raise identity.EvidenceError("empty_or_invalid_products")
    seen = set()
    for product in products:
        sku = identity._product_url_sku(product)
        if not sku:
            raise identity.EvidenceError("listing_identity_invalid")
        if sku in seen:
            raise identity.EvidenceError("duplicate_listing_sku")
        seen.add(sku)
        _quarantine_inline_product_conflict(product)
    # Optional identity conflicts quarantine only the quote, not the URL.
    identity._attach_optional_prices(products, [])
    return copied, {
        "response_queries_is_mapping": is_mapping,
        "response_page_present": has_page,
        "parser_page_context_injected": not has_page,
        "response_sort_present": bool(sorts),
        "parser_sort_context_injected": bool(sort and not sorts),
    }


def _price_result(products, timeout):
    if os.getenv("SEDA_CASAS_BAHIA_ATTACH_PRICES", "1").lower() not in {"1", "true", "yes", "y"}:
        return {"success": False, "status_code": 0, "error": "price_attach_disabled", "offers": []}
    try:
        value = price_api.fetch_listing_prices(products, timeout=timeout, include_offers=True)
    except Exception:
        # Exception messages may contain request URLs or authentication data.
        return {"success": False, "status_code": 0, "error": "price_request_failed", "offers": []}
    return value if isinstance(value, dict) else {
        "success": False, "status_code": 0, "error": "invalid_price_result", "offers": []}


def _price_error(result):
    error = result.get("error")
    allowed = {"empty_price_items", "price_attach_disabled", "price_request_failed",
               "invalid_price_json", "invalid_price_offers", "invalid_price_result"}
    if isinstance(error, str) and (error in allowed or re.fullmatch(r"price_http_[1-5][0-9]{2}", error)):
        return error
    if error or result.get("success") is not True:
        return "price_request_failed"
    return "invalid_price_status" if result.get("status_code") != 200 else ""


def _attach_prices(products, result):
    """Use the complete offer list, never the legacy product-keyed price map."""
    offers = result.get("offers") if result.get("success") is True and result.get("status_code") == 200 else []
    offers = offers if isinstance(offers, list) else []
    # REST offers must identify their SKU and seller. Product identity, where
    # supplied by the listing, is also required to match (not an optional alias).
    # Existing URL-first helpers additionally reject contradictory availability
    # identities and duplicate, differing quotes; no last/cheapest-offer wins.
    valid = []
    for offer in offers:
        if (not isinstance(offer, dict)
                or not identity._positive_id(offer.get("skuId"))
                or not identity._positive_id(offer.get("sellerId"))):
            continue
        availability = offer.get("availability")
        availability = availability if isinstance(availability, dict) else {}
        raw_product = availability.get("IdProduto")
        if (raw_product not in (None, "")
                and (not identity._positive_id(raw_product)
                     or identity._positive_id(raw_product) != identity._positive_id(offer.get("productId")))):
            continue
        valid.append(offer)
    for product in products:
        _quarantine_inline_product_conflict(product)
        product_id = identity._positive_id(product.get("id"))
        sku = identity._product_url_sku(product)
        seller = identity._positive_id(product.get("lojista") or product.get("sellerId"))
        matching = [offer for offer in valid
                    if identity._positive_id(offer.get("skuId")) == sku
                    and (not product_id or identity._positive_id(offer.get("productId")) == product_id)
                    and (not seller or identity._positive_id(offer.get("sellerId")) == seller)]
        identity._attach_optional_prices([product], matching)
    return {
        "price_count": len(offers),
        "price_pending_products": sum(p.get("_casas_listing_price_pending") is True for p in products),
        "seller_pending_products": sum(p.get("_casas_listing_seller_pending") is True for p in products),
    }


def _error(exc):
    if isinstance(exc, identity.EvidenceError) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", str(exc)):
        return str(exc)
    return "rest_listing_request_failed"


def fetch_listing(url, timeout=None):
    """At most three search GETs; one optional price POST per usable search."""
    with _LOCK:
        try:
            return _fetch_listing(url, timeout)
        except Exception as exc:
            error = _error(exc)
            return {"success": False, "text": "", "status_code": 0, "method": METHOD,
                    "error": error, "trace": [{"method": METHOD, "attempt": 0, "status_code": 0, "error": error}]}


def _fetch_listing(url, timeout):
    trace = []
    try:
        requested = _request_identity(url)
    except Exception as exc:
        return {"success": False, "text": "", "status_code": 0, "method": METHOD,
                "error": _error(exc), "trace": trace}
    timeout = _number(timeout if timeout is not None else os.getenv("SEDA_TIMEOUT", "60"), 60, minimum=1)
    delay = _number(os.getenv("SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS", "3.0"), 3)
    limit = _attempt_limit()
    print(f"[seda] Casas Bahia mode=1-1 rest_api_url_first page={requested[1]} start max_attempts={limit}", flush=True)
    session = requests.Session()
    try:
        params = search_api._params(url)
        for attempt in range(1, limit + 1):
            if attempt > 1:
                time.sleep(delay * (attempt - 1))
            started = time.monotonic()
            item = {"method": METHOD, "attempt": attempt, "status_code": 0, "stage": "search"}
            trace.append(item)
            try:
                response = session.get(search_api.SEARCH_URL, params=params,
                                       headers=search_api._headers(url), timeout=timeout)
                item["status_code"] = int(response.status_code)
                item["length"] = len(response.text or "")
                if response.status_code != 200:
                    raise identity.EvidenceError("search_http_not_200")
                if "json" not in response.headers.get("content-type", "").lower():
                    raise identity.EvidenceError("non_json_response")
                try:
                    data = response.json()
                except ValueError:
                    raise identity.EvidenceError("invalid_json") from None
                parser_data, context = _parser_context(data, requested)
                item.update(context)
                products = parser_data["products"]
                ids = {identity._product_url_sku(product) for product in products}
                previous = _PAGE_IDS.get((requested[0], requested[2]), {})
                if any(page != requested[1] and old_ids == ids for page, old_ids in previous.items()):
                    raise identity.EvidenceError("earlier_page_repeated_for_other_page")
                # Validate URL identity, relevance and source order before POST.
                validation_error = _validation_error(search_api._as_next_data_html(parser_data, url), url)
                if validation_error:
                    raise identity.EvidenceError(validation_error)
                item["stage"] = "price"
                prices = _price_result(products, timeout)
                status = prices.get("status_code")
                item["price"] = {"status_code": status if type(status) is int and 0 <= status <= 599 else 0}
                price_error = _price_error(prices)
                if price_error:
                    item["price_error"] = price_error
                    item["price"]["error"] = price_error
                item.update(_attach_prices(products, prices))
                text = search_api._as_next_data_html(parser_data, url)
                validation_error = _validation_error(text, url)
                if validation_error:
                    raise identity.EvidenceError(validation_error)
                from seda.parsers import parse_listing
                rows = parse_listing(text, "Casas Bahia", "https://www.casasbahia.com.br", url)
                _PAGE_IDS.setdefault((requested[0], requested[2]), {})[requested[1]] = ids
                item.update(stage="complete", products=len(products), parsed_rows=len(rows),
                            elapsed_seconds=round(time.monotonic() - started, 3))
                print(f"[seda] Casas Bahia mode=1-1 rest_api_url_first page={requested[1]} OK "
                      f"attempts={attempt} status=200 rows={len(rows)} "
                      f"price_status={item['price']['status_code']} "
                      f"price_pending={item['price_pending_products']} price_error={price_error or 'none'}", flush=True)
                return {"success": True, "text": text, "status_code": 200, "method": METHOD,
                        "error": "", "products": len(products), "trace": trace}
            except Exception as exc:
                item["error"] = _error(exc)
                item["elapsed_seconds"] = round(time.monotonic() - started, 3)
                print(f"[seda] Casas Bahia mode=1-1 rest_api_url_first page={requested[1]} "
                      f"attempt={attempt}/{limit} FAILED status={item['status_code']} "
                      f"error={item['error']}", flush=True)
    except Exception:
        if not trace:
            trace.append({"method": METHOD, "attempt": 0, "status_code": 0, "error": "rest_listing_request_failed"})
    finally:
        session.close()
    return {"success": False, "text": "", "status_code": trace[-1]["status_code"], "method": METHOD,
            "error": trace[-1].get("error", "rest_listing_failed"), "trace": trace}
