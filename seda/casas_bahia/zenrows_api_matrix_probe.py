import argparse
import csv
import html
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

from seda.step00_config import RETAILERS, run_root, write_json


ZENROWS_API_URL = "https://api.zenrows.com/v1/"
SENSITIVE_PARAMS = {"apikey"}
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "apikey"}
BLOCK_MARKERS = (
    "ops! algo deu errado",
    "akamai",
    "access denied",
    "customdeny",
    "captcha",
    "page-not-found",
)
CASAS_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "referer": "https://www.casasbahia.com.br/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}


def default_input_path():
    return run_root() / "output" / "final_output_enriched.csv"


def default_output_dir():
    return run_root() / "zenrows_api_matrix"


def _safe_dict(value, sensitive_keys):
    return {
        key: ("[redacted]" if str(key).lower() in sensitive_keys else item)
        for key, item in (value or {}).items()
    }


def _env_enabled(name, default="0"):
    return os.getenv(name, default).lower() in {"1", "true", "yes", "y"}


def _sku_from_row(row):
    item = str(row.get("item") or "").strip()
    if re.fullmatch(r"\d+", item):
        return item
    match = re.search(r"/p/(\d+)", str(row.get("product_url") or ""))
    return match.group(1) if match else ""


def _load_targets(path, limit):
    targets = []
    seen = set()
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            sku_id = _sku_from_row(row)
            seller_id = re.sub(r"\D+", "", str(row.get("seller_id") or ""))
            product_url = str(row.get("product_url") or "").strip()
            if not sku_id or not seller_id or not product_url:
                continue
            key = (sku_id, seller_id)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "row_index": len(targets),
                    "sku_id": sku_id,
                    "seller_id": seller_id,
                    "product_url": product_url,
                    "retailer_sku_name": str(row.get("retailer_sku_name") or "")[:120],
                }
            )
            if len(targets) >= limit:
                break
    return targets


def _with_frete(url, zipcode):
    if not url:
        return url
    if "frete=" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}frete={zipcode}"


def _freight_url(target, zipcode):
    return (
        f"https://pdp-api.casasbahia.com.br/api/v2/sku/{target['sku_id']}"
        f"/freight/seller/{target['seller_id']}/zipcode/{zipcode}/source/CB?"
        + urlencode({"channel": "DESKTOP", "orderby": "price"})
    )


def _cvip(zipcode):
    zip_digits = re.sub(r"\D+", "", zipcode)
    return f"IPI-CasasBahia=UsuarioGUID=00000000-0000-4000-8000-000000000001&cepClienteProvavel={zip_digits}"


