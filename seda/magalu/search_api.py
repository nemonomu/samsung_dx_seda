import json
import os
import re
import time
import html
from urllib.parse import parse_qs, unquote_plus, urlparse

import requests

from .graphql_contract import graphql_envelope_error


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
      attributes {
        type
        label
        value
        current
      }
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


MAGALU_PRODUCT_PATH_RE = re.compile(r"/p/[^/?#]+(?:/|$)", re.I)
MAGALU_LISTING_PAGE_SIZE = 60


def fetch_search_listing_via_zenrows(url, timeout=None, profile=None):
    """Fetch one strict Magalu search page through the centralized ZenRows client."""
    parsed = urlparse(url)
    if "magazineluiza.com.br" not in parsed.netloc or "/busca/" not in parsed.path:
        return {
            "success": False,
            "error": "not_magalu_search_url",
            "text": "",
            "trace": [],
        }

    from .zenrows_client import request_json

    timeout = int(timeout or os.getenv("SEDA_ZENROWS_TIMEOUT", "45"))
    # This fallback can be mixed with browser/HTML pages in one run. Keep the
    # same canonical page boundary so a stale external page-size override
    # cannot create gaps or duplicates when the transport changes by page.
    page_size = MAGALU_LISTING_PAGE_SIZE
    profile = str(
        profile
        or os.getenv("SEDA_ZENROWS_LISTING_GRAPHQL_PROFILE")
        or "premium_html"
    ).strip() or "premium_html"
    payload = _payload(url, page_size)
    result = request_json(
        f"{GRAPHQL_URL}?operationName=searchQuery",
        payload,
        profile=profile,
        timeout=timeout,
        extra={
            "custom_headers": "true",
            "original_status": "true",
            "proxy_country": "br",
        },
        extra_headers=_headers(url),
    )
    metadata = {
        "profile": result.profile,
        "estimated_multiplier": result.estimated_multiplier,
        "request_cost": (result.headers or {}).get("X-Request-Cost", ""),
        "status_code": result.status_code,
    }
    trace_item = {
        "method": "zenrows_graphql_search",
        "operation": "searchQuery",
        "profile": result.profile,
        "estimated_multiplier": result.estimated_multiplier,
        "request_cost": metadata["request_cost"],
        "status_code": result.status_code,
        "length": len(result.text or ""),
        "products": 0,
        "error": result.error,
    }
    if result.error or not result.success:
        return {
            "success": False,
            "error": result.error or "empty_response",
            "text": "",
            "trace": [trace_item],
            "zenrows": metadata,
        }

    try:
        parsed_response = json.loads(result.text or "")
    except ValueError:
        trace_item["error"] = "invalid_json"
        return {
            "success": False,
            "error": "invalid_json",
            "text": "",
            "trace": [trace_item],
            "zenrows": metadata,
        }

    semantic_error = graphql_envelope_error(parsed_response)
    if semantic_error:
        trace_item["error"] = semantic_error
        return {
            "success": False,
            "error": semantic_error,
            "text": "",
            "trace": [trace_item],
            "zenrows": metadata,
        }

    search = parsed_response["data"].get("search")
    payload_error = _strict_search_payload_error(url, search, page_size)
    if payload_error:
        trace_item["error"] = payload_error
        return {
            "success": False,
            "error": payload_error,
            "text": "",
            "trace": [trace_item],
            "zenrows": metadata,
        }

    products = search["products"]
    trace_item["products"] = len(products)
    return {
        "success": True,
        "error": "",
        "text": _as_next_data_html(search, url),
        "products": len(products),
        "page_size": page_size,
        "trace": [trace_item],
        "method": "zenrows_graphql_search",
        "zenrows": metadata,
    }


