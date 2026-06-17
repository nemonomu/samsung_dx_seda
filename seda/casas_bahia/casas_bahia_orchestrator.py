from seda.common.orchestrator import run_retailer_orchestrator


def main():
    run_retailer_orchestrator("casas_bahia", "seda.casas_bahia", "Casas Bahia SEDA crawler orchestrator")


if __name__ == "__main__":
    main()
