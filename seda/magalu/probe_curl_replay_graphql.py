import argparse
import csv
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests

from seda.step00_config import DEFAULT_RUNS_BASE, run_date


GRAPHQL_HOST = "federation.magazineluiza.com.br/graphql"
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
}
BLOCKED_HEADERS = {
    "accept-encoding",
    "content-length",
    "host",
    "priority",
}

CSV_COLUMNS = [
    "name",
    "client",
    "status_code",
    "seconds",
    "bytes",
    "content_type",
    "operation_name",
    "success",
    "delivery",
    "pickup",
    "similar_count",
    "similar_names",
    "error",
    "safe_header_names",
]


def main():
    args = parse_args()
    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_RUNS_BASE / "magalu" / run_date() / "curl_replay_graphql"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"magalu_curl_replay_graphql_{stamp}.csv"
    json_path = out_dir / f"magalu_curl_replay_graphql_{stamp}.json"

    cases = []
    shipping_path = Path(args.shipping_curl)
    if shipping_path.exists():
        cases.extend(parse_curl_file(shipping_path, allowed_operations={"shippingQuery"}))
    showcase_path = Path(args.showcase_curl)
    if showcase_path.exists():
        cases.extend(parse_curl_file(showcase_path, allowed_operations={"showcaseQuery"}))

    rows = []
    for case in cases:
        for client in clients(args):
            row = execute_case(case, client, args.timeout)
            rows.append(row)
            write_outputs(csv_path, json_path, rows)
            print(
                "[curl_replay] "
                f"{row['name']} client={row['client']} status={row['status_code']} "
                f"success={row['success']} delivery={bool(row['delivery'])} "
                f"pickup={bool(row['pickup'])} similar={row['similar_count']} "
                f"error={row['error'] or '-'} seconds={row['seconds']}",
                flush=True,
            )

    print(f"[curl_replay] wrote {json_path}")
    print(f"[curl_replay] wrote {csv_path}")


