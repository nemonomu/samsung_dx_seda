import argparse
import asyncio
import csv
import html
import json
import os
import time
from datetime import datetime
from pathlib import Path

from seda.parsers import extract_next_data, parse_listing, _magalu_is_relevant_product
from seda.step00_config import DEFAULT_RUNS_BASE, RETAILERS, page_url, run_date


CSV_COLUMNS = [
    "run_id",
    "product_line",
    "page",
    "url",
    "success",
    "seconds",
    "method",
    "error",
    "raw_products",
    "kept_products",
    "parsed_rows",
    "pagination_page",
    "pagination_size",
    "next_data_length",
    "attempts",
]


async def main_async():
    args = parse_args()
    os.environ["SEDA_PRODUCT_LINE"] = args.product_line.upper()
    pages = parse_pages(args.pages)
    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_RUNS_BASE / "magalu" / run_date() / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"playwright_nextdata_listing_{args.run_id}_{args.product_line.upper()}_{stamp}.json"
    csv_path = out_dir / f"playwright_nextdata_listing_{args.run_id}_{args.product_line.upper()}_{stamp}.csv"

    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(f"Missing dependency: playwright ({exc})")

    rows = []
    async with async_playwright() as p:
        context = await create_context(p, args)
        try:
            page_obj = await context.new_page()
            page_obj.set_default_timeout(args.script_timeout * 1000)
            for page in pages:
                url = page_url(RETAILERS["magalu"], page, run_id=args.run_id)
                row = await probe_page(page_obj, url, args.run_id, args.product_line.upper(), page, args)
                rows.append(row)
                write_outputs(json_path, csv_path, rows)
                print(
                    "[magalu_playwright_nextdata] "
                    f"page={page} success={int(row['success'])} "
                    f"raw={row['raw_products']} kept={row['kept_products']} parsed={row['parsed_rows']} "
                    f"pagination_page={row['pagination_page']} next_len={row['next_data_length']} "
                    f"seconds={row['seconds']} error={row['error'] or '-'}",
                    flush=True,
                )
        finally:
            await context.close()

    print(f"[magalu_playwright_nextdata] wrote {json_path}")
    print(f"[magalu_playwright_nextdata] wrote {csv_path}")
    if args.fail_fast and any(not row["success"] for row in rows):
        raise SystemExit("[magalu_playwright_nextdata] FAIL")


