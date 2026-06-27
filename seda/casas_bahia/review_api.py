import os
import uuid

import requests

from ._net import request_with_retry


REVIEWS_URL = "https://pdp-api.casasbahia.com.br/api/v3/reviews/product/{product_id}/source/CB"


def fetch_reviews(product_id, limit=None, timeout=None):
    if not product_id:
        return {"success": False, "error": "missing_product_id"}
    limit = int(limit or os.getenv("SEDA_CASAS_BAHIA_REVIEW_LIMIT", "20"))
    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    collected = []
    general = {}
    page = 1
    page_size = min(20, max(1, limit))
    seen = 0
    session = requests.Session()
    session.trust_env = os.getenv("SEDA_CASAS_BAHIA_TRUST_ENV_PROXY", "0").lower() in {"1", "true", "yes", "y"}
    while len(collected) < limit:
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
            return {"success": False, "error": f"{type(exc).__name__}: {exc}", "reviews": collected, "general": general}
        if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
            return {"success": False, "error": f"status_{response.status_code}", "reviews": collected, "general": general}
        try:
            data = response.json()
        except ValueError:
            return {"success": False, "error": "invalid_json", "reviews": collected, "general": general}
        review = data.get("review") or {}
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
    }


def _review_text(item):
    if not isinstance(item, dict):
        return ""
    for key in ("text", "reviewText", "description", "comment", "content", "title"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


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
