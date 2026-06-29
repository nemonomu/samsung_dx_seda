import os
import re
import time

import requests

from ..parsers import (
    clean_text,
    compact_json,
    format_brl,
    ldy_color_from_text,
)
from ..step00_config import product_line


GRAPHQL_URL = "https://federation.magazineluiza.com.br/graphql"

ITEM_QUERY = """
query itemQuery($itemId: ID!, $zipcode: String) {
  item(id: $itemId, zipcode: $zipcode) {
    id
    offerId
    title
    description
    path
    attributes {
      current
      label
      type
      values {
        available
        image
        path
        value
        variationId
      }
    }
    dimensions {
      depth
      height
      weight
      width
    }
    factsheet {
      displayName
      position
      slug
      elements {
        keyName
        position
        slug
        elements {
          isHtml
          keyName
          position
          slug
          value
        }
      }
    }
    offers {
      variationId
      price
      listPrice
      bestPrice {
        totalAmount
        discount
        paymentMethodDescription
        paymentMethodId
      }
      seller {
        id
        sku
        description
        tags {
          type
          discountValue
          message
        }
      }
    }
    rating {
      count
      score
    }
    category {
      id
      name
      url
    }
    subcategory {
      id
      name
      url
    }
  }
}
"""

SHIPPING_QUERY = """
fragment estimateError on EstimateErrorResponse {
  error
  status
  message
  uuid
  __typename
}

fragment shippings on ShippingResponse {
  status
  shippings {
    id
    packages {
      deliveryTypes {
        id
        description
        type
        time
        price
        __typename
      }
      __typename
    }
    __typename
  }
  __typename
}

fragment estimate on EstimateResponse {
  disclaimers {
    sequence
    message
    __typename
  }
  deliveries {
    closenessGroup {
      id
      __typename
    }
    id
    status {
      code
      __typename
    }
    modalities {
      id
      type
      name
      shippingTime {
        unit
        value {
          min
          max
          __typename
        }
        description
        disclaimers {
          sequence
          message
          __typename
        }
        expectedDeliveryDate {
          max
          min
          __typename
        }
        __typename
      }
      cost {
        customer
        __typename
      }
      prices {
        customer
        operation
        currency
        exchangeRate
        __typename
      }
      __typename
    }
    __typename
  }
  closenessGroups {
    customerCost
    disclaimer
    id
    items {
      seller {
        id
        sku
        __typename
      }
      __typename
    }
    name
    operationCost
    slug
    shortPolicy
    target
    targetRemaining
    __typename
  }
  status
  __typename
}

query shippingQuery($shippingRequest: ShippingRequest!) {
  shipping(shippingRequest: $shippingRequest) {
    ...shippings
    ...estimate
    ...estimateError
    __typename
  }
}
"""

SHOWCASE_QUERY = """
query showcaseQuery(
  $showcaseId: String
  $customerId: String
  $placeId: String
  $pageId: String
  $partnerId: String
  $pmdPromoter: String
  $storeId: String
  $productId: String
  $filters: [FilterInput]
  $includePagination: Boolean = true
  $toggleWishlist: Boolean = true
  $zipcode: String
  $isSourceProductAds: Boolean = false
) {
  recommendation(
    recommendationRequest: {
      customerId: $customerId
      pageId: $pageId
      placeId: $placeId
      productId: $productId
      metadata: {
        partnerId: $partnerId
        loyaltyParams: { pmdPromoter: $pmdPromoter, storeId: $storeId }
      }
      filters: $filters
      searchRequest: { location: { zipCode: $zipcode } }
      isSourceProductAds: $isSourceProductAds
    }
  ) {
    dynamic(showcaseId: $showcaseId) {
      id
      title
      type
      designTokenId
      products {
        id
        adsSellerId
        variationId
        title
        description
        image
        available
        url
        path
        reference
        offerTags
        restrictions
        rating {
          count
          score
        }
        isOnWishlist @include(if: $toggleWishlist)
        shippingTag {
          cost
          time
          complement
        }
      }
      pagination {
        cursor @include(if: $includePagination)
        next @include(if: $includePagination)
        previous @include(if: $includePagination)
      }
    }
  }
}
"""

