import argparse
import csv
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from seda.parsers import clean_text, extract_next_data, sku_from_url


DEFAULT_LIMIT = 20
BASE_DIR = Path(__file__).resolve().parents[1] / "data" / "magalu"


def main():
    args = parse_args()
    rows = load_targets(args)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("[probe] no targets found")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir or BASE_DIR / f"review_20_probe_{stamp}")
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for index, row in enumerate(rows, start=1):
        product_url = row.get("product_url", "").strip()
        item = row.get("item") or sku_from_url(product_url) or row.get("sku", "")
        print(f"[probe] {index}/{len(rows)} item={safe(item)}", flush=True)
        result = probe_one(product_url, row, args)
        results.append(result)
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)

    csv_path = output_dir / "review_20_probe.csv"
    json_path = output_dir / "review_20_probe.json"
    write_csv(csv_path, results)
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for item in results if item.get("status") == "ok")
    partial = sum(1 for item in results if item.get("status") == "partial")
    fail = sum(1 for item in results if item.get("status") == "fail")
    print(f"[probe] wrote {csv_path}")
    print(f"[probe] wrote {json_path}")
    print(f"[probe] summary ok={ok} partial={partial} fail={fail} total={len(results)}")


def parse_args():
    parser = argparse.ArgumentParser(description="Probe whether Magalu review HTML/Next data can cover up to 20 review bodies.")
    parser.add_argument("--input-csv", default=os.getenv("SEDA_REVIEW_PROBE_INPUT_CSV", ""))
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--url-file", default=os.getenv("SEDA_REVIEW_PROBE_URL_FILE", ""))
    parser.add_argument("--output-dir", default=os.getenv("SEDA_REVIEW_PROBE_OUTPUT_DIR", ""))
    parser.add_argument("--limit", type=int, default=int(os.getenv("SEDA_REVIEW_PROBE_LIMIT", "0") or 0))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("SEDA_REVIEW_PROBE_MAX_PAGES", "4") or 4))
    parser.add_argument("--expected-limit", type=int, default=int(os.getenv("SEDA_MAGALU_REVIEW_LIMIT", str(DEFAULT_LIMIT)) or DEFAULT_LIMIT))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_REVIEW_PROBE_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")) or 60))
    parser.add_argument("--transport", choices=["auto", "requests", "browser"], default=os.getenv("SEDA_REVIEW_PROBE_TRANSPORT", "auto"))
    parser.add_argument("--use-zenrows", action="store_true", default=os.getenv("SEDA_REVIEW_PROBE_USE_ZENROWS", "0").lower() in {"1", "true", "yes", "y"})
    parser.add_argument("--sleep-seconds", type=float, default=float(os.getenv("SEDA_REVIEW_PROBE_SLEEP_SECONDS", "0.5") or 0.5))
    return parser.parse_args()


def load_targets(args):
    rows = []
    for url in args.url:
        if url.strip():
            rows.append({"product_url": url.strip()})
    if args.url_file:
        path = Path(args.url_file)
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append({"product_url": line})
    if args.input_csv:
        rows.extend(read_input_csv(Path(args.input_csv)))
    if not rows:
        latest = latest_targets_csv()
        if latest:
            print(f"[probe] using latest targets {latest}", flush=True)
            rows.extend(read_input_csv(latest))
    return dedupe_rows(rows)


