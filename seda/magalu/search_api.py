import json
import os
import time
import html
from urllib.parse import parse_qs, unquote, urlparse

import requests


GRAPHQL_URL = "https://federation.magazineluiza.com.br/graphql"

SEARCH_QUERY = """
query searchQuery(
  $term: String = ""
  $filters: [FilterInput]
  $sortType: String
  $sortOrientation: String
  $page: Int
  $pageSize: Int = 20
  $zipCode: String
  $showUnavailable: Boolean = true
  $channelCode: String
) {
  search(
    searchRequest: {
      query: $term
      pagination: { page: $page, size: $pageSize }
      filters: $filters
      sort: { type: $sortType, orientation: $sortOrientation }
      location: { zipCode: $zipCode }
      metadata: { showUnavailable: $showUnavailable, channelCode: $channelCode }
    }
  ) {
    products {
      id
      adsSellerId
      variationId
      title
      description
      image
      available
      position
      isBuyBox
      url
      path
      reference
      offerTags
      minimumOrderQuantity
      parentMatchingUuid
      price {
        paymentMethodDescription
        price
        fullPrice
        bestPrice
        discount
        currency
        exchangeRate
        idExchangeRate
        originalPriceForeign
      }
      installment {
        paymentMethodDescription
        quantity
        amount
        totalAmount
        paymentMethodId
        interest
      }
      rating {
        count
        score
      }
      ads {
        sponsored
        id
        label
        adRequestId
        adResponseId
        adsMatchReason
        adsRequestedCount
        adsReturnedCount
        brand
        campaignId
        category
        gender
        offerId
        navigationId
        sku
        subCategory
        trackId
      }
      seller {
        id
        sku
        description
        category
        deliveryId
        deliveryDescription
        isChatEnabled
        tags {
          type
          discountValue
          message
        }
      }
      brand {
        label
        slug
      }
      category {
        id
        name
      }
      subcategory {
        id
        name
      }
      badges {
        text
        imageUrl
        container
        position
        tooltip
      }
      shippingTag {
        error
        time
        cost
        uuid
        complement
        source
      }
      type
      hasVariations
    }
    pagination {
      page
      pages
      records
      start
      size
    }
    sorts {
      label
      selected
      type
      orientation
    }
    term {
      raw
      refined
    }
    trackId
  }
}
"""


