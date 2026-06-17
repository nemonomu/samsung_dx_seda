import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from seda.step00_config import DEFAULT_COUNTRY, DEFAULT_PRODUCT_LINE, OUTPUT_COLUMNS


IMAGE_HOSTS = {"a-static.mlcdn.com.br", "i.mlcdn.com.br", "assets.mlcdn.com.br"}


def slug_to_name(slug):
    return " ".join(str(slug or "").replace("-", " ").split())


def looks_like_tv(slug):
    value = str(slug or "").lower()
    return any(token in value for token in ["smart-tv", "tv-", "-tv", "televisor", "qled", "crystal-uhd"])


def product_url(slug, seller, product_id):
    base = f"https://www.magazineluiza.com.br/{slug}/p/{product_id}/"
    if seller:
        return f"{base}?seller_id={seller}"
    return base


def extract_from_har(path):
    har = json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    rows = []
    seen = set()
    now = datetime.now().isoformat(timespec="seconds")
    for entry_index, entry in enumerate(har.get("log", {}).get("entries", [])):
        url = entry.get("request", {}).get("url", "")
        parsed = urlparse(url)
        if parsed.netloc not in IMAGE_HOSTS:
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 5 or not re.match(r"^\d+x\d+$", parts[0]):
            continue
        slug, seller, product_id = parts[1], parts[2], parts[3]
        if not looks_like_tv(slug):
            continue
        key = (slug, seller, product_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "retailer": "Magalu",
                "country": DEFAULT_COUNTRY,
                "product_line": DEFAULT_PRODUCT_LINE,
                "category": "Retail.com",
                "main_rank": len(rows) + 1,
                "product_url": product_url(slug, seller, product_id),
                "retailer_sku_name": slug_to_name(unquote(slug)),
                "sku": product_id,
                "source_url": unquote(url),
                "crawl_datetime": now,
                "fetch_method": "har_image_requests",
                "parse_status": f"har_image_entry:{entry_index}",
            }
        )
    return rows


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in OUTPUT_COLUMNS})


def main():
    parser = argparse.ArgumentParser(description="Extract Magalu listing candidates from HAR image request URLs.")
    parser.add_argument("har_path")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    rows = extract_from_har(args.har_path)
    output = args.output or str(Path(args.har_path).with_name(f"{Path(args.har_path).stem}_magalu_listing_candidates.csv"))
    write_csv(output, rows)
    print(f"[seda] wrote {output} rows={len(rows)}")


if __name__ == "__main__":
    main()