def _case_matrix(targets, args):
    first = targets[0]
    listing_url = args.listing_url or RETAILERS["casas_bahia"].main_url
    pdp_url = _with_frete(first["product_url"], args.zipcode)
    freight_url = _freight_url(first, args.zipcode)
    batch_pdp_url = pdp_url
    batch_listing_url = listing_url
    batch_js = _batch_js(targets, args.zipcode)

    return [
        {
            "name": "pdp_auto_br",
            "target_type": "pdp",
            "url": pdp_url,
            "params": {"mode": "auto", "proxy_country": "br", "original_status": "true"},
        },
        {
            "name": "pdp_auto_global",
            "target_type": "pdp",
            "url": pdp_url,
            "params": {"mode": "auto", "original_status": "true"},
        },
        {
            "name": "pdp_premium_br_headers",
            "target_type": "pdp",
            "url": pdp_url,
            "headers": CASAS_HEADERS,
            "params": {
                "premium_proxy": "true",
                "proxy_country": "br",
                "custom_headers": "true",
                "original_status": "true",
            },
        },
        {
            "name": "pdp_js_premium_br",
            "target_type": "pdp",
            "url": pdp_url,
            "params": {
                "js_render": "true",
                "premium_proxy": "true",
                "proxy_country": "br",
                "wait": str(args.wait_ms),
                "block_resources": args.block_resources,
                "original_status": "true",
            },
        },
        {
            "name": "pdp_js_premium_global",
            "target_type": "pdp",
            "url": pdp_url,
            "params": {
                "js_render": "true",
                "premium_proxy": "true",
                "wait": str(args.wait_ms),
                "block_resources": args.block_resources,
                "original_status": "true",
            },
        },
        {
            "name": "freight_auto_br",
            "target_type": "freight",
            "url": freight_url,
            "headers": _freight_headers(first, args.zipcode),
            "params": {"mode": "auto", "proxy_country": "br", "custom_headers": "true", "original_status": "true"},
        },
        {
            "name": "freight_auto_global",
            "target_type": "freight",
            "url": freight_url,
            "headers": _freight_headers(first, args.zipcode),
            "params": {"mode": "auto", "custom_headers": "true", "original_status": "true"},
        },
        {
            "name": "freight_premium_br_headers",
            "target_type": "freight",
            "url": freight_url,
            "headers": _freight_headers(first, args.zipcode),
            "params": {
                "premium_proxy": "true",
                "proxy_country": "br",
                "custom_headers": "true",
                "original_status": "true",
            },
        },
        {
            "name": "freight_js_premium_br",
            "target_type": "freight",
            "url": freight_url,
            "headers": _freight_headers(first, args.zipcode),
            "params": {
                "js_render": "true",
                "premium_proxy": "true",
                "proxy_country": "br",
                "custom_headers": "true",
                "wait": str(args.wait_ms),
                "original_status": "true",
            },
        },
        {
            "name": "batch_pdp_js_premium_br",
            "target_type": "batch",
            "url": batch_pdp_url,
            "params": {
                "js_render": "true",
                "premium_proxy": "true",
                "proxy_country": "br",
                "wait": str(args.wait_ms),
                "block_resources": args.block_resources,
                "js_instructions": json.dumps([{"evaluate": batch_js}, {"wait": 1000}], ensure_ascii=False),
                "original_status": "true",
            },
        },
        {
            "name": "batch_listing_js_premium_global",
            "target_type": "batch",
            "url": batch_listing_url,
            "params": {
                "js_render": "true",
                "premium_proxy": "true",
                "wait": str(args.wait_ms),
                "block_resources": args.block_resources,
                "js_instructions": json.dumps([{"evaluate": batch_js}, {"wait": 1000}], ensure_ascii=False),
                "original_status": "true",
            },
        },
    ]


def _freight_headers(target, zipcode):
    headers = dict(CASAS_HEADERS)
    headers.update(
        {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.casasbahia.com.br",
            "referer": target.get("product_url") or "https://www.casasbahia.com.br/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "x-cvip": _cvip(zipcode),
        }
    )
    return headers