def fetch_detail(item_id, timeout=None, seller_id=None, context_url=None):
    item_id = clean_text(item_id)
    if not item_id:
        return {"success": False, "error": "missing_item_id", "detail": {}, "trace": []}
    timeout = int(timeout or os.getenv("SEDA_MAGALU_DETAIL_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    trace = []
    item = _request_item(item_id, timeout, trace, context_url=context_url)
    if not item:
        return {"success": False, "error": "item_query_failed", "detail": {}, "trace": trace}
    detail = _detail_from_item(item, seller_id=seller_id)
    if os.getenv("SEDA_MAGALU_SHIPPING_GRAPHQL", "1").lower() not in {"0", "false", "no", "n"}:
        shipping = fetch_shipping(item, timeout=timeout, seller_id=seller_id, context_url=context_url)
        if shipping.get("delivery"):
            detail["delivery_availability"] = shipping["delivery"]
        if shipping.get("pickup"):
            detail["pick_up_availability"] = shipping["pickup"]
        trace.extend(shipping.get("trace") or [])
    if os.getenv("SEDA_MAGALU_SIMILAR_GRAPHQL", "1").lower() not in {"0", "false", "no", "n"}:
        similar = fetch_similar_names(item_id, timeout=timeout, context_url=context_url)
        if similar.get("names"):
            detail["retailer_sku_name_similar"] = compact_json(similar["names"])
        trace.extend(similar.get("trace") or [])
    return {"success": True, "detail": detail, "trace": trace}

def fetch_shipping(item, timeout=None, seller_id=None, context_url=None):
    timeout = int(timeout or os.getenv("SEDA_MAGALU_DETAIL_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    trace = []
    payload = {
        "operationName": "shippingQuery",
        "variables": {"shippingRequest": _shipping_request(item, seller_id=seller_id)},
        "query": SHIPPING_QUERY,
    }
    data = _post(payload, timeout, trace, label="shipping", context_url=context_url)
    shipping = (data.get("data") or {}).get("shipping") or {}
    delivery, pickup = _shipping_texts(shipping)
    return {"success": bool(delivery or pickup), "delivery": delivery, "pickup": pickup, "trace": trace}

def fetch_shipping_for_item_id(item_id, timeout=None, seller_id=None, context_url=None):
    item_id = clean_text(item_id)
    if not item_id:
        return {"success": False, "error": "missing_item_id", "delivery": "", "pickup": "", "trace": []}
    timeout = int(timeout or os.getenv("SEDA_MAGALU_DETAIL_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    trace = []
    item = _request_item(item_id, timeout, trace, context_url=context_url)
    if not item:
        return {"success": False, "error": "item_query_failed", "delivery": "", "pickup": "", "trace": trace}
    result = fetch_shipping(item, timeout=timeout, seller_id=seller_id, context_url=context_url)
    trace.extend(result.get("trace") or [])
    result["trace"] = trace
    return result

def fetch_similar_names(item_id, timeout=None, context_url=None):
    timeout = int(timeout or os.getenv("SEDA_MAGALU_DETAIL_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    trace = []
    for place_id in _similar_place_ids():
        payload = {
            "operationName": "showcaseQuery",
            "variables": {
                "includePagination": False,
                "toggleWishlist": True,
                "isSourceProductAds": False,
                "customerId": os.getenv("SEDA_MAGALU_CUSTOMER_ID", "temp_fff62ce7-702a-448f-bd1f-e9341b5cfe15"),
                "filters": [],
                "pageId": "mAmrPHGlhj",
                "placeId": place_id,
                "productId": item_id,
                "zipcode": _zipcode_for_graphql("SEDA_MAGALU_SIMILAR_ZIP_CODE"),
            },
            "query": SHOWCASE_QUERY,
        }
        response = _post(payload, timeout, trace, label=f"showcase:{place_id}", context_url=context_url)
        dynamic = (response.get("data") or {}).get("recommendation", {}).get("dynamic")
        for showcase in dynamic or []:
            title = clean_text(showcase.get("title"))
            if not _is_similar_showcase_title(title):
                continue
            names = [clean_text(product.get("title")) for product in showcase.get("products") or []]
            names = [name for name in names if name]
            if names:
                return {"success": True, "names": names[:20], "trace": trace}
    return {"success": False, "names": [], "trace": trace}

def _request_item(item_id, timeout, trace, context_url=None):
    payload = {
        "operationName": "itemQuery",
        "variables": {"itemId": item_id, "zipcode": _zipcode_for_graphql("SEDA_MAGALU_ZIP_CODE")},
        "query": ITEM_QUERY,
    }
    data = _post(payload, timeout, trace, label="item", context_url=context_url)
    return (data.get("data") or {}).get("item") or {}

def _post(payload, timeout, trace, label, context_url=None):
    if _browser_context_label(label):
        data = _post_browser_graphql(payload, timeout, trace, label, context_url=context_url)
        if data is not None:
            return data
        return {}

    retries = int(os.getenv("SEDA_MAGALU_DETAIL_RETRIES", "0"))
    sleep_seconds = float(os.getenv("SEDA_MAGALU_DETAIL_RETRY_SLEEP_SECONDS", "3.0"))
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(sleep_seconds * attempt)
        try:
            response = requests.post(GRAPHQL_URL, json=payload, headers=_headers(), timeout=timeout)
        except Exception as exc:
            trace.append({"label": label, "attempt": attempt + 1, "method": "requests", "status_code": 0, "error": f"{type(exc).__name__}: {exc}"})
            continue
        trace_item = {
            "label": label,
            "attempt": attempt + 1,
            "method": "requests",
            "status_code": response.status_code,
            "length": len(response.text or ""),
        }
        trace.append(trace_item)
        if response.status_code != 200 or "application/json" not in response.headers.get("content-type", ""):
            trace_item["error"] = "non_json_or_blocked"
            continue
        try:
            data = response.json()
        except ValueError:
            trace_item["error"] = "invalid_json"
            continue
        if data.get("errors"):
            trace_item["error"] = "graphql_errors"
            trace_item["errors"] = data.get("errors")
            continue
        return data
    if os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL", "0").lower() not in {"0", "false", "no", "n"}:
        data = _post_browser_graphql(payload, timeout, trace, label, context_url=context_url)
        if data is not None:
            return data
    return {}

def _post_browser_graphql(payload, timeout, trace, label, context_url=None):
    try:
        from .browser_session import graphql_post

        result = graphql_post(payload, timeout=timeout, context_url=context_url)
    except Exception as exc:
        trace.append({"label": label, "attempt": 1, "method": "browser_graphql", "status_code": 0, "error": f"{type(exc).__name__}: {exc}"})
        return None

    trace_item = {
        "label": label,
        "attempt": 1,
        "method": "browser_graphql",
        "status_code": result.get("status_code", 0),
        "length": len(result.get("text") or ""),
    }
    trace.append(trace_item)
    data = result.get("data") or {}
    if result.get("status_code") == 200 and data and not data.get("errors"):
        return data
    trace_item["error"] = result.get("error") or ("graphql_errors" if data.get("errors") else "non_json_or_blocked")
    if data.get("errors"):
        trace_item["errors"] = data.get("errors")
    return None

def _browser_context_label(label):
    label = str(label or "")
    return label == "shipping" or label.startswith("showcase:")

def _detail_from_item(item, seller_id=None):
    offer = _select_offer(item, seller_id=seller_id)
    best_price = offer.get("bestPrice") if isinstance(offer.get("bestPrice"), dict) else {}
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
    line = product_line()
    model = _factsheet_value(item, ["modelo"])
    reference = _factsheet_value(item, ["referencia", "referência"])
    detail = {
        "retailer": "Magalu",
        "sku": _sku_for_product_line(line, reference, model, item),
        "retailer_sku_name": clean_text(item.get("title")),
        "original_sku_price": format_brl(offer.get("listPrice")),
        "final_sku_price": format_brl(best_price.get("totalAmount") or offer.get("price")),
        "screen_size": _attribute_value(item, ["polegadas", "tamanho da tela"]) or _factsheet_value(item, ["polegadas", "tamanho da tela"]),
        "estimated_annual_electricity_use": _energy_use(item),
        "model_year": _factsheet_value(item, ["ano de lancamento", "ano de lançamento"]),
        "star_rating": clean_text(rating.get("score")),
        "count_of_star_ratings": clean_text(rating.get("count")),
        "parse_status": "detail_item_graphql",
    }
    if line == "REF":
        detail.update(
            {
                "ref_refrigerator_type": _ref_refrigerator_type(item),
                "ref_capacity": _factsheet_value(
                    item,
                    ["capacidade liquida total", "capacidade líquida total", "capacidade total"],
                ),
            }
        )
    if line == "LDY":
        detail.update(
            {
                "ldy_loading_type": _factsheet_value(
                    item,
                    [
                        "tipo de abertura",
                        "tipo de abertura eletrodomestico",
                        "tipo de abertura eletrodoméstico",
                        "tipo de abertura electrodomestico",
                        "tipo de abertura do eletrodomestico",
                        "abertura da tampa",
                    ],
                ),
                "ldy_capacity": _factsheet_value(item, ["capacidade de lavagem"]) or _factsheet_value(item, ["capacidade"]),
                "ldy_color": _factsheet_value(item, ["cor", "cor do produto"]) or ldy_color_from_text(item.get("title")),
            }
        )
    return detail

def _energy_use(item):
    allowed_keys = {
        "consumo aproximado de energia",
        "consumo de energia",
        "consumo mensal de energia",
        "consumo energetico",
    }
    for fact in _iter_facts(item.get("factsheet") or []):
        key = _ascii_lower(fact.get("keyName") or fact.get("slug"))
        if key not in allowed_keys:
            continue
        value = clean_text(_fact_value(fact))
        if value:
            return value
    return ""

def _ref_refrigerator_type(item):
    for fact in _iter_facts(item.get("factsheet") or []):
        key = _ascii_lower(fact.get("keyName") or fact.get("slug"))
        if key not in {"porta", "tipo"}:
            continue
        cleaned = _clean_ref_refrigerator_type(_fact_value(fact))
        if cleaned:
            return cleaned
    return ""

def _clean_ref_refrigerator_type(value):
    text = clean_text(value)
    normalized = _ascii_lower(text)
    if not normalized:
        return ""
    if normalized in {"sim", "nao", "1", "2", "02", "3", "4"}:
        return ""
    if re.search(r"\b\d+(?:[,.]\d+)?\s*(?:cm|mm|m)\b", normalized, re.I):
        return ""
    valid = re.search(
        r"duplex|inverse|inverso|side\s*by\s*side|french|multidoor|multi\s*door|top\s*freezer|"
        r"porta\s+francesa|\b(?:1|2|3|4)\s*portas?\b|uma\s+porta|duas\s+portas|tres\s+portas|quatro\s+portas",
        normalized,
        re.I,
    )
    return text if valid else ""

def _sku_for_product_line(line, reference, model, item):
    return reference or model

def _first_offer(item):
    offers = item.get("offers") if isinstance(item.get("offers"), list) else []
    return offers[0] if offers and isinstance(offers[0], dict) else {}

def _select_offer(item, seller_id=None):
    offers = item.get("offers") if isinstance(item.get("offers"), list) else []
    wanted = clean_text(seller_id).lower()
    if wanted:
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            seller = offer.get("seller") if isinstance(offer.get("seller"), dict) else {}
            if clean_text(seller.get("id")).lower() == wanted:
                return offer
    return _first_offer(item)

def _shipping_request(item, seller_id=None):
    offer = _select_offer(item, seller_id=seller_id)
    seller = offer.get("seller") if isinstance(offer.get("seller"), dict) else {}
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    subcategory = item.get("subcategory") if isinstance(item.get("subcategory"), dict) else {}
    dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
    return {
        "metadata": {
            "categoryId": clean_text(category.get("id")),
            "clientId": os.getenv("SEDA_MAGALU_SHIPPING_CLIENT_ID", ""),
            "organizationId": os.getenv("SEDA_MAGALU_ORGANIZATION_ID", "magazine_luiza"),
            "pageName": "",
            "partnerId": os.getenv("SEDA_MAGALU_PARTNER_ID", "0"),
            "salesChannelId": os.getenv("SEDA_MAGALU_SALES_CHANNEL_ID", "45"),
            "sellerId": clean_text(seller.get("id")),
            "sellerName": clean_text(seller.get("description")),
            "subcategoryId": clean_text(subcategory.get("id")),
        },
        "product": {
            "dimensions": {
                "height": _float_or_zero(dimensions.get("height")),
                "length": _float_or_zero(dimensions.get("depth") or dimensions.get("length")),
                "weight": _float_or_zero(dimensions.get("weight")),
                "width": _float_or_zero(dimensions.get("width")),
            },
            "id": clean_text(item.get("id")),
            "price": _float_or_zero(_price_number(offer)),
            "quantity": int(os.getenv("SEDA_MAGALU_SHIPPING_QUANTITY", "1")),
            "type": "product",
        },
        "zipcode": _zipcode_for_graphql("SEDA_MAGALU_SHIPPING_ZIP_CODE"),
    }

def _price_number(offer):
    best_price = offer.get("bestPrice") if isinstance(offer.get("bestPrice"), dict) else {}
    return best_price.get("totalAmount") or offer.get("price") or offer.get("listPrice") or 0

def _float_or_zero(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0

def _shipping_texts(shipping):
    delivery = ""
    pickup = ""
    for delivery_group in shipping.get("deliveries") or []:
        for modality in delivery_group.get("modalities") or []:
            shipping_time = modality.get("shippingTime") if isinstance(modality.get("shippingTime"), dict) else {}
            description = clean_text(shipping_time.get("description"))
            modality_type = clean_text(modality.get("type")).lower()
            modality_name = clean_text(modality.get("name")).lower()
            if not description:
                continue
            if "pickup" in modality_type or "retira" in modality_name:
                pickup = pickup or description
            else:
                delivery = delivery or description
    if not delivery:
        for shipment in shipping.get("shippings") or []:
            for package in shipment.get("packages") or []:
                for delivery_type in package.get("deliveryTypes") or []:
                    description = clean_text(delivery_type.get("description"))
                    if description:
                        delivery = delivery or description
    return delivery, pickup

def _attribute_value(item, labels):
    wanted = {_ascii_lower(label) for label in labels}
    for attribute in item.get("attributes") or []:
        label = _ascii_lower(attribute.get("label") or attribute.get("type"))
        if label in wanted:
            return clean_text(attribute.get("current") or attribute.get("value"))
    return ""

def _factsheet_value(item, labels):
    wanted = [_ascii_lower(label) for label in labels]
    for fact in _iter_facts(item.get("factsheet") or []):
        key = _ascii_lower(fact.get("keyName") or fact.get("slug"))
        if key in wanted:
            return _fact_value(fact)
    return ""

def _iter_facts(facts):
    if not isinstance(facts, list):
        return
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if fact.get("keyName") or fact.get("slug"):
            yield fact
        yield from _iter_facts(fact.get("elements") or [])

def _fact_value(fact):
    values = [clean_text(element.get("value")) for element in fact.get("elements") or [] if isinstance(element, dict)]
    values = [value for value in values if value]
    return "; ".join(values) if values else clean_text(fact.get("value"))

def _similar_place_ids():
    raw = os.getenv("SEDA_MAGALU_SIMILAR_PLACE_IDS", "RYmKwYF0uh,RiGQ7RdPP0,qugQi55lh4,dnhhGeeru9,jupMXmS6EV")
    return [item.strip() for item in raw.split(",") if item.strip()]

def _is_similar_showcase_title(title):
    normalized = _ascii_lower(title)
    raw = clean_text(title).lower()
    if "quem viu" in normalized and "tamb" in normalized and "viu" in normalized:
        return True
    return "quem viu" in raw and "tamb" in raw and "viu" in raw

def _zipcode_for_graphql(env_name):
    raw = os.getenv(env_name) or os.getenv("SEDA_POSTAL_CODE") or "01001-001"
    digits = re.sub(r"\D+", "", str(raw))
    return digits or "01001001"

def _ascii_lower(value):
    import unicodedata

    normalized = unicodedata.normalize("NFKD", clean_text(value))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()

def _headers():
    return {
        "accept": "*/*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": "https://www.magazineluiza.com.br",
        "pragma": "no-cache",
        "referer": "https://www.magazineluiza.com.br/",
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
        "x-channel-id": os.getenv("SEDA_MAGALU_SALES_CHANNEL_ID", "45"),
        "x-channel-name": os.getenv("SEDA_MAGALU_CHANNEL_NAME", "mixer-desk.magazineluiza.com.br"),
    }
