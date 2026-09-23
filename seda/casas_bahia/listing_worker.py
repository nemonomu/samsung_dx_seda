"""One mode-3/mode-4 process for selected listing stages and their owned Chrome.

No detail or DB modules are accepted. Each listing still finishes its normal
manifest/CSV/diagnostic ZIP before the next selected stage starts.
"""

import argparse
from contextlib import contextmanager
import importlib
import os


ALLOWED_MODULES = {
    "seda.casas_bahia.step01_main_list": ("main", "seda.step01_main_list", True),
    "seda.casas_bahia.step02_main_targets": ("main", "seda.step02_main_targets", False),
    "seda.casas_bahia.step03_bsr_list": ("bsr", "seda.step01_main_list", True),
}


@contextmanager
def _run_context(run_id):
    # Restore this explicit non-secret field only, never snapshot environment.
    previous = os.environ.get("SEDA_RUN_ID")
    os.environ["SEDA_RUN_ID"] = run_id
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SEDA_RUN_ID", None)
        else:
            os.environ["SEDA_RUN_ID"] = previous


def run_steps(module_names):
    modules = tuple(module_names)
    if not modules or any(name not in ALLOWED_MODULES for name in modules):
        raise ValueError("casas_listing_worker_invalid_stage")

    from seda.common.retailer_runner import configure_retailer
    from seda.casas_bahia.listing_modes import close_browsers, selected_mode

    if selected_mode() not in {"3", "4"}:
        raise ValueError("casas_listing_worker_requires_mode_3_or_4")
    try:
        configure_retailer("casas_bahia")
        for module_name in modules:
            run_id, common_module, is_listing = ALLOWED_MODULES[module_name]
            print(f"[run] in_process {module_name} run_id={run_id} shared_listing_browser=true", flush=True)
            with _run_context(run_id):
                module = importlib.import_module(common_module)
                if is_listing:
                    module.main(retain_browser=True)
                else:
                    module.main()
    finally:
        # Normal completion, listing failure, target failure and interruption all
        # release this worker's browser once; other collection processes remain
        # separate and are never killed or attached to.
        close_browsers()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Share mode-3/mode-4 Chrome across selected Casas listing stages.")
    parser.add_argument("modules", nargs="+", choices=tuple(ALLOWED_MODULES))
    args = parser.parse_args(argv)
    return run_steps(args.modules)


if __name__ == "__main__":
    raise SystemExit(main())
