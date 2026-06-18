import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

from seda.parsers import parse_listing, sku_from_url
from seda.step00_config import RETAILERS, page_url, product_line, write_json
from seda.transport import fetch_url


def main():
    args = parse_args()
    if not args.preserve_review_sleeps:
        os.environ["SEDA_MAGALU_REVIEW_INITIAL_SLEEP_SECONDS"] = "0"
        os.environ["SEDA_MAGALU_REVIEW_SLEEP_SECONDS"] = "0"

    output_dir = Path(args.output_dir or _default_output_dir())
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(output_dir),
        "args": vars(args),
        "environment": {
            "fetch_mode": os.getenv("SEDA_FETCH_MODE", ""),
            "pdp_html_fetch": os.getenv("SEDA_MAGALU_PDP_HTML_FETCH", ""),
            "pdp_nav_fallback": os.getenv("SEDA_MAGALU_PDP_NAV_FALLBACK", ""),
            "browser_graphql": os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL", ""),
            "allow_zenrows": os.getenv("SEDA_ALLOW_ZENROWS", ""),
        },
        "product_lines": [],
    }
    event_rows = []

    for line in args.product_lines:
        line = line.strip().upper()
        if not line:
            continue
        os.environ["SEDA_PRODUCT_LINE"] = line
        line_result, line_events = probe_product_line(line, args)
        summary["product_lines"].append(line_result)
        event_rows.extend(line_events)

    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(output_dir / "probe_summary.json", summary)
    write_events_csv(output_dir / "probe_events.csv", event_rows)
    print(f"[probe] wrote {output_dir / 'probe_summary.json'}")
    print(f"[probe] wrote {output_dir / 'probe_events.csv'}")


def parse_args():
    parser = argparse.ArgumentParser(description="Probe Magalu collection transport timings without running a full pipeline.")
    parser.add_argument("--product-lines", nargs="+", default=["TV", "REF", "LDY"], help="Product lines to probe.")
    parser.add_argument("--listing-pages", type=int, default=1, help="Pages per main/bsr listing to probe.")
    parser.add_argument("--detail-limit", type=int, default=5, help="Number of listing products to probe through detail GraphQL.")
    parser.add_argument("--review-limit", type=int, default=3, help="Number of products to probe through review GraphQL.")
    parser.add_argument("--review-text-limit", type=int, default=20, help="Max review bodies per product during review probe.")
    parser.add_argument("--pdp-html-limit", type=int, default=0, help="Number of products to test with PDP HTML fetch. Default 0.")
    parser.add_argument("--batch-limit", type=int, default=2, help="Number of itemQuery payloads for GraphQL array batch probe.")
    parser.add_argument("--timeout", type=int, default=25, help="Timeout for listing/detail/review GraphQL calls.")
    parser.add_argument("--html-timeout", type=int, default=20, help="Timeout for optional PDP HTML fetch.")
    parser.add_argument("--preserve-review-sleeps", action="store_true", help="Do not zero review sleeps in this probe.")
    parser.add_argument("--output-dir", default="", help="Probe output directory.")
    return parser.parse_args()


def probe_product_line(line, args):
    config = RETAILERS["magalu"]
    result = {
        "product_line": line,
        "main": {},
        "bsr": {},
        "detail": [],
        "review": [],
        "pdp_html": [],
        "graphql_batch": {},
    }
    events = []

    main_rows, main_result = probe_listing(config, "main", args)
    bsr_rows, bsr_result = probe_listing(config, "bsr", args)
    result["main"] = main_result
    result["bsr"] = bsr_result
    events.extend(main_result.pop("_events", []))
    events.extend(bsr_result.pop("_events", []))

    sample_rows = unique_rows(main_rows + bsr_rows)[: max(args.detail_limit, args.review_limit, args.pdp_html_limit, args.batch_limit)]
    for row in sample_rows[: args.detail_limit]:
        detail_result = probe_detail(row, args)
        result["detail"].append(detail_result)
        events.append(event_from_result(line, "detail", row, detail_result))

    for row in sample_rows[: args.review_limit]:
        review_result = probe_review(row, args)
        result["review"].append(review_result)
        events.append(event_from_result(line, "review", row, review_result))

    for row in sample_rows[: args.pdp_html_limit]:
        html_result = probe_pdp_html(row, args)
        result["pdp_html"].append(html_result)
        events.append(event_from_result(line, "pdp_html", row, html_result))

    result["graphql_batch"] = probe_graphql_batch(sample_rows[: args.batch_limit], args)
    events.append(
        {
            "product_line": line,
            "stage": "graphql_batch",
            "item": "",
            "sku": "",
            "product_url": "",
            "success": result["graphql_batch"].get("success", ""),
            "seconds": result["graphql_batch"].get("seconds", ""),
            "method": result["graphql_batch"].get("method", ""),
            "status": result["graphql_batch"].get("status", ""),
            "error": result["graphql_batch"].get("error", ""),
        }
    )
    return result, events


