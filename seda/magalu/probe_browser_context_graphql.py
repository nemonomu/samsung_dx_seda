import argparse
import csv
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from seda.magalu.probe_curl_replay_graphql import parse_curl_file
from seda.step00_config import DEFAULT_RUNS_BASE, run_date


DEFAULT_PDP_URL = (
    "https://www.magazineluiza.com.br/"
    "smart-tv-50-tcl-4k-uhd-qled-50p7k-google-tv-aipq-google-assistente-3-hdmi/"
    "p/240144700/et/tves/?seller_id=magazineluiza"
)

CSV_COLUMNS = [
    "name",
    "operation_name",
    "profile",
    "status_code",
    "seconds",
    "bytes",
    "content_type",
    "success",
    "delivery",
    "pickup",
    "similar_count",
    "similar_names",
    "error",
    "graphql_error",
    "response_preview",
    "safe_header_names",
    "pdp_url",
    "browser_url",
    "browser_html_len",
]


def main():
    args = parse_args()
    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_RUNS_BASE / "magalu" / run_date() / "browser_context_graphql"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"magalu_browser_context_graphql_{stamp}.csv"
    json_path = out_dir / f"magalu_browser_context_graphql_{stamp}.json"

    curl_paths = resolve_curl_paths(args)
    cases = []
    if curl_paths["shipping"].exists():
        cases.extend(parse_curl_file(curl_paths["shipping"], allowed_operations={"shippingQuery"}))
    if curl_paths["showcase"].exists():
        cases.extend(parse_curl_file(curl_paths["showcase"], allowed_operations={"showcaseQuery"}))

    rows = []
    meta = {
        "pdp_url": args.pdp_url,
        "shipping_curl": str(curl_paths["shipping"]),
        "shipping_curl_exists": curl_paths["shipping"].exists(),
        "showcase_curl": str(curl_paths["showcase"]),
        "showcase_curl_exists": curl_paths["showcase"].exists(),
        "case_count": len(cases),
    }
    write_outputs(csv_path, json_path, rows, meta)

    print(
        "[browser_context] "
        f"cases={len(cases)} shipping_exists={int(meta['shipping_curl_exists'])} "
        f"showcase_exists={int(meta['showcase_curl_exists'])}",
        flush=True,
    )
    if not cases:
        print(f"[browser_context] wrote {json_path}")
        print(f"[browser_context] wrote {csv_path}")
        return

    page_state = open_pdp(args.pdp_url, args.nav_timeout, args.wait_seconds)
    meta.update(page_state)
    write_outputs(csv_path, json_path, rows, meta)
    print(
        "[browser_context] "
        f"pdp_open url={short(page_state.get('browser_url'))} "
        f"html_len={page_state.get('browser_html_len', 0)} "
        f"error={page_state.get('browser_error') or '-'}",
        flush=True,
    )

    for case in cases:
        for profile in profiles(args):
            row = execute_case(case, profile, args.timeout, args.pdp_url, page_state)
            rows.append(row)
            write_outputs(csv_path, json_path, rows, meta)
            print(
                "[browser_context] "
                f"{row['operation_name']} profile={row['profile']} status={row['status_code']} "
                f"success={row['success']} delivery={bool(row['delivery'])} "
                f"pickup={bool(row['pickup'])} similar={row['similar_count']} "
                f"error={row['error'] or '-'} seconds={row['seconds']}",
                flush=True,
            )

    print(f"[browser_context] wrote {json_path}")
    print(f"[browser_context] wrote {csv_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Probe Magalu GraphQL from inside an already-open PDP browser context.")
    parser.add_argument("--shipping-curl", default="")
    parser.add_argument("--showcase-curl", default="")
    parser.add_argument("--pdp-url", default=os.getenv("SEDA_MAGALU_PROBE_PDP_URL", DEFAULT_PDP_URL))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_TIMEOUT", "60")))
    parser.add_argument("--nav-timeout", type=float, default=float(os.getenv("SEDA_MAGALU_PROBE_NAV_TIMEOUT", "30")))
    parser.add_argument("--wait-seconds", type=float, default=float(os.getenv("SEDA_MAGALU_PROBE_WAIT_SECONDS", "5")))
    parser.add_argument(
        "--profiles",
        default=os.getenv("SEDA_MAGALU_BROWSER_CONTEXT_PROFILES", "default,include_credentials,captured_safe_include_credentials"),
    )
    return parser.parse_args()


def resolve_curl_paths(args):
    root = Path(__file__).resolve().parents[2]
    candidates = [
        root / "references" / "260625",
        root / "seda" / "references" / "260625",
    ]
    shipping = Path(args.shipping_curl) if args.shipping_curl else find_first(candidates, "magalu_shippingQuery_240144700_curl.txt")
    showcase = Path(args.showcase_curl) if args.showcase_curl else find_first(candidates, "magalu_showcaseQuery_240144700_curl.txt")
    return {"shipping": shipping, "showcase": showcase}


def find_first(directories, filename):
    for directory in directories:
        path = directory / filename
        if path.exists():
            return path
    return directories[0] / filename


def profiles(args):
    result = []
    for item in str(args.profiles or "").split(","):
        item = item.strip()
        if item:
            result.append(item)
    return result or ["include_credentials"]