def _batch_js(targets, zipcode):
    safe_targets = [
        {"row_index": item["row_index"], "sku_id": item["sku_id"], "seller_id": item["seller_id"]}
        for item in targets
    ]
    payload = json.dumps(safe_targets, ensure_ascii=False, separators=(",", ":"))
    zip_digits = re.sub(r"\D+", "", zipcode)
    return f"""
    (async () => {{
      const items = {payload};
      const zipcode = {json.dumps(zipcode)};
      const zipDigits = {json.dumps(zip_digits)};
      const cookie = document.cookie || "";
      const match = cookie.match(/(?:^|;\\s*)IPI-CasasBahia=([^;]+)/);
      let cvip = match ? `IPI-CasasBahia=${{decodeURIComponent(match[1])}}` : "";
      if (!cvip) {{
        const guid = crypto.randomUUID ? crypto.randomUUID() : "00000000-0000-4000-8000-000000000000";
        cvip = `IPI-CasasBahia=UsuarioGUID=${{guid}}`;
      }}
      if (zipDigits && !cvip.includes("cepClienteProvavel=")) {{
        cvip = `${{cvip}}&cepClienteProvavel=${{zipDigits}}`;
      }}
      const params = new URLSearchParams({{channel: "DESKTOP", orderby: "price"}});
      const headers = {{"content-type": "application/json", "x-cvip": cvip}};
      async function fetchOne(item) {{
        const url = `https://pdp-api.casasbahia.com.br/api/v2/sku/${{item.sku_id}}/freight/seller/${{item.seller_id}}/zipcode/${{zipcode}}/source/CB?${{params}}`;
        try {{
          const response = await fetch(url, {{method: "GET", headers, credentials: "include", cache: "no-cache"}});
          const text = await response.text();
          return {{
            row_index: item.row_index,
            sku_id: item.sku_id,
            seller_id: item.seller_id,
            status: response.status,
            ok: response.ok,
            content_type: response.headers.get("content-type") || "",
            body_prefix: text.slice(0, response.ok ? 500 : 200)
          }};
        }} catch (error) {{
          return {{
            row_index: item.row_index,
            sku_id: item.sku_id,
            seller_id: item.seller_id,
            status: 0,
            ok: false,
            content_type: "",
            error: String(error),
            body_prefix: ""
          }};
        }}
      }}
      const results = [];
      for (const item of items) {{
        results.push(await fetchOne(item));
      }}
      document.documentElement.innerHTML = "<body><pre id='seda-result'></pre></body>";
      document.querySelector("#seda-result").textContent = JSON.stringify({{
        hasCvip: Boolean(cvip),
        hasCvipCep: Boolean(cvip && cvip.includes("cepClienteProvavel=")),
        items: results
      }});
    }})()
    """


def _estimate_multiplier(params):
    if params.get("mode") == "auto":
        return "auto:success_only"
    js = str(params.get("js_render", "")).lower() == "true"
    premium = str(params.get("premium_proxy", "")).lower() == "true"
    if js and premium:
        return "25x"
    if premium:
        return "10x"
    if js:
        return "5x"
    return "1x"


def _execute_case(case, api_key, args):
    params = dict(case.get("params") or {})
    params["url"] = case["url"]
    params["apikey"] = api_key
    headers = case.get("headers") or {}
    started = time.time()
    try:
        response = requests.get(
            ZENROWS_API_URL,
            params=params,
            headers=headers,
            timeout=args.timeout,
        )
        error = ""
        text = response.text or ""
    except Exception as exc:
        response = None
        error = f"{type(exc).__name__}: {exc}"
        text = ""
    elapsed = round(time.time() - started, 3)
    return _summarize_case_result(case, params, headers, response, text, error, elapsed, args)


def _summarize_case_result(case, params, headers, response, text, error, elapsed, args):
    status_code = response.status_code if response is not None else 0
    response_headers = response.headers if response is not None else {}
    parsed_json = _parse_json(text)
    batch_summary = _batch_summary(text)
    body_prefix = _body_prefix(text, args.body_prefix_chars)
    return {
        "case": case["name"],
        "target_type": case.get("target_type", ""),
        "url": case["url"],
        "estimated_multiplier": _estimate_multiplier(case.get("params") or {}),
        "request_params": _safe_dict({k: v for k, v in params.items() if k != "url"}, SENSITIVE_PARAMS),
        "request_headers": _safe_dict(headers, SENSITIVE_HEADERS),
        "status_code": status_code,
        "ok": bool(response is not None and response.ok),
        "elapsed_seconds": elapsed,
        "error": error,
        "zenrows_headers": {
            key: response_headers.get(key, "")
            for key in ("X-Request-Cost", "Concurrency-Limit", "Concurrency-Remaining", "X-Request-Id")
            if response_headers.get(key)
        },
        "content_type": response_headers.get("content-type", ""),
        "body_length": len(text),
        "json_like": bool(parsed_json),
        "json_top_keys": list(parsed_json.keys())[:20] if isinstance(parsed_json, dict) else [],
        "zenrows_error_title": parsed_json.get("title", "") if isinstance(parsed_json, dict) else "",
        "looks_blocked": _looks_blocked(text),
        "block_markers": _block_markers(text),
        "batch_summary": batch_summary,
        "body_prefix": body_prefix,
    }


