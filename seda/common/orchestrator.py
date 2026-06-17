import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from seda.common.retailer_runner import configure_retailer, run_module, step_env
from seda.step00_config import csv_count, dated_run_root, product_line, read_json, run_root


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
    steps = [
        Step(0, "erd_schema", f"{package_name}.step00_erd_schema"),
        Step(1, "main_list", f"{package_name}.step01_main_list"),
        Step(2, "main_targets", f"{package_name}.step02_main_targets"),
        Step(3, "bsr_list", f"{package_name}.step03_bsr_list"),
        Step(4, "bsr_rank", f"{package_name}.step04_bsr_rank"),
        Step(5, "promotion_deals", f"{package_name}.step05_promotion_deals"),
        Step(6, "trending_deals", f"{package_name}.step06_trending_deals"),
        Step(7, "final_targets", f"{package_name}.step07_final_targets"),
        Step(8, "detail_enrichment", f"{package_name}.step08_detail_enrichment"),
        Step(9, "review20", f"{package_name}.step09_review20"),
    ]
    next_number = 10
    if package_name.endswith(".casas_bahia"):
        steps.extend(
            [
                Step(next_number, "freight_cdp_backfill", "seda.casas_bahia.freight_cdp_backfill"),
                Step(next_number + 1, "listing_badge_backfill", "seda.casas_bahia.listing_badge_backfill"),
            ]
        )
        next_number += 2
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


def step_by_key(steps, value):
    for step in steps:
        if value in {step.key, step.name, str(step.number)}:
            return step
    raise SystemExit(f"Unknown step: {value}")


def step_complete(step):
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
        "review20": (root / "detail" / "manifest_review20.json", "review manifest"),
        "freight_cdp_backfill": (root / "output" / "final_output_delivery_backfilled.csv", "delivery backfilled output"),
        "listing_badge_backfill": (root / "output" / "final_output_badged.csv", "listing badge backfilled output"),
        "final_output": (root / "output" / "final_output.csv", "final output"),
        "field_audit": (root / "output" / "field_audit_v2.json", "field audit"),
    }
    if step.name in checks:
        path, label = checks[step.name]
        if path.suffix.lower() == ".csv":
            return csv_count(path) > 0, label
        return path.exists(), label
    if step.name in {"promotion_deals", "trending_deals"}:
        rel = "promotion/manifest_promotion_deals.json" if step.name == "promotion_deals" else "trending/manifest_trending_deals.json"
        manifest = read_json(root / rel)
        return manifest.get("success") is True, manifest.get("skip_reason", step.name)
    if step.name in {"status_check", "s3_sync", "local_cleanup", "db_prepare", "db_load"}:
        return False, "always refresh when selected"
    return False, "no completion rule"


def resume_steps(steps):
    selected = []
    force_downstream = False
    for step in steps:
        complete, reason = step_complete(step)
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
        return resume_steps(steps)
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
    explicit_run_root = bool(str(os.environ.get("SEDA_RUN_ROOT", "")).strip())
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
        help="Product line key, e.g. TV, REF, LDY. Current Magalu implementation is TV-only.",
    )
    args = parser.parse_args()
    os.environ["SEDA_PRODUCT_LINE"] = str(args.product_line).strip().upper()
    if not explicit_run_root:
        os.environ["SEDA_RUN_ROOT"] = str(dated_run_root(retailer=retailer_key))
    chosen = selected_steps(args, steps)
    if not chosen:
        print(f"{retailer_key} pipeline steps:")
        for step in steps:
            print(f"  {step.key} {step.name:<18} {step.module}")
        return
    for step in chosen:
        code = run_module(step.module, env=step_env(retailer_key, step.env), dry_run=args.dry_run)
        if code:
            raise SystemExit(code)
