"""Attach verified REST listing offers without changing the requested SKU."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation


def _positive_id(value):
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    return text if text.isascii() and text.isdigit() and int(text) > 0 else ""


def _identity(product):
    from seda.parsers import sku_from_url

    product_id = _positive_id(product.get("id"))
    sku = _positive_id(product.get("sku"))
    url_sku = _positive_id(sku_from_url(product.get("href") or product.get("url") or ""))
    if sku and url_sku and sku != url_sku:
        return None
    seller = _positive_id(product.get("lojista") or product.get("sellerId"))
    for key in ("lojista", "sellerId"):
        if product.get(key) not in (None, "", 0, "0"):
            supplied = _positive_id(product[key])
            if not supplied or supplied != seller:
                return None
    # A malformed supplied identifier is not the same as an absent identifier.
    if product.get("sku") not in (None, "", 0, "0") and not sku:
        return None
    return (product_id, url_sku or sku, seller) if product_id and (url_sku or sku) else None


def _offers(result):
    if not result.get("success"):
        return []
    return [offer for offer in result.get("offers", []) if isinstance(offer, dict)]


def _matches(offers, identity):
    product_id, sku, seller = identity
    matches = []
    for offer in offers:
        if (_positive_id(offer.get("productId")) == product_id
                and _positive_id(offer.get("skuId")) == sku
                and (not seller or _positive_id(offer.get("sellerId")) == seller)):
            # The product and SKU requests may return the very same quote twice.
            if offer not in matches:
                matches.append(offer)
    return matches


def _verified_offer(offers, identity):
    matches = _matches(offers, identity)
    if len(matches) != 1:
        return None
    offer = matches[0]
    seller = _positive_id(offer.get("sellerId"))
    if not seller:
        return None
    availability = offer.get("availability") or {}
    if not isinstance(availability, dict):
        return None
    for key, expected in zip(("IdProduto", "IdSku", "IdLojista"), (*identity[:2], seller)):
        if availability.get(key) not in (None, "") and _positive_id(availability[key]) != expected:
            return None
    try:
        current = Decimal(str(offer.get("currentPrice")))
        if not current.is_finite() or current <= 0:
            return None
    except (InvalidOperation, ValueError, TypeError):
        return None
    return offer


def attach_prices(products, *, timeout=None, fetcher):
    """Retain every product; unresolved identities remain failures downstream.

    A seller from another SKU is only a request candidate. It is never attached
    without a subsequent response proving the original product and SKU match.
    """
    identities = [_identity(p) if isinstance(p, dict) else None for p in products]
    request_products = []
    for product, identity in zip(products, identities):
        if identity:
            item = dict(product)
            item["sku"] = identity[1]
            request_products.append(item)
    result = fetcher(request_products, timeout=timeout, include_offers=True)
    if not result.get("success"):
        return result
    offers = _offers(result)
    recovery_items = []
    seen = set()
    for identity in identities:
        if not identity or identity[2] or _matches(offers, identity):
            continue
        product_id, sku, _ = identity
        for offer in offers:
            seller = _positive_id(offer.get("sellerId"))
            if _positive_id(offer.get("productId")) != product_id or not seller:
                continue
            key = (product_id, sku, seller)
            if key not in seen:
                seen.add(key)
                recovery_items.append({"id": product_id, "sku": sku, "lojista": seller})
    recovery = None
    if recovery_items:
        # At most one extra batch per listing attempt, using the existing API.
        recovery = fetcher(recovery_items, timeout=timeout, include_offers=True)
        offers += _offers(recovery)
    unresolved = []
    for position, (product, identity) in enumerate(zip(products, identities), 1):
        if not isinstance(product, dict):
            continue
        offer = _verified_offer(offers, identity) if identity else None
        # Never leave an unverified search-payload price in the outgoing page.
        product.pop("price", None)
        if offer is None:
            unresolved.append(position)
            continue
        product["price"] = deepcopy(offer)
        if not identity[2]:
            product["lojista"] = offer["sellerId"]
        if not product.get("sku"):
            product["sku"] = identity[1]
    result = dict(result)
    result["identity_unresolved_positions"] = unresolved
    result["seller_recovery_attempted"] = bool(recovery_items)
    if recovery is not None:
        result["seller_recovery_success"] = bool(recovery.get("success"))
    return result
