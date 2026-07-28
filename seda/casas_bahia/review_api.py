import json
import os
import uuid
from urllib.parse import urlencode, urlsplit

import requests

from ._net import request_with_retry


REVIEWS_URL = "https://pdp-api.casasbahia.com.br/api/v3/reviews/product/{product_id}/source/CB"


def fetch_reviews(product_id, limit=None, timeout=None, referer_url=None):
    if not product_id:
        return {"success": False, "error": "missing_product_id", "zenrows_requested": False}
    limit = int(limit or os.getenv("SEDA_CASAS_BAHIA_REVIEW_LIMIT", "20"))
    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    collected = []
    general = {}
    page = 1
    page_size = min(20, max(1, limit))
    seen = 0
    zenrows_attempted = False
    zenrows_requested = False
    fetch_methods = []
    result_headers = {}
    session = requests.Session()
    session.trust_env = os.getenv("SEDA_CASAS_BAHIA_TRUST_ENV_PROXY", "0").lower() in {"1", "true", "yes", "y"}
    while len(collected) < limit:
        page_result = _fetch_review_page_direct(session, product_id, page, page_size, timeout)
        direct_error = ""
        page_method = page_result.get("method", "casas_bahia_reviews_api")
        if page_method not in fetch_methods:
            fetch_methods.append(page_method)
        if not page_result.get("success") and not zenrows_attempted:
            direct_error = page_result.get("error", "direct_unknown")
            zenrows_attempted = True
            fallback_result = _fetch_review_page_zenrows(
                product_id,
                page,
                page_size,
                timeout,
                referer_url=referer_url,
            )
            fallback_method = fallback_result.get("method", "casas_bahia_reviews_api_zenrows:10x")
            if fallback_method not in fetch_methods:
                fetch_methods.append(fallback_method)
            zenrows_requested = zenrows_requested or bool(fallback_result.get("zenrows_requested"))
            if not fallback_result.get("success"):
                fallback_result = dict(fallback_result)
                fallback_result["error"] = _combined_errors(
                    direct_error,
                    fallback_result.get("error", "zenrows_unknown"),
                )
            page_result = fallback_result
        page_headers = page_result.get("headers") or {}
        if page_headers:
            result_headers = page_headers
        if not page_result.get("success"):
            return {
                "success": False,
                "error": page_result.get("error", "unknown"),
                "reviews": collected,
                "general": general,
                "method": "+".join(fetch_methods) or "casas_bahia_reviews_api",
                "headers": result_headers,
                "zenrows_requested": zenrows_requested,
            }
        data = page_result.get("data") or {}
        page_method = page_result.get("method", "casas_bahia_reviews_api")
        if page_method not in fetch_methods:
            fetch_methods.append(page_method)
        review = data["review"]
        if not general:
            ai_summary = review.get("aiSummary") if isinstance(review.get("aiSummary"), dict) else {}
            general = {
                "rating": review.get("rating", ""),
                "ratingQty": review.get("ratingQty", ""),
                "recommendationPercentage": review.get("recommendationPercentage", ""),
                "summary": str(ai_summary.get("aiSummaryText") or "").strip(),
            }
        items = review.get("userReviews") or []
        seen += len(items)
        for item in items:
            text = _review_text(item)
            if not text:
                continue
            collected.append(text)
            if len(collected) >= limit:
                break
        if len(items) < page_size:
            break
        page += 1
    return {
        "success": True,
        "reviews": collected[:limit],
        "general": general,
        "summary": general.get("summary", ""),
        "review_items_seen": seen,
        "blank_review_items": sum(1 for text in collected[:limit] if not str(text or "").strip()),
        "method": "+".join(fetch_methods) or "casas_bahia_reviews_api",
        "headers": result_headers,
        "zenrows_requested": zenrows_requested,
    }


