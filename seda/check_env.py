"""Pre-run .env sanity check for Magalu full runs (no secret values printed).

Verifies the two things that silently break runs per machine:
  1. DB_CONFIG parses and actually has a database/dbname key
     (a leading '#' on that line makes ast.literal_eval drop it -> DB load fails).
  2. SEDA_DB_FINAL_TABLE_REF / _LDY resolve to the dx_seda tables
     (missing -> REF/LDY load into the wrong table).
Also reports whether SEDA_MAGALU_BROWSER_GRAPHQL is on (itemQuery needs it).

Run on the target machine (e.g. RDP):  python -m seda.check_env
Prints PASS/FAIL per check. Exit code 0 = all critical checks pass, 1 = problem.
Only booleans / table names / masked lengths are printed -- no host/user/password/db value.
"""
import os

from .step00_config import db_config, output_table, env_candidate_paths

EXPECTED = {
    "TV": "dx_seda.dx_seda_tv_retail_com",
    "REF": "dx_seda.dx_seda_ref_retail_com",
    "LDY": "dx_seda.dx_seda_ldy_retail_com",
}


def _mask(value):
    n = len(str(value or ""))
    return f"<set, {n} chars>" if n else "<empty>"


def main():
    ok = True

    print("== .env discovery ==")
    found = None
    for path in env_candidate_paths():
        exists = path.exists()
        print(f"  {'FOUND ' if exists else 'absent'} {path}")
        if exists and found is None:
            found = path
    if found is None:
        print("  FAIL: no .env file found on any candidate path")
        ok = False

    print("\n== DB_CONFIG ==")
    cfg = db_config()
    if not cfg:
        print("  FAIL: DB_CONFIG missing or unparseable (empty dict)")
        ok = False
    else:
        for key in ("host", "port", "user", "password"):
            print(f"  {key:9s}: {'present' if cfg.get(key) not in (None, '') else 'MISSING'}")
        database = cfg.get("database") or cfg.get("dbname")
        if database:
            print(f"  database : present {_mask(database)}")
        else:
            print("  database : MISSING  <- likely a '#' on the database/dbname line, or not set")
            ok = False

    print("\n== output_table() resolution ==")
    saved = os.environ.get("SEDA_PRODUCT_LINE")
    try:
        for line, expected in EXPECTED.items():
            os.environ["SEDA_PRODUCT_LINE"] = line
            actual = output_table()
            mark = "OK " if actual == expected else "FAIL"
            if actual != expected:
                ok = False
            print(f"  {mark} {line:3s} -> {actual}    (expected {expected})")
    finally:
        if saved is None:
            os.environ.pop("SEDA_PRODUCT_LINE", None)
        else:
            os.environ["SEDA_PRODUCT_LINE"] = saved

    print("\n== GraphQL transport ==")
    bg = os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL", "")
    on = bg.lower() not in {"", "0", "false", "no", "n"}
    # Informational: batches set this themselves, but a stale ambient '0' would override nothing here.
    print(f"  SEDA_MAGALU_BROWSER_GRAPHQL (env): {bg or '<unset>'}  ->  {'on' if on else 'off (batch must set =1)'}")

    print("\n== RESULT ==")
    print("  PASS - safe to run" if ok else "  FAIL - fix the items above before running")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
