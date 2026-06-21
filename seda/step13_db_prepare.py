from .step00_config import db_connect, output_table, write_json, run_root
from .step15_final_output import final_output_columns


INTEGER_COLUMNS = {"main_rank", "bsr_rank"}


def main():
    table = output_table()
    columns = final_output_columns()
    columns_sql = ",\n".join(f'"{column}" {_column_type(column)}' for column in columns)
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
    write_json(output, {"success": True, "table": table, "columns": columns})
    print(f"[seda] prepared table {table}")


def _column_type(column):
    return "integer" if column in INTEGER_COLUMNS else "text"


if __name__ == "__main__":
    main()
