import argparse
import base64
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .common.har_tools import SENSITIVE_HEADER_NAMES, summarize_har


FETCH_TYPES = {"Fetch", "XHR"}


def main():
    parser = argparse.ArgumentParser(description="Capture browser Fetch/XHR traffic and summarize GraphQL/API candidates.")
    parser.add_argument("urls", nargs="+", help="URLs to open in order")
    parser.add_argument("--output-dir", default="", help="Output directory. Defaults to seda/data/network_capture/<timestamp>")
    parser.add_argument("--wait", type=float, default=float(os.getenv("SEDA_CAPTURE_WAIT_SECONDS", "8")))
    parser.add_argument("--scrolls", type=int, default=int(os.getenv("SEDA_CAPTURE_SCROLLS", "2")))
    parser.add_argument("--scroll-wait", type=float, default=float(os.getenv("SEDA_CAPTURE_SCROLL_WAIT_SECONDS", "1.5")))
    parser.add_argument("--click-text", action="append", default=[], help="Click the first visible element containing this text. Repeatable.")
    parser.add_argument("--click-selector", action="append", default=[], help="Click the first element matching this CSS selector. Repeatable.")
    parser.add_argument("--after-click-wait", type=float, default=float(os.getenv("SEDA_CAPTURE_AFTER_CLICK_WAIT_SECONDS", "3")))
    parser.add_argument("--profile", default=os.getenv("SEDA_CAPTURE_PROFILE", "C:/tmp/seda_network_capture_profile"))
    parser.add_argument("--version-main", type=int, default=int(os.getenv("SEDA_CAPTURE_CHROME_VERSION", "0")))
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir or _default_output_dir())
    output_dir.mkdir(parents=True, exist_ok=True)

    driver = _create_driver(args.profile, headless=args.headless, version_main=args.version_main or None)
    try:
        captured = capture_urls(
            driver,
            args.urls,
            wait=args.wait,
            scrolls=args.scrolls,
            scroll_wait=args.scroll_wait,
            click_texts=args.click_text,
            click_selectors=args.click_selector,
            after_click_wait=args.after_click_wait,
        )
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    har_path = output_dir / "capture.har.json"
    har = _to_har(captured)
    har_path.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")

    graphql_summary = summarize_graphql_entries(har)
    graphql_requests = extract_graphql_requests(har)
    (output_dir / "graphql_summary.json").write_text(
        json.dumps(graphql_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "graphql_requests.json").write_text(
        json.dumps(graphql_requests, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "api_summary.json").write_text(
        json.dumps(summarize_har(har_path), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"har": str(har_path), "graphql_operations": graphql_summary}, ensure_ascii=False, indent=2))


def capture_urls(driver, urls, wait=8, scrolls=2, scroll_wait=1.5, click_texts=None, click_selectors=None, after_click_wait=3):
    click_texts = click_texts or []
    click_selectors = click_selectors or []
    driver.execute_cdp_cmd("Network.enable", {})
    records = {}
    for url in urls:
        driver.get(url)
        time.sleep(wait)
        for _ in range(scrolls):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(scroll_wait)
            _drain_logs(driver, records)
        for selector in click_selectors:
            if _click_selector(driver, selector):
                time.sleep(after_click_wait)
                _drain_logs(driver, records)
        for text in click_texts:
            if _click_text(driver, text):
                time.sleep(after_click_wait)
                _drain_logs(driver, records)
        _drain_logs(driver, records)
    time.sleep(1)
    _drain_logs(driver, records)
    _attach_response_bodies(driver, records)
    return records


def _click_selector(driver, selector):
    script = """
const selector = arguments[0];
const node = document.querySelector(selector);
if (!node) return false;
node.scrollIntoView({block: 'center'});
node.click();
return true;
"""
    try:
        return bool(driver.execute_script(script, selector))
    except Exception:
        return False


def _click_text(driver, text):
    script = """
const wanted = String(arguments[0] || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], summary, div, span'));
for (const node of nodes) {
  const label = String(node.innerText || node.textContent || '').trim();
  if (!label) continue;
  const normalized = label.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
  const rect = node.getBoundingClientRect();
  if (normalized.includes(wanted) && rect.width > 0 && rect.height > 0) {
    node.scrollIntoView({block: 'center'});
    node.click();
    return label.slice(0, 120);
  }
}
return '';
"""
    try:
        return bool(driver.execute_script(script, text))
    except Exception:
        return False


def summarize_graphql_entries(har):
    groups = {}
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        body = ((request.get("postData") or {}).get("text") or "").strip()
        payloads = _graphql_payloads(body)
        url = request.get("url", "")
        if not payloads and "graphql" not in url.lower():
            continue
        endpoint = _endpoint(url)
        for payload in payloads or [{}]:
            operation = _operation_name(payload)
            query = str(payload.get("query") or "")
            variables = payload.get("variables") if isinstance(payload.get("variables"), dict) else {}
            key = (endpoint, operation)
            item = groups.setdefault(
                key,
                {
                    "endpoint": endpoint,
                    "operationName": operation,
                    "count": 0,
                    "methods": set(),
                    "status_codes": set(),
                    "variable_keys": set(),
                    "query_head": _query_head(query),
                    "batch_payload_seen": isinstance(_json_loads(body), list),
                    "sample_variables": {},
                },
            )
            item["count"] += 1
            item["methods"].add(request.get("method", ""))
            item["status_codes"].add(entry.get("response", {}).get("status", ""))
            item["variable_keys"].update(str(key) for key in variables.keys())
            if not item["sample_variables"] and variables:
                item["sample_variables"] = _small_json(variables)
    rows = []
    for item in groups.values():
        item["methods"] = sorted(value for value in item["methods"] if value)
        item["status_codes"] = sorted(value for value in item["status_codes"] if value != "")
        item["variable_keys"] = sorted(item["variable_keys"])
        item["batch_candidate"] = bool(item["operationName"] or item["query_head"])
        rows.append(item)
    return sorted(rows, key=lambda row: (-row["count"], row["endpoint"], row["operationName"]))


def extract_graphql_requests(har):
    rows = []
    for index, entry in enumerate(har.get("log", {}).get("entries", [])):
        request = entry.get("request", {})
        body = ((request.get("postData") or {}).get("text") or "").strip()
        payloads = _graphql_payloads(body)
        url = request.get("url", "")
        if not payloads and "graphql" not in url.lower():
            continue
        for payload in payloads or [{}]:
            rows.append(
                {
                    "index": index,
                    "endpoint": url,
                    "method": request.get("method", ""),
                    "status": entry.get("response", {}).get("status", ""),
                    "operationName": _operation_name(payload),
                    "query": payload.get("query", ""),
                    "variables": payload.get("variables", {}),
                    "extensions": payload.get("extensions", {}),
                }
            )
    return rows


def _create_driver(profile, headless=False, version_main=None):
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if headless:
        options.add_argument("--headless=new")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    kwargs = {"options": options, "use_subprocess": True}
    if version_main:
        kwargs["version_main"] = version_main
    return uc.Chrome(**kwargs)


def _drain_logs(driver, records):
    try:
        logs = driver.get_log("performance")
    except Exception:
        return
    for item in logs:
        try:
            message = json.loads(item.get("message", "{}")).get("message", {})
        except ValueError:
            continue
        method = message.get("method")
        params = message.get("params", {})
        request_id = params.get("requestId")
        if not request_id:
            continue
        record = records.setdefault(request_id, {"requestId": request_id})
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            record["request"] = request
            record["type"] = params.get("type") or record.get("type", "")
            record["wallTime"] = params.get("wallTime")
        elif method == "Network.responseReceived":
            record["response"] = params.get("response", {})
            record["type"] = params.get("type") or record.get("type", "")
        elif method == "Network.loadingFinished":
            record["finished"] = True
            record["encodedDataLength"] = params.get("encodedDataLength")


def _attach_response_bodies(driver, records):
    for request_id, record in list(records.items()):
        if record.get("type") not in FETCH_TYPES:
            continue
        if not record.get("response"):
            continue
        try:
            body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
        except Exception as exc:
            record["bodyError"] = f"{type(exc).__name__}: {exc}"
            continue
        record["responseBody"] = body.get("body", "")
        record["base64Encoded"] = bool(body.get("base64Encoded"))


def _to_har(records):
    entries = []
    for record in records.values():
        if record.get("type") not in FETCH_TYPES:
            continue
        request = record.get("request") or {}
        response = record.get("response") or {}
        if not request.get("url"):
            continue
        body = record.get("responseBody") or ""
        content_text = body if not record.get("base64Encoded") else body
        if record.get("base64Encoded") and _looks_binary(response.get("mimeType", "")):
            content_text = ""
        entries.append(
            {
                "startedDateTime": _started_datetime(record.get("wallTime")),
                "time": 0,
                "request": {
                    "method": request.get("method", ""),
                    "url": request.get("url", ""),
                    "headers": _headers_list(request.get("headers") or {}),
                    "postData": {"mimeType": _header_value(request.get("headers") or {}, "content-type"), "text": request.get("postData", "")},
                },
                "response": {
                    "status": response.get("status", ""),
                    "statusText": response.get("statusText", ""),
                    "headers": _headers_list(response.get("headers") or {}),
                    "content": {
                        "mimeType": response.get("mimeType", ""),
                        "text": content_text,
                        "encoding": "base64" if record.get("base64Encoded") and content_text else "",
                    },
                },
            }
        )
    return {"log": {"version": "1.2", "creator": {"name": "seda.network_capture", "version": "1"}, "entries": entries}}


def _graphql_payloads(body):
    parsed = _json_loads(body)
    if isinstance(parsed, dict):
        return [parsed] if _looks_graphql_payload(parsed) else []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict) and _looks_graphql_payload(item)]
    return []