def open_pdp(url, nav_timeout, wait_seconds):
    try:
        from seda.magalu.browser_session import get_page

        page = get_page()
        try:
            page.get(url, timeout=nav_timeout)
        except Exception as exc:
            nav_error = f"{type(exc).__name__}: {exc}"
        else:
            nav_error = ""
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            page.stop_loading()
        except Exception:
            pass
        try:
            browser_url = page.url or ""
            html = page.html or ""
        except Exception:
            browser_url = ""
            html = ""
        return {
            "browser_url": browser_url,
            "browser_html_len": len(html),
            "browser_error": nav_error,
        }
    except Exception as exc:
        return {
            "browser_url": "",
            "browser_html_len": 0,
            "browser_error": f"{type(exc).__name__}: {exc}",
        }


def execute_case(case, profile, timeout, pdp_url, page_state):
    started = time.perf_counter()
    status_code = 0
    text = ""
    content_type = ""
    error = ""
    try:
        safe_headers = browser_settable_headers(case.get("headers") or {})
        result = browser_fetch(case["json"], profile, timeout, safe_headers)
        status_code = int(result.get("status") or 0)
        text = result.get("text") or ""
        content_type = result.get("contentType") or ""
        error = result.get("error") or ""
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    summary = summarize_payload(case, text, status_code, content_type)
    if not error:
        error = summary.pop("error", "")
    else:
        summary.pop("error", None)
    return {
        "name": case.get("name", ""),
        "operation_name": case["json"].get("operationName") or "",
        "profile": profile,
        "status_code": status_code,
        "seconds": round(time.perf_counter() - started, 3),
        "bytes": len(text),
        "content_type": content_type,
        "error": error,
        "response_preview": short(text, 240),
        "safe_header_names": ",".join(sorted(safe_headers)),
        "pdp_url": pdp_url,
        "browser_url": page_state.get("browser_url", ""),
        "browser_html_len": page_state.get("browser_html_len", 0),
        **summary,
    }


def browser_fetch(payload, profile, timeout, safe_headers=None):
    from seda.magalu.browser_session import get_page

    page = get_page()
    safe_headers = safe_headers or {}
    payload_text = json.dumps(payload, ensure_ascii=False)
    script = """
return (async () => {
  try {
    const payload = JSON.parse(arguments[0]);
    const profile = arguments[1] || '';
    const safeHeaders = arguments[2] || {};
    const operation = payload.operationName || '';
    const endpoint = 'https://federation.magazineluiza.com.br/graphql?operationName=' + encodeURIComponent(operation);
    const headers = {
      'accept': 'application/json',
      'content-type': 'application/json',
      'x-channel-id': '45',
      'x-channel-name': 'mixer-desk.magazineluiza.com.br'
    };
    if (profile.indexOf('captured_safe') >= 0) {
      headers['cache-control'] = 'no-cache';
      headers['pragma'] = 'no-cache';
    }
    if (profile.indexOf('captured_safe') >= 0) {
      Object.entries(safeHeaders).forEach(([key, value]) => {
        if (value) headers[key] = value;
      });
    }
    const options = {
      method: 'POST',
      mode: 'cors',
      cache: 'no-cache',
      headers,
      body: JSON.stringify(payload)
    };
    if (profile.indexOf('include_credentials') >= 0) {
      options.credentials = 'include';
    }
    const response = await fetch(endpoint, options);
    const text = await response.text();
    return JSON.stringify({
      status: response.status,
      contentType: response.headers.get('content-type') || '',
      text,
      pageUrl: location.href
    });
  } catch (error) {
    return JSON.stringify({status: 0, contentType: '', text: '', error: String(error), pageUrl: location.href});
  }
})()
"""
    raw = page.run_js(script, payload_text, profile, safe_headers, timeout=timeout) or "{}"
    return json.loads(raw) if isinstance(raw, str) else raw


def summarize_payload(case, text, status_code, content_type):
    operation = case["json"].get("operationName") or ""
    base = {
        "success": 0,
        "delivery": "",
        "pickup": "",
        "similar_count": 0,
        "similar_names": "",
        "error": "",
        "graphql_error": "",
    }
    data = {}
    if "json" in (content_type or "").lower() and text:
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
    if isinstance(data, dict) and data.get("errors"):
        base["graphql_error"] = " | ".join(clean_text((item or {}).get("message")) for item in data.get("errors") or [] if isinstance(item, dict))
    if status_code != 200:
        base["error"] = f"http_{status_code}" if status_code else "status_0"
        return base
    if "json" not in (content_type or "").lower():
        base["error"] = "non_json"
        return base
    if not data:
        base["error"] = "invalid_json"
        return base
    if data.get("errors"):
        base["error"] = "graphql_errors"
        return base
    if operation == "shippingQuery":
        delivery, pickup = shipping_texts((data.get("data") or {}).get("shipping") or {})
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
        if not is_similar_showcase_title(showcase.get("title")):
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


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def ascii_lower(value):
    import unicodedata

    normalized = unicodedata.normalize("NFKD", clean_text(value))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def browser_settable_headers(headers):
    allowed = {
        "accept",
        "accept-language",
        "cache-control",
        "content-type",
        "pragma",
        "x-channel-id",
        "x-channel-name",
    }
    blocked = {
        "authorization",
        "cookie",
        "host",
        "origin",
        "referer",
        "set-cookie",
        "user-agent",
    }
    result = {}
    for key, value in (headers or {}).items():
        normalized = str(key or "").strip().lower()
        if not normalized or normalized in blocked or normalized.startswith("sec-"):
            continue
        if normalized not in allowed:
            continue
        result[normalized] = str(value or "")
    return result


def short(value, limit=120):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def write_outputs(csv_path, json_path, rows, meta):
    json_path.write_text(json.dumps({"meta": meta, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
