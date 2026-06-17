from .step00_config import OUTPUT_COLUMNS, db_connect, output_table, write_json, run_root


def main():
    table = output_table()
    columns_sql = ",\n".join(f'"{column}" text' for column in OUTPUT_COLUMNS)
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {table} (
        id bigserial PRIMARY KEY,
        {columns_sql},
        loaded_at timestamptz DEFAULT now()
    )
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
    output = run_root() / "db" / "manifest_db_prepare.json"
    write_json(output, {"success": True, "table": table, "columns": OUTPUT_COLUMNS})
    print(f"[seda] prepared table {table}")


if __name__ == "__main__":
    main()