def fetch_search_listing(url, timeout=None):
    parsed = urlparse(url)
    if "magazineluiza.com.br" not in parsed.netloc or "/busca/" not in parsed.path:
        return {"success": False, "error": "not_magalu_search_url", "text": "", "trace": []}

    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    retries = int(os.getenv("SEDA_MAGALU_SEARCH_RETRIES", "2"))
    sleep_seconds = float(os.getenv("SEDA_MAGALU_SEARCH_RETRY_SLEEP_SECONDS", "3.0"))
    page_sizes = _page_sizes()
    trace = []
    direct_min_products = int(os.getenv("SEDA_MAGALU_SEARCH_DIRECT_MIN_PRODUCTS", "40"))
    best_direct_result = None

    session = requests.Session()
    for page_size in page_sizes:
        payload = _payload(url, page_size)
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(sleep_seconds * attempt)
            try:
                response = session.post(GRAPHQL_URL, json=payload, headers=_headers(url), timeout=timeout)
            except Exception as exc:
                trace.append(
                    {
                        "method": "direct_graphql",
                        "page_size": page_size,
                        "attempt": attempt + 1,
                        "status_code": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            trace_item = {
                "method": "direct_graphql",
                "page_size": page_size,
                "attempt": attempt + 1,
                "status_code": response.status_code,
                "length": len(response.text or ""),
            }
            trace.append(trace_item)
            if response.status_code != 200 or "application/json" not in response.headers.get("content-type", ""):
                trace_item["error"] = "non_json_or_blocked"
                continue
            try:
                parsed_response = response.json()
            except ValueError:
                trace_item["error"] = "invalid_json"
                continue
            search = (parsed_response.get("data") or {}).get("search") or {}
            products = search.get("products") or []
            if products:
                direct_result = {
                    "success": True,
                    "text": _as_next_data_html(search, url),
                    "products": len(products),
                    "page_size": page_size,
                    "trace": trace,
                    "method": "direct_graphql_search",
                }
                if len(products) >= direct_min_products:
                    return direct_result
                best_direct_result = direct_result
                trace_item["error"] = f"insufficient_direct_products:{len(products)}<{direct_min_products}"
                break
            trace_item["error"] = "empty_products"

    if os.getenv("SEDA_MAGALU_SEARCH_BROWSER_GRAPHQL", "1").lower() not in {"0", "false", "no", "n"}:
        result = _fetch_search_listing_browser(url, page_sizes, timeout, trace)
        if result.get("success"):
            return result
        if os.getenv("SEDA_MAGALU_SEARCH_BROWSER_STRICT", "0").lower() in {"1", "true", "yes", "y"}:
            return result

    if best_direct_result:
        best_direct_result["trace"] = trace
        best_direct_result["method"] = "direct_graphql_search_insufficient"
        return best_direct_result
    return {"success": False, "error": "magalu_search_graphql_failed", "text": "", "trace": trace}


def _fetch_search_listing_browser(url, page_sizes, timeout, trace):
    try:
        from .browser_session import ensure_magalu_session, graphql_post
    except Exception as exc:
        trace.append(
            {
                "method": "browser_graphql",
                "status_code": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return {"success": False, "error": "browser_graphql_unavailable", "text": "", "trace": trace}

    attempts = int(os.getenv("SEDA_MAGALU_SEARCH_BROWSER_ATTEMPTS", "2") or "2")
    session = ensure_magalu_session("search_browser_graphql")
    trace.extend(session.get("trace") or [])
    if not session.get("success"):
        return {"success": False, "error": "browser_session_unavailable", "text": "", "trace": trace}
    last_error = "browser_graphql_failed"
    for page_size in page_sizes:
        payload = _payload(url, page_size)
        for attempt in range(1, attempts + 1):
            result = graphql_post(payload, timeout=timeout)
            data = result.get("data") or {}
            search = (data.get("data") or {}).get("search") or {}
            products = search.get("products") or []
            trace_item = {
                "method": "browser_graphql",
                "page_size": page_size,
                "attempt": attempt,
                "status_code": result.get("status_code", 0),
                "length": len(result.get("text") or ""),
                "products": len(products),
                "error": result.get("error", ""),
            }
            trace.append(trace_item)
            if products:
                return {
                    "success": True,
                    "text": _as_next_data_html(search, url),
                    "products": len(products),
                    "page_size": page_size,
                    "trace": trace,
                    "method": "browser_graphql_search",
                }
            last_error = trace_item["error"] or "empty_products"
            if attempt < attempts:
                session = ensure_magalu_session("search_browser_graphql_retry")
                trace.extend(session.get("trace") or [])
                if not session.get("success"):
                    last_error = "browser_session_unavailable"
                    break
    return {"success": False, "error": last_error, "text": "", "trace": trace}


def _payload(url, page_size):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return {
        "operationName": "searchQuery",
        "variables": {
            "term": _term_from_path(parsed.path),
            "filters": [],
            "sortType": _first(query, "sortType"),
            "sortOrientation": _first(query, "sortOrientation"),
            "page": int(_first(query, "page") or "1"),
            "pageSize": page_size,
            "zipCode": os.getenv("SEDA_POSTAL_CODE", os.getenv("SEDA_MAGALU_ZIP_CODE", "01001-001")),
            "showUnavailable": True,
            "channelCode": "WEB",
        },
        "query": SEARCH_QUERY,
    }


def _page_sizes():
    first = int(os.getenv("SEDA_MAGALU_SEARCH_PAGE_SIZE", "60"))
    fallback = os.getenv("SEDA_MAGALU_SEARCH_FALLBACK_PAGE_SIZES", "20")
    values = [first]
    values.extend(int(item.strip()) for item in fallback.split(",") if item.strip())
    return list(dict.fromkeys(max(1, min(value, 60)) for value in values))


def _term_from_path(path):
    parts = [part for part in path.split("/") if part]
    try:
        index = parts.index("busca")
    except ValueError:
        return ""
    return unquote(parts[index + 1]) if len(parts) > index + 1 else ""


def _first(query, key):
    values = query.get(key) or []
    return values[0] if values else None


def _headers(url):
    return {
        "accept": "application/json",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://www.magazineluiza.com.br",
        "referer": url,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "x-channel-id": "45",
        "x-channel-name": "mixer-desk.magazineluiza.com.br",
    }


def _as_next_data_html(search, url):
    data = {
        "props": {
            "pageProps": {
                "data": {
                    "search": search,
                }
            }
        },
        "page": "/busca/[path1]",
        "query": {"source_url": url},
    }
    raw = json.dumps(data, ensure_ascii=False)
    return '<script id="__NEXT_DATA__" type="application/json">' + html.escape(raw) + "</script>"