def _review_text(item):
    if not isinstance(item, dict):
        return ""
    for key in ("text", "reviewText", "description", "comment", "content", "title"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _fetch_review_page_direct(session, product_id, page, page_size, timeout):
    try:
        response = request_with_retry(
            lambda: session.get(
                REVIEWS_URL.format(product_id=product_id),
                params={"page": page, "size": page_size, "orderBy": "MOST_USEFUL"},
                headers=_headers(),
                timeout=timeout,
            ),
            throttle_host="cb_pdp",
        )
    except Exception as exc:
        return {
            "success": False,
            "error": f"direct_{type(exc).__name__}",
            "method": "casas_bahia_reviews_api",
        }
    if response.status_code != 200 or "json" not in response.headers.get("content-type", "").lower():
        return {
            "success": False,
            "error": f"direct_status_{response.status_code}",
            "method": "casas_bahia_reviews_api",
        }
    try:
        data = response.json()
    except ValueError:
        return {"success": False, "error": "direct_invalid_json", "method": "casas_bahia_reviews_api"}
    payload_error = _review_payload_error(data)
    if payload_error:
        return {
            "success": False,
            "error": f"direct_invalid_payload:{payload_error}",
            "method": "casas_bahia_reviews_api",
        }
    return {"success": True, "data": data, "method": "casas_bahia_reviews_api", "headers": {}}


def _fetch_review_page_zenrows(product_id, page, page_size, timeout, referer_url=None):
    if os.getenv("SEDA_CASAS_BAHIA_REVIEW_ZENROWS_FALLBACK", "1").lower() in {"0", "false", "no", "n"}:
        return {
            "success": False,
            "error": "zenrows_disabled",
            "method": "casas_bahia_reviews_api_zenrows:10x",
            "zenrows_requested": False,
        }
    try:
        from ..magalu.zenrows_client import request_url
    except Exception as exc:
        return {
            "success": False,
            "error": f"zenrows_import_{type(exc).__name__}",
            "method": "casas_bahia_reviews_api_zenrows:10x",
            "zenrows_requested": False,
        }

    query = urlencode({"page": page, "size": page_size, "orderBy": "MOST_USEFUL"})
    target_url = f"{REVIEWS_URL.format(product_id=product_id)}?{query}"
    extra = {
        "premium_proxy": "true",
        "proxy_country": "br",
        "custom_headers": "true",
        "original_status": "true",
    }
    try:
        result = request_url(
            target_url,
            profile="premium_html",
            timeout=timeout,
            extra=extra,
            extra_headers=_zenrows_headers(referer_url),
        )
    except Exception as exc:
        return {
            "success": False,
            "error": f"zenrows_request_{type(exc).__name__}",
            "method": "casas_bahia_reviews_api_zenrows:10x",
            "zenrows_requested": False,
        }

    method = f"casas_bahia_reviews_api_zenrows:{result.estimated_multiplier or '10x'}"
    requested = _zenrows_network_requested(result)
    if not result.success:
        return {
            "success": False,
            "error": f"zenrows_{result.error or result.status_code}",
            "method": method,
            "headers": result.headers,
            "zenrows_requested": requested,
        }
    try:
        data = json.loads(result.text or "")
    except ValueError:
        return {
            "success": False,
            "error": "zenrows_invalid_json",
            "method": method,
            "headers": result.headers,
            "zenrows_requested": requested,
        }
    payload_error = _review_payload_error(data)
    if payload_error:
        return {
            "success": False,
            "error": f"zenrows_invalid_payload:{payload_error}",
            "method": method,
            "headers": result.headers,
            "zenrows_requested": requested,
        }
    return {
        "success": True,
        "data": data,
        "method": method,
        "headers": result.headers,
        "zenrows_requested": requested,
    }


def _zenrows_network_requested(result):
    error = str(getattr(result, "error", "") or "").strip()
    if error in {"zenrows_disabled", "zenrows_dry_run", "key_missing"}:
        return False
    if error.startswith("unknown_profile:"):
        return False
    return True


def _review_payload_error(data):
    if not isinstance(data, dict):
        return "not_object"
    review = data.get("review")
    if not isinstance(review, dict):
        return "missing_review"
    items = review.get("userReviews")
    if items is not None and not isinstance(items, list):
        return "invalid_user_reviews"
    if any(not isinstance(item, dict) for item in (items or [])):
        return "invalid_review_item"
    for key, minimum, maximum in (
        ("rating", 0, 5),
        ("ratingQty", 0, None),
        ("recommendationPercentage", 0, 100),
    ):
        value = review.get(key, "")
        if value in ("", None):
            continue
        try:
            numeric = float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return f"invalid_{key}"
        if numeric < minimum or (maximum is not None and numeric > maximum):
            return f"invalid_{key}"
    ai_summary = review.get("aiSummary")
    if ai_summary not in (None, "") and not isinstance(ai_summary, dict):
        return "invalid_ai_summary"
    return ""


def _combined_errors(*errors):
    combined = []
    for error in errors:
        text = str(error or "").strip()
        if text and text not in combined:
            combined.append(text)
    return "|".join(combined) or "unknown"


def _zenrows_headers(referer_url=None):
    referer = "https://www.casasbahia.com.br/"
    candidate = str(referer_url or "").strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme in {"http", "https"} and parsed.hostname in {
        "casasbahia.com.br",
        "www.casasbahia.com.br",
    }:
        referer = candidate
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "origin": "https://www.casasbahia.com.br",
        "referer": referer,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "x-correlation-id": str(uuid.uuid4()),
    }


def _headers():
    headers = {
        "accept": "*/*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "access-control-allow-headers": "*",
        "origin": "https://www.casasbahia.com.br",
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
        "x-correlation-id": str(uuid.uuid4()),
        "x-safe": os.getenv("SEDA_CASAS_BAHIA_X_SAFE", "34be7a8b1f87"),
    }
    cookie = os.getenv("SEDA_CASAS_BAHIA_API_COOKIE", "").strip()
    if cookie:
        headers["cookie"] = cookie
    return headers