def _looks_graphql_payload(payload):
    return any(key in payload for key in ("operationName", "query", "variables", "extensions"))


def _operation_name(payload):
    explicit = str(payload.get("operationName") or "").strip()
    if explicit:
        return explicit
    query = str(payload.get("query") or "")
    match = re.search(r"\b(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)", query)
    return match.group(1) if match else ""


def _query_head(query):
    query = " ".join(str(query or "").split())
    return query[:180]


def _small_json(value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= 1200:
        return value
    return {"_truncated_json": text[:1200]}


def _json_loads(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _headers_list(headers):
    result = []
    for name, value in sorted(headers.items(), key=lambda item: item[0].lower()):
        safe_value = "[redacted]" if name.lower() in SENSITIVE_HEADER_NAMES else str(value)
        result.append({"name": name, "value": safe_value})
    return result


def _header_value(headers, wanted):
    wanted = wanted.lower()
    for name, value in headers.items():
        if name.lower() == wanted:
            return str(value)
    return ""


def _endpoint(url):
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}"


def _started_datetime(wall_time):
    try:
        return datetime.fromtimestamp(float(wall_time)).isoformat()
    except Exception:
        return datetime.now().isoformat(timespec="seconds")


def _looks_binary(mime_type):
    text = str(mime_type or "").lower()
    return any(token in text for token in ("image/", "font/", "video/", "audio/"))


def _default_output_dir():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(Path("seda") / "data" / "network_capture" / stamp)


if __name__ == "__main__":
    main()
