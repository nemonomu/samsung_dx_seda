import argparse
import asyncio
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

from ..step00_config import DEFAULT_RUNS_BASE, run_date
from .review_api import GRAPHQL_URL, PRODUCT_RATING_QUERY
from .zenrows_client import ZENROWS_API_URL, api_key, estimated_multiplier
from .zenrows_browser_probe import (
    DEFAULT_PDP_URL,
    connection_url,
    ensure_execute_allowed as ensure_browser_execute_allowed,
    fetch_page,
    fetch_product_rating_in_page,
    install_native_fetch_capture,
)


DEFAULT_VARIATION_ID = "240144700"
DEFAULT_REFERER = DEFAULT_PDP_URL

UNIVERSAL_POST_PROFILES = {
    "zenrows_post_basic": {},
    "zenrows_post_custom_headers": {"custom_headers": "true"},
    "zenrows_post_premium": {"premium_proxy": "true", "proxy_country": "br"},
    "zenrows_post_premium_custom_headers": {"premium_proxy": "true", "proxy_country": "br", "custom_headers": "true"},
    "zenrows_post_premium_original_status": {
        "premium_proxy": "true",
        "proxy_country": "br",
        "custom_headers": "true",
        "original_status": "true",
    },
    "zenrows_post_auto": {"mode": "auto", "proxy_country": "br"},
    "zenrows_post_auto_custom_headers": {"mode": "auto", "proxy_country": "br", "custom_headers": "true"},
    "zenrows_post_js_premium": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "custom_headers": "true",
        "original_status": "true",
    },
}

LOCAL_CASES = ["local_direct_requests"]
BROWSER_CASES = ["browser_native_fetch_then_context_request"]


def output_dir():
    override = os.getenv("SEDA_MAGALU_REVIEW_MATRIX_DIR", "").strip()
    if override:
        return Path(override)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_RUNS_BASE / "magalu" / run_date() / "review_transport_matrix" / stamp


def payload(variation_id, page=1, page_size=16):
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


def graphql_headers(referer):
    return {
        "accept": "application/json",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://www.magazineluiza.com.br",
        "referer": referer,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
    }


def summarize_review(parsed):
    product_rating = (((parsed or {}).get("data") or {}).get("productRating") or {}) if isinstance(parsed, dict) else {}
    general = product_rating.get("general") if isinstance(product_rating.get("general"), dict) else {}
    user_reviews = product_rating.get("userReviews") if isinstance(product_rating.get("userReviews"), dict) else {}
    items = user_reviews.get("items") if isinstance(user_reviews.get("items"), list) else []
    descriptions = [
        str(item.get("description") or "").strip()
        for item in items
        if isinstance(item, dict) and str(item.get("description") or "").strip()
    ]
    return {
        "has_product_rating": bool(product_rating),
        "rating": general.get("rating", ""),
        "reviewCount": general.get("reviewCount", ""),
        "commentCount": general.get("commentCount", ""),
        "review_items": len(items),
        "review_descriptions": len(descriptions),
        "sample_reviews": descriptions[:3],
        "success": bool(product_rating and (general or descriptions)),
    }


def looks_blocked(text, status_code=0):
    lowered = (text or "").lower()
    if status_code in {401, 403, 429}:
        return True
    return any(marker in lowered for marker in ("akamai", "nao e poss", "não é poss", "access denied", "captcha", "customdeny"))


def parse_json(text):
    try:
        return json.loads(text or "")
    except ValueError:
        return None


def run_local_direct(case_name, request_payload, referer, timeout):
    started = time.monotonic()
    try:
        response = requests.post(GRAPHQL_URL, json=request_payload, headers=graphql_headers(referer), timeout=timeout)
        text = response.text or ""
        parsed = parse_json(text)
        error = "" if response.ok else f"http_{response.status_code}"
        return result_row(case_name, response.status_code, response.headers, text, parsed, started, error, "0")
    except Exception as exc:
        return error_row(case_name, started, f"{type(exc).__name__}: {exc}", "0")


