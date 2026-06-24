import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

from seda.magalu import search_api
from seda.magalu.zenrows_client import ZENROWS_API_URL, estimated_multiplier
from seda.parsers import parse_listing
from seda.step00_config import DEFAULT_RUNS_BASE, RETAILERS, page_url, run_date


CSV_COLUMNS = [
    "run_id",
    "page",
    "profile",
    "url",
    "target_url",
    "zenrows_status",
    "target_status",
    "seconds",
    "bytes",
    "products",
    "parsed_rows",
    "pagination_page",
    "pagination_size",
    "success",
    "error",
    "estimated_multiplier",
    "request_cost",
    "zenrows_request_id",
    "params_public",
    "preview",
]


PROFILE_PARAMS = {
    "basic_post": {
        "custom_headers": "true",
        "original_status": "true",
    },
    "auto_post": {
        "mode": "auto",
        "proxy_country": "br",
        "custom_headers": "true",
        "original_status": "true",
    },
    "premium_br_post": {
        "premium_proxy": "true",
        "proxy_country": "br",
        "custom_headers": "true",
        "original_status": "true",
    },
    "premium_br_session_post": {
        "premium_proxy": "true",
        "proxy_country": "br",
        "custom_headers": "true",
        "original_status": "true",
        "session_id": "24061",
    },
}


