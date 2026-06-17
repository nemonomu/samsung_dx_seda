from .step00_config import run_root, write_json


def main():
    output = run_root() / "trending" / "manifest_trending_deals.json"
    write_json(output, {"success": True, "rows": 0, "skip_reason": "No SEDA trending page is defined in ERD."})
    print(f"[seda] wrote {output}")


if __name__ == "__main__":
    main()
