import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from seda.common.retailer_runner import configure_retailer, run_module, step_env
from seda.detail_publish import assert_detail_publish_complete
from seda.step00_config import csv_count, dated_run_root, product_line, run_root
from seda.zenrows_usage import start_execution as start_zenrows_usage_execution


TRUE_VALUES = {"1", "true", "yes", "y"}
DETAIL_CONSUMER_STEPS = frozenset(
    {
        "freight_cdp_backfill",
        "review20",
        "listing_discount_backfill",
        "final_output",
        "field_audit",
        "s3_sync",
        "db_prepare",
        "db_load",
    }
)


@dataclass(frozen=True)
class Step:
    number: int
    name: str
    module: str
    env: dict | None = None

    @property
    def key(self):
        return f"{self.number:02d}"


def steps_for(package_name):
    listing_env = _magalu_listing_env(package_name)
    steps = [
        Step(0, "erd_schema", f"{package_name}.step00_erd_schema"),
        Step(1, "main_list", f"{package_name}.step01_main_list", env=listing_env),
        Step(2, "main_targets", f"{package_name}.step02_main_targets"),
        Step(3, "bsr_list", f"{package_name}.step03_bsr_list", env=listing_env),
        Step(4, "bsr_rank", f"{package_name}.step04_bsr_rank"),
        Step(5, "final_targets", f"{package_name}.step07_final_targets"),
        Step(6, "detail_enrichment", f"{package_name}.step08_detail_enrichment"),
    ]
    next_number = 7
    if package_name.endswith(".casas_bahia"):
        steps.extend(
            [
                Step(next_number, "freight_cdp_backfill", "seda.casas_bahia.freight_cdp_backfill"),
                Step(next_number + 1, "review20", f"{package_name}.step09_review20"),
                Step(next_number + 2, "listing_discount_backfill", "seda.casas_bahia.listing_discount_backfill"),
            ]
        )
        next_number += 3
    else:
        steps.extend(
            [
                Step(next_number, "review20", f"{package_name}.step09_review20"),
            ]
        )
        next_number += 1
    steps.extend(
        [
            Step(next_number, "final_output", f"{package_name}.step15_final_output"),
            Step(next_number + 1, "field_audit", f"{package_name}.step16_field_audit"),
            Step(next_number + 2, "s3_sync", f"{package_name}.step11_s3_sync"),
            Step(next_number + 3, "db_prepare", f"{package_name}.step13_db_prepare"),
            Step(next_number + 4, "db_load", f"{package_name}.step14_db_load"),
            Step(next_number + 5, "status_check", f"{package_name}.step10_status_check"),
            Step(next_number + 6, "local_cleanup", f"{package_name}.step12_local_cleanup"),
        ]
    )
    return steps


def _magalu_listing_env(package_name):
    if not package_name.endswith(".magalu"):
        return None
    fetch_mode = os.getenv("SEDA_MAGALU_LISTING_FETCH_MODE", "").strip()
    if not fetch_mode:
        return None
    return {
        "SEDA_FETCH_MODE": fetch_mode,
        "SEDA_ALLOW_ZENROWS": os.getenv("SEDA_MAGALU_LISTING_ALLOW_ZENROWS", "1"),
        "SEDA_ZENROWS_DRY_RUN": os.getenv("SEDA_MAGALU_LISTING_ZENROWS_DRY_RUN", "0"),
        "SEDA_ZENROWS_LISTING_PROFILE": os.getenv("SEDA_MAGALU_LISTING_ZENROWS_PROFILE", "listing_js_full"),
        "SEDA_ZENROWS_TIMEOUT": os.getenv("SEDA_MAGALU_LISTING_ZENROWS_TIMEOUT", "180"),
    }


def step_by_key(steps, value):
    for step in steps:
        if value in {step.key, step.name, str(step.number)}:
            return step
    raise SystemExit(f"Unknown step: {value}")


def _requires_detail_completion(step):
    return step.name in DETAIL_CONSUMER_STEPS


