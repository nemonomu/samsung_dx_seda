import os

from .step01_main_list import main as main_list


def main():
    os.environ["SEDA_RUN_ID"] = "bsr"
    main_list()


if __name__ == "__main__":
    main()
