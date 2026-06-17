from seda.common.orchestrator import run_retailer_orchestrator


def main():
    run_retailer_orchestrator("magalu", "seda.magalu", "Magalu SEDA crawler orchestrator")


if __name__ == "__main__":
    main()
