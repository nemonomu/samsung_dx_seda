"""Mode 1-1 only: verify an exact URL quote, then fill missing output fields.

The existing Mode 4 recovery and price join are deliberately not changed.
Only the existing direct REST endpoint is used; this module never loads env,
selects a default seller, opens a browser, or adds a paid fallback.
"""

import re
from decimal import Decimal, InvalidOperation

from .detail_price_recovery import (
    PRICE_FIELDS, _existing_money, _identity, _number, _positive_id,
    _safe_response, is_blank,
)


def has_pending_price_fields(row):
    """Optional blanks need verification, but are not automatically errors."""
    return any(is_blank(row.get(field)) for field in (*PRICE_FIELDS, "seller_id"))


def _result(error="", *, attempted=False, cache_hit=False, status_code=0,
            detail=None, verified=False, quote_absent_fields=(), evidence=None):
    return {"success": verified, "verified": verified, "error": error,
            "attempted": attempted, "cache_hit": cache_hit,
            "status_code": status_code, "detail": detail or {},
            "quote_absent_fields": list(quote_absent_fields),
            "evidence": evidence or {}}


def _same_money(existing, quoted):
    """Compare the exact two-decimal output contract, not raw float tails."""
    from seda.parsers import format_brl

    value = _existing_money(existing)
    return value is not None and quoted is not None and format_brl(value) == format_brl(quoted)


def _same_savings(existing, quoted):
    left, right = str(existing or "").strip(), str(quoted or "").strip()
    if left == right:
        return True
    # Only normalize equivalent percentages with the same explicit label.
    # Never treat unrelated promotion text as a percentage or infer savings.
    pattern = r"baixou\s+([0-9]+(?:[.,][0-9]+)?)\s*%"
    matches = [re.fullmatch(pattern, value, flags=re.I) for value in (left, right)]
    if not all(matches):
        return False
    try:
        return Decimal(matches[0][1].replace(",", ".")) == Decimal(matches[1][1].replace(",", "."))
    except InvalidOperation:
        return False


def recover_rest_price_fields(row, *, cache=None, fetcher=None):
    """Return missing-only updates and sanitized verification evidence.

    Reusing ``cache`` guarantees one request per identity in this pass, including
    failed responses. Product-only responses can serve several exact URL SKUs.
    Existing values are never overwritten, even when a quote conflicts.
    """
    if row.get("retailer") != "Casas Bahia":
        return _result("not_target_retailer")
    if not has_pending_price_fields(row):
        return _result("price_fields_already_present")
    identity, error = _identity(row)
    if error:
        return _result(error)
    product_id, sku_id, seller_id = identity
    request_key = identity if seller_id else ("product", product_id)
    request_items = ([{"id": product_id, "sku": sku_id, "lojista": seller_id}]
                     if seller_id else [{"id": product_id}])
    cache_hit = cache is not None and request_key in cache
    if cache_hit:
        response = _safe_response(cache[request_key])
    else:
        if fetcher is None:
            from .price_api import fetch_listing_prices

            fetcher = fetch_listing_prices
        try:
            response = _safe_response(fetcher(request_items, include_offers=True))
        except Exception:
            response = {"success": False, "offers": [], "status_code": 0}
        if cache is not None:
            cache[request_key] = response
    offers = [offer for offer in response["offers"] if isinstance(offer, dict)]
    product_matches = [offer for offer in offers if _positive_id(offer.get("productId")) == product_id]
    sku_matches = [offer for offer in product_matches if _positive_id(offer.get("skuId")) == sku_id]
    matches = [offer for offer in sku_matches if not seller_id or _positive_id(offer.get("sellerId")) == seller_id]
    # All identifiers are validated numeric source IDs; no body, header, URL,
    # session token, exception string or arbitrary API error is copied.
    evidence = {"product_id": product_id, "sku_id": sku_id, "seller_id": seller_id,
                "offer_count": len(offers), "product_matches": len(product_matches),
                "sku_matches": len(sku_matches), "seller_matches": len(matches)}
    meta = {"attempted": not cache_hit, "cache_hit": cache_hit,
            "status_code": response["status_code"], "evidence": evidence}
    if not response["success"] or response["status_code"] != 200:
        code = response["status_code"]
        return _result(f"price_http_{code}" if code and code != 200 else "price_response_unavailable", **meta)
    if not offers:
        return _result("missing_price_offer", **meta)
    if not product_matches:
        return _result("price_product_identity_conflict", **meta)
    if not sku_matches:
        return _result("price_sku_identity_conflict", **meta)
    if not matches:
        return _result("price_seller_identity_conflict", **meta)
    if len(matches) != 1:
        return _result("ambiguous_price_offers", **meta)
    offer = matches[0]
    resolved_seller = _positive_id(offer.get("sellerId"))
    if not resolved_seller:
        return _result("missing_offer_seller_id", **meta)
    availability = offer.get("availability")
    if isinstance(availability, dict):
        for field, expected in (("IdProduto", product_id), ("IdSku", sku_id), ("IdLojista", resolved_seller)):
            if not is_blank(availability.get(field)) and _positive_id(availability[field]) != expected:
                evidence["conflict_field"] = field
                return _result("price_availability_identity_conflict", **meta)
    current, old, standard = (_number(offer.get(field)) for field in ("currentPrice", "oldPrice", "standardPrice"))
    if current is None:
        return _result("invalid_current_price", **meta)
    for field, number in (("oldPrice", old), ("standardPrice", standard)):
        if not is_blank(offer.get(field)) and offer.get(field) not in (0, "0") and number is None:
            evidence["conflict_field"] = field
            return _result("invalid_price_quote", **meta)

    from seda.parsers import _casas_bahia_savings_text, _casas_bahia_ssr_discount_text, format_brl

    try:
        if not is_blank(row.get("final_sku_price")) and not _same_money(row["final_sku_price"], current):
            return _result("existing_current_price_quote_conflict", **meta)
        if not is_blank(row.get("original_sku_price")) and not _same_money(row["original_sku_price"], old):
            return _result("existing_original_price_quote_conflict", **meta)
        candidates = {"final_sku_price": format_brl(current),
                      "original_sku_price": format_brl(old) if old is not None else "",
                      "savings": _casas_bahia_savings_text(offer, offer.get("discountRate", "")),
                      "discount_type": _casas_bahia_ssr_discount_text(offer, [], []),
                      "seller_id": resolved_seller}
        if not is_blank(row.get("savings")) and not _same_savings(row["savings"], candidates["savings"]):
            return _result("existing_savings_quote_conflict", **meta)
        if is_blank(candidates["final_sku_price"]):
            return _result("price_quote_parse_failed", **meta)
    except Exception:
        return _result("price_quote_parse_failed", **meta)
    detail = {field: value for field, value in candidates.items()
              if is_blank(row.get(field)) and not is_blank(value)}
    absent = [field for field in PRICE_FIELDS if is_blank(row.get(field)) and is_blank(candidates[field])]
    if cache is not None and not seller_id:
        # Early recovery discovers seller_id. The late pass must reuse the
        # already verified response rather than repeat it under its new key.
        cache[(product_id, sku_id, resolved_seller)] = response
    return _result(detail=detail, verified=True, quote_absent_fields=absent, **meta)