def step_complete(step, *, recover=True):
    root = run_root()
    project_root = Path(__file__).resolve().parents[2]
    checks = {
        "erd_schema": (project_root / "seda" / "config" / "seda_erd_schema.json", "ERD schema"),
        "main_list": (root / "main" / "parsed" / "main_occurrences.csv", "main rows"),
        "main_targets": (root / "output" / "seda_main_targets.csv", "main targets"),
        "bsr_list": (root / "bsr" / "parsed" / "main_occurrences.csv", "bsr rows"),
        "bsr_rank": (root / "bsr" / "parsed" / "bsr_rank_map.csv", "bsr rank map"),
        "final_targets": (root / "output" / "seda_final_targets.csv", "final targets"),
        "detail_enrichment": (root / "output" / "final_output_enriched.csv", "enriched output"),
        "freight_cdp_backfill": (root / "output" / "final_output_delivery_backfilled.csv", "delivery backfilled output"),
        "review20": (root / "detail" / "manifest_review20.json", "review manifest"),
        "listing_discount_backfill": (root / "output" / "final_output_badged.csv", "listing discount backfilled output"),
        "final_output": (root / "output" / "final_output.csv", "final output"),
        "field_audit": (root / "output" / "field_audit_v2.json", "field audit"),
    }
    if step.name in checks:
        path, label = checks[step.name]
        if step.name == "detail_enrichment":
            try:
                journal = assert_detail_publish_complete(root, recover=recover)
            except RuntimeError as exc:
                return False, str(exc)
            status = str(journal.get("status") or "none")
            if status == "committed":
                return True, label
        if step.name == "freight_cdp_backfill":
            manifest = _read_json(path.with_suffix(".manifest.json"))
            stats = manifest.get("stats") if isinstance(manifest, dict) else {}
            if not path.exists() or csv_count(path) <= 0:
                return False, label
            if manifest.get("aborted"):
                return False, f"{label} aborted: {manifest.get('aborted_reason', '')}"
            if int(stats.get("targets", 0) or 0) > 0 and int(stats.get("updated", 0) or 0) <= 0:
                return False, f"{label} has no delivery updates"
            return True, label
        if path.suffix.lower() == ".csv":
            return csv_count(path) > 0, label
        return path.exists(), label
    if step.name in {"status_check", "s3_sync", "local_cleanup", "db_prepare", "db_load"}:
        return False, "always refresh when selected"
    return False, "no completion rule"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def resume_steps(steps, *, recover=True):
    selected = []
    force_downstream = False
    for step in steps:
        complete, reason = step_complete(step, recover=recover)
        if complete and not force_downstream and step.name not in {"status_check", "s3_sync", "local_cleanup", "db_prepare", "db_load"}:
            print(f"[ok] step {step.key} {step.name}: {reason}")
            continue
        print(f"[todo] step {step.key} {step.name}: {reason}")
        selected.append(step)
        if step.name not in {"status_check", "s3_sync", "local_cleanup", "db_prepare", "db_load"}:
            force_downstream = True
    return selected


def selected_steps(args, steps):
    if args.resume:
        return resume_steps(steps, recover=not args.dry_run)
    if args.all:
        selected = [step for step in steps if step.number != 0]
        if args.include_setup:
            return steps
        return selected
    if args.from_step:
        start = step_by_key(steps, args.from_step).number
        return [step for step in steps if step.number >= start]
    if args.steps:
        return [step_by_key(steps, value) for value in args.steps]
    return []


def run_retailer_orchestrator(retailer_key, package_name, description):
    _configure_combined_db_mode()
    force_dated_run_root = os.environ.get("SEDA_FORCE_DATED_RUN_ROOT", "0").lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    explicit_run_root = (
        bool(str(os.environ.get("SEDA_RUN_ROOT", "")).strip())
        and not force_dated_run_root
    )
    configure_retailer(retailer_key)
    steps = steps_for(package_name)
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("steps", nargs="*", help="Step numbers or names to run. Omit to list steps.")
    parser.add_argument("--from-step", dest="from_step", help="Run from this step through the last step.")
    parser.add_argument("--all", action="store_true", help="Run all steps.")
    parser.add_argument("--include-setup", action="store_true", help="Include setup step 00 when --all is selected.")
    parser.add_argument("--resume", action="store_true", help="Run incomplete steps and always refresh operational steps.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument(
        "--product-line",
        default=product_line(),
        help="Product line key, e.g. TV, REF, LDY.",
    )
    args = parser.parse_args()
    os.environ["SEDA_PRODUCT_LINE"] = str(args.product_line).strip().upper()
    if force_dated_run_root or not explicit_run_root:
        os.environ["SEDA_RUN_ROOT"] = str(dated_run_root(retailer=retailer_key))
    chosen = selected_steps(args, steps)
    if not chosen:
        print(f"{retailer_key} pipeline steps:")
        for step in steps:
            print(f"  {step.key} {step.name:<18} {step.module}")
        return
    if not args.dry_run:
        start_zenrows_usage_execution()
    for step in chosen:
        if _requires_detail_completion(step) and not args.dry_run:
            assert_detail_publish_complete(run_root())
        code = run_module(step.module, env=step_env(retailer_key, step.env), dry_run=args.dry_run)
        if code:
            raise SystemExit(code)


def _configure_combined_db_mode():
    if os.environ.get("SEDA_COMBINED_RETAILER_RUN", "0").strip().lower() not in TRUE_VALUES:
        return
    replace_requested = (
        os.environ.get("SEDA_DB_TRUNCATE_BEFORE_LOAD", "0").strip().lower()
        in TRUE_VALUES
    )
    os.environ["SEDA_DB_TRUNCATE_BEFORE_LOAD"] = "0"
    os.environ["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"] = "1" if replace_requested else "0"
    os.environ["SEDA_COMBINED_RETAILER_RUN"] = "0"
