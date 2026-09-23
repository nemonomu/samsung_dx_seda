"""One existing direct price call for a missing, explicitly identified offer.

No environment loading, browser, generic transport, retries, seller guessing,
paid fallback, or mutation of an existing quote is performed here.
"""

from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit


PRICE_FIELDS = ("original_sku_price", "final_sku_price", "savings", "discount_type")


def is_blank(value):
    return value is None or isinstance(value, str) and not value.strip()


def _positive_id(value):
    if type(value) not in (int, str):
        return ""
    value = str(value).strip()
    return str(int(value)) if re.fullmatch(r"[0-9]{1,16}", value) and int(value) > 0 else ""


def _number(value):
    if type(value) not in (int, float, str, Decimal) or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _existing_money(value):
    """Accept only the existing BRL output contract or plain numeric values."""
    if not isinstance(value, str):
        return _number(value)
    text = re.sub(r"\s+", "", value)
    if text.startswith("R$"):
        text = text[2:]
        if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+),[0-9]{2}", text):
            return None
        return _number(text.replace(".", "").replace(",", "."))
    return _number(text)


def _result(error, *, attempted=False, cache_hit=False, status_code=0, detail=None):
    return {"success": bool(detail), "error": error, "attempted": attempted,
            "cache_hit": cache_hit, "status_code": status_code, "detail": detail or {}}


def _identity(row):
    from seda.parsers import sku_from_url

    try:
        parsed = urlsplit(row.get("product_url") or "")
    except (TypeError, ValueError):
        return None, "invalid_product_url"
    if parsed.scheme != "https" or parsed.hostname not in {"casasbahia.com.br", "www.casasbahia.com.br"}:
        return None, "invalid_product_url"
    product_id = _positive_id(row.get("retailer_product_id"))
    sku_id = _positive_id(sku_from_url(row.get("product_url") or ""))
    seller_id = _positive_id(row.get("seller_id"))
    for value, error in ((product_id, "missing_product_id"), (sku_id, "missing_sku_id"), (seller_id, "missing_seller_id")):
        if not value:
            return None, error
    return (product_id, sku_id, seller_id), ""


def _safe_response(value):
    if not isinstance(value, dict):
        return {"success": False, "offers": [], "status_code": 0}
    code = value.get("status_code")
    return {
        "success": value.get("success") is True,
        "offers": value.get("offers") if isinstance(value.get("offers"), list) else [],
        "status_code": code if type(code) is int and 0 <= code <= 599 else 0,
    }


def recover_missing_price(row, *, cache=None, fetcher=None):
    """Return missing-field updates only; never change ``row`` or inspect keys.

    ``cache`` holds the current backfill's normalized offer responses, not row
    updates, so coherence is checked separately for each duplicate output row.
    """
    if row.get("retailer") != "Casas Bahia":
        return _result("not_target_retailer")
    if not is_blank(row.get("final_sku_price")):
        return _result("price_already_present")
    identity, error = _identity(row)
    if error:
        return _result(error)
    product_id, sku_id, seller_id = identity
    cache_hit = cache is not None and identity in cache
    attempted = not cache_hit
    if cache_hit:
        response = _safe_response(cache[identity])
    else:
        if fetcher is None:
            from .price_api import fetch_listing_prices

            fetcher = fetch_listing_prices
        try:
            response = _safe_response(fetcher(
                [{"id": product_id, "sku": sku_id, "lojista": seller_id}],
                include_offers=True,
            ))
        except Exception:
            # No exception text, URL, response body, or headers enter a trace.
            response = {"success": False, "offers": [], "status_code": 0}
        if cache is not None:
            cache[identity] = response
    meta = {"attempted": attempted, "cache_hit": cache_hit, "status_code": response["status_code"]}
    if not response["success"] or response["status_code"] != 200:
        code = response["status_code"]
        return _result(f"price_http_{code}" if code and code != 200 else "price_response_unavailable", **meta)

    matches = [offer for offer in response["offers"] if isinstance(offer, dict)
               and tuple(_positive_id(offer.get(key)) for key in ("productId", "skuId", "sellerId")) == identity]
    if not matches:
        return _result("no_matching_price_offer", **meta)
    # Do not choose the last, cheapest, default seller, or an arbitrary quote.
    if len(matches) != 1:
        return _result("ambiguous_price_offers", **meta)
    offer = matches[0]
    availability = offer.get("availability")
    if isinstance(availability, dict):
        for key, expected in (("IdSku", sku_id), ("IdLojista", seller_id)):
            if not is_blank(availability.get(key)) and _positive_id(availability[key]) != expected:
                return _result("price_availability_identity_conflict", **meta)
    current = _number(offer.get("currentPrice"))
    if current is None:
        return _result("invalid_current_price", **meta)
    old = _number(offer.get("oldPrice"))
    standard = _number(offer.get("standardPrice"))
    for key, number in (("oldPrice", old), ("standardPrice", standard)):
        if not is_blank(offer.get(key)) and offer.get(key) not in (0, "0") and number is None:
            return _result("invalid_price_quote", **meta)
    if not is_blank(row.get("original_sku_price")):
        existing_old = _existing_money(row["original_sku_price"])
        if existing_old is None or old is None or existing_old != old:
            return _result("existing_price_quote_conflict", **meta)

    from seda.parsers import _casas_bahia_savings_text, _casas_bahia_ssr_discount_text, format_brl

    try:
        savings = _casas_bahia_savings_text(offer, offer.get("discountRate", ""))
        candidates = {
            "final_sku_price": format_brl(current),
            "original_sku_price": format_brl(old) if old is not None else "",
            "savings": savings,
            # Preserve the existing listing discount-text extraction contract;
            # do not reinterpret independent promotion flags or payment methods.
            "discount_type": _casas_bahia_ssr_discount_text(offer, [], []),
        }
    except Exception:
        # Keep request-count/status evidence even if an existing formatter
        # cannot represent a malformed quote; never emit its raw exception.
        return _result("price_quote_parse_failed", **meta)
    if not is_blank(row.get("savings")) and str(row["savings"]).strip() != str(savings or "").strip():
        return _result("existing_savings_quote_conflict", **meta)
    detail = {key: value for key, value in candidates.items() if is_blank(row.get(key)) and not is_blank(value)}
    return _result("", detail=detail, **meta)