def parse_args():
    default_base = Path(r"C:\samsung_dx_seda\references\260625")
    parser = argparse.ArgumentParser(description="Replay captured Magalu GraphQL cURL requests for shipping/showcase validation.")
    parser.add_argument("--shipping-curl", default=str(default_base / "magalu_shippingQuery_240144700_curl.txt"))
    parser.add_argument("--showcase-curl", default=str(default_base / "magalu_showcaseQuery_240144700_curl.txt"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_TIMEOUT", "45")))
    parser.add_argument("--no-curl-cffi", action="store_true")
    return parser.parse_args()


def clients(args):
    result = ["requests"]
    if not args.no_curl_cffi and curl_cffi_available():
        result.append("curl_cffi")
    return result


def parse_curl_file(path, allowed_operations=None):
    text = path.read_text(encoding="utf-8", errors="replace")
    cases = []
    for index, block in enumerate(curl_blocks(text), start=1):
        parsed = parse_curl_block(block)
        if not parsed:
            continue
        operation = parsed["json"].get("operationName") or operation_from_url(parsed["url"])
        if allowed_operations and operation not in allowed_operations:
            continue
        if operation not in {"shippingQuery", "showcaseQuery"}:
            continue
        parsed["name"] = f"{path.stem}:{operation}:{index}"
        cases.append(parsed)
    return cases


def curl_blocks(text):
    blocks = []
    current = []
    for line in text.splitlines():
        if line.startswith("curl ") and current:
            blocks.append("\n".join(current))
            current = [line]
        elif line.startswith("curl "):
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def parse_curl_block(block):
    url_match = re.search(r'curl\s+\^"([^"]+)', block) or re.search(r"curl\s+'([^']+)'", block)
    if not url_match:
        return None
    url = decode_windows_curl(url_match.group(1))
    if GRAPHQL_HOST not in url:
        return None

    headers = {}
    for header_match in re.finditer(r'-H\s+\^"([^"]+)\^"', block):
        header = decode_windows_curl(header_match.group(1))
        if ":" not in header:
            continue
        key, value = header.split(":", 1)
        key = key.strip()
        if not key or key.lower() in BLOCKED_HEADERS:
            continue
        headers[key] = value.strip()

    cookie_match = re.search(r'-b\s+\^"([^"]*)\^"', block, re.S)
    if cookie_match:
        headers["Cookie"] = decode_windows_curl(cookie_match.group(1))

    raw_body = data_raw_from_block(block)
    if not raw_body:
        return None
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return None
    normalize_graphql_payload(payload)

    return {
        "url": url,
        "headers": headers,
        "json": payload,
    }


def execute_case(case, client, timeout):
    started = time.perf_counter()
    status_code = 0
    text = ""
    content_type = ""
    error = ""
    try:
        response = post_graphql(case, client, timeout)
        status_code = int(response.status_code)
        text = response.text or ""
        content_type = response.headers.get("content-type", "")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    summary = summarize_payload(case, text, status_code, content_type)
    if not error:
        error = summary.pop("error", "")
    else:
        summary.pop("error", None)
    return {
        "name": case.get("name", ""),
        "client": client,
        "status_code": status_code,
        "seconds": round(time.perf_counter() - started, 3),
        "bytes": len(text),
        "content_type": content_type,
        "operation_name": case["json"].get("operationName") or operation_from_url(case["url"]),
        "error": error,
        "safe_header_names": ",".join(safe_header_names(case["headers"])),
        **summary,
    }


def post_graphql(case, client, timeout):
    headers = dict(case["headers"])
    headers.setdefault("content-type", "application/json")
    headers.setdefault("accept", "application/json")
    if client == "curl_cffi":
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate=os.getenv("SEDA_MAGALU_CURL_IMPERSONATE", "chrome136"))
        return session.post(case["url"], json=case["json"], headers=headers, timeout=timeout)
    session = requests.Session()
    return session.post(case["url"], json=case["json"], headers=headers, timeout=timeout)


def summarize_payload(case, text, status_code, content_type):
    operation = case["json"].get("operationName") or operation_from_url(case["url"])
    base = {
        "success": 0,
        "delivery": "",
        "pickup": "",
        "similar_count": 0,
        "similar_names": "",
        "error": "",
    }
    if status_code != 200:
        base["error"] = f"http_{status_code}"
        return base
    if "json" not in (content_type or "").lower():
        base["error"] = "non_json"
        return base
    try:
        data = json.loads(text)
    except ValueError:
        base["error"] = "invalid_json"
        return base
    if data.get("errors"):
        base["error"] = "graphql_errors"
        return base
    if operation == "shippingQuery":
        shipping = (data.get("data") or {}).get("shipping") or {}
        delivery, pickup = shipping_texts(shipping)
        base.update({"success": int(bool(delivery or pickup)), "delivery": delivery, "pickup": pickup})
        if not base["success"]:
            base["error"] = "missing_shipping_text"
        return base
    if operation == "showcaseQuery":
        names = similar_names(data)
        base.update({"success": int(bool(names)), "similar_count": len(names), "similar_names": " ||| ".join(names[:20])})
        if not base["success"]:
            base["error"] = "missing_similar_names"
        return base
    base["error"] = f"unsupported_operation:{operation}"
    return base


def shipping_texts(shipping):
    delivery = ""
    pickup = ""
    for delivery_group in shipping.get("deliveries") or []:
        for modality in delivery_group.get("modalities") or []:
            shipping_time = modality.get("shippingTime") if isinstance(modality.get("shippingTime"), dict) else {}
            description = clean_text(shipping_time.get("description"))
            modality_type = clean_text(modality.get("type")).lower()
            modality_name = clean_text(modality.get("name")).lower()
            if not description:
                continue
            if "pickup" in modality_type or "retira" in modality_name:
                pickup = pickup or description
            else:
                delivery = delivery or description
    return delivery, pickup


def similar_names(data):
    dynamic = ((data.get("data") or {}).get("recommendation") or {}).get("dynamic") or []
    names = []
    for showcase in dynamic:
        title = clean_text(showcase.get("title"))
        if not is_similar_showcase_title(title):
            continue
        for product in showcase.get("products") or []:
            name = clean_text(product.get("title"))
            if name:
                names.append(name)
    return names


def is_similar_showcase_title(title):
    normalized = ascii_lower(title)
    raw = clean_text(title).lower()
    if "quem viu" in normalized and "tamb" in normalized and "viu" in normalized:
        return True
    return "quem viu" in raw and "tamb" in raw and "viu" in raw


def operation_from_url(url):
    match = re.search(r"[?&]operationName=([^&]+)", url)
    return match.group(1) if match else ""


def decode_windows_curl(value):
    decoded = re.sub(r"\^(.)", r"\1", str(value or ""))
    return decoded.replace('\\"', '"').rstrip("^")


def normalize_graphql_payload(payload):
    if not isinstance(payload, dict):
        return
    query = payload.get("query")
    if isinstance(query, str):
        payload["query"] = query.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")


def data_raw_from_block(block):
    for line in block.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("--data-raw ") or stripped.startswith("-d ")):
            continue
        value = stripped.split(" ", 1)[1].strip()
        if value.endswith(" &"):
            value = value[:-2].rstrip()
        if value.endswith(" ^"):
            value = value[:-2].rstrip()
        if value.startswith('^"') and value.endswith('^"'):
            value = value[2:-2]
        elif value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return decode_windows_curl(value)
    return ""


def safe_header_names(headers):
    return sorted(key for key in headers if key.lower() not in SENSITIVE_HEADERS)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def ascii_lower(value):
    import unicodedata

    normalized = unicodedata.normalize("NFKD", clean_text(value))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def curl_cffi_available():
    try:
        import curl_cffi  # noqa: F401
    except Exception:
        return False
    return True


def write_outputs(csv_path, json_path, rows):
    json_path.write_text(json.dumps({"rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
