import csv
import os

from .step00_config import product_identity, read_csv, run_root


def main():
    root = run_root()
    target_size = int(os.getenv("SEDA_BSR_TARGET_SIZE", "100"))
    rows = read_csv(root / "bsr" / "parsed" / "main_occurrences.csv")
    output = root / "bsr" / "parsed" / "bsr_rank_map.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    selected = []
    seen = set()
    for row in rows:
        key = product_identity(row)
        if key in seen:
            continue
        seen.add(key)
        row = dict(row)
        row["bsr_rank"] = len(selected) + 1
        selected.append(row)
        if len(selected) >= target_size:
            break
    with output.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["retailer", "sku", "product_url", "bsr_rank"])
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    "retailer": row.get("retailer", ""),
                    "sku": row.get("sku", ""),
                    "product_url": row.get("product_url", ""),
                    "bsr_rank": row.get("bsr_rank", ""),
                }
            )
    print(f"[seda] wrote {output} rows={len(selected)} source_rows={len(rows)} target_size={target_size}")


if __name__ == "__main__":
    main()
