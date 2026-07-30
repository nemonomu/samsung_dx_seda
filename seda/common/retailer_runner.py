import os
import subprocess
import sys
from pathlib import Path

from seda.step00_config import dated_run_root, product_line


PYTHON = sys.executable
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def configure_retailer(retailer_key):
    os.environ["SEDA_RETAILERS"] = retailer_key
    os.environ["SEDA_ACTIVE_RETAILER"] = retailer_key
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
    log_file = merged_env.get("SEDA_RUN_LOG_FILE", "").strip()
    _log_line(f"[run] {' '.join(command)}", log_file)
    if dry_run:
        return 0
    if not log_file:
        return subprocess.call(command, env=merged_env, cwd=PROJECT_ROOT)
    return _call_with_live_log(command, merged_env, log_file)


def _log_line(message, log_file=""):
    print(message, flush=True)
    if not log_file:
        return
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"{message}\n")
    except OSError:
        pass


def _call_with_live_log(command, env, log_file):
    process = subprocess.Popen(
        command,
        env=env,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    try:
        handle = open(log_file, "a", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[run] log file disabled: {type(exc).__name__}: {exc}", flush=True)
        handle = None
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            if handle:
                handle.write(line)
                handle.flush()
    finally:
        if handle:
            handle.close()
    return process.wait()


def step_env(retailer_key, extra=None):
    default_fetch_mode = "magalu_graphql_first" if retailer_key == "magalu" else f"{retailer_key}_uc_first"
    env = {
        "SEDA_RETAILERS": retailer_key,
        "SEDA_ACTIVE_RETAILER": retailer_key,
        "SEDA_PRODUCT_LINE": product_line(),
        "SEDA_FETCH_MODE": os.environ.get("SEDA_FETCH_MODE") or default_fetch_mode,
        "SEDA_RUN_ROOT": os.environ.get("SEDA_RUN_ROOT") or str(dated_run_root(retailer=retailer_key)),
    }
    for name in (
        "SEDA_ZENROWS_USAGE_EXECUTION_ID",
        "SEDA_ZENROWS_USAGE_REQUIRED",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            env[name] = value
    if extra:
        env.update(extra)
    return env
