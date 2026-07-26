import csv
import os
from collections import Counter
from pathlib import Path

from .detail_publish import detail_consumer_guard
from .step00_config import csv_rows_contract_error, db_connect, output_table, product_line, read_csv, run_root, write_json
from .step15_final_output import _active_retailer, _validate_source_context, final_output_columns


INTEGER_COLUMNS = {"main_rank", "bsr_rank"}
TRUE_VALUES = {"1", "true", "yes", "y"}
RETAILER_ACCOUNT_KEYS = {
    "magalu": ("magalu", "magazineluiza"),
    "casas_bahia": ("casasbahia", "casasbahiacombr"),
}


def main():
    root = run_root()
    with detail_consumer_guard(root):
        return _main(root)


def _main(root):
    csv_path = os.getenv("SEDA_DB_LOAD_CSV", str(root / "output" / "final_output.csv"))
    rows = read_csv(csv_path)
    _validate_source_context(rows, csv_path)
    _validate_db_csv_schema(rows, csv_path)
    truncate_requested = _env_flag("SEDA_DB_TRUNCATE_BEFORE_LOAD")
    replace_retailer = _env_flag("SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD")
    if truncate_requested and replace_retailer:
        raise RuntimeError("db_load_conflicting_replace_modes:truncate_and_replace_retailer")
    try:
        from psycopg2.extras import execute_values
    except ImportError as exc:
        raise SystemExit("psycopg2 is required for step14_db_load") from exc

    table = output_table()
    inserted = 0
    deleted = 0
    mode = "replace_retailer" if replace_retailer else "truncate" if truncate_requested else "append"
    if rows:
        columns = list(rows[0].keys())
        columns_sql = ", ".join(f'"{column}"' for column in columns)
        values = [[_db_value(column, row.get(column, "")) for column in columns] for row in rows]
        sql = f"INSERT INTO {table} ({columns_sql}) VALUES %s"
        with db_connect() as conn:
            with conn.cursor() as cur:
                if truncate_requested:
                    cur.execute(f"TRUNCATE TABLE {table}")
                elif replace_retailer:
                    deleted = _delete_retailer_rows(
                        cur,
                        table,
                        _active_retailer(),
                        product_line(),
                    )
                execute_values(cur, sql, values)
                inserted = len(values)
    output = root / "db" / "manifest_db_load.json"
    write_json(
        output,
        {
            "success": True,
            "table": table,
            "csv_path": csv_path,
            "inserted": inserted,
            "deleted": deleted,
            "mode": mode,
        },
    )
    print(f"[seda] loaded table={table} rows={inserted} mode={mode} deleted={deleted}")


def _env_flag(name):
    return str(os.getenv(name, "0")).strip().lower() in TRUE_VALUES


def _delete_retailer_rows(cursor, table, retailer, product_line_value):
    keys = RETAILER_ACCOUNT_KEYS.get(str(retailer or "").strip().lower())
    if not keys:
        raise RuntimeError(f"db_load_replace_retailer_unknown:{retailer or 'blank'}")
    line = str(product_line_value or "").strip().upper()
    if line not in {"TV", "REF", "LDY"}:
        raise RuntimeError(f"db_load_replace_product_line_unknown:{line or 'blank'}")
    placeholders = ", ".join(["%s"] * len(keys))
    cursor.execute(
        f'DELETE FROM {table} '
        'WHERE regexp_replace(lower(coalesce("account_name", \'\')), \'[^a-z]\', \'\', \'g\') '
        f'IN ({placeholders}) '
        'AND upper(trim(coalesce("product", \'\'))) = %s',
        (*keys, line),
    )
    return cursor.rowcount if isinstance(cursor.rowcount, int) and cursor.rowcount >= 0 else 0


def _validate_db_csv_schema(rows, csv_path):
    """Fail before importing the DB driver when an internal CSV is selected."""
    path = Path(csv_path)
    fieldnames = list(rows[0].keys()) if rows else []
    if path.is_file():
        try:
            with path.open('r', encoding='utf-8-sig', newline='') as handle:
                fieldnames = csv.DictReader(handle).fieldnames or []
        except OSError as exc:
            raise RuntimeError(f'db_load_schema_unreadable:source={csv_path}') from exc
    duplicates = sorted(
        str(column)
        for column, count in Counter(fieldnames).items()
        if count > 1
    )
    if duplicates:
        raise RuntimeError(
            'db_load_schema_duplicate_columns:'
            f'columns={",".join(duplicates)}:source={csv_path}'
        )
    expected = set(final_output_columns())
    actual = set(fieldnames)
    for row in rows:
        actual.update(row.keys())
    missing = sorted(expected - actual)
    extra = sorted(str(column) for column in actual - expected)
    if missing or extra:
        missing_text = ",".join(missing) or "none"
        extra_text = ",".join(extra) or "none"
        raise RuntimeError(
            "db_load_schema_mismatch:"
            f"missing={missing_text}:extra={extra_text}:source={csv_path}"
        )
    row_error = csv_rows_contract_error(rows, final_output_columns())
    if row_error:
        raise RuntimeError(f"db_load_schema_malformed:{row_error}:source={csv_path}")


def _db_value(column, value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    if column in INTEGER_COLUMNS:
        try:
            return int(text)
        except ValueError:
            return None
    return text


if __name__ == "__main__":
    main()
