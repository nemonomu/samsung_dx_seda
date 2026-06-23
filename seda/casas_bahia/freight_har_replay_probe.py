import argparse
import base64
import csv
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from seda.common.retailer_runner import configure_retailer
from seda.step00_config import run_root, write_json

from .detail_api import _freight_detail, fetch_freight


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HAR = PROJECT_ROOT / "references" / "casas_delivery.har"


def default_input():
    return str(run_root() / "output" / "seda_final_targets.csv")


def default_output():
    return str(run_root() / "output" / "freight_har_replay_probe.json")


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


def _read_har(path):
    data = json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    entries = data.get("log", {}).get("entries", [])
    selected = []
    for index, entry in enumerate(entries):
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        url = request.get("url") or ""
        if "/freight/" not in url:
            continue
        headers = {
            str(header.get("name") or "").lower(): str(header.get("value") or "")
            for header in request.get("headers") or []
            if header.get("name")
        }
        body = _response_text(response)
        selected.append(
            {
                "index": index,
                "url": url,
                "status": response.get("status"),
                "mime_type": (response.get("content") or {}).get("mimeType", ""),
                "headers": headers,
                "body_text": body,
            }
        )
    if not selected:
        raise SystemExit(f"No freight entries found in HAR: {path}")
    return selected


def _response_text(response):
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    text = content.get("text") or ""
    if content.get("encoding") == "base64" and text:
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return text


def _first_success_har_entry(entries):
    for entry in entries:
        if entry.get("status") == 200 and "json" in str(entry.get("mime_type") or "").lower():
            return entry
    return entries[0]


def _headers_for_replay(har_headers, referer_url=None, cvip_mode="har"):
    allowed = {
        "accept",
        "accept-language",
        "cache-control",
        "content-type",
        "origin",
        "pragma",
        "priority",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "user-agent",
        "x-cvip",
    }
    headers = {key: value for key, value in har_headers.items() if key in allowed and value}
    if referer_url:
        headers["referer"] = referer_url
    if cvip_mode == "none":
        headers.pop("x-cvip", None)
    elif cvip_mode == "env":
        cvip = os.getenv("SEDA_CASAS_BAHIA_X_CVIP", "").strip()
        if cvip:
            headers["x-cvip"] = cvip
    return headers


def _replace_freight_identity(url, sku_id, seller_id, zipcode=None):
    parsed = urlsplit(url)
    path = re.sub(r"/sku/[^/]+/freight/seller/[^/]+/zipcode/[^/]+/", f"/sku/{sku_id}/freight/seller/{seller_id}/zipcode/{zipcode or os.getenv('SEDA_POSTAL_CODE', '01010-010')}/", parsed.path)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    if not query:
        query = [("channel", "DESKTOP"), ("orderby", "price")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), parsed.fragment))


def _json_detail(text):
    try:
        data = json.loads(text or "")
    except ValueError:
        return None, {}
    return data, _freight_detail(data)


def _request_requests(url, headers, timeout):
    started = time.time()
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        return _response_result("requests", url, headers, response.status_code, response.headers, response.text or "", "", time.time() - started)
    except Exception as exc:
        return {
            "transport": "requests",
            "url": url,
            "success": False,
            "status_code": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.time() - started, 3),
        }


def _request_curl_cffi(url, headers, timeout):
    started = time.time()
    try:
        from curl_cffi import requests as curl_requests

        response = curl_requests.get(
            url,
            headers=headers,
            timeout=timeout,
            impersonate=os.getenv("SEDA_CASAS_BAHIA_CURL_IMPERSONATE", "chrome"),
        )
        return _response_result("curl_cffi", url, headers, response.status_code, response.headers, response.text or "", "", time.time() - started)
    except Exception as exc:
        return {
            "transport": "curl_cffi",
            "url": url,
            "success": False,
            "status_code": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.time() - started, 3),
        }


def _response_result(transport, url, headers, status_code, response_headers, text, error, seconds):
    data, detail = _json_detail(text)
    content_type = response_headers.get("content-type", "") if hasattr(response_headers, "get") else ""
    delivery = detail.get("delivery_availability", "")
    pickup = detail.get("pick_up_availability", "")
    return {
        "transport": transport,
        "url": url,
        "success": bool(status_code == 200 and data is not None and (delivery or pickup)),
        "status_code": status_code,
        "content_type": content_type,
        "text_length": len(text or ""),
        "json": data is not None,
        "delivery_availability": delivery,
        "pick_up_availability": pickup,
        "error": error or ("" if status_code == 200 else f"status_{status_code}"),
        "seconds": round(seconds, 3),
        "request_header_summary": _header_summary(headers),
    }


def _header_summary(headers):
    return {
        "cache-control": headers.get("cache-control", ""),
        "referer": headers.get("referer", ""),
        "sec-ch-ua": headers.get("sec-ch-ua", ""),
        "user-agent": headers.get("user-agent", ""),
        "x-cvip": _mask_cvip(headers.get("x-cvip", "")),
    }


