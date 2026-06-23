import argparse
import json
import re
from collections import Counter
from pathlib import Path

from seda.step00_config import OUTPUT_COLUMNS, read_csv, run_root, write_csv

from .destaque_api import fetch_discount_type


def default_input():
    delivery_backfilled = run_root() / "output" / "final_output_delivery_backfilled.csv"
    if delivery_backfilled.exists():
        return str(delivery_backfilled)
    return str(run_root() / "output" / "final_output_enriched.csv")


def default_output():
    return str(run_root() / "output" / "final_output_badged.csv")


def run(args):
    rows = read_csv(args.input)
    stats = Counter(rows=len(rows))
    errors = []
    cache = {}
    limit = int(args.limit or 0)
    processed = 0

    for index, row in enumerate(rows, start=1):
        if limit and processed >= limit:
            stats.update(skipped_limit=1)
            continue
        if not args.force and str(row.get("discount_type") or "").strip():
            stats.update(skipped_existing=1)
            continue
        sku_id = _sku_id(row)
        seller_id = _seller_id(row)
        if not sku_id or not seller_id:
            stats.update(skipped_missing_keys=1)
            continue
        processed += 1
        cache_key = (sku_id, seller_id)
        if cache_key not in cache:
            cache[cache_key] = fetch_discount_type(sku_id, seller_id, timeout=args.timeout)
        result = cache[cache_key]
        if not result.get("success"):
            stats.update(failed=1)
            errors.append({"row": index, "sku_id": sku_id, "seller_id": seller_id, "error": result.get("error", "")})
            continue
        value = str(result.get("discount_type") or "").strip()
        if not value:
            stats.update(no_discount=1)
            continue
        row["discount_type"] = value
        row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_destaque_api")
        row["parse_status"] = _append_token(row.get("parse_status", ""), "discount_type_destaque_api")
        stats.update(updated=1)

    output = Path(args.output)
    write_csv(output, rows, columns=list(rows[0].keys()) if rows else OUTPUT_COLUMNS)
    manifest = {
        "input": args.input,
        "output": args.output,
        "stats": dict(stats),
        "errors": errors[:100],
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _sku_id(row):
    for key in ("retailer_sku_id", "idSku", "sku_id"):
        value = str(row.get(key) or "").strip()
        if value.isdigit():
            return value
    url_value = _sku_from_url(row.get("product_url", ""))
    if url_value:
        return url_value
    for key in ("item", "retailer_product_id"):
        value = str(row.get(key) or "").strip()
        if value.isdigit():
            return value
    return ""


def _seller_id(row):
    for key in ("seller_id", "lojista", "sellerId"):
        value = str(row.get(key) or "").strip()
        if value.isdigit():
            return value
    return "10037"


def _sku_from_url(url):
    match = re.search(r"/p/([^/?#]+)", str(url or ""))
    return match.group(1) if match and match.group(1).isdigit() else ""


def _append_token(value, token):
    tokens = [part for part in str(value or "").split("+") if part]
    if token not in tokens:
        tokens.append(token)
    return "+".join(tokens)


def main():
    parser = argparse.ArgumentParser(description="Backfill Casas Bahia listing discount_type through the direct destaque API.")
    parser.add_argument("--input", default=default_input())
    parser.add_argument("--output", default=default_output())
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({"stats": result.get("stats", {}), "output": result.get("output", args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