def backfill_rest_price_fields(rows, *, checkpoint_every=25, trace_rows=None,
                              row_index_offset=0, checkpoint_writer=None,
                              fetcher=None, cache=None, append_token,
                              record_subcall):
    """Mode 1-1 late reconciliation using normal checkpoint/trace callbacks."""
    candidates = [(index, row) for index, row in enumerate(rows, row_index_offset + 1)
                  if row.get("retailer") == "Casas Bahia" and has_pending_price_fields(row)]
    if not candidates:
        return rows
    print(f"[seda] casas mode=1-1 detail price verification candidates={len(candidates)} "
          "source=existing_rest max_calls_per_identity=1", flush=True)
    if cache is None:
        cache = {}
    recovered = verified_unchanged = failed = requests_made = 0
    for position, (index, row) in enumerate(candidates, 1):
        result = recover_rest_price_fields(row, cache=cache, fetcher=fetcher)
        requests_made += int(result["attempted"])
        filled = []
        if result["verified"]:
            for field, value in result["detail"].items():
                if field in (*PRICE_FIELDS, "seller_id") and is_blank(row.get(field)) and not is_blank(value):
                    row[field] = value
                    filled.append(field)
            if filled:
                recovered += 1
                token = "casas_mode11_price_recovered"
            else:
                verified_unchanged += 1
                token = "casas_mode11_price_verified"
            method = "casas_bahia_rest_price_cache" if result["cache_hit"] else "casas_bahia_rest_price_api"
            row["fetch_method"] = append_token(row.get("fetch_method", ""), method)
        else:
            failed += 1
            token = f"casas_mode11_price_failed:{result['error']}"
        row["parse_status"] = append_token(row.get("parse_status", ""), token)
        evidence = result["evidence"]
        detail = (f"cache_hit:{int(result['cache_hit'])};filled:{','.join(filled)}"
                  f";quote_verified:{int(result['verified'])};quote_absent:{','.join(result['quote_absent_fields'])}"
                  + "".join(f";{key}:{value}" for key, value in evidence.items()))
        record_subcall(trace_rows, row, index, row.get("product_url", ""), "casas_mode11_price_recovery",
                       method="casas_bahia_rest_price_cache" if result["cache_hit"] else "casas_bahia_rest_price_api",
                       success=result["verified"], status_code=result["status_code"],
                       attempt=1 if result["attempted"] else 0, error=result["error"], detail=detail)
        if checkpoint_writer is not None and checkpoint_every and position % checkpoint_every == 0:
            checkpoint_writer(rows)
    if checkpoint_writer is not None and (not checkpoint_every or len(candidates) % checkpoint_every):
        checkpoint_writer(rows)
    remaining = sum(is_blank(row.get("final_sku_price")) for _, row in candidates)
    print(f"[seda] casas mode=1-1 detail price verification recovered={recovered}/{len(candidates)} "
          f"verified_unchanged={verified_unchanged} failed={failed} requests={requests_made} "
          f"remaining_final={remaining}", flush=True)
    return rows
