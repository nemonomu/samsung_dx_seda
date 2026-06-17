import csv
import os
from pathlib import Path

from .step00_config import OUTPUT_COLUMNS, PROJECT_ROOT, product_line, write_json


def _rows_from_erd(path):
    try:
        import openpyxl
    except ImportError as exc:
        raise SystemExit("openpyxl is required for step00_erd_schema") from exc

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook["SEDA 데이터셋"]
    rows = []
    for row in sheet.iter_rows(min_row=9, values_only=True):
        values = list(row)
        if len(values) < 10 or values[2] != "SEDA" or str(values[3]).strip().upper() != product_line():
            continue
        rows.append(
            {
                "no": values[1],
                "country": values[2],
                "product_line": values[3],
                "category": values[4],
                "retailer": values[5],
                "page": values[6],
                "schema": values[7],
                "data_kor": values[8],
                "info_kor": values[9],
            }
        )
    return rows


def main():
    erd_path = Path(os.getenv("SEDA_ERD_PATH", PROJECT_ROOT / "erd.xlsx"))
    if not erd_path.exists():
        raise SystemExit(f"ERD file not found: {erd_path}")
    output_dir = Path(os.getenv("SEDA_CONFIG_OUTPUT_DIR", PROJECT_ROOT / "seda" / "config"))
    rows = _rows_from_erd(erd_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "seda_erd_fields.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["no", "country", "product_line", "category", "retailer", "page", "schema", "data_kor", "info_kor"],
        )
        writer.writeheader()
        writer.writerows(rows)

    write_json(
        output_dir / "seda_erd_schema.json",
        {
            "erd_path": str(erd_path),
            "field_count": len(rows),
            "retailers": sorted({row["retailer"] for row in rows if row.get("retailer")}),
            "output_columns": OUTPUT_COLUMNS,
            "fields": rows,
        },
    )
    print(f"[seda] wrote {csv_path} rows={len(rows)}")


if __name__ == "__main__":
    main()