def fetch_search_listing(url, timeout=None):
    parsed = urlparse(url)
    if "magazineluiza.com.br" not in parsed.netloc or "/busca/" not in parsed.path:
        return {"success": False, "error": "not_magalu_search_url", "text": "", "trace": []}

    requested_timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    timeout_cap = _env_int("SEDA_MAGALU_SEARCH_TIMEOUT_CAP", 30)
    timeout = min(requested_timeout, timeout_cap) if timeout_cap > 0 else requested_timeout
    retries = int(os.getenv("SEDA_MAGALU_SEARCH_RETRIES", "2"))
    sleep_seconds = float(os.getenv("SEDA_MAGALU_SEARCH_RETRY_SLEEP_SECONDS", "3.0"))
    # Keep every transport on one canonical page boundary. Mixing 20/60-item
    # responses across direct, browser and ZenRows creates rank gaps or
    # duplicates when a later transport recovers only selected pages.
    page_sizes = [MAGALU_LISTING_PAGE_SIZE]
    trace = []
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
            semantic_error = graphql_envelope_error(parsed_response)
            if semantic_error:
                trace_item["error"] = semantic_error
                if isinstance(parsed_response, dict) and parsed_response.get("errors"):
                    trace_item["errors"] = parsed_response.get("errors")
                continue
            search = parsed_response["data"].get("search") or {}
            if not isinstance(search, dict):
                trace_item["error"] = "invalid_search_payload"
                continue
            products = search.get("products")
            if products is None:
                products = []
            elif not isinstance(products, list):
                trace_item["error"] = "invalid_search_products"
                continue
            if products:
                direct_result = {
                    "success": True,
                    "text": _as_next_data_html(search, url),
                    "products": len(products),
                    "page_size": page_size,
                    "trace": trace,
                    "method": "direct_graphql_search",
                }
                payload_error = _strict_search_payload_error(
                    url,
                    search,
                    page_size,
                )
                if not payload_error:
                    return direct_result
                best_direct_result = direct_result
                trace_item["error"] = payload_error
                break
            trace_item["error"] = "empty_products"

    if os.getenv("SEDA_MAGALU_SEARCH_BROWSER_GRAPHQL", "1").lower() not in {"0", "false", "no", "n"}:
        result = _fetch_search_listing_browser(url, page_sizes, timeout, trace)
        if result.get("success"):
            return result
        if os.getenv("SEDA_MAGALU_SEARCH_BROWSER_STRICT", "0").lower() in {"1", "true", "yes", "y"}:
            return result

    if best_direct_result:
        return {
            "success": False,
            "error": "invalid_direct_payload",
            "text": "",
            "trace": trace,
            "products": best_direct_result.get("products", 0),
            "method": "direct_graphql_search_invalid",
        }
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

    attempts = max(1, _env_int("SEDA_MAGALU_SEARCH_BROWSER_ATTEMPTS", 2))
    browser_timeout = max(1, _env_int("SEDA_MAGALU_SEARCH_BROWSER_GRAPHQL_TIMEOUT", 20))
    session = ensure_magalu_session("search_browser_graphql")
    trace.extend(session.get("trace") or [])
    if not session.get("success"):
        return {"success": False, "error": "browser_session_unavailable", "text": "", "trace": trace}
    last_error = "browser_graphql_failed"
    for page_size in page_sizes:
        payload = _payload(url, page_size)
        for attempt in range(1, attempts + 1):
            result = graphql_post(payload, timeout=min(timeout, browser_timeout))
            result = result if isinstance(result, dict) else {}
            data = result.get("data") or {}
            response_data = data.get("data") if isinstance(data, dict) else {}
            search = response_data.get("search") if isinstance(response_data, dict) else {}
            search = search if isinstance(search, dict) else {}
            raw_products = search.get("products")
            products = raw_products if isinstance(raw_products, list) else []
            raw_attempts = result.get("trace")
            browser_attempts = (
                [item for item in raw_attempts if isinstance(item, dict)]
                if isinstance(raw_attempts, list)
                else []
            )
            if not browser_attempts:
                browser_attempts = [result]
            appended = []
            for inner_index, browser_attempt in enumerate(browser_attempts):
                trace_item = {
                    "method": browser_attempt.get("method", "browser_graphql"),
                    "page_size": page_size,
                    "browser_call_attempt": attempt,
                    "attempt": browser_attempt.get(
                        "attempt",
                        result.get("attempt", 1),
                    ),
                    "status_code": browser_attempt.get(
                        "status_code",
                        result.get("status_code", 0),
                    ),
                    "length": browser_attempt.get(
                        "length",
                        len(result.get("text") or ""),
                    ),
                    "products": (
                        len(products)
                        if inner_index == len(browser_attempts) - 1
                        else 0
                    ),
                    "error": browser_attempt.get("error", ""),
                }
                graphql_errors = (
                    browser_attempt.get("graphql_errors")
                    or browser_attempt.get("errors")
                )
                if graphql_errors:
                    trace_item["errors"] = graphql_errors
                if browser_attempt.get("response_preview"):
                    trace_item["response_preview"] = browser_attempt[
                        "response_preview"
                    ]
                trace.append(trace_item)
                appended.append(trace_item)
            operation_error = result.get("error", "")
            if raw_products is not None and not isinstance(raw_products, list):
                operation_error = operation_error or "invalid_search_products"
            if appended and operation_error and not appended[-1].get("error"):
                appended[-1]["error"] = operation_error
            if products:
                payload_error = _strict_search_payload_error(
                    url,
                    search,
                    page_size,
                )
                if not payload_error:
                    return {
                        "success": True,
                        "text": _as_next_data_html(search, url),
                        "products": len(products),
                        "page_size": page_size,
                        "trace": trace,
                        "method": "browser_graphql_search",
                    }
                operation_error = operation_error or payload_error
            last_error = (
                operation_error
                or (appended[-1].get("error") if appended else "")
                or "empty_products"
            )
            if appended and not appended[-1].get("error"):
                appended[-1]["error"] = last_error
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


