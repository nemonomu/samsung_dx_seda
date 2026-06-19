import json
import os
from pathlib import Path

from .step00_config import read_csv, run_root, write_csv, write_json


REVIEW_COLUMNS = ["retailer", "sku", "product_url", "review_rank", "detailed_review_content"]
DELIMITER = " ||| "


def _reviews(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except ValueError:
        return [part.strip() for part in str(value).split(DELIMITER) if part.strip()]


def _source_path(root):
    override = os.getenv("SEDA_REVIEW20_SOURCE_CSV", "").strip()
    if override:
        return Path(override)
    for candidate in (
        root / "output" / "final_output_badged.csv",
        root / "output" / "final_output_delivery_backfilled.csv",
        root / "output" / "final_output_enriched.csv",
        root / "output" / "final_output.csv",
    ):
        if candidate.exists():
            return candidate
    return root / "output" / "seda_final_targets.csv"


def _metric_int(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(".", "").replace(",", ".")
    try:
        return int(float(text))
    except ValueError:
        return None


def _expected_review_count(row):
    limit = int(os.getenv("SEDA_MAGALU_REVIEW_LIMIT", "20"))
    count = _metric_int(row.get("count_of_reviews"))
    if count is None:
        return 0
    return min(limit, max(0, count))


def main():
    root = run_root()
    source = _source_path(root)
    rows = read_csv(source)
    review_rows = []
    short_rows = []
    no_count_rows = 0
    for row in rows:
        reviews = _reviews(row.get("detailed_review_content"))
        expected = _expected_review_count(row)
        if expected == 0 and not row.get("count_of_reviews"):
            no_count_rows += 1
        if expected and len(reviews) < expected:
            short_rows.append(
                {
                    "retailer": row.get("retailer", ""),
                    "sku": row.get("sku", ""),
                    "product_url": row.get("product_url", ""),
                    "count_of_reviews": row.get("count_of_reviews", ""),
                    "expected_review_count": expected,
                    "actual_review_count": len(reviews),
                    "parse_status": row.get("parse_status", ""),
                    "fetch_method": row.get("fetch_method", ""),
                }
            )
        for index, review in enumerate(reviews, start=1):
            if index > 20:
                break
            review_rows.append(
                {
                    "retailer": row.get("retailer", ""),
                    "sku": row.get("sku", ""),
                    "product_url": row.get("product_url", ""),
                    "review_rank": index,
                    "detailed_review_content": review,
                }
            )
    parsed_dir = root / "detail" / "parsed"
    output = parsed_dir / "review20_rows.csv"
    short_output = parsed_dir / "review20_short_rows.csv"
    write_csv(output, review_rows, columns=REVIEW_COLUMNS)
    short_columns = [
        "retailer",
        "sku",
        "product_url",
        "count_of_reviews",
        "expected_review_count",
        "actual_review_count",
        "parse_status",
        "fetch_method",
    ]
    write_csv(short_output, short_rows, columns=short_columns)
    manifest = {
        "success": True,
        "source": str(source),
        "input_rows": len(rows),
        "review_rows": len(review_rows),
        "short_rows": len(short_rows),
        "no_count_rows": no_count_rows,
        "output": str(output),
        "short_output": str(short_output),
    }
    write_json(root / "detail" / "manifest_review20.json", manifest)
    print(f"[seda] wrote {output} rows={len(review_rows)}")
    print(f"[seda] wrote {short_output} rows={len(short_rows)}")


if __name__ == "__main__":
    main()
