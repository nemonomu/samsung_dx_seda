from seda.common.retailer_runner import run_common_step


def main():
    run_common_step("casas_bahia", "seda.step00_erd_schema")


if __name__ == "__main__":
    main()