def parse_args():
    parser = argparse.ArgumentParser(description="Probe Magalu listing __NEXT_DATA__ via Playwright.")
    parser.add_argument("--product-line", default=os.getenv("SEDA_PRODUCT_LINE", "TV"), choices=["TV", "REF", "LDY"])
    parser.add_argument("--run-id", default=os.getenv("SEDA_RUN_ID", "main"), choices=["main", "bsr"])
    parser.add_argument("--pages", default=os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_PAGES", "1,2,3"))
    parser.add_argument("--output-dir", default=os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_OUTPUT_DIR", ""))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_TIMEOUT", "30")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_POLL_SECONDS", "0.5")))
    parser.add_argument("--nav-timeout", type=float, default=float(os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_NAV_TIMEOUT", "12")))
    parser.add_argument("--script-timeout", type=float, default=float(os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_SCRIPT_TIMEOUT", "3")))
    parser.add_argument("--attempts", type=int, default=int(os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_ATTEMPTS", "2")))
    parser.add_argument("--profile", default=os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_PROFILE", "C:/tmp/seda_magalu_playwright_nextdata_profile"))
    parser.add_argument("--headless", action="store_true", default=os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_HEADLESS", "0").lower() in {"1", "true", "yes", "y"})
    parser.add_argument("--channel", default=os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_CHANNEL", "chrome"))
    parser.add_argument("--browser", default=os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_BROWSER", "chromium"), choices=["chromium", "firefox", "webkit"])
    parser.add_argument("--fail-fast", action="store_true", default=os.getenv("SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_FAIL_FAST", "0").lower() in {"1", "true", "yes", "y"})
    return parser.parse_args()


def parse_pages(raw):
    pages = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if item:
            pages.append(int(item))
    return pages or [1, 2, 3]


async def create_context(playwright, args):
    browser_type = getattr(playwright, args.browser)
    launch_args = [
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    kwargs = {
        "headless": args.headless,
        "locale": "pt-BR",
        "viewport": {"width": 1366, "height": 900},
        "args": launch_args,
    }
    if args.browser == "chromium" and args.channel:
        kwargs["channel"] = args.channel
    return await browser_type.launch_persistent_context(args.profile, **kwargs)


async def probe_page(page_obj, url, run_id, product_line, page_number, args):
    started = time.perf_counter()
    attempts = []
    last = empty_result("not_attempted", 0)
    for attempt in range(1, max(1, args.attempts) + 1):
        nav_error = ""
        try:
            await page_obj.goto(url, wait_until="commit", timeout=args.nav_timeout * 1000)
        except Exception as exc:
            nav_error = f"navigation:{type(exc).__name__}: {exc}"
        result = await wait_for_next_data(page_obj, url, run_id, args.timeout, args.poll_seconds)
        attempts.append({"attempt": attempt, "navigation_error": nav_error, **result["trace"]})
        last = result
        if result["success"]:
            break
        try:
            await page_obj.reload(wait_until="commit", timeout=args.nav_timeout * 1000)
        except Exception:
            pass

    return {
        "run_id": run_id,
        "product_line": product_line,
        "page": page_number,
        "url": url,
        "success": bool(last["success"]),
        "seconds": round(time.perf_counter() - started, 3),
        "method": "playwright_evaluate_nextdata",
        "error": last.get("error", ""),
        "raw_products": last.get("raw_products", 0),
        "kept_products": last.get("kept_products", 0),
        "parsed_rows": last.get("parsed_rows", 0),
        "pagination_page": last.get("pagination_page", 0),
        "pagination_size": last.get("pagination_size", 0),
        "next_data_length": last.get("next_data_length", 0),
        "attempts": json.dumps(attempts, ensure_ascii=False),
    }


async def wait_for_next_data(page_obj, url, run_id, timeout, poll_seconds):
    deadline = time.perf_counter() + max(1.0, timeout)
    trace = {}
    last_error = ""
    while time.perf_counter() <= deadline:
        payload = await read_next_data_payload(page_obj)
        trace = {
            "href": payload.get("href", ""),
            "title": payload.get("title", ""),
            "ready_state": payload.get("readyState", ""),
            "next_data_length": len(payload.get("nextData") or ""),
            "error": payload.get("error", ""),
        }
        summary = summarize_next_data(payload.get("nextData") or "", url, run_id)
        if summary["success"]:
            return {**summary, "trace": trace}
        last_error = summary.get("error") or payload.get("error") or last_error
        await page_obj.wait_for_timeout(max(50, int(poll_seconds * 1000)))
    trace.update(await page_source_diagnostics(page_obj, url, run_id))
    return {**empty_result(last_error or "next_data_timeout", trace.get("next_data_length", 0)), "trace": trace}


async def read_next_data_payload(page_obj):
    script = """
() => {
  try {
    const node = document.querySelector('script#__NEXT_DATA__');
    return {
      href: location.href || '',
      title: document.title || '',
      readyState: document.readyState || '',
      nextData: node ? (node.textContent || '') : ''
    };
  } catch (error) {
    return {error: String(error), href: location.href || '', title: document.title || '', readyState: document.readyState || '', nextData: ''};
  }
}
"""
    try:
        result = await page_obj.evaluate(script)
        return result if isinstance(result, dict) else {"error": "invalid_script_result", "nextData": ""}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "nextData": ""}


async def page_source_diagnostics(page_obj, url, run_id):
    try:
        source = await page_obj.content()
    except Exception as exc:
        return {"page_source_error": f"{type(exc).__name__}: {exc}"}
    summary = summarize_next_data_from_html(source, url, run_id)
    return {
        "page_source_length": len(source),
        "page_source_has_next_data": int("__NEXT_DATA__" in source),
        "page_source_parsed_rows": summary.get("parsed_rows", 0),
        "page_source_error": summary.get("error", ""),
    }


def summarize_next_data(next_data_text, url, run_id):
    html_text = next_data_text_to_html(next_data_text) if str(next_data_text or "").strip() else ""
    return summarize_next_data_from_html(html_text, url, run_id, len(next_data_text or ""))


def summarize_next_data_from_html(html_text, url, run_id, next_data_length=None):
    data = extract_next_data(html_text)
    if not data:
        return empty_result("missing_next_data", len(html_text or "") if next_data_length is None else next_data_length)
    page_data = (((data.get("props") or {}).get("pageProps") or {}).get("data") or {})
    search = page_data.get("search") if isinstance(page_data, dict) else {}
    if not isinstance(search, dict):
        return empty_result("missing_search", len(html_text or "") if next_data_length is None else next_data_length)
    products = search.get("products")
    if not isinstance(products, list) or not products:
        return empty_result("missing_products", len(html_text or "") if next_data_length is None else next_data_length)
    pagination = search.get("pagination") if isinstance(search.get("pagination"), dict) else {}
    pagination_page = safe_int(pagination.get("page"), 0)
    requested_page = requested_page_from_url(url)
    if pagination_page != requested_page:
        return empty_result(f"page_mismatch:{pagination_page}!={requested_page}", len(html_text or "") if next_data_length is None else next_data_length)
    parsed = parse_listing(html_text, "Magalu", RETAILERS["magalu"].base_url, url, run_id=run_id)
    kept = sum(1 for product in products if isinstance(product, dict) and _magalu_is_relevant_product(product))
    return {
        "success": True,
        "error": "",
        "next_data_length": len(html_text or "") if next_data_length is None else next_data_length,
        "raw_products": len(products),
        "kept_products": kept,
        "parsed_rows": len(parsed),
        "pagination_page": pagination_page,
        "pagination_size": safe_int(pagination.get("size"), 0),
    }


def empty_result(error, length):
    return {
        "success": False,
        "error": error,
        "next_data_length": length,
        "raw_products": 0,
        "kept_products": 0,
        "parsed_rows": 0,
        "pagination_page": 0,
        "pagination_size": 0,
    }


def next_data_text_to_html(next_data_text):
    return '<script id="__NEXT_DATA__" type="application/json">' + html.escape(str(next_data_text or ""), quote=False) + "</script>"


def requested_page_from_url(url):
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(str(url or "")).query)
    return safe_int((query.get("page") or ["1"])[0], 1)


def safe_int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def write_outputs(json_path, csv_path, rows):
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
