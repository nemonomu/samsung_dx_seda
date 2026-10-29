"""Mode 3 quote admission: retain relevant URLs with unresolved prices as NULL."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import browser_listing as strict
from . import browser_listing_url_first as optional

PENDING = "casas_mode3_price_pending"


def pending(row):
    return PENDING in str(row.get("parse_status") or "").split("+")


def _url(value):
    parts = urlsplit(urljoin("https://www.casasbahia.com.br", str(value or "")))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _equal_number(a, b):
    if not optional._quote_value(a) or not optional._quote_value(b):
        return False
    try:
        return Decimal(str(a).replace(",", ".")) == Decimal(str(b).replace(",", "."))
    except InvalidOperation:
        return False


def _equal_quote(product, alternatives):
    """Only compare two independently identified quotes in the same context."""
    quote = product.get("price")
    if not isinstance(quote, dict) or not alternatives:
        return None
    pid, sku = strict._positive_id(product.get("id")), strict._positive_id(product.get("idSku"))
    seller = strict._positive_id(product.get("lojista") or product.get("sellerId"))
    if not seller or any(strict._positive_id(quote.get(k)) != v
                         for k, v in (("productId", pid), ("skuId", sku), ("sellerId", seller))):
        return None
    checked = optional._offer_map([quote])
    if not checked:
        return None
    for other in alternatives:
        if (other["productId"] != pid or other["sellerId"] != seller
                or not _equal_number(quote.get("currentPrice"), other.get("currentPrice"))
                or not _equal_number(quote.get("standardPrice"), other.get("standardPrice"))
                or quote.get("discountDescription") != other.get("discountDescription")
                or quote.get("installment") != other.get("installment")):
            return None
    return deepcopy(quote)


def _report(product, alternatives, products, reason):
    """Log only public product identities, not entire requests or responses."""
    by_sku = {strict._positive_id(p.get("idSku")): p for p in products}
    def item(p, quote=None):
        quote = quote or {}
        return {"product_id": strict._positive_id(p.get("id")) or quote.get("productId"),
                "sku_id": strict._positive_id(p.get("idSku")) or quote.get("skuId"),
                "name": p.get("name") or p.get("title"),
                "url": _url(p.get("href") or p.get("url")) if p.get("href") or p.get("url") else None,
                "sku_lookup_url": "https://www.casasbahia.com.br/p/" + str(quote.get("skuId") or p.get("idSku") or ""),
                "seller_id": quote.get("sellerId") or p.get("lojista") or p.get("sellerId"),
                "price": quote.get("currentPrice"), "standard_price": quote.get("standardPrice"),
                "payment_condition": quote.get("discountDescription")}
    source_quote = product.get("price") if isinstance(product.get("price"), dict) else {}
    record = {"reason": reason, "source": item(product, source_quote),
              "alternatives": [item(by_sku.get(q["skuId"], {}), q) for q in alternatives]}
    # Unknown alternate names/URLs remain null rather than copying the source variant.
    try:
        print("[seda] casas mode=3 price_comparison " + json.dumps(record, ensure_ascii=True), flush=True)
    except Exception:
        pass


def enrich(raw, url, price_payloads):
    from seda.parsers import (extract_next_data, parse_listing, sku_from_url,
                              _casas_bahia_tv_listing, _casas_bahia_is_tv_product,
                              _casas_bahia_is_relevant_product)
    from seda.step00_config import product_line
    requested = strict._request_identity(url)
    payload = extract_next_data(raw)
    try:
        search = payload["props"]["pageProps"]["initialState"]["search"]
        products, query = search["results"]["products"], search["query"]
    except (TypeError, KeyError):
        raise strict.EvidenceError("missing_ssr_listing") from None
    if not isinstance(query, dict) or strict._positive_id(query.get("page")) != requested[1]:
        raise strict.EvidenceError("requested_page_mismatch")
    if requested[2] and strict._sort_value(query) != requested[2]:
        raise strict.EvidenceError("requested_sort_mismatch")
    if not isinstance(products, list) or not products:
        raise strict.EvidenceError("empty_ssr_products")
    offers = optional._offer_map(optional._normalize_offers(price_payloads, allow_partial=True))
    tv_only = product_line() == "TV" and _casas_bahia_tv_listing(url, search)
    ids, relevant_ids = [], []
    for product in products:
        if not isinstance(product, dict):
            raise strict.EvidenceError("invalid_ssr_product")
        pid, sku = strict._positive_id(product.get("id")), strict._positive_id(product.get("idSku"))
        seller_raw = product.get("lojista") or product.get("sellerId")
        seller = strict._positive_id(seller_raw)
        if not pid or not sku or sku != optional._product_url_sku(product) or (seller_raw and not seller):
            raise strict.EvidenceError("listing_identity_invalid")
        if sku in ids:
            raise strict.EvidenceError("duplicate_listing_sku")
        ids.append(sku)
        relevant = _casas_bahia_is_tv_product(product) if tv_only else _casas_bahia_is_relevant_product(product)
        if not relevant:
            continue
        relevant_ids.append(sku)
        exact = [q for key, q in offers.items() if key[:2] == (pid, sku) and (not seller or key[2] == seller)]
        alternatives = [q for key, q in offers.items() if key[0] == pid and key[1] != sku]
        quote = exact[0] if len(exact) == 1 and optional._quote_value(exact[0].get("currentPrice")) else None
        if quote is None and not exact:
            quote = _equal_quote(product, alternatives)
        product["_casas_listing_url_first"] = True
        if quote is not None:
            if not exact:
                _report(product, alternatives, products, "equal_independent_quotes")
            product["price"], product["lojista"] = quote, quote["sellerId"]
        else:
            _report(product, alternatives or exact, products, "price_pending")
            product["price"] = {}
            product["_casas_listing_price_pending"] = True
            product["_casas_mode3_price_pending"] = True
            product["_casas_listing_seller_pending"] = not bool(seller)
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    html, count = strict._NEXT_SCRIPT.subn(lambda m: m[1] + encoded + m[3], raw)
    if count != 1:
        raise strict.EvidenceError("next_data_script_count_invalid")
    rows = parse_listing(html, "Casas Bahia", "https://www.casasbahia.com.br", url)
    if not rows or [sku_from_url(r["product_url"]) for r in rows] != relevant_ids:
        raise strict.EvidenceError("parsed_listing_identity_invalid")
    return html, ids, len(rows)


def validation_error(raw, url):
    """Validate already-admitted mode-3 raw without reviving pending quotes."""
    from seda.parsers import extract_next_data
    try:
        payload = extract_next_data(raw)
        products = payload["props"]["pageProps"]["initialState"]["search"]["results"]["products"]
        offers = []
        for p in products:
            q = p.get("price") or {}
            if isinstance(q, dict) and q.get("currentPrice"):
                offers.append({"PrecoVenda": {"IdProduto": q.get("productId"), "IdSku": q.get("skuId"),
                               "IdLojista": q.get("sellerId"), "Preco": q["currentPrice"]}})
        enrich(raw, url, [{"Ofertas": offers}])
        return ""
    except Exception as exc:
        return str(exc) if isinstance(exc, strict.EvidenceError) else "invalid_listing_payload"
