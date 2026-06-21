import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests


PRICE_URL = "https://api.casasbahia.com.br/merchandising/oferta/v1/Preco/Oferta/PrecoVenda/"


def fetch_listing_prices(products, timeout=None):
    product_items, sku_items = _price_items(products)
    if not product_items and not sku_items:
        return {"success": False, "prices": {}, "error": "empty_price_items"}

    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    body = {"produtos": product_items, "skus": sku_items}
    try:
        response = requests.post(PRICE_URL, params=_params(), headers=_headers(), json=body, timeout=timeout)
    except Exception as exc:
        return {"success": False, "prices": {}, "error": f"{type(exc).__name__}: {exc}"}

    if response.status_code != 200:
        return {
            "success": False,
            "prices": {},
            "error": f"price_http_{response.status_code}:{response.text[:200]}",
        }
    try:
        data = response.json()
    except ValueError:
        return {"success": False, "prices": {}, "error": "invalid_price_json"}

    prices = {}
    for offer in data.get("Ofertas") or []:
        price = offer.get("PrecoVenda") if isinstance(offer.get("PrecoVenda"), dict) else {}
        discount = (
            offer.get("DescontoFormaPagamento")
            if isinstance(offer.get("DescontoFormaPagamento"), dict)
            else {}
        )
        availability = offer.get("Disponibilidade") if isinstance(offer.get("Disponibilidade"), dict) else {}
        normalized = {
            "oldPrice": price.get("PrecoDe"),
            "currentPrice": discount.get("PrecoVendaComDesconto") or price.get("Preco"),
            "standardPrice": price.get("Preco"),
            "discountRate": _discount_rate(price, discount),
            "savings": _savings_text(price),
            "discountDescription": discount.get("DescricaoDesconto") or discount.get("FormaPagamento"),
            "installment": price.get("Parcelamento"),
            "availability": availability,
            "sellerId": price.get("IdLojista") or availability.get("IdLojista"),
            "skuId": price.get("IdSku") or availability.get("IdSku"),
            "productId": price.get("IdProduto"),
        }
        for key in (price.get("IdProduto"), price.get("IdSku")):
            if key not in (None, ""):
                prices[str(key)] = normalized
    return {"success": True, "prices": prices, "count": len(prices)}


def attach_listing_prices(products, timeout=None):
    result = fetch_listing_prices(products, timeout=timeout)
    if not result.get("success"):
        return result
    prices = result.get("prices") or {}
    for product in products:
        if not isinstance(product, dict):
            continue
        price = prices.get(str(product.get("id"))) or prices.get(str(product.get("sku")))
        if not price:
            continue
        product["price"] = price
        if price.get("sellerId") and not product.get("lojista"):
            product["lojista"] = price.get("sellerId")
        if price.get("skuId") and not product.get("sku"):
            product["sku"] = price.get("skuId")
    return result


def _price_items(products):
    product_items = []
    sku_items = []
    seen_products = set()
    seen_skus = set()
    for product in products:
        if not isinstance(product, dict):
            continue
        product_id = _int_value(product.get("id"))
        if product_id is not None and product_id not in seen_products:
            product_items.append({"idProduto": product_id})
            seen_products.add(product_id)
        sku_id = _int_value(product.get("sku"))
        seller_id = _int_value(product.get("lojista") or product.get("sellerId"))
        if sku_id is not None and seller_id is not None and (seller_id, sku_id) not in seen_skus:
            sku_items.append({"idLojista": seller_id, "idSku": sku_id})
            seen_skus.add((seller_id, sku_id))
    return product_items, sku_items


def _int_value(value):
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    return int(text)


def _discount_rate(price, discount):
    explicit = _decimal_value(price.get("PercentualDesconto"))
    if explicit is not None:
        explicit = abs(explicit)
        return _rounded_percent(explicit) if explicit >= Decimal("1") else ""
    old_price = price.get("PrecoDe")
    final_price = price.get("Preco")
    old_number = _decimal_value(old_price)
    final_number = _decimal_value(final_price)
    if old_number is None or final_number is None:
        return ""
    if old_number <= 0 or final_number >= old_number:
        return ""
    percent = (old_number - final_number) / old_number * Decimal("100")
    return _rounded_percent(percent) if percent >= Decimal("1") else ""


def _decimal_value(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _rounded_percent(value):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _savings_text(price):
    percent = _discount_rate(price, {})
    if percent in (None, ""):
        return ""
    try:
        number = float(str(percent).replace(",", "."))
    except ValueError:
        text = str(percent).strip()
        return f"Baixou {text}" if text else ""
    if number <= 0:
        return ""
    return f"Baixou {number:g}%"


def _params():
    return {
        "IdRegiao": os.getenv("SEDA_CASAS_BAHIA_REGION_ID", "126000"),
        "composicao": "DescontoFormaPagamento,MelhoresParcelamentos",
        "utm_campaign": os.getenv("SEDA_CASAS_BAHIA_UTM_CAMPAIGN", "cb_all_gg_brand_exata"),
        "utm_medium": os.getenv("SEDA_CASAS_BAHIA_UTM_MEDIUM", "cpc"),
        "utm_source": os.getenv("SEDA_CASAS_BAHIA_UTM_SOURCE", "gp_branding"),
        "cep": os.getenv("SEDA_CASAS_BAHIA_ZIPCODE", "01010010").replace("-", ""),
    }


def _headers():
    return {
        "accept": "*/*",
        "accept-language": os.getenv("SEDA_CASAS_BAHIA_ACCEPT_LANGUAGE", "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"),
        "apikey": os.getenv("SEDA_CASAS_BAHIA_PRICE_APIKEY", "d081fef8c2c44645bb082712ed32a047"),
        "cache-control": "no-cache",
        "content-type": "application/json",
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
    }
