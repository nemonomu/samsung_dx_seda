import argparse
import csv
import json
import os
import re
from pathlib import Path
from urllib.parse import urlencode

from seda.common.retailer_runner import configure_retailer
from seda.step00_config import run_root

from .detail_api import PDP_API, _freight_detail, _freight_params


def default_input():
    return str(run_root() / "output" / "seda_final_targets.csv")


def default_output():
    return str(run_root() / "output" / "zenrows_freight_smoke.json")


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


def _freight_url(sku_id, seller_id, zipcode):
    zipcode = zipcode or os.getenv("SEDA_POSTAL_CODE", "01010-010")
    base = f"{PDP_API}/api/v2/sku/{sku_id}/freight/seller/{seller_id}/zipcode/{zipcode}/source/CB"
    return f"{base}?{urlencode(_freight_params())}"


def _target_headers(referer_url):
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "origin": "https://www.casasbahia.com.br",
        "referer": referer_url or "https://www.casasbahia.com.br/",
    }
    return headers


def _parse_json(text):
    try:
        value = json.loads(text or "")
    except ValueError:
        return None
    return value


def run(args):
    configure_retailer("casas_bahia")
    if args.execute:
        os.environ["SEDA_ALLOW_ZENROWS"] = "1"
        os.environ["SEDA_ZENROWS_DRY_RUN"] = "0"
    targets = _load_targets(args.input, args.limit)
    profiles = [item.strip() for item in args.profiles.split(",") if item.strip()]
    results = []
    ok = 0

    from seda.magalu.zenrows_client import request_url

    for pos, target in enumerate(targets, start=1):
        url = _freight_url(target["sku_id"], target["seller_id"], args.zipcode)
        row_result = {
            **target,
            "url": url,
            "success": False,
            "delivery_availability": "",
            "pick_up_availability": "",
            "attempts": [],
        }
        for profile in profiles:
            result = request_url(
                url,
                profile=profile,
                timeout=args.timeout,
                extra={"custom_headers": "true"},
                extra_headers=_target_headers(target.get("product_url", "")),
            )
            data = _parse_json(result.text)
            detail = _freight_detail(data) if data is not None else {}
            delivery = detail.get("delivery_availability", "")
            pickup = detail.get("pick_up_availability", "")
            success = bool(result.success and data is not None and (delivery or pickup))
            attempt = {
                "profile": profile,
                "success": success,
                "status_code": result.status_code,
                "error": result.error,
                "estimated_multiplier": result.estimated_multiplier,
                "headers": result.headers,
                "text_length": len(result.text or ""),
                "json": data is not None,
                "delivery_availability": delivery,
                "pick_up_availability": pickup,
            }
            row_result["attempts"].append(attempt)
            print(
                "[casas_zenrows_freight] "
                f"{pos}/{len(targets)} profile={profile} success={success} "
                f"status={result.status_code} cost={result.estimated_multiplier} "
                f"sku={target['sku_id']} seller={target['seller_id']} "
                f"error={result.error} delivery={delivery} pickup={pickup}",
                flush=True,
            )
            if success:
                row_result.update(
                    {
                        "success": True,
                        "profile": profile,
                        "delivery_availability": delivery,
                        "pick_up_availability": pickup,
                    }
                )
                ok += 1
                break
        results.append(row_result)

    summary = {
        "input": args.input,
        "output": args.output,
        "execute": args.execute,
        "profiles": profiles,
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
    parser = argparse.ArgumentParser(description="Probe Casas Bahia freight endpoint through ZenRows.")
    parser.add_argument("--input", default=default_input())
    parser.add_argument("--output", default=default_output())
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--zipcode", default=None)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--profiles", default=os.getenv("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_PROFILES", "premium_html,auto_custom_headers,js_premium_original_status"))
    parser.add_argument("--execute", action="store_true", help="Execute paid ZenRows calls. Requires ZENROWS_API_KEY.")
    args = parser.parse_args()
    if args.execute and not os.getenv("ZENROWS_API_KEY", "").strip():
        raise SystemExit("ZENROWS_API_KEY is not set.")
    result = run(args)
    if args.execute and result["ok"] == 0 and result["target_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
