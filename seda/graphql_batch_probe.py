import argparse
import copy
import json
import os
import time
from pathlib import Path

from .network_capture import _create_driver


def main():
    parser = argparse.ArgumentParser(description="Probe whether a captured GraphQL request supports HTTP array batching.")
    parser.add_argument("--requests-json", required=True, help="Path to graphql_requests.json from seda.network_capture")
    parser.add_argument("--operation", required=True, help="GraphQL operationName to test")
    parser.add_argument("--product-id", action="append", default=[], help="Override variables.productId. Repeatable.")
    parser.add_argument("--alias-batch", action="store_true", help="Also probe one GraphQL document with aliased productId fields.")
    parser.add_argument("--endpoint", default="", help="Override GraphQL endpoint")
    parser.add_argument("--warmup-url", default=os.getenv("SEDA_PROBE_WARMUP_URL", "https://www.magazineluiza.com.br/busca/tv/"))
    parser.add_argument("--output", default="", help="Optional JSON output path")
    parser.add_argument("--wait", type=float, default=float(os.getenv("SEDA_PROBE_WAIT_SECONDS", "8")))
    parser.add_argument("--profile", default=os.getenv("SEDA_PROBE_PROFILE", "C:/tmp/seda_graphql_batch_probe_profile"))
    parser.add_argument("--version-main", type=int, default=int(os.getenv("SEDA_CAPTURE_CHROME_VERSION", "0")))
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    requests_path = Path(args.requests_json)
    payload = _load_payload(requests_path, args.operation)
    payloads = _payload_variants(payload, args.product_id)
    endpoint = args.endpoint or _endpoint_for(payload)

    driver = _create_driver(args.profile, headless=args.headless, version_main=args.version_main or None)
    try:
        driver.get(args.warmup_url)
        time.sleep(args.wait)
        single = _browser_graphql_fetch(driver, endpoint, payloads[0])
        batch = _browser_graphql_fetch(driver, _strip_operation_query(endpoint), payloads)
        alias_batch = {}
        if args.alias_batch and len(payloads) > 1:
            alias_payload = _alias_batch_payload(payload, args.product_id)
            alias_batch = _browser_graphql_fetch(
                driver,
                _endpoint_with_operation(_strip_operation_query(endpoint), alias_payload["operationName"]),
                alias_payload,
            )
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    result = {
        "requests_json": str(requests_path),
        "operationName": args.operation,
        "endpoint": endpoint,
        "payload_count": len(payloads),
        "single": _summarize_response(single),
        "batch": _summarize_response(batch),
        "batch_supported": _batch_supported(batch, len(payloads)),
    }
    if alias_batch:
        result["alias_batch"] = _summarize_response(alias_batch)
        result["alias_batch_supported"] = _alias_batch_supported(alias_batch, len(payloads))
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))


def _load_payload(path, operation):
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        if row.get("operationName") == operation:
            return {
                "endpoint": row.get("endpoint") or "",
                "operationName": row.get("operationName") or "",
                "variables": row.get("variables") or {},
                "query": row.get("query") or "",
                "extensions": row.get("extensions") or {},
            }
    raise SystemExit(f"operation not found: {operation}")


def _payload_variants(payload, product_ids):
    product_ids = [value for value in product_ids if value]
    if not product_ids:
        return [_request_body(payload)]
    result = []
    for product_id in product_ids:
        item = copy.deepcopy(payload)
        variables = item.setdefault("variables", {})
        variables["productId"] = product_id
        result.append(_request_body(item))
    return result


def _request_body(payload):
    result = {
        "operationName": payload.get("operationName") or "",
        "variables": payload.get("variables") or {},
        "query": payload.get("query") or "",
    }
    if payload.get("extensions"):
        result["extensions"] = payload["extensions"]
    return result


def _alias_batch_payload(payload, product_ids):
    query = payload.get("query") or ""
    product_ids = [value for value in product_ids if value]
    if "productId: $productId" not in query or "recommendation(" not in query:
        raise SystemExit("alias batch currently requires a recommendation query with productId: $productId")

    fragment_index = query.find("\n\nfragment ")
    operation = query if fragment_index < 0 else query[:fragment_index]
    fragments = "" if fragment_index < 0 else query[fragment_index:]
    first_brace = operation.find("{")
    last_brace = operation.rfind("}")
    if first_brace < 0 or last_brace < first_brace:
        raise SystemExit("could not split GraphQL operation body")

    declaration = operation[:first_brace]
    selection = operation[first_brace + 1 : last_brace].strip()
    variable_declarations = ", ".join(f"$productId{index}: String" for index, _ in enumerate(product_ids))
    if "$productId: String," in declaration:
        declaration = declaration.replace("$productId: String,", f"{variable_declarations},")
    elif "$productId: String" in declaration:
        declaration = declaration.replace("$productId: String", variable_declarations)
    declaration = declaration.replace("query showcaseQuery", "query showcaseAliasBatch")

    blocks = []
    variables = copy.deepcopy(payload.get("variables") or {})
    variables.pop("productId", None)
    for index, product_id in enumerate(product_ids):
        block = selection.replace("recommendation(", f"p{index}: recommendation(", 1)
        block = block.replace("productId: $productId", f"productId: $productId{index}")
        blocks.append(block)
        variables[f"productId{index}"] = product_id

    return {
        "operationName": "showcaseAliasBatch",
        "variables": variables,
        "query": f"{declaration}{{\n" + "\n".join(blocks) + "\n}" + fragments,
    }


def _endpoint_for(payload):
    return payload.get("endpoint") or "https://federation.magazineluiza.com.br/graphql"


def _strip_operation_query(endpoint):
    return endpoint.split("?", 1)[0]


def _endpoint_with_operation(endpoint, operation):
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}operationName={operation}"


def _browser_graphql_fetch(driver, endpoint, payload):
    script = """
const endpoint = arguments[0];
const payload = JSON.parse(arguments[1]);
const done = arguments[2];
fetch(endpoint, {
  method: 'POST',
  headers: {
    'accept': 'application/json',
    'content-type': 'application/json',
    'x-channel-id': '45',
    'x-channel-name': 'mixer-desk.magazineluiza.com.br'
  },
  body: JSON.stringify(payload)
})
  .then(async response => done({status: response.status, text: await response.text()}))
  .catch(error => done({status: 0, error: String(error), text: ''}));
"""
    return driver.execute_async_script(script, endpoint, json.dumps(payload, ensure_ascii=False))


def _summarize_response(response):
    response = response or {}
    text = response.get("text") or ""
    data_type = ""
    data_len = None
    error = response.get("error") or ""
    if text:
        try:
            parsed = json.loads(text)
            data_type = type(parsed).__name__
            if isinstance(parsed, list):
                data_len = len(parsed)
        except ValueError:
            data_type = "invalid_json"
            error = error or "invalid_json"
    return {
        "status": response.get("status") or 0,
        "error": error,
        "data_type": data_type,
        "data_len": data_len,
        "text_head": text[:500],
    }


def _batch_supported(response, expected_count):
    if not response or response.get("status") != 200:
        return False
    try:
        parsed = json.loads(response.get("text") or "")
    except ValueError:
        return False
    return isinstance(parsed, list) and len(parsed) == expected_count


def _alias_batch_supported(response, expected_count):
    if not response or response.get("status") != 200:
        return False
    try:
        parsed = json.loads(response.get("text") or "")
    except ValueError:
        return False
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, dict):
        return False
    return sum(1 for key in data if key.startswith("p")) == expected_count


if __name__ == "__main__":
    main()