def run_zenrows_post(case_name, profile_params, request_payload, referer, timeout):
    started = time.monotonic()
    key = api_key()
    if not key:
        return error_row(case_name, started, "ZENROWS_API_KEY is not set", estimated_multiplier(profile_params))
    params = dict(profile_params)
    if str(params.get("mode", "")).lower() == "auto" or str(params.get("premium_proxy", "")).lower() == "true":
        params.setdefault("proxy_country", os.getenv("SEDA_ZENROWS_PROXY_COUNTRY", "br"))
    session_id = os.getenv("SEDA_ZENROWS_SESSION_ID", "").strip()
    if session_id:
        params["session_id"] = session_id
    params.update({"apikey": key, "url": GRAPHQL_URL})
    public_params = {k: v for k, v in params.items() if k != "apikey"}
    headers = graphql_headers(referer)
    try:
        response = requests.post(
            ZENROWS_API_URL,
            params=params,
            data=json.dumps(request_payload),
            headers=headers,
            timeout=timeout,
        )
        text = response.text or ""
        parsed = parse_json(text)
        error = "" if response.ok else f"http_{response.status_code}"
        row = result_row(case_name, response.status_code, response.headers, text, parsed, started, error, estimated_multiplier(public_params))
        row["params"] = public_params
        return row
    except Exception as exc:
        row = error_row(case_name, started, f"{type(exc).__name__}: {exc}", estimated_multiplier(public_params))
        row["params"] = public_params
        return row


def result_row(case_name, status_code, headers, text, parsed, started, error, cost):
    summary = summarize_review(parsed)
    return {
        "case": case_name,
        "success": bool(summary.get("success")),
        "status_code": status_code,
        "content_type": headers.get("content-type", "") if hasattr(headers, "get") else "",
        "seconds": round(time.monotonic() - started, 3),
        "text_length": len(text or ""),
        "looks_blocked": looks_blocked(text, status_code),
        "error": error,
        "estimated_multiplier": cost,
        "x_request_cost": headers.get("X-Request-Cost", "") if hasattr(headers, "get") else "",
        "x_request_id": headers.get("X-Request-Id", "") if hasattr(headers, "get") else "",
        "summary": summary,
        "text_preview": (text or "")[:500],
    }


def error_row(case_name, started, error, cost):
    return {
        "case": case_name,
        "success": False,
        "status_code": 0,
        "content_type": "",
        "seconds": round(time.monotonic() - started, 3),
        "text_length": 0,
        "looks_blocked": False,
        "error": error,
        "estimated_multiplier": cost,
        "x_request_cost": "",
        "x_request_id": "",
        "summary": summarize_review(None),
        "text_preview": "",
    }


async def run_browser_case(case_name, pdp_url):
    started = time.monotonic()
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        return error_row(case_name, started, "Missing dependency: playwright", "scraping_browser")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(connection_url())
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
                page = await context.new_page()
                await install_native_fetch_capture(page)
                page_result = await fetch_page(page, pdp_url)
                review_result = await fetch_product_rating_in_page(page, pdp_url)
            finally:
                await browser.close()
        review_result["case"] = case_name
        review_result["success"] = bool((review_result.get("summary") or {}).get("success"))
        review_result["seconds"] = round(time.monotonic() - started, 3)
        review_result["looks_blocked"] = looks_blocked(review_result.get("text_preview", ""), int(review_result.get("status_code") or 0))
        review_result["estimated_multiplier"] = "scraping_browser_session"
        review_result["pdp_status_code"] = page_result.get("status_code", 0)
        review_result["pdp_error"] = page_result.get("error", "")
        review_result["pdp_final_url"] = page_result.get("final_url", "")
        return review_result
    except Exception as exc:
        return error_row(case_name, started, f"{type(exc).__name__}: {exc}", "scraping_browser_session")


def flatten_row(row):
    summary = row.get("summary") or {}
    return {
        "case": row.get("case", ""),
        "success": row.get("success", False),
        "status_code": row.get("status_code", ""),
        "content_type": row.get("content_type", ""),
        "seconds": row.get("seconds", ""),
        "text_length": row.get("text_length", ""),
        "looks_blocked": row.get("looks_blocked", ""),
        "error": row.get("error", ""),
        "estimated_multiplier": row.get("estimated_multiplier", ""),
        "x_request_cost": row.get("x_request_cost", ""),
        "has_product_rating": summary.get("has_product_rating", ""),
        "rating": summary.get("rating", ""),
        "reviewCount": summary.get("reviewCount", ""),
        "commentCount": summary.get("commentCount", ""),
        "review_items": summary.get("review_items", ""),
        "review_descriptions": summary.get("review_descriptions", ""),
        "text_preview": row.get("text_preview", ""),
    }


