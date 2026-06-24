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


MIN_DIRECT_ROWS = 40
MIN_BROWSER_ROWS = 40
MAX_PRODUCTS = 120


def main():
    args = parse_args()
    os.environ["SEDA_PRODUCT_LINE"] = args.product_line
    if args.postal_code:
        os.environ["SEDA_POSTAL_CODE"] = args.postal_code

    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_RUNS_BASE / "magalu" / run_date() / "preflight"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"listing_transport_{args.product_line}_{stamp}.json"
    csv_path = out_dir / f"listing_transport_{args.product_line}_{stamp}.csv"

    rows = []
    for run_id in ("main", "bsr"):
        for page in parse_pages(args.pages):
            url = page_url(RETAILERS["magalu"], page, run_id=run_id)
            print(f"[magalu_preflight] {run_id} page={page} direct start", flush=True)
            direct = check_direct(url, run_id, page, args.timeout)
            print(
                f"[magalu_preflight] {run_id} page={page} direct done "
                f"status={direct['status_code']} rows={direct['parsed_rows']} error={direct['error'] or '-'}",
                flush=True,
            )
            if not args.no_browser:
                print(f"[magalu_preflight] {run_id} page={page} browser_graphql start", flush=True)
            browser = check_browser(url, run_id, page, args.timeout) if not args.no_browser else skipped_browser()
            if not args.no_browser:
                print(
                    f"[magalu_preflight] {run_id} page={page} browser_graphql done "
                    f"status={browser['status_code']} rows={browser['parsed_rows']} error={browser['error'] or '-'}",
                    flush=True,
                )
            verdict = decide_verdict(direct, browser)
            record = {
                "product_line": args.product_line,
                "run_id": run_id,
                "page": page,
                "url": url,
                "verdict": verdict,
                **prefix("direct", direct),
                **prefix("browser", browser),
            }
            rows.append(record)
            print(
                f"[magalu_preflight] {run_id} page={page} verdict={verdict} "
                f"direct={direct['status_code']}:{direct['parsed_rows']}:{direct['error'] or '-'} "
                f"browser={browser['status_code']}:{browser['parsed_rows']}:{browser['error'] or '-'}",
                flush=True,
            )

    write_csv(csv_path, rows)
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[magalu_preflight] wrote {json_path}")
    print(f"[magalu_preflight] wrote {csv_path}")

    if args.fail_fast and any(row["verdict"] not in {"direct_ok", "browser_ok"} for row in rows):
        raise SystemExit("[magalu_preflight] FAIL")