def probe_listing(config, run_id, args):
    rows = []
    page_results = []
    events = []
    for page in range(1, args.listing_pages + 1):
        url = page_url(config, page, run_id=run_id)
        start = time.monotonic()
        fetch_result = fetch_url(url, timeout=args.timeout)
        seconds = round(time.monotonic() - start, 3)
        parsed = []
        error = fetch_result.error or ""
        if fetch_result.text and not error:
            parsed = parse_listing(fetch_result.text, config.name, config.base_url, url, run_id=run_id)
            rows.extend(parsed)
        page_result = {
            "run_id": run_id,
            "page": page,
            "url": url,
            "success": bool(parsed),
            "rows": len(parsed),
            "seconds": seconds,
            "method": fetch_result.method,
            "status_code": fetch_result.status_code,
            "length": len(fetch_result.text or ""),
            "error": error,
            "attempts": fetch_result.attempts,
        }
        page_results.append(page_result)
        events.append(
            {
                "product_line": product_line(),
                "stage": f"{run_id}_listing",
                "item": "",
                "sku": "",
                "product_url": url,
                "success": page_result["success"],
                "seconds": seconds,
                "method": fetch_result.method,
                "status": fetch_result.status_code,
                "error": error,
            }
        )
        print(
            f"[probe] {product_line()} {run_id} page={page} rows={len(parsed)} "
            f"seconds={seconds} method={fetch_result.method} error={error}"
        )
    return rows, {
        "rows": len(rows),
        "unique": len(unique_rows(rows)),
        "pages": page_results,
        "_events": events,
    }


def probe_detail(row, args):
    from seda.magalu.detail_api import fetch_detail

    item = row.get("item") or sku_from_url(row.get("product_url", "")) or row.get("sku", "")
    start = time.monotonic()
    try:
        response = fetch_detail(item, timeout=args.timeout)
        error = response.get("error", "")
    except Exception as exc:
        response = {"success": False, "trace": []}
        error = f"{type(exc).__name__}: {exc}"
    seconds = round(time.monotonic() - start, 3)
    detail = response.get("detail") or {}
    return {
        "item": item,
        "sku": detail.get("sku", row.get("sku", "")),
        "product_url": row.get("product_url", ""),
        "success": bool(response.get("success")),
        "seconds": seconds,
        "method": "fetch_detail",
        "error": error,
        "trace": response.get("trace") or [],
        "fields": {key: detail.get(key, "") for key in _detail_probe_fields()},
    }


def probe_review(row, args):
    from seda.magalu.review_api import fetch_product_rating

    variation_id = sku_from_url(row.get("product_url", "")) or row.get("sku") or row.get("item", "")
    start = time.monotonic()
    try:
        response = fetch_product_rating(variation_id, limit=args.review_text_limit, timeout=args.timeout)
        error = response.get("error", "")
    except Exception as exc:
        response = {"success": False, "reviews": [], "trace": []}
        error = f"{type(exc).__name__}: {exc}"
    seconds = round(time.monotonic() - start, 3)
    general = response.get("general") or {}
    return {
        "item": row.get("item", ""),
        "sku": row.get("sku", ""),
        "variation_id": variation_id,
        "product_url": row.get("product_url", ""),
        "success": bool(response.get("success") or general),
        "seconds": seconds,
        "method": response.get("method", "graphql_product_rating"),
        "error": error,
        "reviews": len(response.get("reviews") or []),
        "rating": general.get("rating", ""),
        "reviewCount": general.get("reviewCount", ""),
        "commentCount": general.get("commentCount", ""),
        "trace": response.get("trace") or [],
    }


