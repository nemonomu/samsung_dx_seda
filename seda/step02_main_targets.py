import os

from .step00_config import OUTPUT_COLUMNS, product_identity, read_csv, run_root, write_csv


def main():
    root = run_root()
    target_size = int(os.getenv("SEDA_TARGET_SIZE", "300"))
    source = root / "main" / "parsed" / "main_occurrences.csv"
    rows = read_csv(source)
    selected = []
    seen = set()
    for row in rows:
        key = product_identity(row)
        if key in seen:
            continue
        seen.add(key)
        row["main_rank"] = len(selected) + 1
        selected.append(row)
        if len(selected) >= target_size:
            break
    output = root / "output" / "seda_main_targets.csv"
    write_csv(output, selected, columns=OUTPUT_COLUMNS)
    print(f"[seda] wrote {output} rows={len(selected)} source_rows={len(rows)} target_size={target_size}")


if __name__ == "__main__":
    main()