def selected_cases(args):
    if args.cases:
        return args.cases
    return LOCAL_CASES + list(UNIVERSAL_POST_PROFILES) + BROWSER_CASES


def dry_run_plan(args):
    cases = []
    for case in selected_cases(args):
        if case in UNIVERSAL_POST_PROFILES:
            params = dict(UNIVERSAL_POST_PROFILES[case])
            cases.append({"case": case, "kind": "zenrows_universal_post", "params": params, "estimated_multiplier": estimated_multiplier(params)})
        elif case in BROWSER_CASES:
            cases.append({"case": case, "kind": "zenrows_scraping_browser", "estimated_multiplier": "scraping_browser_session"})
        else:
            cases.append({"case": case, "kind": "local_direct", "estimated_multiplier": "0"})
    return cases


def write_outputs(directory, results, meta):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "review_transport_matrix.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = directory / "review_transport_matrix.csv"
    rows = [flatten_row(row) for row in results]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["case"])
        writer.writeheader()
        writer.writerows(rows)
    for row in results:
        preview = row.get("text_preview", "")
        if preview:
            safe_case = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in row.get("case", "case"))
            (directory / f"{safe_case}_preview.txt").write_text(preview, encoding="utf-8", errors="ignore")
    return csv_path


def execute_http_cases(cases, request_payload, referer, timeout, max_workers):
    jobs = []
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for case in cases:
            if case in LOCAL_CASES:
                jobs.append(executor.submit(run_local_direct, case, request_payload, referer, timeout))
            elif case in UNIVERSAL_POST_PROFILES:
                jobs.append(executor.submit(run_zenrows_post, case, UNIVERSAL_POST_PROFILES[case], request_payload, referer, timeout))
        for job in as_completed(jobs):
            row = job.result()
            print(
                f"[review-matrix] {row.get('case')} status={row.get('status_code')} "
                f"success={int(bool(row.get('success')))} blocked={int(bool(row.get('looks_blocked')))} "
                f"reviews={(row.get('summary') or {}).get('review_descriptions', 0)} "
                f"cost={row.get('x_request_cost') or row.get('estimated_multiplier')}",
                flush=True,
            )
            results.append(row)
    return results


def main():
    parser = argparse.ArgumentParser(description="Magalu review transport matrix for ProductRating GraphQL.")
    parser.add_argument("--execute", action="store_true", help="Run live tests. ZenRows cases require SEDA_ALLOW_ZENROWS=1 and ZENROWS_API_KEY.")
    parser.add_argument("--variation-id", default=DEFAULT_VARIATION_ID)
    parser.add_argument("--pdp-url", default=DEFAULT_PDP_URL)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--cases", nargs="+", default=[])
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    directory = Path(args.output_dir) if args.output_dir else output_dir()
    cases = selected_cases(args)
    request_payload = payload(args.variation_id)
    meta = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "execute": bool(args.execute),
        "variation_id": args.variation_id,
        "pdp_url": args.pdp_url,
        "output_dir": str(directory),
        "planned_cases": dry_run_plan(args),
        "results": [],
    }

    if not args.execute:
        write_outputs(directory, [], meta)
        print(f"[review-matrix] dry-run wrote {directory / 'review_transport_matrix.json'}")
        return

    if any(case in UNIVERSAL_POST_PROFILES or case in BROWSER_CASES for case in cases):
        ensure_browser_execute_allowed(True)

    http_cases = [case for case in cases if case not in BROWSER_CASES]
    results = execute_http_cases(http_cases, request_payload, args.pdp_url, args.timeout, args.max_workers)
    for case in [case for case in cases if case in BROWSER_CASES]:
        row = asyncio.run(run_browser_case(case, args.pdp_url))
        print(
            f"[review-matrix] {row.get('case')} status={row.get('status_code')} "
            f"success={int(bool(row.get('success')))} blocked={int(bool(row.get('looks_blocked')))} "
            f"reviews={(row.get('summary') or {}).get('review_descriptions', 0)} "
            f"cost={row.get('estimated_multiplier')}",
            flush=True,
        )
        results.append(row)

    meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
    meta["results"] = results
    csv_path = write_outputs(directory, results, meta)
    print(f"[review-matrix] wrote {directory / 'review_transport_matrix.json'}")
    print(f"[review-matrix] wrote {csv_path}")


if __name__ == "__main__":
    main()