def _parse_json(text):
    if not text or not text.lstrip().startswith(("{", "[")):
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {"_list_length": len(parsed)}


def _batch_summary(text):
    if not text:
        return {}
    candidate = html.unescape(text)
    match = re.search(r"<pre[^>]*id=[\"']seda-result[\"'][^>]*>(.*?)</pre>", candidate, re.S | re.I)
    if match:
        candidate = html.unescape(match.group(1).strip())
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(candidate[start : end + 1])
    except ValueError:
        return {}
    items = parsed.get("items") if isinstance(parsed, dict) else []
    if not isinstance(items, list):
        return {}
    counts = Counter(str(item.get("status", "unknown")) for item in items)
    return {
        "parsed": True,
        "has_cvip": bool(parsed.get("hasCvip")),
        "has_cvip_cep": bool(parsed.get("hasCvipCep")),
        "item_count": len(items),
        "ok_count": sum(1 for item in items if item.get("ok")),
        "status_counts": dict(sorted(counts.items())),
    }


def _looks_blocked(text):
    haystack = (text or "").lower()
    return any(marker in haystack for marker in BLOCK_MARKERS)


def _block_markers(text):
    haystack = (text or "").lower()
    return [marker for marker in BLOCK_MARKERS if marker in haystack]


def _body_prefix(text, size):
    if size <= 0:
        return ""
    return html.unescape((text or "")[:size])


def _select_cases(cases, args):
    if args.case:
        wanted = set(args.case)
        selected = [case for case in cases if case["name"] in wanted]
        missing = sorted(wanted - {case["name"] for case in selected})
        if missing:
            raise SystemExit(f"Unknown case(s): {missing}. Valid: {[case['name'] for case in cases]}")
    else:
        selected = cases
    if args.max_cases:
        selected = selected[: args.max_cases]
    return selected


def _write_csv_summary(path, results):
    columns = [
        "case",
        "target_type",
        "estimated_multiplier",
        "status_code",
        "ok",
        "x_request_cost",
        "elapsed_seconds",
        "body_length",
        "looks_blocked",
        "zenrows_error_title",
        "batch_ok_count",
        "batch_status_counts",
        "url",
    ]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "case": item.get("case", ""),
                    "target_type": item.get("target_type", ""),
                    "estimated_multiplier": item.get("estimated_multiplier", ""),
                    "status_code": item.get("status_code", ""),
                    "ok": item.get("ok", ""),
                    "x_request_cost": (item.get("zenrows_headers") or {}).get("X-Request-Cost", ""),
                    "elapsed_seconds": item.get("elapsed_seconds", ""),
                    "body_length": item.get("body_length", ""),
                    "looks_blocked": item.get("looks_blocked", ""),
                    "zenrows_error_title": item.get("zenrows_error_title", ""),
                    "batch_ok_count": (item.get("batch_summary") or {}).get("ok_count", ""),
                    "batch_status_counts": json.dumps((item.get("batch_summary") or {}).get("status_counts", {}), ensure_ascii=False),
                    "url": item.get("url", ""),
                }
            )


