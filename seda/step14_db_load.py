import os

from .step00_config import db_connect, output_table, read_csv, run_root, write_json


INTEGER_COLUMNS = {"main_rank", "bsr_rank"}


def main():
    try:
        from psycopg2.extras import execute_values
    except ImportError as exc:
        raise SystemExit("psycopg2 is required for step14_db_load") from exc

    root = run_root()
    csv_path = os.getenv("SEDA_DB_LOAD_CSV", str(root / "output" / "final_output.csv"))
    rows = read_csv(csv_path)
    table = output_table()
    inserted = 0
    if rows:
        columns = list(rows[0].keys())
        columns_sql = ", ".join(f'"{column}"' for column in columns)
        values = [[_db_value(column, row.get(column, "")) for column in columns] for row in rows]
        sql = f"INSERT INTO {table} ({columns_sql}) VALUES %s"
        with db_connect() as conn:
            with conn.cursor() as cur:
                if os.getenv("SEDA_DB_TRUNCATE_BEFORE_LOAD", "0").lower() in {"1", "true", "yes", "y"}:
                    cur.execute(f"TRUNCATE TABLE {table}")
                execute_values(cur, sql, values)
                inserted = len(values)
    output = root / "db" / "manifest_db_load.json"
    write_json(output, {"success": True, "table": table, "csv_path": csv_path, "inserted": inserted})
    print(f"[seda] loaded table={table} rows={inserted}")


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
