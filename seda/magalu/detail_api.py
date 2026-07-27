import os
import re
import time

import requests

from ..parsers import (
    clean_text,
    compact_json,
    format_brl,
    ldy_color_from_text,
    magalu_exact_factsheet_reference,
    preferred_magalu_sku,
)
from ..step00_config import product_line
from .field_extraction import extract_fields as extract_semantic_fields
from .graphql_contract import (
    graphql_envelope_error,
    graphql_terminal_business_error,
)


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
    bundles {
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
    identity_error = _item_identity_error(item_id, item)
    if identity_error:
        return {"success": False, "error": identity_error, "detail": {}, "trace": trace}
    detail = _detail_from_item(item, seller_id=seller_id)
    detail["_detail_identity_verified"] = True
    detail["_detail_item_id"] = clean_text(item.get("id"))
    if os.getenv("SEDA_MAGALU_SHIPPING_GRAPHQL", "1").lower() not in {"0", "false", "no", "n"}:
        shipping = fetch_shipping(item, timeout=timeout, seller_id=seller_id, context_url=context_url)
        if shipping.get("delivery"):
            detail["delivery_availability"] = shipping["delivery"]
        if shipping.get("pickup"):
            detail["pick_up_availability"] = shipping["pickup"]
        trace.extend(shipping.get("trace") or [])
    similar_error = ""
    if os.getenv("SEDA_MAGALU_SIMILAR_GRAPHQL", "1").lower() not in {"0", "false", "no", "n"}:
        similar = fetch_similar_names(item_id, timeout=timeout, context_url=context_url)
        if similar.get("names"):
            detail["retailer_sku_name_similar"] = compact_json(similar["names"])
        similar_error = similar.get("error") or ""
        trace.extend(similar.get("trace") or [])
    result = {"success": True, "detail": detail, "trace": trace}
    if similar_error:
        result["similar_error"] = similar_error
    return result

def fetch_shipping(item, timeout=None, seller_id=None, context_url=None):
    timeout = int(timeout or os.getenv("SEDA_MAGALU_DETAIL_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    trace = []
    payload = {
        "operationName": "shippingQuery",
        "variables": {"shippingRequest": _shipping_request(item, seller_id=seller_id)},
        "query": SHIPPING_QUERY,
    }
    data = _post(payload, timeout, trace, label="shipping", context_url=context_url)
    response_data = data.get("data") if isinstance(data, dict) else {}
    shipping = (
        response_data.get("shipping")
        if isinstance(response_data, dict)
        else {}
    )
    shipping = shipping if isinstance(shipping, dict) else {}
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
    identity_error = _item_identity_error(item_id, item)
    if identity_error:
        return {"success": False, "error": identity_error, "delivery": "", "pickup": "", "trace": trace}
    result = fetch_shipping(item, timeout=timeout, seller_id=seller_id, context_url=context_url)
    trace.extend(result.get("trace") or [])
    result["trace"] = trace
    return result

def fetch_similar_names(item_id, timeout=None, context_url=None):
    timeout = int(timeout or os.getenv("SEDA_MAGALU_DETAIL_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    trace = []
    for place_id in _similar_place_ids():
        trace_start = len(trace)
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
        if any(
            item.get("showcase_failed_fetch_circuit_open")
            for item in trace[trace_start:]
        ):
            return {
                "success": False,
                "error": "showcase_failed_fetch_circuit_open",
                "names": [],
                "trace": trace,
            }
        response_data = response.get("data") if isinstance(response, dict) else {}
        recommendation = (
            response_data.get("recommendation")
            if isinstance(response_data, dict)
            else {}
        )
        recommendation = recommendation if isinstance(recommendation, dict) else {}
        dynamic = recommendation.get("dynamic")
        dynamic = dynamic if isinstance(dynamic, list) else []
        for showcase in dynamic:
            if not isinstance(showcase, dict):
                continue
            title = clean_text(showcase.get("title"))
            if not _is_similar_showcase_title(title):
                continue
            products = showcase.get("products")
            products = products if isinstance(products, list) else []
            names = [
                clean_text(product.get("title"))
                for product in products
                if isinstance(product, dict)
            ]
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


def _item_identity_error(requested_item_id, item):
    requested = clean_text(requested_item_id).casefold()
    actual = clean_text(item.get("id") if isinstance(item, dict) else "").casefold()
    if not actual:
        return "item_identity_missing"
    if requested != actual:
        return "item_identity_mismatch"
    return ""

def _post(payload, timeout, trace, label, context_url=None):
    if _browser_context_label(label):
        data = _post_browser_graphql(payload, timeout, trace, label, context_url=context_url)
        if data is not None:
            return data
        return {}

    browser_enabled = os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL", "0").lower() not in {"0", "false", "no", "n"}
    if browser_enabled:
        # federation blocks plain requests.post when browser mode is on; go straight to
        # the browser channel instead of wasting an always-blocked requests attempt.
        data = _post_browser_graphql(payload, timeout, trace, label, context_url=context_url)
        if data is not None:
            return data
        if os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL_REQUESTS_FALLBACK", "0").lower() in {
            "0",
            "false",
            "no",
            "n",
        }:
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
        semantic_error = _graphql_payload_error(data, label)
        if semantic_error:
            trace_item["error"] = semantic_error
            if isinstance(data, dict) and data.get("errors"):
                trace_item["errors"] = data.get("errors")
            if response.text:
                trace_item["response_preview"] = response.text[:500]
            terminal_business_error = graphql_terminal_business_error(
                payload.get("operationName") or "",
                data,
            )
            if terminal_business_error:
                trace_item["terminal_business_error"] = terminal_business_error
                break
            continue
        return data
    return {}

def _post_browser_graphql(payload, timeout, trace, label, context_url=None):
    try:
        from .browser_session import graphql_post

        result = graphql_post(payload, timeout=timeout, context_url=context_url)
    except Exception as exc:
        trace.append({"label": label, "attempt": 1, "method": "browser_graphql", "status_code": 0, "error": f"{type(exc).__name__}: {exc}"})
        return None

    browser_trace = result.get("trace") or [result]
    for browser_item in browser_trace:
        trace_item = {
            "label": label,
            "operation": browser_item.get("operation") or payload.get("operationName", ""),
            "attempt": browser_item.get("attempt", 1),
            "method": "browser_graphql",
            "status_code": browser_item.get("status_code", result.get("status_code", 0)),
            "length": browser_item.get("length", len(result.get("text") or "")),
            "item_present": browser_item.get("item_present", ""),
        }
        if browser_item.get("error"):
            trace_item["error"] = browser_item["error"]
        graphql_errors = browser_item.get("graphql_errors") or browser_item.get("errors")
        if graphql_errors:
            trace_item["errors"] = graphql_errors
        if browser_item.get("response_preview"):
            trace_item["response_preview"] = browser_item["response_preview"]
        for key in (
            "terminal_business_error",
            "showcase_failed_fetch_circuit_open",
            "recovery",
            "recovery_error",
        ):
            if browser_item.get(key):
                trace_item[key] = browser_item[key]
        trace.append(trace_item)
    data = result.get("data") or {}
    semantic_error = _graphql_payload_error(data, label)
    if result.get("status_code") == 200 and data and not semantic_error and not result.get("error"):
        return data
    if trace:
        trace_item = trace[-1]
        trace_item["error"] = result.get("error") or semantic_error or "non_json_or_blocked"
        if isinstance(data, dict) and data.get("errors"):
            trace_item["errors"] = data.get("errors")
        if result.get("text") and not trace_item.get("response_preview"):
            trace_item["response_preview"] = result["text"][:500]
    return None


def _graphql_payload_error(data, label):
    return graphql_envelope_error(data, require_item=label == "item")

def _browser_context_label(label):
    label = str(label or "")
    return label == "shipping" or label.startswith("showcase:")

def _detail_from_item(item, seller_id=None):
    offer = _select_offer(item, seller_id=seller_id)
    best_price = offer.get("bestPrice") if isinstance(offer.get("bestPrice"), dict) else {}
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
    line = product_line()
    semantic_fields = extract_semantic_fields(item, line)
    model = _factsheet_value(item, ["modelo"])
    reference = (
        magalu_exact_factsheet_reference(item)
        if line == "TV"
        else _factsheet_value(item, ["referencia", "referência"])
    )
    detail = {
        "retailer": "Magalu",
        "sku": _sku_for_product_line(line, reference, model, item),
        "_magalu_factsheet_reference": str(reference or "").strip(),
        "retailer_sku_name": clean_text(item.get("title")),
        "original_sku_price": format_brl(offer.get("listPrice")),
        "final_sku_price": format_brl(best_price.get("totalAmount") or offer.get("price")),
        "screen_size": semantic_fields["screen_size"],
        "estimated_annual_electricity_use": semantic_fields["estimated_annual_electricity_use"],
        "model_year": _factsheet_value(item, ["ano de lancamento", "ano de lançamento", "ano do modelo"])
        or _model_year_from_description(item),
        "star_rating": clean_text(rating.get("score")),
        "count_of_star_ratings": clean_text(rating.get("count")),
        "parse_status": "detail_item_graphql",
    }
    if line == "REF":
        detail.update(
            {
                "ref_refrigerator_type": _ref_refrigerator_type(item),
                "ref_capacity": semantic_fields["ref_capacity"],
            }
        )
    if line == "LDY":
        detail.update(
            {
                "ldy_loading_type": semantic_fields["ldy_loading_type"],
                "ldy_capacity": semantic_fields["ldy_capacity"],
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
    for fact in _iter_facts(_item_facts(item)):
        key = _ascii_lower(fact.get("keyName") or fact.get("slug"))
        if key not in allowed_keys:
            continue
        value = clean_text(_fact_value(fact))
        if value:
            return value
    return _energy_from_description(item)

def _description_text(item):
    return re.sub(r"<[^>]+>", "\n", str(item.get("description") or ""))

def _energy_from_description(item):
    # Fallback: free-text "Descrição e ficha técnica" block, e.g. "Consumo (máximo): 130 W".
    # Accept energy units (W / kWh) only; skip water consumption ("Consumo ... de Água").
    for m in re.finditer(r"Consumo[^:<\n]{0,40}:\s*([^<;\n|]+)", _description_text(item), re.I):
        if "agua" in _ascii_lower(m.group(0)):
            continue
        value = clean_text(m.group(1))
        if re.search(r"\d[\d.,]*\s*(?:k?wh|w)\b", value, re.I):
            return value
    return ""

def _model_year_from_description(item):
    # Fallback: free-text description block, e.g. "Ano: 2025".
    m = re.search(r"\bAno\b[^<\n]{0,20}?\b((?:19|20)\d{2})\b", _description_text(item), re.I)
    return m.group(1) if m else ""

def _capacity_from_description(item):
    # Fallback: free-text description block, e.g. "Capacidade: 394 litros" (liters only).
    for m in re.finditer(r"Capacidade[^:<\n]{0,40}:\s*([^<;\n|]+)", _description_text(item), re.I):
        value = clean_text(m.group(1))
        if re.search(r"\d[\d.,]*\s*(?:l\b|litros?)", value, re.I):
            return value
    return ""

def _ref_refrigerator_type(item):
    for fact in _iter_facts(_item_facts(item)):
        key = _ascii_lower(fact.get("keyName") or fact.get("slug"))
        if key not in {"porta", "portas", "tipo", "tipo de porta"}:
            continue
        cleaned = _clean_ref_refrigerator_type(_fact_value(fact))
        if cleaned:
            return cleaned
    # no valid door format in the type fields (e.g. "Porta"="Inverter" is compressor
    # tech, not a door type) -> infer from the door count. Only 2 -> Duplex; other
    # counts are ambiguous (Duplex vs Inverse, French vs Side-by-side) so left blank.
    doors = _factsheet_value(item, ["quantidade de portas", "numero de portas", "número de portas"])
    match = re.search(r"\d+", doors or "")
    if match and int(match.group()) == 2:
        return "Duplex"
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
    return preferred_magalu_sku(line, reference, model, item.get("title"))

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
    if not isinstance(shipping, dict):
        return delivery, pickup
    deliveries = shipping.get("deliveries")
    deliveries = deliveries if isinstance(deliveries, list) else []
    for delivery_group in deliveries:
        if not isinstance(delivery_group, dict):
            continue
        modalities = delivery_group.get("modalities")
        modalities = modalities if isinstance(modalities, list) else []
        for modality in modalities:
            if not isinstance(modality, dict):
                continue
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
        shippings = shipping.get("shippings")
        shippings = shippings if isinstance(shippings, list) else []
        for shipment in shippings:
            if not isinstance(shipment, dict):
                continue
            packages = shipment.get("packages")
            packages = packages if isinstance(packages, list) else []
            for package in packages:
                if not isinstance(package, dict):
                    continue
                delivery_types = package.get("deliveryTypes")
                delivery_types = (
                    delivery_types if isinstance(delivery_types, list) else []
                )
                for delivery_type in delivery_types:
                    if not isinstance(delivery_type, dict):
                        continue
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
    for fact in _iter_facts(_item_facts(item)):
        key = _ascii_lower(fact.get("keyName") or fact.get("slug"))
        if key in wanted:
            return _fact_value(fact)
    return ""

def _item_facts(item):
    """Factsheet for an item, falling back to bundled sub-products (TV + soundbar/
    remote combos leave item.factsheet empty and carry specs under bundles[].factsheet)."""
    if not isinstance(item, dict):
        return []
    facts = item.get("factsheet")
    if facts:
        return facts
    merged = []
    for bundle in item.get("bundles") or []:
        if isinstance(bundle, dict) and bundle.get("factsheet"):
            merged.extend(bundle["factsheet"])
    return merged

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