def probe_pdp_html(row, args):
    from seda.magalu.browser_session import fetch_html

    url = row.get("product_url", "")
    start = time.monotonic()
    try:
        response = fetch_html(url, timeout=args.html_timeout)
        error = response.get("error", "")
    except Exception as exc:
        response = {"status_code": 0, "text": "", "trace": []}
        error = f"{type(exc).__name__}: {exc}"
    seconds = round(time.monotonic() - start, 3)
    text = response.get("text") or ""
    return {
        "item": row.get("item", ""),
        "sku": row.get("sku", ""),
        "product_url": url,
        "success": "__NEXT_DATA__" in text,
        "seconds": seconds,
        "method": "fetch_html",
        "status": response.get("status_code", 0),
        "length": len(text),
        "has_next_data": "__NEXT_DATA__" in text,
        "error": error,
        "trace": response.get("trace") or [],
    }


def probe_graphql_batch(rows, args):
    if len(rows) < 2:
        return {"success": False, "error": "need_at_least_two_rows", "seconds": 0}
    try:
        from seda.magalu.browser_session import graphql_post_raw
        from seda.magalu.detail_api import ITEM_QUERY
    except Exception as exc:
        return {"success": False, "error": f"import_{type(exc).__name__}: {exc}", "seconds": 0}
    payloads = []
    for row in rows:
        item_id = row.get("item") or sku_from_url(row.get("product_url", "")) or row.get("sku", "")
        if not item_id:
            continue
        payloads.append({"operationName": "itemQuery", "variables": {"itemId": item_id}, "query": ITEM_QUERY})
    if len(payloads) < 2:
        return {"success": False, "error": "not_enough_item_ids", "seconds": 0}
    start = time.monotonic()
    try:
        response = graphql_post_raw(payloads[: args.batch_limit], timeout=args.timeout)
        error = response.get("error", "")
    except Exception as exc:
        response = {"status_code": 0, "data": None, "text": ""}
        error = f"{type(exc).__name__}: {exc}"
    seconds = round(time.monotonic() - start, 3)
    data = response.get("data")
    return {
        "success": isinstance(data, list) and len(data) == min(args.batch_limit, len(payloads)),
        "seconds": seconds,
        "method": "graphql_post_raw_array",
        "status": response.get("status_code", 0),
        "payload_count": min(args.batch_limit, len(payloads)),
        "response_type": type(data).__name__,
        "response_count": len(data) if isinstance(data, list) else "",
        "text_length": len(response.get("text") or ""),
        "error": error,
    }


def unique_rows(rows):
    seen = set()
    unique = []
    for row in rows:
        key = row.get("item") or sku_from_url(row.get("product_url", "")) or row.get("product_url", "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def event_from_result(line, stage, row, result):
    return {
        "product_line": line,
        "stage": stage,
        "item": result.get("item", row.get("item", "")),
        "sku": result.get("sku", row.get("sku", "")),
        "product_url": result.get("product_url", row.get("product_url", "")),
        "success": result.get("success", ""),
        "seconds": result.get("seconds", ""),
        "method": result.get("method", ""),
        "status": result.get("status", ""),
        "error": result.get("error", ""),
    }


def write_events_csv(path, rows):
    columns = ["product_line", "stage", "item", "sku", "product_url", "success", "seconds", "method", "status", "error"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _detail_probe_fields():
    return [
        "sku",
        "screen_size",
        "estimated_annual_electricity_use",
        "model_year",
        "ref_refrigerator_type",
        "ref_capacity",
        "ldy_loading_type",
        "ldy_capacity",
        "delivery_availability",
        "pick_up_availability",
        "retailer_sku_name_similar",
        "star_rating",
        "count_of_star_ratings",
    ]


def _default_output_dir():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(Path("seda") / "magalu" / "log" / f"probe_collection_flow_{stamp}")


if __name__ == "__main__":
    main()
