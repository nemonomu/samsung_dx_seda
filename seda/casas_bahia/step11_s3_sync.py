from seda.common.retailer_runner import run_common_step


def main():
    run_common_step("casas_bahia", "seda.step11_s3_sync")


if __name__ == "__main__":
    main()
