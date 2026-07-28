import math
import os
import time

import requests

from ..parsers import clean_text
from .graphql_contract import graphql_envelope_error


GRAPHQL_URL = "https://federation.magazineluiza.com.br/graphql"

PRODUCT_RATING_QUERY = """
query ProductRating(
  $variationId: String
  $filters: [Filter]
  $includeUserReviews: Boolean = false
  $page: Int = 1
  $pageSize: Int = 8
  $sortType: UserReviewsSortType = MORE_RELEVANT
  $hasTag: Boolean = true
) {
  productRating(variationId: $variationId, hasTag: $hasTag) {
    productId
    userReviews(
      userReviewRequest: {
        filters: $filters
        pagination: { page: $page, size: $pageSize }
        sortType: $sortType
      }
      hasTag: $hasTag
    ) @include(if: $includeUserReviews) {
      items {
        partner
        product {
          images
          productLink
          productName
          ratingValue
          sku
          videos
        }
        reviewId
        description
        rating
        title
        submissionDate
        userData {
          name
        }
        attributes {
          label
          value
        }
        dimensions {
          id
          label
        }
      }
      page {
        current
        totalItems
        totalPages
      }
    }
    dimensions {
      id
      label
      rating
    }
    general {
      rating
      reviewCount
      commentCount
    }
    reviewsByRating {
      rating
      total
    }
  }
}
"""