def _valid_search_payload(url, search):
    pagination = search.get("pagination") if isinstance(search, dict) else {}
    if not isinstance(pagination, dict):
        return False
    requested_page = int(_first(parse_qs(urlparse(url).query), "page") or "1")
    payload_page = _env_int_from_value(pagination.get("page"), 0)
    return payload_page == requested_page


def _strict_search_payload_error(url, search, requested_page_size):
    if not isinstance(search, dict):
        return "invalid_search_payload"
    products = search.get("products")
    if not isinstance(products, list):
        return "invalid_search_products"
    if not products:
        return "empty_products"

    pagination = search.get("pagination")
    if not isinstance(pagination, dict):
        return "missing_pagination"
    requested_page = _env_int_from_value(
        _first(parse_qs(urlparse(url).query), "page"),
        1,
    )
    payload_page = _env_int_from_value(pagination.get("page"), 0)
    if payload_page != requested_page:
        return f"page_mismatch:{payload_page}!={requested_page}"
    payload_size = _env_int_from_value(pagination.get("size"), 0)
    if payload_size != requested_page_size:
        return f"page_size_mismatch:{payload_size}!={requested_page_size}"

    expected_type, expected_orientation = _expected_search_sort(url)
    selected_sort = _selected_search_sort(search)
    if not selected_sort:
        return "selected_sort_missing"
    selected_type = selected_sort.get("type", "")
    selected_orientation = selected_sort.get("orientation", "")
    if (
        selected_type != expected_type
        or selected_orientation != expected_orientation
    ):
        return (
            "sort_mismatch:"
            f"{selected_type or 'missing'}:{selected_orientation or 'missing'}"
            f"!={expected_type}:{expected_orientation}"
        )

    term = search.get("term")
    if not isinstance(term, dict):
        return "search_term_missing"
    expected_term = _normalize_search_term(_term_from_path(urlparse(url).path))
    payload_term = _normalize_search_term(term.get("raw"))
    if payload_term != expected_term:
        return f"search_term_mismatch:{payload_term or 'missing'}!={expected_term}"

    max_products = _env_int("SEDA_MAGALU_SEARCH_BROWSER_MAX_PRODUCTS", 120)
    if max_products > 0 and len(products) > max_products:
        return f"too_many_products:{len(products)}"
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            return f"invalid_product:{index}:not_object"
        if not str(product.get("title") or "").strip():
            return f"invalid_product:{index}:missing_title"
        path = str(product.get("path") or "").strip()
        if not path:
            return f"invalid_product:{index}:missing_path"
        if not MAGALU_PRODUCT_PATH_RE.search(urlparse(path).path):
            return f"invalid_product:{index}:invalid_path"
    return ""


def _expected_search_sort(url):
    query = parse_qs(urlparse(url).query)
    return (
        str(_first(query, "sortType") or "score").strip(),
        str(_first(query, "sortOrientation") or "desc").strip(),
    )


def _selected_search_sort(search):
    sorts = search.get("sorts") if isinstance(search, dict) else []
    if not isinstance(sorts, list):
        return {}
    for item in sorts:
        if isinstance(item, dict) and item.get("selected"):
            return {
                "type": str(item.get("type") or "").strip(),
                "orientation": str(item.get("orientation") or "").strip(),
            }
    return {}


def _normalize_search_term(value):
    return " ".join(str(value or "").split()).casefold()


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
    return unquote_plus(parts[index + 1]) if len(parts) > index + 1 else ""


def _first(query, key):
    values = query.get(key) or []
    return values[0] if values else None


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_int_from_value(value, default):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


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
