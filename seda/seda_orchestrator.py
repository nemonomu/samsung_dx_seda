import argparse
import os
import subprocess
import sys
from pathlib import Path

from .step00_config import dated_run_root, product_line


PYTHON = sys.executable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RETAILER_MODULES = {
    "magalu": "seda.magalu.magalu_orchestrator",
    "casas_bahia": "seda.casas_bahia.casas_bahia_orchestrator",
}
DEFAULT_FETCH_MODES = {
    "magalu": "magalu_graphql_first",
    "casas_bahia": "casas_bahia_uc_first",
}
RETAILER_FETCH_MODE_ENV = {
    "magalu": "SEDA_MAGALU_FETCH_MODE",
    "casas_bahia": "SEDA_CASAS_BAHIA_FETCH_MODE",
}
TRUE_VALUES = {"1", "true", "yes", "y"}
SHARED_FETCH_MODES = {
    "auto",
    "browser",
    "graphql",
    "graphql_first",
    "requests",
    "requests_first",
    "uc",
    "uc_first",
    "zenrows",
    "zenrows_first",
}


def selected_retailers(value):
    if value == "all":
        return list(RETAILER_MODULES)
    return [value]


def child_arguments(args):
    values = list(args.steps)
    if args.from_step:
        values.extend(["--from-step", args.from_step])
    if args.all:
        values.append("--all")
    if args.include_setup:
        values.append("--include-setup")
    if args.resume:
        values.append("--resume")
    if args.dry_run:
        values.append("--dry-run")
    values.extend(["--product-line", args.product_line])
    return values


def retailer_env(
    retailer,
    product_line_value,
    retailer_count,
    retailer_index=0,
    environ=None,
):
    env = dict(os.environ if environ is None else environ)
    env["SEDA_RETAILERS"] = retailer
    env["SEDA_ACTIVE_RETAILER"] = retailer
    env["SEDA_PRODUCT_LINE"] = product_line_value
    force_dated_root = str(env.get("SEDA_FORCE_DATED_RUN_ROOT", "0")).strip().lower() in TRUE_VALUES
    env["SEDA_FORCE_DATED_RUN_ROOT"] = "0"

    explicit_root = "" if force_dated_root else str(env.get("SEDA_RUN_ROOT", "")).strip()
    if retailer_count > 1 and explicit_root:
        env["SEDA_RUN_ROOT"] = str(
            Path(explicit_root) / retailer / product_line_value.lower()
        )
    elif not explicit_root:
        env["SEDA_RUN_ROOT"] = str(
            dated_run_root(retailer=retailer, product_line_value=product_line_value)
        )

    retailer_fetch_mode = str(env.get(RETAILER_FETCH_MODE_ENV[retailer], "")).strip()
    shared_fetch_mode = str(env.get("SEDA_FETCH_MODE", "")).strip()
    if retailer_fetch_mode:
        env["SEDA_FETCH_MODE"] = retailer_fetch_mode
    elif retailer_count > 1 and shared_fetch_mode.lower() in SHARED_FETCH_MODES:
        env["SEDA_FETCH_MODE"] = shared_fetch_mode
    elif retailer_count > 1:
        env["SEDA_FETCH_MODE"] = DEFAULT_FETCH_MODES[retailer]
    elif not shared_fetch_mode:
        env["SEDA_FETCH_MODE"] = DEFAULT_FETCH_MODES[retailer]

    if retailer_count > 1:
        replace_requested = str(
            env.get("SEDA_DB_TRUNCATE_BEFORE_LOAD", "0")
        ).strip().lower() in TRUE_VALUES
        env["SEDA_DB_TRUNCATE_BEFORE_LOAD"] = "0"
        env["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"] = "1" if replace_requested else "0"
        # The shared dispatcher has consumed the combined-run DB conversion.
        # Prevent the retailer child from deriving replace mode a second time
        # from the already-cleared truncate flag.
        env["SEDA_COMBINED_RETAILER_RUN"] = "0"
    return env


def parser():
    result = argparse.ArgumentParser(
        description="Run Magalu and Casas Bahia in isolated retailer pipelines."
    )
    result.add_argument("steps", nargs="*", help="Retailer step numbers or names to run.")
    result.add_argument(
        "--retailer",
        choices=("all", *RETAILER_MODULES),
        default="all",
        help="Retailer pipeline to run. 'all' runs each retailer separately.",
    )
    result.add_argument("--from-step", dest="from_step", help="Run from this retailer step through its last step.")
    result.add_argument("--all", action="store_true", help="Run every operational step for each selected retailer.")
    result.add_argument("--include-setup", action="store_true", help="Include setup step 00 with --all.")
    result.add_argument("--resume", action="store_true", help="Resume each selected retailer independently.")
    result.add_argument("--dry-run", action="store_true", help="Print child commands without running them.")
    result.add_argument(
        "--product-line",
        default=product_line(),
        help="Product line key, e.g. TV, REF, LDY.",
    )
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    args.product_line = str(args.product_line).strip().upper()
    numeric_steps = [value for value in args.steps if str(value).strip().isdigit()]
    if args.from_step and str(args.from_step).strip().isdigit():
        numeric_steps.append(args.from_step)
    if args.retailer == "all" and numeric_steps:
        raise SystemExit(
            "Numeric step identifiers require --retailer magalu or "
            "--retailer casas_bahia; use named steps when running both retailers."
        )
    retailers = selected_retailers(args.retailer)
    forwarded = child_arguments(args)

    for retailer_index, retailer in enumerate(retailers):
        command = [PYTHON, "-m", RETAILER_MODULES[retailer], *forwarded]
        env = retailer_env(
            retailer,
            args.product_line,
            len(retailers),
            retailer_index=retailer_index,
        )
        print(
            f"[retailer] {retailer}: {' '.join(command)} "
            f"(run_root={env['SEDA_RUN_ROOT']})",
            flush=True,
        )
        code = subprocess.call(command, env=env, cwd=PROJECT_ROOT)
        if code:
            raise SystemExit(code)


if __name__ == "__main__":
    main()
