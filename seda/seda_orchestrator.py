import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .step00_config import csv_count, read_json, run_root


PYTHON = sys.executable


@dataclass(frozen=True)
class Step:
    number: int
    name: str
    module: str
    env: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.number:02d}"


STEPS = [
    Step(0, "erd_schema", "seda.step00_erd_schema"),
    Step(1, "main_list", "seda.step01_main_list", {"SEDA_RUN_ID": "main"}),
    Step(2, "main_targets", "seda.step02_main_targets"),
    Step(3, "bsr_list", "seda.step03_bsr_list"),
    Step(4, "bsr_rank", "seda.step04_bsr_rank"),
    Step(5, "promotion_deals", "seda.step05_promotion_deals"),
    Step(6, "trending_deals", "seda.step06_trending_deals"),
    Step(7, "final_targets", "seda.step07_final_targets"),
    Step(8, "detail_enrichment", "seda.step08_detail_enrichment"),
    Step(9, "review20", "seda.step09_review20"),
    Step(10, "status_check", "seda.step10_status_check"),
    Step(11, "s3_sync", "seda.step11_s3_sync"),
    Step(12, "local_cleanup", "seda.step12_local_cleanup"),
    Step(13, "db_prepare", "seda.step13_db_prepare"),
    Step(14, "db_load", "seda.step14_db_load"),
]


def step_by_key(value):
    for step in STEPS:
        if value in {step.key, step.name, str(step.number)}:
            return step
    raise SystemExit(f"Unknown step: {value}")


def selected_steps(args):
    if args.resume:
        return resume_steps()
    if args.all:
        return STEPS
    if args.from_step:
        start = step_by_key(args.from_step).number
        return [step for step in STEPS if step.number >= start]
    if args.steps:
        return [step_by_key(value) for value in args.steps]
    return []


def step_complete(step):
    root = run_root()
    project_root = Path(__file__).resolve().parent.parent
    checks = {
        "erd_schema": (project_root / "seda" / "config" / "seda_erd_schema.json", "ERD schema"),
        "main_list": (root / "main" / "parsed" / "main_occurrences.csv", "main rows"),
        "main_targets": (root / "output" / "seda_main_targets.csv", "main targets"),
        "bsr_list": (root / "bsr" / "parsed" / "main_occurrences.csv", "bsr rows"),
        "bsr_rank": (root / "bsr" / "parsed" / "bsr_rank_map.csv", "bsr rank map"),
        "final_targets": (root / "output" / "seda_final_targets.csv", "final targets"),
        "detail_enrichment": (root / "output" / "final_output.csv", "final output"),
        "review20": (root / "detail" / "manifest_review20.json", "review manifest"),
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


def resume_steps():
    selected = []
    force_downstream = False
    for step in STEPS:
        complete, reason = step_complete(step)
        if complete and not force_downstream and step.name not in {"status_check", "s3_sync", "local_cleanup", "db_prepare", "db_load"}:
            print(f"[ok] step {step.key} {step.name}: {reason}")
            continue
        print(f"[todo] step {step.key} {step.name}: {reason}")
        selected.append(step)
        if step.name not in {"status_check", "s3_sync", "local_cleanup", "db_prepare", "db_load"}:
            force_downstream = True
    return selected


def run_step(step, dry_run=False):
    env = os.environ.copy()
    env.setdefault("SEDA_FETCH_MODE", "uc_first")
    env.update(step.env)
    command = [PYTHON, "-m", step.module]
    print(f"[run] step {step.key} {step.name}: {' '.join(command)}")
    if dry_run:
        return 0
    return subprocess.call(command, env=env, cwd=Path(__file__).resolve().parent.parent)


def print_steps():
    print("SEDA pipeline steps:")
    for step in STEPS:
        print(f"  {step.key} {step.name:<18} {step.module}")


def main():
    parser = argparse.ArgumentParser(description="SEDA retail.com crawler orchestrator")
    parser.add_argument("steps", nargs="*", help="Step numbers or names to run. Omit to list steps.")
    parser.add_argument("--from-step", dest="from_step", help="Run from this step through the last step.")
    parser.add_argument("--all", action="store_true", help="Run all steps.")
    parser.add_argument("--resume", action="store_true", help="Run incomplete steps and always refresh operational steps.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    args = parser.parse_args()
    steps = selected_steps(args)
    if not steps:
        print_steps()
        return
    for step in steps:
        code = run_step(step, dry_run=args.dry_run)
        if code:
            raise SystemExit(code)


if __name__ == "__main__":
    main()