def main():
    args = parse_args()
    os.environ["SEDA_PRODUCT_LINE"] = args.product_line
    if args.postal_code:
        os.environ["SEDA_POSTAL_CODE"] = args.postal_code

    if args.execute:
        ensure_zenrows_allowed()

    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_RUNS_BASE / "magalu" / run_date() / "zenrows_graphql_listing"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"zenrows_graphql_listing_{args.product_line}_{stamp}.csv"
    json_path = out_dir / f"zenrows_graphql_listing_{args.product_line}_{stamp}.json"

    rows = []
    for run_id in parse_run_ids(args.run_ids):
        for page in parse_pages(args.pages):
            url = page_url(RETAILERS["magalu"], page, run_id=run_id)
            payload = search_api._payload(url, args.page_size)
            for profile_name in parse_profiles(args.profiles):
                print(f"[zenrows_graphql] {run_id} page={page} profile={profile_name} start", flush=True)
                row = run_profile(args, profile_name, run_id, page, url, payload)
                rows.append(row)
                write_outputs(csv_path, json_path, rows)
                print(
                    f"[zenrows_graphql] {run_id} page={page} profile={profile_name} "
                    f"zenrows={row['zenrows_status']} target={row['target_status']} "
                    f"products={row['products']} parsed={row['parsed_rows']} "
                    f"success={row['success']} error={row['error'] or '-'} cost={row['request_cost'] or row['estimated_multiplier']}",
                    flush=True,
                )
                if args.stop_on_success and row["success"]:
                    break

    print(f"[zenrows_graphql] wrote {json_path}")
    print(f"[zenrows_graphql] wrote {csv_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Probe Magalu listing GraphQL POST through ZenRows Universal Scraper API.")
    parser.add_argument("product_line", nargs="?", default=os.getenv("SEDA_PRODUCT_LINE", "TV").upper(), choices=["TV", "REF", "LDY"])
    parser.add_argument("--run-ids", default=os.getenv("SEDA_MAGALU_ZENROWS_GRAPHQL_RUN_IDS", "main,bsr"))
    parser.add_argument("--pages", default=os.getenv("SEDA_MAGALU_ZENROWS_GRAPHQL_PAGES", "1"))
    parser.add_argument("--profiles", default=os.getenv("SEDA_MAGALU_ZENROWS_GRAPHQL_PROFILES", "basic_post,auto_post,premium_br_post,premium_br_session_post"))
    parser.add_argument("--page-size", type=int, default=int(os.getenv("SEDA_MAGALU_SEARCH_PAGE_SIZE", "60")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_ZENROWS_TIMEOUT", os.getenv("ZENROWS_TIMEOUT", "180"))))
    parser.add_argument("--postal-code", default=os.getenv("SEDA_POSTAL_CODE", "01001-001"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--execute", action="store_true", help="Execute paid ZenRows calls.")
    parser.add_argument(
        "--no-stop-on-success",
        dest="stop_on_success",
        action="store_false",
        default=os.getenv("SEDA_MAGALU_ZENROWS_GRAPHQL_STOP_ON_SUCCESS", "1").lower() not in {"0", "false", "no", "n"},
    )
    return parser.parse_args()


def ensure_zenrows_allowed():
    if os.getenv("SEDA_ALLOW_ZENROWS", "0").lower() not in {"1", "true", "yes", "y"}:
        raise SystemExit("Refusing paid probe: set SEDA_ALLOW_ZENROWS=1.")
    if not os.getenv("ZENROWS_API_KEY", "").strip():
        raise SystemExit("Refusing paid probe: ZENROWS_API_KEY is not set.")


def run_profile(args, profile_name, run_id, page, listing_url, payload):
    started = time.perf_counter()
    profile_params = PROFILE_PARAMS.get(profile_name)
    if profile_params is None:
        return empty_row(profile_name, run_id, page, listing_url, "", f"unknown_profile:{profile_name}")

    target_url = endpoint_url(payload.get("operationName", ""))
    params = dict(profile_params)
    params["url"] = target_url
    params["apikey"] = os.getenv("ZENROWS_API_KEY", "").strip() if args.execute else ""
    headers = graphql_headers(listing_url)
    public_params = {key: value for key, value in params.items() if key != "apikey"}
    multiplier = estimated_multiplier(public_params)

    if not args.execute:
        return {
            **empty_row(profile_name, run_id, page, listing_url, target_url, "dry_run"),
            "estimated_multiplier": multiplier,
            "params_public": json.dumps(public_params, ensure_ascii=False, separators=(",", ":")),
        }

    text = ""
    zenrows_status = 0
    target_status = 0
    error = ""
    response_headers = {}
    try:
        response = requests.post(
            ZENROWS_API_URL,
            params=params,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False),
            timeout=args.timeout,
        )
        zenrows_status = int(response.status_code)
        text = response.text or ""
        response_headers = response.headers
        target_status = target_status_from_headers(response_headers, zenrows_status)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    summary = summarize_response(text, listing_url, run_id, page)
    if not error:
        if zenrows_status != 200:
            error = f"zenrows_http_{zenrows_status}"
        elif summary["json_error"]:
            error = summary["json_error"]
        elif summary["pagination_page"] and summary["pagination_page"] != page:
            error = f"page_mismatch:{summary['pagination_page']}!={page}"
        elif not summary["products"]:
            error = "empty_products"

    return {
        "run_id": run_id,
        "page": page,
        "profile": profile_name,
        "url": listing_url,
        "target_url": target_url,
        "zenrows_status": zenrows_status,
        "target_status": target_status,
        "seconds": round(time.perf_counter() - started, 3),
        "bytes": len(text),
        "products": summary["products"],
        "parsed_rows": summary["parsed_rows"],
        "pagination_page": summary["pagination_page"],
        "pagination_size": summary["pagination_size"],
        "success": int(not error and summary["products"] > 0 and summary["parsed_rows"] > 0),
        "error": error,
        "estimated_multiplier": multiplier,
        "request_cost": response_headers.get("X-Request-Cost", ""),
        "zenrows_request_id": response_headers.get("X-Request-Id", ""),
        "params_public": json.dumps(public_params, ensure_ascii=False, separators=(",", ":")),
        "preview": text[:220].replace("\r", " ").replace("\n", " "),
    }


def endpoint_url(operation):
    if operation:
        return f"{search_api.GRAPHQL_URL}?operationName={operation}"
    return search_api.GRAPHQL_URL


def graphql_headers(listing_url):
    headers = search_api._headers(listing_url)
    headers.update(
        {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://www.magazineluiza.com.br",
            "referer": listing_url,
        }
    )
    return headers


def summarize_response(text, url, run_id, page):
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {"json_error": "invalid_json", "products": 0, "parsed_rows": 0, "pagination_page": 0, "pagination_size": 0}
    search = ((data.get("data") or {}).get("search") or {}) if isinstance(data, dict) else {}
    if not isinstance(search, dict) or not search:
        return {"json_error": "missing_search", "products": 0, "parsed_rows": 0, "pagination_page": 0, "pagination_size": 0}
    products = search.get("products") or []
    pagination = search.get("pagination") or {}
    html_text = search_api._as_next_data_html(search, url)
    parsed_rows = len(parse_listing(html_text, "Magalu", RETAILERS["magalu"].base_url, url, run_id=run_id))
    return {
        "json_error": "",
        "products": len(products) if isinstance(products, list) else 0,
        "parsed_rows": parsed_rows,
        "pagination_page": as_int(pagination.get("page"), 0),
        "pagination_size": as_int(pagination.get("size"), 0),
    }


def target_status_from_headers(headers, fallback):
    for key in ("X-Original-Status", "X-ZenRows-Original-Status", "Original-Status"):
        value = headers.get(key)
        if value:
            return as_int(value, fallback)
    return fallback


def empty_row(profile_name, run_id, page, listing_url, target_url, error):
    return {
        "run_id": run_id,
        "page": page,
        "profile": profile_name,
        "url": listing_url,
        "target_url": target_url,
        "zenrows_status": 0,
        "target_status": 0,
        "seconds": 0,
        "bytes": 0,
        "products": 0,
        "parsed_rows": 0,
        "pagination_page": 0,
        "pagination_size": 0,
        "success": 0,
        "error": error,
        "estimated_multiplier": "",
        "request_cost": "",
        "zenrows_request_id": "",
        "params_public": "",
        "preview": "",
    }


def parse_run_ids(raw):
    values = [item.strip().lower() for item in str(raw or "").split(",") if item.strip()]
    return [value for value in values if value in {"main", "bsr"}] or ["main"]


def parse_pages(raw):
    pages = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if item:
            pages.append(int(item))
    return pages or [1]


def parse_profiles(raw):
    profiles = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    return profiles or ["basic_post", "auto_post", "premium_br_post"]


def as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def write_outputs(csv_path, json_path, rows):
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
