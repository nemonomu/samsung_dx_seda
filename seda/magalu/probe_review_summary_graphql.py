import json
import os
import re
from pathlib import Path

import requests


GRAPHQL_URL = "https://federation.magazineluiza.com.br/graphql"


QUERIES = [
    (
        "control_itemQuery",
        """
        query itemQuery($itemId: ID!, $zipcode: String) {
          item(id: $itemId, zipcode: $zipcode) {
            id
            title
          }
        }
        """,
        {"itemId": "240144700", "zipcode": "01010010"},
    ),
    (
        "reviewSummaryQuery_field_id",
        """
        query reviewSummaryQuery($productId: String!) {
          reviewSummaryQuery(productId: $productId) {
            productId
            summary
            tags
          }
        }
        """,
        {"productId": "240144700"},
    ),
    (
        "reviewSummaryQuery_field_id_scalar_id",
        """
        query reviewSummaryQuery($productId: ID!) {
          reviewSummaryQuery(productId: $productId) {
            productId
            summary
            tags
          }
        }
        """,
        {"productId": "240144700"},
    ),
    (
        "reviewSummaryQuery_field_productId_only",
        """
        query reviewSummaryQuery($productId: ID!) {
          reviewSummaryQuery(productId: $productId) {
            summary
          }
        }
        """,
        {"productId": "240144700"},
    ),
    (
        "reviewSummaryQuery_field_id_id",
        """
        query reviewSummaryQuery($id: String!) {
          reviewSummaryQuery(id: $id) {
            productId
            summary
            tags
          }
        }
        """,
        {"id": "240144700"},
    ),
    (
        "reviewSummary_field_productId",
        """
        query reviewSummaryQuery($productId: String!) {
          reviewSummary(productId: $productId) {
            productId
            summary
            tags
          }
        }
        """,
        {"productId": "240144700"},
    ),
    (
        "productReviewSummary_field_productId",
        """
        query reviewSummaryQuery($productId: String!) {
          productReviewSummary(productId: $productId) {
            productId
            summary
            tags
          }
        }
        """,
        {"productId": "240144700"},
    ),
    (
        "reviewSummaryQuery_field_object",
        """
        query reviewSummaryQuery($reviewSummaryRequest: ReviewSummaryRequest!) {
          reviewSummaryQuery(reviewSummaryRequest: $reviewSummaryRequest) {
            productId
            summary
            tags
          }
        }
        """,
        {"reviewSummaryRequest": {"productId": "240144700"}},
    ),
]


def main():
    cookie = _cookie_from_curl_text()
    for label, query, variables in QUERIES:
        payload = {"operationName": "reviewSummaryQuery", "variables": variables, "query": query}
        if label == "control_itemQuery":
            payload["operationName"] = "itemQuery"
        try:
            response = requests.post(
                f"{GRAPHQL_URL}?operationName={payload['operationName']}",
                json=payload,
                headers=_headers(cookie),
                timeout=30,
            )
        except Exception as exc:
            print(f"\n== {label} ==\nstatus=0 error={type(exc).__name__}: {exc}")
            continue
        print(f"\n== {label} ==\nstatus={response.status_code} len={len(response.text or '')}")
        try:
            data = response.json()
        except ValueError:
            print(_ascii((response.text or "")[:800]))
            continue
        print(_ascii(json.dumps(data, ensure_ascii=False)[:2000]))


def _headers(cookie=""):
    headers = {
        "accept": "application/json",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://www.magazineluiza.com.br",
        "referer": "https://www.magazineluiza.com.br/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "x-channel-id": os.getenv("SEDA_MAGALU_SALES_CHANNEL_ID", "45"),
        "x-channel-name": os.getenv("SEDA_MAGALU_CHANNEL_NAME", "mixer-desk.magazineluiza.com.br"),
    }
    if cookie:
        headers["cookie"] = cookie
    return headers


def _cookie_from_curl_text():
    candidates = [
        Path("references/magalu_ai_summary.txt"),
        Path("references/magalu_ai_reviews_all.txt"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r'-b \^"([^"]*)\^"', text, re.S)
        if match:
            return _decode_windows_curl(match.group(1))
    return ""


def _decode_windows_curl(value):
    return (
        value.replace("^&", "&")
        .replace("^%", "%")
        .replace("^#", "#")
        .replace('^"', '"')
        .replace("^^", "^")
    )


def _ascii(value):
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


if __name__ == "__main__":
    main()