def run(args):
    api_key = os.getenv("ZENROWS_API_KEY", "").strip()
    if args.execute and not api_key:
        raise SystemExit("ZENROWS_API_KEY is not set.")
    if args.execute and not _env_enabled("SEDA_ALLOW_ZENROWS") and not args.force:
        raise SystemExit("Refusing paid ZenRows probe: set SEDA_ALLOW_ZENROWS=1 or pass --force.")
    input_path = Path(args.input)
    targets = _load_targets(input_path, args.target_limit)
    if not targets:
        raise SystemExit(f"No Casas Bahia targets with product_url, sku_id, seller_id in {input_path}")

    cases = _select_cases(_case_matrix(targets, args), args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    planned = [
        {
            "case": case["name"],
            "target_type": case.get("target_type", ""),
            "url": case["url"],
            "estimated_multiplier": _estimate_multiplier(case.get("params") or {}),
            "params": _safe_dict(case.get("params") or {}, SENSITIVE_PARAMS),
            "headers": _safe_dict(case.get("headers") or {}, SENSITIVE_HEADERS),
        }
        for case in cases
    ]
    if not args.execute:
        result = {
            "dry_run": True,
            "input": str(input_path),
            "target_count": len(targets),
            "targets": targets,
            "planned_cases": planned,
        }
        path = output_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_dry_run.json"
        write_json(path, result)
        print(f"[casas_zenrows_api_matrix] dry_run wrote {path}")
        return result

    results = []
    for index, case in enumerate(cases, start=1):
        print(
            f"[casas_zenrows_api_matrix] {index}/{len(cases)} "
            f"{case['name']} multiplier={_estimate_multiplier(case.get('params') or {})}",
            flush=True,
        )
        results.append(_execute_case(case, api_key, args))
        if args.sleep_seconds > 0 and index < len(cases):
            time.sleep(args.sleep_seconds)

    manifest = {
        "dry_run": False,
        "input": str(input_path),
        "target_count": len(targets),
        "targets": targets,
        "case_count": len(cases),
        "executed_at": datetime.now().isoformat(timespec="seconds"),
        "results": results,
        "summary": {
            "ok_cases": [item["case"] for item in results if item.get("ok")],
            "blocked_cases": [item["case"] for item in results if item.get("looks_blocked")],
            "x_request_cost_total_observed": sum(
                _safe_float((item.get("zenrows_headers") or {}).get("X-Request-Cost", "0"))
                for item in results
            ),
        },
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"{stamp}_matrix.json"
    csv_path = output_dir / f"{stamp}_summary.csv"
    write_json(json_path, manifest)
    _write_csv_summary(csv_path, results)
    print(f"[casas_zenrows_api_matrix] wrote {json_path}")
    print(f"[casas_zenrows_api_matrix] wrote {csv_path}")
    print(json.dumps(manifest["summary"], ensure_ascii=False))
    return manifest


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def main():
    parser = argparse.ArgumentParser(description="Run a small ZenRows Universal API matrix for Casas Bahia PDP/freight access.")
    parser.add_argument("--input", default=str(default_input_path()))
    parser.add_argument("--output-dir", default=str(default_output_dir()))
    parser.add_argument("--listing-url", default="")
    parser.add_argument("--zipcode", default=os.getenv("SEDA_POSTAL_CODE", "01010-010"))
    parser.add_argument("--target-limit", type=int, default=10)
    parser.add_argument("--max-cases", type=int, default=0, help="Run only the first N cases.")
    parser.add_argument("--case", action="append", default=[], help="Run a named case. Can be repeated.")
    parser.add_argument("--execute", action="store_true", help="Execute paid ZenRows requests.")
    parser.add_argument("--force", action="store_true", help="Allow execution without SEDA_ALLOW_ZENROWS=1.")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_ZENROWS_TIMEOUT", "180")))
    parser.add_argument("--wait-ms", type=int, default=int(os.getenv("SEDA_ZENROWS_MATRIX_WAIT_MS", "8000")))
    parser.add_argument("--sleep-seconds", type=float, default=float(os.getenv("SEDA_ZENROWS_MATRIX_SLEEP_SECONDS", "1")))
    parser.add_argument("--block-resources", default=os.getenv("SEDA_ZENROWS_MATRIX_BLOCK", "image,media,font,stylesheet"))
    parser.add_argument("--body-prefix-chars", type=int, default=int(os.getenv("SEDA_ZENROWS_MATRIX_BODY_PREFIX_CHARS", "1200")))
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
