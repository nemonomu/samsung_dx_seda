import os
import subprocess
import sys
from pathlib import Path

from seda.step00_config import dated_run_root, product_line


PYTHON = sys.executable
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def configure_retailer(retailer_key):
    os.environ["SEDA_RETAILERS"] = retailer_key
    os.environ.setdefault("SEDA_ACTIVE_RETAILER", retailer_key)
    os.environ.setdefault("SEDA_PRODUCT_LINE", product_line())
    default_fetch_mode = "magalu_graphql_first" if retailer_key == "magalu" else f"{retailer_key}_uc_first"
    os.environ.setdefault("SEDA_FETCH_MODE", default_fetch_mode)
    os.environ.setdefault("SEDA_RUN_ROOT", str(dated_run_root(retailer=retailer_key)))


def run_common_step(retailer_key, module_name):
    configure_retailer(retailer_key)
    module = __import__(module_name, fromlist=["main"])
    module.main()


def run_module(module_name, env=None, dry_run=False):
    command = [PYTHON, "-m", module_name]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    print(f"[run] {' '.join(command)}")
    if dry_run:
        return 0
    return subprocess.call(command, env=merged_env, cwd=PROJECT_ROOT)


def step_env(retailer_key, extra=None):
    default_fetch_mode = "magalu_graphql_first" if retailer_key == "magalu" else f"{retailer_key}_uc_first"
    env = {
        "SEDA_RETAILERS": retailer_key,
        "SEDA_ACTIVE_RETAILER": retailer_key,
        "SEDA_PRODUCT_LINE": product_line(),
        "SEDA_FETCH_MODE": os.environ.get("SEDA_FETCH_MODE") or default_fetch_mode,
        "SEDA_RUN_ROOT": os.environ.get("SEDA_RUN_ROOT") or str(dated_run_root(retailer=retailer_key)),
    }
    if extra:
        env.update(extra)
    return env
