import json

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


def main():
    root = run_root()
    rows = read_csv(root / "output" / "final_output.csv")
    review_rows = []
    for row in rows:
        for index, review in enumerate(_reviews(row.get("detailed_review_content")), start=1):
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
    output = root / "detail" / "parsed" / "review20_rows.csv"
    write_csv(output, review_rows, columns=REVIEW_COLUMNS)
    write_json(root / "detail" / "manifest_review20.json", {"success": True, "rows": len(review_rows)})
    print(f"[seda] wrote {output} rows={len(review_rows)}")


if __name__ == "__main__":
    main()