def latest_targets_csv():
    patterns = [
        BASE_DIR / "*" / "*" / "output" / "seda_final_targets.csv",
        BASE_DIR / "*" / "output" / "seda_final_targets.csv",
        BASE_DIR / "*" / "*" / "output" / "final_output.csv",
        BASE_DIR / "*" / "output" / "final_output.csv",
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(Path(BASE_DIR).glob(str(pattern.relative_to(BASE_DIR))))
    candidates = [path for path in candidates if path.exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def read_input_csv(path):
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        return [row for row in reader if row.get("product_url")]


def dedupe_rows(rows):
    seen = set()
    out = []
    for row in rows:
        url = (row.get("product_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(row)
    return out


def probe_one(product_url, source_row, args):
    item_id = sku_from_url(product_url) or clean_text(source_row.get("item") or source_row.get("sku"))
    review_url = build_review_url(product_url)
    fetched_pages = []
    reviews = []
    seen = set()
    best_general = {}
    errors = []

    candidate_urls = [("pdp", product_url)]
    if review_url:
        candidate_urls.append(("review_page_1", review_url))
        for page in range(2, max(2, args.max_pages) + 1):
            candidate_urls.append((f"review_page_{page}", add_page_query(review_url, page)))

    for label, url in candidate_urls:
        fetch = fetch_html(url, args)
        page_info = {
            "label": label,
            "url": url,
            "transport": fetch.get("transport", ""),
            "status_code": fetch.get("status_code", 0),
            "length": len(fetch.get("text") or ""),
            "has_next_data": "__NEXT_DATA__" in (fetch.get("text") or ""),
            "error": fetch.get("error", ""),
        }
        product_rating, parse_error = product_rating_from_html(fetch.get("text") or "")
        if parse_error:
            page_info["parse_error"] = parse_error
        if product_rating:
            general = product_rating.get("general") if isinstance(product_rating.get("general"), dict) else {}
            if general and not best_general:
                best_general = general
            descriptions = review_descriptions(product_rating)
            page_info["page"] = ((product_rating.get("userReviews") or {}).get("page") or {})
            page_info["descriptions"] = len(descriptions)
            new_count = 0
            for description in descriptions:
                key = description.casefold()
                if key in seen:
                    continue
                seen.add(key)
                reviews.append(description)
                new_count += 1
                if len(reviews) >= args.expected_limit:
                    break
            page_info["new_descriptions"] = new_count
        fetched_pages.append(page_info)
        if fetch.get("error"):
            errors.append(f"{label}:{fetch.get('error')}")
        count_of_reviews = parse_count(best_general.get("commentCount") or source_row.get("count_of_reviews"))
        expected = expected_count(count_of_reviews, args.expected_limit)
        if expected > 0 and len(reviews) >= expected:
            break

    count_of_reviews = parse_count(best_general.get("commentCount") or source_row.get("count_of_reviews"))
    count_of_star_ratings = parse_count(best_general.get("reviewCount") or source_row.get("count_of_star_ratings"))
    expected = expected_count(count_of_reviews, args.expected_limit)
    actual = len(reviews)
    if expected == 0:
        status = "ok"
    elif actual >= expected:
        status = "ok"
    elif actual > 0:
        status = "partial"
    else:
        status = "fail"

    return {
        "product_url": product_url,
        "review_url": review_url,
        "item": item_id,
        "star_rating": clean_text(best_general.get("rating") or source_row.get("star_rating", "")),
        "count_of_star_ratings": count_of_star_ratings,
        "count_of_reviews": count_of_reviews,
        "expected_review_count": expected,
        "actual_review_count": actual,
        "status": status,
        "method_chain": "+".join(dict.fromkeys(page.get("transport", "") for page in fetched_pages if page.get("transport"))),
        "pages_fetched": len(fetched_pages),
        "page_labels": "+".join(page["label"] for page in fetched_pages),
        "errors": " | ".join(errors[:5]),
        "sample_reviews": json.dumps(reviews[:3], ensure_ascii=False),
        "all_reviews": json.dumps(reviews[: args.expected_limit], ensure_ascii=False),
        "page_trace": json.dumps(fetched_pages, ensure_ascii=False),
    }


def fetch_html(url, args):
    if args.transport in {"auto", "requests"}:
        result = fetch_requests(url, args.timeout)
        if is_usable_html(result.get("text", ""), result.get("status_code", 0)) or args.transport == "requests":
            return result
    if args.transport in {"auto", "browser"}:
        result = fetch_browser(url)
        if is_usable_html(result.get("text", ""), result.get("status_code", 0)) or args.transport == "browser":
            return result
    if args.use_zenrows:
        return fetch_zenrows(url, args.timeout)
    return result if "result" in locals() else {"transport": "none", "status_code": 0, "text": "", "error": "not_attempted"}


def fetch_requests(url, timeout):
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        return {"transport": "requests", "status_code": response.status_code, "text": response.text, "error": ""}
    except Exception as exc:
        return {"transport": "requests", "status_code": 0, "text": "", "error": f"{type(exc).__name__}: {exc}"}


def fetch_browser(url):
    try:
        from seda.magalu.browser_session import fetch_html as browser_fetch_html

        result = browser_fetch_html(url)
        return {
            "transport": "browser_fetch",
            "status_code": int(result.get("status_code") or 0),
            "text": result.get("text") or "",
            "error": result.get("error") or "",
        }
    except Exception as exc:
        return {"transport": "browser_fetch", "status_code": 0, "text": "", "error": f"{type(exc).__name__}: {exc}"}


def fetch_zenrows(url, timeout):
    try:
        from seda.magalu.zenrows_client import fetch_next_data_html

        result = fetch_next_data_html(url, timeout=timeout)
        return {
            "transport": f"zenrows:{result.profile}:{result.estimated_multiplier}",
            "status_code": int(result.status_code or 0),
            "text": result.text or "",
            "error": result.error or "",
        }
    except Exception as exc:
        return {"transport": "zenrows", "status_code": 0, "text": "", "error": f"{type(exc).__name__}: {exc}"}


def is_usable_html(text, status_code):
    lower = (text or "").lower()
    if status_code != 200:
        return False
    if "__next_data__" in lower and "productrating" in lower:
        return True
    return False


def product_rating_from_html(html_text):
    if not html_text or "__NEXT_DATA__" not in html_text:
        return {}, "missing_next_data"
    try:
        data = extract_next_data(html_text)
    except Exception as exc:
        return {}, f"next_data_error:{type(exc).__name__}"
    page_data = ((data.get("props") or {}).get("pageProps") or {}).get("data") or {}
    product_rating = page_data.get("productRating") if isinstance(page_data.get("productRating"), dict) else {}
    if not product_rating:
        return {}, "missing_product_rating"
    return product_rating, ""


def review_descriptions(product_rating):
    user_reviews = product_rating.get("userReviews") if isinstance(product_rating.get("userReviews"), dict) else {}
    descriptions = []
    for item in user_reviews.get("items") or []:
        if not isinstance(item, dict):
            continue
        description = clean_text(item.get("description"))
        if description:
            descriptions.append(description)
    return descriptions


def build_review_url(product_url):
    parsed = urlsplit(product_url)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        p_index = parts.index("p")
    except ValueError:
        return ""
    if p_index < 1 or len(parts) <= p_index + 3:
        return ""
    slug = parts[p_index - 1]
    item = parts[p_index + 1]
    category = parts[p_index + 2].upper()
    subcategory = parts[p_index + 3].upper()
    path = f"/review/{item}/{slug}/{category}/{subcategory}/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def add_page_query(url, page):
    parsed = urlsplit(url)
    query = f"page={page}"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def parse_count(value):
    text = clean_text(value)
    if not text:
        return -1
    text = text.replace(".", "").replace(",", ".")
    try:
        return int(float(text))
    except ValueError:
        return -1


def expected_count(count_of_reviews, limit):
    if count_of_reviews < 0:
        return limit
    return min(limit, count_of_reviews)


def write_csv(path, rows):
    columns = [
        "product_url",
        "review_url",
        "item",
        "star_rating",
        "count_of_star_ratings",
        "count_of_reviews",
        "expected_review_count",
        "actual_review_count",
        "status",
        "method_chain",
        "pages_fetched",
        "page_labels",
        "errors",
        "sample_reviews",
        "all_reviews",
        "page_trace",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def safe(value):
    return str(value or "").encode("ascii", "backslashreplace").decode("ascii")


if __name__ == "__main__":
    main()
