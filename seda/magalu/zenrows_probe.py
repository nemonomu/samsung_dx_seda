import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from ..parsers import parse_detail, parse_listing
from ..step00_config import DEFAULT_RUNS_BASE, run_date
from .zenrows_client import _result_to_next_data_html, fetch_html, request_url


BASE_URL = "https://www.magazineluiza.com.br"
DEFAULT_LISTING_URL = "https://www.magazineluiza.com.br/busca/tv/"
DEFAULT_PDP_URL = (
    "https://www.magazineluiza.com.br/smart-tv-50-tcl-4k-uhd-qled-50p7k-google-tv-aipq-google-assistente-3-hdmi/"
    "p/240144700/et/tves/?seller_id=magazineluiza"
)

PROBES = {
    "listing": {"profile": "auto_html", "url": DEFAULT_LISTING_URL, "kind": "listing"},
    "pdp": {"profile": "pdp_next_data", "url": DEFAULT_PDP_URL, "kind": "pdp"},
    "xhr": {"profile": "xhr_discovery", "url": DEFAULT_PDP_URL, "kind": "xhr"},
}


def probe_dir():
    override = os.getenv("SEDA_ZENROWS_PROBE_DIR", "").strip()
    if override:
        return Path(override)
    return DEFAULT_RUNS_BASE / "magalu" / run_date() / "zenrows_probe"


def ensure_execute_allowed(execute):
    if not execute:
        os.environ.setdefault("SEDA_ZENROWS_DRY_RUN", "1")
        return
    os.environ["SEDA_ZENROWS_DRY_RUN"] = "0"
    if os.getenv("SEDA_ALLOW_ZENROWS", "0").lower() not in {"1", "true", "yes", "y"}:
        raise SystemExit("Refusing paid probe: set SEDA_ALLOW_ZENROWS=1 to execute ZenRows calls.")
    if not os.getenv("ZENROWS_API_KEY", "").strip():
        raise SystemExit("Refusing paid probe: ZENROWS_API_KEY is not set.")


def run_probe(name, url=None, profile_override=None):
    spec = PROBES[name]
    target_url = url or spec["url"]
    profile = profile_override or spec["profile"]
    started = datetime.now().isoformat(timespec="seconds")
    if name == "pdp":
        from .zenrows_client import fetch_next_data_html

        result = fetch_next_data_html(target_url, profile=profile)
    else:
        result = request_url(target_url, profile=profile)
    ended = datetime.now().isoformat(timespec="seconds")
    summary = summarize(name, target_url, result)
    return {
        "probe": name,
        "profile": result.profile,
        "url": target_url,
        "started_at": started,
        "ended_at": ended,
        "success": result.success,
        "status_code": result.status_code,
        "error": result.error,
        "estimated_multiplier": result.estimated_multiplier,
        "params": result.params,
        "response_headers": result.headers,
        "response_length": len(result.text or ""),
        "summary": summary,
        "body": result.text or "",
    }


def summarize(name, url, result):
    text = result.text or ""
    summary = {
        "has_next_data": "__NEXT_DATA__" in text,
        "looks_blocked": any(marker in text.lower() for marker in ("access denied", "captcha", "akamai", "customdeny")),
    }
    if not text:
        return summary
    if name == "listing":
        parse_text = text if "__NEXT_DATA__" in text else _result_to_next_data_html(text)
        summary["has_next_data"] = "__NEXT_DATA__" in parse_text
        try:
            rows = parse_listing(parse_text or text, "Magalu", BASE_URL, url, run_id="main")
        except Exception as exc:
            summary["parse_error"] = f"{type(exc).__name__}: {exc}"
        else:
            summary["parsed_rows"] = len(rows)
            summary["unique_urls"] = len({row.get("product_url", "") for row in rows if row.get("product_url")})
            summary["sample_names"] = [row.get("retailer_sku_name", "") for row in rows[:3]]
    elif name == "pdp":
        try:
            detail = parse_detail(text, "Magalu", BASE_URL, url)
        except Exception as exc:
            summary["parse_error"] = f"{type(exc).__name__}: {exc}"
        else:
            for key in ("sku", "screen_size", "model_year", "summarized_review_content", "retailer_sku_name_similar"):
                value = detail.get(key, "")
                summary[f"has_{key}"] = bool(value)
                if value and key in {"sku", "screen_size", "model_year"}:
                    summary[key] = value
    elif name == "xhr":
        try:
            parsed = json.loads(text)
        except ValueError:
            summary["json_parse_error"] = True
        else:
            xhr = parsed.get("xhr") if isinstance(parsed, dict) else []
            summary["xhr_count"] = len(xhr or [])
            summary["graphql_count"] = len([item for item in (xhr or []) if "graphql" in str(item.get("url", "")).lower()])
            summary["sample_xhr_urls"] = [item.get("url", "") for item in (xhr or [])[:10] if isinstance(item, dict)]
    return summary


def write_result(output_dir, result):
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{timestamp}_{result['probe']}_{result['profile']}"
    body = result.pop("body", "")
    suffix = ".json" if result["probe"] == "xhr" else ".html"
    body_path = output_dir / f"{stem}{suffix}"
    meta_path = output_dir / f"{stem}.meta.json"
    if body:
        body_path.write_text(body, encoding="utf-8", errors="ignore")
        result["body_file"] = str(body_path)
    else:
        result["body_file"] = ""
    meta_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta_path


def main():
    parser = argparse.ArgumentParser(description="Magalu ZenRows probe runner")
    parser.add_argument("probe", choices=["listing", "pdp", "xhr", "all"])
    parser.add_argument("--url", help="Override URL for a single probe")
    parser.add_argument("--profile", help="Override ZenRows profile for a probe")
    parser.add_argument("--execute", action="store_true", help="Execute paid ZenRows calls. Requires SEDA_ALLOW_ZENROWS=1 and API key.")
    args = parser.parse_args()

    ensure_execute_allowed(args.execute)
    names = ["listing", "pdp", "xhr"] if args.probe == "all" else [args.probe]
    if args.url and len(names) > 1:
        raise SystemExit("--url can only be used with a single probe")
    output_dir = probe_dir()
    for name in names:
        result = run_probe(name, url=args.url, profile_override=args.profile)
        meta_path = write_result(output_dir, result)
        print(f"[zenrows_probe] {name} wrote {meta_path}")


if __name__ == "__main__":
    main()