def _mask_cvip(value):
    text = str(value or "")
    return re.sub(r"UsuarioGUID=[^&]+", "UsuarioGUID=<masked>", text)


def _print_result(label, result, target=None):
    target_text = ""
    if target:
        target_text = f" sku={target['sku_id']} seller={target['seller_id']}"
    delivery = _ascii(result.get("delivery_availability", ""))
    pickup = _ascii(result.get("pick_up_availability", ""))
    print(
        "[casas_freight_har_probe] "
        f"{label}{target_text} transport={result.get('transport')} "
        f"success={int(bool(result.get('success')))} status={result.get('status_code')} "
        f"json={int(bool(result.get('json')))} len={result.get('text_length')} "
        f"error={result.get('error')} delivery={delivery} pickup={pickup}",
        flush=True,
    )


def _ascii(value):
    return str(value or "").encode("ascii", "backslashreplace").decode("ascii")[:160]


def run(args):
    configure_retailer("casas_bahia")
    har_entries = _read_har(args.har)
    har_entry = _first_success_har_entry(har_entries)
    har_headers = har_entry["headers"]
    targets = _load_targets(args.input, args.limit) if args.input else []
    transports = [item.strip().lower() for item in args.transports.split(",") if item.strip()]
    cvip_modes = [item.strip().lower() for item in args.cvip_modes.split(",") if item.strip()]
    results = {
        "har": str(args.har),
        "har_entry_index": har_entry.get("index"),
        "har_status": har_entry.get("status"),
        "input": args.input,
        "target_count": len(targets),
        "transports": transports,
        "cvip_modes": cvip_modes,
        "rows": [],
    }

    for cvip_mode in cvip_modes:
        headers = _headers_for_replay(har_headers, cvip_mode=cvip_mode)
        for transport in transports:
            result = _request(transport, har_entry["url"], headers, args.timeout)
            result["case"] = f"har_url_{cvip_mode}"
            _print_result(result["case"], result)
            results["rows"].append(result)

    for pos, target in enumerate(targets, start=1):
        target_url = _replace_freight_identity(har_entry["url"], target["sku_id"], target["seller_id"], zipcode=args.zipcode)
        for cvip_mode in cvip_modes:
            headers = _headers_for_replay(har_headers, referer_url=target.get("product_url"), cvip_mode=cvip_mode)
            for transport in transports:
                result = _request(transport, target_url, headers, args.timeout)
                result.update({"case": f"target_{cvip_mode}", "target_index": pos, **target})
                _print_result(result["case"], result, target=target)
                results["rows"].append(result)

        if args.compare_current:
            current = fetch_freight(target["sku_id"], target["seller_id"], zipcode=args.zipcode, timeout=args.timeout, referer_url=target.get("product_url", ""))
            detail = current.get("detail") or {}
            result = {
                "case": "current_fetch_freight",
                "transport": current.get("method", ""),
                "url": "",
                "success": bool(current.get("success") and (detail.get("delivery_availability") or detail.get("pick_up_availability"))),
                "status_code": 0,
                "content_type": "",
                "text_length": 0,
                "json": bool(current.get("success")),
                "delivery_availability": detail.get("delivery_availability", ""),
                "pick_up_availability": detail.get("pick_up_availability", ""),
                "error": current.get("error", ""),
                "target_index": pos,
                **target,
            }
            _print_result(result["case"], result, target=target)
            results["rows"].append(result)

    ok = sum(1 for row in results["rows"] if row.get("success"))
    results["ok"] = ok
    results["fail"] = len(results["rows"]) - ok
    write_json(args.output, results)
    print(json.dumps({"ok": ok, "fail": results["fail"], "output": str(args.output)}, ensure_ascii=False))
    return results


def _request(transport, url, headers, timeout):
    if transport == "requests":
        return _request_requests(url, headers, timeout)
    if transport == "curl_cffi":
        return _request_curl_cffi(url, headers, timeout)
    raise SystemExit(f"Unknown transport: {transport}")


def main():
    configure_retailer("casas_bahia")
    parser = argparse.ArgumentParser(description="Replay successful Casas Bahia freight HAR requests against current RDP/network.")
    parser.add_argument("--har", default=str(DEFAULT_HAR))
    parser.add_argument("--input", default=default_input())
    parser.add_argument("--output", default=default_output())
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--zipcode", default=None)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--transports", default=os.getenv("SEDA_CASAS_BAHIA_FREIGHT_PROBE_TRANSPORTS", "requests,curl_cffi"))
    parser.add_argument("--cvip-modes", default=os.getenv("SEDA_CASAS_BAHIA_FREIGHT_PROBE_CVIP_MODES", "har,none"))
    parser.add_argument("--compare-current", action="store_true", help="Also call the current fetch_freight implementation for each target.")
    args = parser.parse_args()
    args.har = Path(args.har)
    args.output = Path(args.output)
    run(args)


if __name__ == "__main__":
    main()
