import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

from seda.magalu import search_api
from seda.parsers import parse_listing
from seda.step00_config import DEFAULT_RUNS_BASE, RETAILERS, page_url, run_date


GRAPHQL_BASE = search_api.GRAPHQL_URL


def main():
    args = parse_args()
    os.environ["SEDA_PRODUCT_LINE"] = args.product_line
    if args.postal_code:
        os.environ["SEDA_POSTAL_CODE"] = args.postal_code

    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_RUNS_BASE / "magalu" / run_date() / "direct_graphql_matrix"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"direct_graphql_matrix_{args.product_line}_{stamp}.csv"
    json_path = out_dir / f"direct_graphql_matrix_{args.product_line}_{stamp}.json"

    rows = []
    for run_id in ("main", "bsr"):
        for page in parse_pages(args.pages):
            url = page_url(RETAILERS["magalu"], page, run_id=run_id)
            payload = search_api._payload(url, args.page_size)
            for profile in profiles(args):
                print(f"[direct_matrix] {run_id} page={page} profile={profile['name']} start", flush=True)
                row = run_profile(profile, url, payload, run_id, page, args.timeout)
                rows.append(row)
                print(
                    f"[direct_matrix] {run_id} page={page} profile={profile['name']} "
                    f"status={row['status_code']} products={row['products']} parsed={row['parsed_rows']} "
                    f"error={row['error'] or '-'} seconds={row['seconds']}",
                    flush=True,
                )
                write_outputs(csv_path, json_path, rows)

    print(f"[direct_matrix] wrote {json_path}")
    print(f"[direct_matrix] wrote {csv_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Magalu direct GraphQL transport matrix. No browser fallback.")
    parser.add_argument("product_line", nargs="?", default=os.getenv("SEDA_PRODUCT_LINE", "TV").upper(), choices=["TV", "REF", "LDY"])
    parser.add_argument("--pages", default=os.getenv("SEDA_MAGALU_DIRECT_MATRIX_PAGES", "1,2,3"))
    parser.add_argument("--page-size", type=int, default=int(os.getenv("SEDA_MAGALU_SEARCH_PAGE_SIZE", "60")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_TIMEOUT", "60")))
    parser.add_argument("--postal-code", default=os.getenv("SEDA_POSTAL_CODE", "01001-001"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-curl-cffi", action="store_true")
    return parser.parse_args()


def profiles(args):
    base = [
        {"name": "requests_current", "client": "requests", "headers": "current", "endpoint": "plain", "warmup": False},
        {"name": "requests_current_op", "client": "requests", "headers": "current", "endpoint": "operation", "warmup": False},
        {"name": "requests_browser_headers", "client": "requests", "headers": "browser", "endpoint": "plain", "warmup": False},
        {"name": "requests_browser_headers_op", "client": "requests", "headers": "browser", "endpoint": "operation", "warmup": False},
        {"name": "requests_warmup_browser_headers_op", "client": "requests", "headers": "browser", "endpoint": "operation", "warmup": True},
    ]
    if not args.no_curl_cffi:
        base.extend(
            [
                {"name": "curl_cffi_browser_headers_op", "client": "curl_cffi", "headers": "browser", "endpoint": "operation", "warmup": False},
                {"name": "curl_cffi_warmup_browser_headers_op", "client": "curl_cffi", "headers": "browser", "endpoint": "operation", "warmup": True},
            ]
        )
    return base


def run_profile(profile, listing_url, payload, run_id, page, timeout):
    started = time.perf_counter()
    response = None
    error = ""
    text = ""
    status_code = 0
    headers = request_headers(listing_url, profile["headers"])
    endpoint = endpoint_url(profile["endpoint"], payload.get("operationName", ""))
    try:
        if profile["client"] == "curl_cffi":
            response = curl_cffi_post(profile, listing_url, endpoint, payload, headers, timeout)
        else:
            response = requests_post(profile, listing_url, endpoint, payload, headers, timeout)
        status_code = int(response.status_code)
        text = response.text or ""
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    summary = summarize_response(text, listing_url, run_id, page)
    if status_code != 200:
        error = error or f"http_{status_code}"
    elif summary["json_error"]:
        error = error or summary["json_error"]
    elif summary["pagination_page"] and summary["pagination_page"] != page:
        error = error or f"page_mismatch:{summary['pagination_page']}!={page}"
    elif not summary["products"]:
        error = error or "empty_products"

    return {
        "run_id": run_id,
        "page": page,
        "profile": profile["name"],
        "client": profile["client"],
        "headers_profile": profile["headers"],
        "endpoint_profile": profile["endpoint"],
        "warmup": int(profile["warmup"]),
        "url": listing_url,
        "status_code": status_code,
        "seconds": round(time.perf_counter() - started, 3),
        "bytes": len(text),
        "products": summary["products"],
        "parsed_rows": summary["parsed_rows"],
        "pagination_page": summary["pagination_page"],
        "pagination_size": summary["pagination_size"],
        "error": error,
        "preview": text[:180].replace("\r", " ").replace("\n", " "),
    }


def requests_post(profile, listing_url, endpoint, payload, headers, timeout):
    session = requests.Session()
    if profile["warmup"]:
        session.get(listing_url, headers=warmup_headers(listing_url), timeout=timeout)
    return session.post(endpoint, json=payload, headers=headers, timeout=timeout)


def curl_cffi_post(profile, listing_url, endpoint, payload, headers, timeout):
    from curl_cffi import requests as curl_requests

    session = curl_requests.Session(impersonate=os.getenv("SEDA_MAGALU_CURL_IMPERSONATE", "chrome136"))
    if profile["warmup"]:
        session.get(listing_url, headers=warmup_headers(listing_url), timeout=timeout)
    return session.post(endpoint, json=payload, headers=headers, timeout=timeout)


def endpoint_url(profile, operation):
    if profile == "operation" and operation:
        return f"{GRAPHQL_BASE}?operationName={operation}"
    return GRAPHQL_BASE


def request_headers(url, profile):
    if profile == "current":
        return search_api._headers(url)
    headers = {
        "accept": "*/*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://www.magazineluiza.com.br",
        "referer": "https://www.magazineluiza.com.br/",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": os.getenv(
            "SEDA_MAGALU_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        ),
        "x-channel-id": os.getenv("SEDA_MAGALU_SALES_CHANNEL_ID", "45"),
        "x-channel-name": os.getenv("SEDA_MAGALU_CHANNEL_NAME", "mixer-desk.magazineluiza.com.br"),
    }
    return headers


def warmup_headers(url):
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "referer": "https://www.magazineluiza.com.br/",
        "upgrade-insecure-requests": "1",
        "user-agent": request_headers(url, "browser")["user-agent"],
    }


def summarize_response(text, url, run_id, page):
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {"json_error": "invalid_json", "products": 0, "parsed_rows": 0, "pagination_page": 0, "pagination_size": 0}
    search = ((data.get("data") or {}).get("search") or {}) if isinstance(data, dict) else {}
    products = search.get("products") if isinstance(search, dict) else []
    pagination = search.get("pagination") if isinstance(search, dict) else {}
    html_text = search_api._as_next_data_html(search, url) if isinstance(search, dict) and search else ""
    parsed_rows = len(parse_listing(html_text, "Magalu", RETAILERS["magalu"].base_url, url, run_id=run_id)) if html_text else 0
    return {
        "json_error": "missing_search" if not search else "",
        "products": len(products) if isinstance(products, list) else 0,
        "parsed_rows": parsed_rows,
        "pagination_page": as_int((pagination or {}).get("page"), 0),
        "pagination_size": as_int((pagination or {}).get("size"), 0),
    }


def parse_pages(raw):
    pages = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if item:
            pages.append(int(item))
    return pages or [1, 2, 3]


def as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def write_outputs(csv_path, json_path, rows):
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
