import argparse
import csv
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from seda.step00_config import product_line, run_date, run_root, write_json


def main():
    args = parse_args()
    root = Path(args.run_root or run_root()).resolve()
    checks = []
    facts = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "product_line": product_line(),
        "run_date": run_date(),
        "run_root": str(root),
        "env": _env_state(),
    }

    target_path = root / "output" / "seda_final_targets.csv"
    main_targets_path = root / "output" / "seda_main_targets.csv"
    main_occurrences_path = root / "main" / "parsed" / "main_occurrences.csv"
    bsr_rank_path = root / "bsr" / "parsed" / "bsr_rank_map.csv"
    enriched_path = root / "output" / "final_output_enriched.csv"
    final_path = root / "output" / "final_output.csv"

    facts["files"] = {name: _file_fact(path) for name, path in {
        "main_occurrences": main_occurrences_path,
        "main_targets": main_targets_path,
        "bsr_rank_map": bsr_rank_path,
        "final_targets": target_path,
        "final_output_enriched": enriched_path,
        "final_output": final_path,
    }.items()}

    final_targets = _read_csv(target_path)
    main_targets = _read_csv(main_targets_path)
    bsr_rank_rows = _read_csv(bsr_rank_path)
    enriched_rows = _read_csv(enriched_path)
    final_rows = _read_csv(final_path)

    facts["counts"] = {
        "main_targets": len(main_targets),
        "bsr_rank_map": len(bsr_rank_rows),
        "final_targets": len(final_targets),
        "final_output_enriched": len(enriched_rows),
        "final_output": len(final_rows),
    }
    facts["rank_summary"] = _rank_summary(final_targets)
    facts["seller_summary"] = _seller_summary(final_targets)

    _check(checks, root.exists(), "run_root_exists", f"run root exists: {root}", f"run root missing: {root}")
    _check(checks, bool(final_targets), "final_targets_exists", f"final targets rows={len(final_targets)}", f"missing or empty: {target_path}")
    _check(checks, len(final_targets) >= args.min_final_targets, "final_targets_min_rows", f"final targets >= {args.min_final_targets}", f"final targets rows {len(final_targets)} < {args.min_final_targets}")
    _check(checks, len(main_targets) >= args.min_main_targets, "main_targets_min_rows", f"main targets >= {args.min_main_targets}", f"main targets rows {len(main_targets)} < {args.min_main_targets}")
    _check(checks, len(bsr_rank_rows) >= args.min_bsr_ranks, "bsr_rank_min_rows", f"bsr rank rows >= {args.min_bsr_ranks}", f"bsr rank rows {len(bsr_rank_rows)} < {args.min_bsr_ranks}")

    rank_summary = facts["rank_summary"]
    _check(checks, rank_summary["main_rank_count"] >= args.min_main_ranks and rank_summary["main_rank_duplicates"] == 0, "main_rank_shape", f"main_rank count={rank_summary['main_rank_count']} duplicates=0", f"main_rank count={rank_summary['main_rank_count']} duplicates={rank_summary['main_rank_duplicates']}")
    _check(checks, rank_summary["bsr_rank_count"] >= args.min_bsr_ranks and rank_summary["bsr_rank_duplicates"] == 0, "bsr_rank_shape", f"bsr_rank count={rank_summary['bsr_rank_count']} duplicates=0", f"bsr_rank count={rank_summary['bsr_rank_count']} duplicates={rank_summary['bsr_rank_duplicates']}")

    detail_limit = _int_env("SEDA_DETAIL_LIMIT")
    detail_skip = _int_env("SEDA_DETAIL_SKIP")
    if args.full_run:
        _check(checks, detail_limit == 0, "detail_limit_full_run", "SEDA_DETAIL_LIMIT is unset/0", f"SEDA_DETAIL_LIMIT={detail_limit}; full run would be limited")
        _check(checks, detail_skip == 0, "detail_skip_full_run", "SEDA_DETAIL_SKIP is unset/0", f"SEDA_DETAIL_SKIP={detail_skip}; full run would skip rows")

    partial_enriched = bool(enriched_rows) and bool(final_targets) and len(enriched_rows) != len(final_targets)
    if partial_enriched:
        message = f"final_output_enriched rows={len(enriched_rows)} but final_targets rows={len(final_targets)}"
        _check(checks, args.allow_partial_enriched, "partial_enriched_guard", "partial enriched allowed by flag", message)
    else:
        _check(checks, True, "partial_enriched_guard", "no partial enriched conflict", "")

    if final_rows:
        final_rank_summary = _rank_summary(final_rows)
        facts["final_output_rank_summary"] = final_rank_summary
        _check(checks, final_rank_summary["main_rank_duplicates"] == 0 and final_rank_summary["bsr_rank_duplicates"] == 0, "existing_final_rank_shape", "existing final output ranks have no duplicates", f"existing final output rank duplicates: main={final_rank_summary['main_rank_duplicates']} bsr={final_rank_summary['bsr_rank_duplicates']}")

    facts["checks"] = checks
    facts["success"] = all(item["ok"] for item in checks)
    out_dir = Path(args.output_dir or root / "log" / "preflight")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"magalu_full_run_preflight_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    write_json(out_path, facts)

    for item in checks:
        status = "OK" if item["ok"] else "FAIL"
        print(f"[preflight] {status} {item['name']}: {item['message']}")
    print(f"[preflight] wrote {out_path}")
    if not facts["success"]:
        raise SystemExit(2)