def parse_args():
    parser = argparse.ArgumentParser(description="Magalu listing transport preflight.")
    parser.add_argument("product_line", nargs="?", default=os.getenv("SEDA_PRODUCT_LINE", "TV").upper(), choices=["TV", "REF", "LDY"])
    parser.add_argument("--pages", default=os.getenv("SEDA_MAGALU_PREFLIGHT_PAGES", "1,2,3"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_TIMEOUT", "60")))
    parser.add_argument("--postal-code", default=os.getenv("SEDA_POSTAL_CODE", "01001-001"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def parse_pages(raw):
    pages = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if item:
            pages.append(int(item))
    return pages or [1, 2, 3]


def check_direct(url, run_id, page, timeout):
    payload = search_api._payload(url, int(os.getenv("SEDA_MAGALU_SEARCH_PAGE_SIZE", "60")))
    started = time.perf_counter()
    try:
        response = requests.post(search_api.GRAPHQL_URL, json=payload, headers=search_api._headers(url), timeout=timeout)
    except Exception as exc:
        return empty_result("direct_graphql", f"{type(exc).__name__}: {exc}", elapsed(started))
    return summarize_graphql_response("direct_graphql", response.status_code, response.text, url, run_id, page, elapsed(started))


def check_browser(url, run_id, page, timeout):
    started = time.perf_counter()
    try:
        from seda.magalu.browser_session import get_page
    except Exception as exc:
        return empty_result("browser_graphql", f"browser_import:{type(exc).__name__}: {exc}", elapsed(started))

    try:
        browser_page = get_page()
        current_url = str(getattr(browser_page, "url", "") or "")
        if "magazineluiza.com.br" not in current_url:
            if os.getenv("SEDA_MAGALU_PREFLIGHT_BROWSER_NAVIGATE", "0").lower() in {"1", "true", "yes", "y"}:
                browser_page.get(url)
                time.sleep(float(os.getenv("SEDA_MAGALU_PREFLIGHT_BROWSER_WAIT", "3")))
            else:
                return empty_result(
                    "browser_graphql",
                    f"current_page_not_magalu:{current_url or 'blank'}",
                    elapsed(started),
                )
        payload = search_api._payload(url, int(os.getenv("SEDA_MAGALU_SEARCH_PAGE_SIZE", "60")))
        result = browser_graphql_fetch(browser_page, payload, timeout)
    except Exception as exc:
        return empty_result("browser_graphql", f"{type(exc).__name__}: {exc}", elapsed(started))

    data = result.get("data") or {}
    text = result.get("text") or json.dumps(data, ensure_ascii=False)
    status_code = int(result.get("status_code") or 0)
    return summarize_graphql_data("browser_graphql", status_code, data, text, url, run_id, page, elapsed(started), result.get("error", ""))


def browser_graphql_fetch(browser_page, payload, timeout):
    payload_text = json.dumps(payload, ensure_ascii=False)
    script = """
return (async () => {
  try {
    const payload = JSON.parse(arguments[0]);
    const operation = payload.operationName || '';
    const response = await fetch(
      'https://federation.magazineluiza.com.br/graphql?operationName=' + encodeURIComponent(operation),
      {
        method: 'POST',
        headers: {
          'accept': 'application/json',
          'content-type': 'application/json',
          'x-channel-id': '45',
          'x-channel-name': 'mixer-desk.magazineluiza.com.br'
        },
        body: JSON.stringify(payload)
      }
    );
    return JSON.stringify({status: response.status, text: await response.text()});
  } catch (error) {
    return JSON.stringify({status: 0, error: String(error), text: ''});
  }
})()
"""
    raw = browser_page.run_js(script, payload_text, timeout=int(timeout))
    try:
        result = json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        result = {"status": 0, "error": "invalid_js_result", "text": str(raw or "")}
    text = result.get("text") or ""
    data = {}
    error = result.get("error") or ""
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            error = error or "invalid_json"
    return {
        "status_code": int(result.get("status") or 0),
        "text": text,
        "data": data,
        "error": error,
    }


def summarize_graphql_response(method, status_code, text, url, run_id, page, seconds):
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return empty_result(method, "invalid_json", seconds, status_code=status_code, bytes_count=len(text or ""))
    return summarize_graphql_data(method, status_code, data, text, url, run_id, page, seconds, "")


def summarize_graphql_data(method, status_code, data, text, url, run_id, page, seconds, error):
    search = ((data.get("data") or {}).get("search") or {}) if isinstance(data, dict) else {}
    products = search.get("products") if isinstance(search, dict) else []
    pagination = search.get("pagination") if isinstance(search, dict) else {}
    product_count = len(products) if isinstance(products, list) else 0
    payload_page = as_int((pagination or {}).get("page"), 0)
    payload_size = as_int((pagination or {}).get("size"), 0)
    html_text = search_api._as_next_data_html(search, url) if isinstance(search, dict) and search else ""
    parsed_rows = len(parse_listing(html_text, "Magalu", RETAILERS["magalu"].base_url, url, run_id=run_id)) if html_text else 0

    if status_code != 200:
        error = error or f"http_{status_code}"
    elif not isinstance(search, dict) or not search:
        error = error or "missing_search"
    elif payload_page != page:
        error = error or f"page_mismatch:{payload_page}!={page}"
    elif payload_size > MAX_PRODUCTS or product_count > MAX_PRODUCTS or parsed_rows > MAX_PRODUCTS:
        error = error or f"too_many_products:{max(payload_size, product_count, parsed_rows)}"
    elif not product_count:
        error = error or "empty_products"

    return {
        "method": method,
        "status_code": status_code,
        "seconds": seconds,
        "bytes": len(text or ""),
        "products": product_count,
        "parsed_rows": parsed_rows,
        "pagination_page": payload_page,
        "pagination_size": payload_size,
        "error": error,
    }


def skipped_browser():
    return empty_result("browser_graphql", "skipped", 0)


def empty_result(method, error, seconds, status_code=0, bytes_count=0):
    return {
        "method": method,
        "status_code": status_code,
        "seconds": seconds,
        "bytes": bytes_count,
        "products": 0,
        "parsed_rows": 0,
        "pagination_page": 0,
        "pagination_size": 0,
        "error": error,
    }


def decide_verdict(direct, browser):
    if direct["status_code"] == 200 and not direct["error"] and direct["parsed_rows"] >= MIN_DIRECT_ROWS:
        return "direct_ok"
    if browser["status_code"] == 200 and not browser["error"] and browser["parsed_rows"] >= MIN_BROWSER_ROWS:
        return "browser_ok"
    if direct["status_code"] == 403 and browser["status_code"] == 403:
        return "blocked"
    if "page_mismatch" in (direct["error"] + browser["error"]):
        return "page_mismatch"
    if 0 < max(direct["parsed_rows"], browser["parsed_rows"]) < MIN_DIRECT_ROWS:
        return "partial_rows"
    return "unstable"


def prefix(name, values):
    return {f"{name}_{key}": value for key, value in values.items()}


def elapsed(started):
    return round(time.perf_counter() - started, 3)


def as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def write_csv(path, rows):
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
