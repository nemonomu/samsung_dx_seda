from .step00_config import OUTPUT_COLUMNS, normalized_product_url, product_identity, read_csv, run_root, write_csv


def _key(row):
    return product_identity(row)


def _rank_key(row):
    url = normalized_product_url(row.get("product_url", ""))
    return (row.get("retailer"), url) if url else ("", "")


def main():
    root = run_root()
    targets = read_csv(root / "output" / "seda_main_targets.csv")
    bsr_rank_rows = read_csv(root / "bsr" / "parsed" / "bsr_rank_map.csv")
    bsr_occurrences = read_csv(root / "bsr" / "parsed" / "main_occurrences.csv")
    bsr_by_key = {}
    bsr_rank_keys = set()
    for row in bsr_rank_rows:
        bsr_rank_keys.add(_key(row))
        key = _rank_key(row)
        if key[1]:
            bsr_by_key.setdefault(key, row.get("bsr_rank", ""))

    final_rows = []
    seen = set()
    for row in targets:
        row_key = _key(row)
        if row_key in seen:
            continue
        row["bsr_rank"] = row.get("bsr_rank") or bsr_by_key.get(_rank_key(row), "")
        seen.add(row_key)
        final_rows.append(row)

    for row in bsr_occurrences:
        row_key = _key(row)
        if row_key in seen or row_key not in bsr_rank_keys:
            continue
        row["main_rank"] = ""
        row["bsr_rank"] = bsr_by_key.get(_rank_key(row), row.get("bsr_rank", ""))
        seen.add(row_key)
        final_rows.append(row)

    output = root / "output" / "seda_final_targets.csv"
    write_csv(output, final_rows, columns=OUTPUT_COLUMNS)
    print(f"[seda] wrote {output} rows={len(final_rows)}")


if __name__ == "__main__":
    main()
