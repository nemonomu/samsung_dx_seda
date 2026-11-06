import os

from seda.common.orchestrator import run_retailer_orchestrator
from seda.common.retailer_runner import _log_line


def main():
    try:
        run_retailer_orchestrator("casas_bahia", "seda.casas_bahia", "Casas Bahia SEDA crawler orchestrator")
    except Exception as exc:
        _log_line(
            f"[casas_bahia] orchestrator failed: {type(exc).__name__}",
            os.getenv("SEDA_RUN_LOG_FILE", "").strip(),
        )
        raise


if __name__ == "__main__":
    main()