def parse_args():
    parser = argparse.ArgumentParser(description="Validate Magalu run artifacts before a full detail/final run.")
    parser.add_argument("--run-root", default="", help="Override run root. Defaults to seda.step00_config.run_root().")
    parser.add_argument("--output-dir", default="", help="Directory for the preflight JSON report.")
    parser.add_argument("--min-final-targets", type=int, default=300)
    parser.add_argument("--min-main-targets", type=int, default=300)
    parser.add_argument("--min-main-ranks", type=int, default=300)
    parser.add_argument("--min-bsr-ranks", type=int, default=100)
    parser.add_argument("--full-run", action="store_true", help="Fail if detail limit/skip env vars would make this a partial run.")
    parser.add_argument("--allow-partial-enriched", action="store_true", help="Allow existing final_output_enriched.csv row count to differ from final targets.")
    return parser.parse_args()


def _read_csv(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _file_fact(path):
    path = Path(path)
    return {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
    }


def _rank_summary(rows):
    main = [_to_int(row.get("main_rank")) for row in rows]
    bsr = [_to_int(row.get("bsr_rank")) for row in rows]
    main = [value for value in main if value is not None]
    bsr = [value for value in bsr if value is not None]
    return {
        "main_rank_count": len(main),
        "main_rank_min": min(main) if main else None,
        "main_rank_max": max(main) if main else None,
        "main_rank_unique": len(set(main)),
        "main_rank_duplicates": len(main) - len(set(main)),
        "bsr_rank_count": len(bsr),
        "bsr_rank_min": min(bsr) if bsr else None,
        "bsr_rank_max": max(bsr) if bsr else None,
        "bsr_rank_unique": len(set(bsr)),
        "bsr_rank_duplicates": len(bsr) - len(set(bsr)),
    }


def _seller_summary(rows):
    counter = Counter((row.get("seller_id") or "").strip() for row in rows)
    return {
        "blank": counter.get("", 0),
        "nonblank": sum(count for seller, count in counter.items() if seller),
        "top": counter.most_common(10),
    }


def _env_state():
    keys = [
        "SEDA_RUN_ROOT",
        "SEDA_RUN_DATE",
        "SEDA_PRODUCT_LINE",
        "SEDA_FETCH_MODE",
        "SEDA_DETAIL_LIMIT",
        "SEDA_DETAIL_SKIP",
        "SEDA_MAGALU_SHIPPING_BLANK_RETRY_LIMIT",
        "SEDA_DB_INSERT_ENABLED",
        "SEDA_EMAIL_NOTIFY",
        "SEDA_EMAIL_DRY_RUN",
    ]
    return {key: ("<set>" if key in os.environ and os.environ.get(key) else "") for key in keys}


def _int_env(key):
    try:
        return int(str(os.getenv(key, "0") or "0"))
    except ValueError:
        return 0


def _to_int(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _check(checks, condition, name, ok_message, fail_message):
    checks.append({"name": name, "ok": bool(condition), "message": ok_message if condition else fail_message})


if __name__ == "__main__":
    main()