def fetch_product_rating(variation_id, limit=None, timeout=None, page_size=None, context_url=None):
    variation_id = clean_text(variation_id)
    if not variation_id:
        return {"success": False, "error": "missing_variation_id", "reviews": [], "trace": []}

    limit = int(limit or os.getenv("SEDA_MAGALU_REVIEW_LIMIT", "20"))
    page_size = int(page_size or os.getenv("SEDA_MAGALU_REVIEW_PAGE_SIZE", "16"))
    timeout = int(timeout or os.getenv("SEDA_MAGALU_REVIEW_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    sleep_seconds = float(os.getenv("SEDA_MAGALU_REVIEW_SLEEP_SECONDS", "1.5"))
    initial_sleep_seconds = float(os.getenv("SEDA_MAGALU_REVIEW_INITIAL_SLEEP_SECONDS", "1.0"))
    retries = int(os.getenv("SEDA_MAGALU_REVIEW_RETRIES", "2"))
    retry_sleep_seconds = float(os.getenv("SEDA_MAGALU_REVIEW_RETRY_SLEEP_SECONDS", "5.0"))
    page_size = max(1, min(page_size, 16))
    max_pages = int(os.getenv("SEDA_MAGALU_REVIEW_MAX_PAGES", "20"))
    if max_pages <= 0:
        max_pages = max(1, math.ceil(limit / page_size))

    session = requests.Session()
    reviews = []
    seen_ids = set()
    seen_texts = set()
    trace = []
    merged = {
        "product_id": "",
        "general": {},
        "dimensions": [],
        "reviews_by_rating": [],
        "page": {},
    }

    total_pages = None
    target_limit = limit
    page = 1
    while page <= max_pages:
        if page == 1 and initial_sleep_seconds:
            time.sleep(initial_sleep_seconds)
        elif page > 1:
            time.sleep(sleep_seconds)
        product_rating = _request_product_rating(
            session,
            variation_id,
            page,
            page_size,
            timeout,
            retries,
            retry_sleep_seconds,
            trace,
            context_url=context_url,
        )
        if not product_rating:
            break

        _merge_rating_summary(merged, product_rating)
        general = product_rating.get("general")
        general = general if isinstance(general, dict) else {}
        comment_count = general.get("commentCount")
        if comment_count is not None:
            try:
                target_limit = min(limit, max(0, int(float(str(comment_count).replace(",", ".")))))
            except (TypeError, ValueError):
                target_limit = limit
            if target_limit <= 0:
                break
        user_reviews = product_rating.get("userReviews")
        user_reviews = user_reviews if isinstance(user_reviews, dict) else {}
        page_info = user_reviews.get("page")
        page_info = page_info if isinstance(page_info, dict) else {}
        try:
            total_pages = int(page_info.get("totalPages") or total_pages or 0) or total_pages
        except (TypeError, ValueError):
            pass
        review_items = user_reviews.get("items")
        review_items = review_items if isinstance(review_items, list) else []
        for item in review_items:
            if not isinstance(item, dict):
                continue
            description = clean_text(item.get("description"))
            if not description:
                continue
            review_id = item.get("reviewId") or description
            text_key = description.casefold()
            if not review_id or review_id in seen_ids or text_key in seen_texts:
                continue
            seen_ids.add(review_id)
            seen_texts.add(text_key)
            reviews.append(description)
            if len(reviews) >= target_limit:
                break
        if len(reviews) >= target_limit:
            break
        if total_pages is not None and page >= total_pages:
            break
        page += 1

    merged.update(
        {
            "success": bool(reviews),
            "reviews": reviews[:limit],
            "trace": trace,
            "method": "graphql_product_rating",
        }
    )
    return merged


def _append_browser_attempt_trace(trace, result, **context):
    result = result if isinstance(result, dict) else {}
    raw_attempts = result.get("trace")
    attempts = (
        [item for item in raw_attempts if isinstance(item, dict)]
        if isinstance(raw_attempts, list)
        else []
    )
    if not attempts:
        attempts = [result]
    appended = []
    for raw in attempts:
        item = dict(context)
        item.update(
            {
                "operation": raw.get("operation", result.get("operation", "")),
                "attempt": raw.get("attempt", result.get("attempt", 1)),
                "method": raw.get("method", "browser_graphql"),
                "status_code": raw.get(
                    "status_code",
                    result.get("status_code", 0),
                ),
                "content_type": raw.get(
                    "content_type",
                    result.get("content_type", ""),
                ),
                "length": raw.get(
                    "length",
                    len(result.get("text") or ""),
                ),
            }
        )
        error = raw.get("error")
        if error:
            item["error"] = error
        graphql_errors = raw.get("graphql_errors") or raw.get("errors")
        if graphql_errors:
            item["errors"] = graphql_errors
        if raw.get("response_preview"):
            item["response_preview"] = raw["response_preview"]
        trace.append(item)
        appended.append(item)
    return appended


def _request_product_rating(session, variation_id, page, page_size, timeout, retries, retry_sleep_seconds, trace, context_url=None):
    payload = _payload(variation_id, page, page_size)
    browser_enabled = os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL", "0").lower() not in {"0", "false", "no", "n"}
    for attempt in range(retries + 1):
        if browser_enabled:
            # federation blocks plain requests.post when browser mode is on; skip the
            # always-blocked requests attempt and use the browser channel below.
            break
        if attempt:
            time.sleep(retry_sleep_seconds * attempt)
        try:
            response = session.post(GRAPHQL_URL, json=payload, headers=_headers(), timeout=timeout)
        except Exception as exc:
            trace.append(
                {
                    "page": page,
                    "attempt": attempt + 1,
                    "method": "requests",
                    "status_code": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        trace_item = {
            "page": page,
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
            parsed = response.json()
        except ValueError:
            trace_item["error"] = "invalid_json"
            continue

        semantic_error = graphql_envelope_error(parsed)
        if semantic_error:
            trace_item["error"] = semantic_error
            if isinstance(parsed, dict) and parsed.get("errors"):
                trace_item["errors"] = parsed.get("errors")
            continue
        product_rating = parsed["data"].get("productRating") or {}
        if not isinstance(product_rating, dict):
            trace_item["error"] = "invalid_product_rating"
            continue
        if product_rating:
            return product_rating
        trace_item["error"] = "missing_product_rating"

    if os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL", "0").lower() not in {"0", "false", "no", "n"}:
        try:
            from .browser_session import graphql_post

            result = graphql_post(payload, timeout=timeout, context_url=context_url)
        except Exception as exc:
            trace.append(
                {
                    "page": page,
                    "attempt": 1,
                    "method": "browser_graphql",
                    "status_code": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            result = result if isinstance(result, dict) else {}
            attempt_items = _append_browser_attempt_trace(
                trace,
                result,
                page=page,
            )
            parsed = result.get("data") or {}
            response_data = parsed.get("data") if isinstance(parsed, dict) else {}
            raw_product_rating = (
                response_data.get("productRating")
                if isinstance(response_data, dict)
                else None
            )
            product_rating = (
                raw_product_rating
                if isinstance(raw_product_rating, dict)
                else {}
            )
            if result.get("status_code") == 200 and product_rating:
                return product_rating
            has_graphql_errors = isinstance(parsed, dict) and bool(parsed.get("errors"))
            operation_error = result.get("error") or (
                "graphql_errors"
                if has_graphql_errors
                else (
                    "invalid_product_rating"
                    if raw_product_rating not in (None, {}) and not isinstance(
                        raw_product_rating,
                        dict,
                    )
                    else "missing_product_rating"
                )
            )
            if attempt_items and not attempt_items[-1].get("error"):
                attempt_items[-1]["error"] = operation_error
    return {}


def _payload(variation_id, page, page_size):
    return {
        "operationName": "ProductRating",
        "variables": {
            "variationId": variation_id,
            "filters": None,
            "includeUserReviews": True,
            "page": page,
            "pageSize": page_size,
            "sortType": "MORE_RELEVANT",
            "hasTag": True,
        },
        "query": PRODUCT_RATING_QUERY,
    }


def _headers():
    return {
        "accept": "application/json",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://www.magazineluiza.com.br",
        "referer": "https://www.magazineluiza.com.br/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
    }


def _merge_rating_summary(target, product_rating):
    if not target["product_id"]:
        target["product_id"] = clean_text(product_rating.get("productId"))
    if not target["general"]:
        general = product_rating.get("general")
        target["general"] = general if isinstance(general, dict) else {}
    if not target["dimensions"]:
        dimensions = product_rating.get("dimensions")
        target["dimensions"] = dimensions if isinstance(dimensions, list) else []
    if not target["reviews_by_rating"]:
        reviews_by_rating = product_rating.get("reviewsByRating")
        target["reviews_by_rating"] = (
            reviews_by_rating if isinstance(reviews_by_rating, list) else []
        )
    if not target["page"]:
        user_reviews = product_rating.get("userReviews")
        user_reviews = user_reviews if isinstance(user_reviews, dict) else {}
        page = user_reviews.get("page")
        target["page"] = page if isinstance(page, dict) else {}


# ---------------------------------------------------------------------------
# Review summary (summarized_review_content) — GraphQL via browser channel.
#
# The AI review summary lives ONLY in the PDP (Akamai-403 to plain requests),
# but the underlying GraphQL field is reachable via the browser GraphQL channel
# (same transport as itemQuery / ProductRating) with no PDP HTML and no ZenRows.
#
# Verified against the live federation endpoint (introspection is disabled, so
# this was found by error-message probing): the field is `reviewSummary`, takes
# productId: String! (the /p/<id> item id), and returns { summary, tags }.
# NOTE: the __NEXT_DATA__ cache key is "reviewSummaryQuery", but that is the
# page's operation/cache name, NOT the schema field — the field is reviewSummary.
# reviewSummary returns null for products without a generated summary.
# ---------------------------------------------------------------------------
REVIEW_SUMMARY_QUERY = """
query reviewSummary($productId: String!) {
  reviewSummary(productId: $productId) {
    summary
    tags
  }
}
"""


def fetch_review_summary(product_id=None, variation_id=None, timeout=None, context_url=None):
    """Fetch the AI review summary for a product via the browser GraphQL channel.

    Returns {"success", "summary", "tags", "trace", "method"}.
    Prefers product_id (from productRating.productId); falls back to variation_id (sku).
    """
    identifier = clean_text(product_id) or clean_text(variation_id)
    if not identifier:
        return {"success": False, "error": "missing_product_id", "summary": "", "tags": [], "trace": []}

    timeout = int(timeout or os.getenv("SEDA_MAGALU_REVIEW_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    trace = []
    payload = _summary_payload(identifier)
    data = _post_summary_browser(payload, timeout, trace, context_url=context_url)
    response_data = data.get("data") if isinstance(data, dict) else {}
    node = response_data.get("reviewSummary") if isinstance(response_data, dict) else {}
    node = node if isinstance(node, dict) else {}
    summary = clean_text(node.get("summary"))
    return {
        "success": bool(summary),
        "summary": summary,
        "tags": node.get("tags") or [],
        "trace": trace,
        "method": "graphql_review_summary",
    }


def _summary_payload(identifier):
    return {
        "operationName": "reviewSummary",
        "variables": {"productId": identifier},
        "query": REVIEW_SUMMARY_QUERY,
    }


def _post_summary_browser(payload, timeout, trace, context_url=None):
    # Akamai blocks plain requests on this endpoint, so go straight through the
    # browser GraphQL channel (same transport as itemQuery / ProductRating).
    try:
        from .browser_session import graphql_post

        result = graphql_post(payload, timeout=timeout, context_url=context_url)
    except Exception as exc:
        trace.append({"method": "browser_graphql", "status_code": 0, "error": f"{type(exc).__name__}: {exc}"})
        return {}

    result = result if isinstance(result, dict) else {}
    attempt_items = _append_browser_attempt_trace(trace, result)
    data = result.get("data") or {}
    semantic_error = graphql_envelope_error(data)
    if result.get("status_code") == 200 and data and not semantic_error and not result.get("error"):
        return data
    operation_error = result.get("error") or semantic_error or "non_json_or_blocked"
    if attempt_items and not attempt_items[-1].get("error"):
        attempt_items[-1]["error"] = operation_error
    if (
        attempt_items
        and isinstance(data, dict)
        and data.get("errors")
        and not attempt_items[-1].get("errors")
    ):
        attempt_items[-1]["errors"] = data.get("errors")
    return {}
