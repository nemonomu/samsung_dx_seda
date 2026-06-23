import argparse
import csv
import json
import re
from pathlib import Path

from seda.common.retailer_runner import configure_retailer
from seda.step00_config import run_root

from .detail_api import fetch_freight


def default_input():
    return str(run_root() / "output" / "seda_final_targets.csv")


def default_output():
    return str(run_root() / "output" / "direct_freight_smoke.json")


def _sku_id(row):
    match = re.search(r"/p/(\d+)", str(row.get("product_url") or ""))
    if match:
        return match.group(1)
    item = str(row.get("item") or "").strip()
    return item if re.fullmatch(r"\d+", item) else ""


def _seller_id(row):
    return re.sub(r"\D+", "", str(row.get("seller_id") or ""))


def _load_targets(path, limit):
    targets = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            sku_id = _sku_id(row)
            seller_id = _seller_id(row)
            if not sku_id or not seller_id:
                continue
            targets.append(
                {
                    "index": index,
                    "sku_id": sku_id,
                    "seller_id": seller_id,
                    "product_url": row.get("product_url", ""),
                    "retailer_sku_name": row.get("retailer_sku_name", ""),
                }
            )
            if limit and len(targets) >= limit:
                break
    return targets


def run(args):
    configure_retailer("casas_bahia")
    targets = _load_targets(args.input, args.limit)
    results = []
    ok = 0
    for pos, target in enumerate(targets, start=1):
        result = fetch_freight(
            target["sku_id"],
            target["seller_id"],
            zipcode=args.zipcode,
            timeout=args.timeout,
            referer_url=target.get("product_url", ""),
        )
        detail = result.get("detail") or {}
        delivery = detail.get("delivery_availability", "")
        success = bool(result.get("success") and delivery)
        ok += int(success)
        item = {
            **target,
            "success": success,
            "method": result.get("method", ""),
            "error": result.get("error", ""),
            "delivery_availability": delivery,
            "detail": detail,
        }
        results.append(item)
        print(
            "[casas_direct_freight] "
            f"{pos}/{len(targets)} success={success} sku={target['sku_id']} "
            f"seller={target['seller_id']} method={item['method']} "
            f"error={item['error']} delivery={delivery}",
            flush=True,
        )

    summary = {
        "input": args.input,
        "output": args.output,
        "target_count": len(targets),
        "ok": ok,
        "fail": len(targets) - ok,
        "rows": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("target_count", "ok", "fail", "output")}, ensure_ascii=False))
    return summary


def main():
    configure_retailer("casas_bahia")
    parser = argparse.ArgumentParser(description="Smoke test Casas Bahia direct freight REST API without Playwright.")
    parser.add_argument("--input", default=default_input())
    parser.add_argument("--output", default=default_output())
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--zipcode", default=None)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    result = run(args)
    if result["ok"] == 0 and result["target_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
