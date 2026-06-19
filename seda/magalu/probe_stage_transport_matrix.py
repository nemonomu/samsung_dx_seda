import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from seda.parsers import parse_detail, parse_listing
from seda.step00_config import DEFAULT_RUNS_BASE, page_url, run_date, write_json
from seda.transport import fetch_attempts

from .zenrows_client import PROFILE_PARAMS, _result_to_next_data_html, estimated_multiplier, request_url


BASE_URL = "https://www.magazineluiza.com.br"
DEFAULT_PDP_URL = (
    "https://www.magazineluiza.com.br/smart-tv-50-tcl-4k-uhd-qled-50p7k-google-tv-aipq-google-assistente-3-hdmi/"
    "p/240144700/et/tves/?seller_id=magazineluiza"
)

STAGE_RECOMMENDED_PROFILES = {
    "listing": ["auto_html", "listing_next_data_js_wait", "listing_next_data_js", "xhr_discovery"],
    "detail": ["pdp_next_data", "pdp_next_data_js", "pdp_js_full", "xhr_discovery"],
    "review": ["xhr_discovery"],
}

ALL_UNIVERSAL_API_PROFILES = list(PROFILE_PARAMS.keys())


def main():
    args = parse_args()
    output_dir = Path(args.output_dir or _default_output_dir())
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.execute:
        _assert_paid_execution_allowed()
        os.environ["SEDA_ZENROWS_DRY_RUN"] = "0"
    else:
        os.environ.setdefault("SEDA_ZENROWS_DRY_RUN", "1")

    matrix = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(output_dir),
        "execute": bool(args.execute),
        "current_pipeline": current_pipeline_summary(),
        "zenrows_doc_findings": zenrows_doc_findings(),
        "planned_matrix": build_planned_matrix(args),
        "execution_results": [],
    }

    if args.execute:
        matrix["execution_results"] = execute_matrix(args, output_dir)

    matrix["finished_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(output_dir / "stage_transport_matrix.json", matrix)
    print(f"[probe] wrote {output_dir / 'stage_transport_matrix.json'}")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or execute a Magalu stage transport matrix. "
            "ZenRows calls are paid and require --execute plus SEDA_ALLOW_ZENROWS=1."
        )
    )
    parser.add_argument("--stages", nargs="+", default=["listing", "detail", "review"], choices=["listing", "detail", "review"])
    parser.add_argument(
        "--profile-set",
        default="all",
        choices=["all", "recommended"],
        help="Profile set to test. Default all sequentially tests every profile in zenrows_client.PROFILE_PARAMS.",
    )
    parser.add_argument("--profiles", nargs="+", default=[], help="Override ZenRows profiles for every selected stage. Use 'all' for every profile.")
    parser.add_argument("--product-line", default="TV", choices=["TV", "REF", "LDY"])
    parser.add_argument("--listing-run-id", default="main", choices=["main", "bsr"])
    parser.add_argument("--listing-page", type=int, default=1)
    parser.add_argument("--pdp-url", default=DEFAULT_PDP_URL)
    parser.add_argument("--listing-pages-full-run", type=int, default=9)
    parser.add_argument("--bsr-pages-full-run", type=int, default=3)
    parser.add_argument("--target-skus", type=int, default=300)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--execute", action="store_true", help="Execute paid ZenRows calls.")
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def current_pipeline_summary():
    fetch_mode = os.getenv("SEDA_FETCH_MODE", "uc_first")
    return {
        "listing": {
            "entrypoint": "seda.transport.fetch_url -> seda.magalu.search_api.fetch_search_listing",
            "fetch_mode": fetch_mode,
            "attempt_order": fetch_attempts(fetch_mode),
            "magalu_graphql_path": [
                "optional UC/page HTML attempt",
                "browser GraphQL search via DrissionPage session",
                "direct GraphQL POST to federation.magazineluiza.com.br/graphql",
                "requests HTML fallback",
                "ZenRows only when SEDA_ALLOW_ZENROWS=1",
            ],
            "current_risk": "automation browser context/fingerprint can return 403 even when manual Chrome works",
        },
        "detail": {
            "entrypoint": "seda.step08_detail_enrichment._magalu_graphql_detail",
            "primary": "seda.magalu.detail_api.fetch_detail itemQuery via browser_graphql",
            "subcalls": ["shippingQuery", "showcaseQuery for similar names"],
            "direct_fallback": "direct requests GraphQL only if SEDA_MAGALU_BROWSER_GRAPHQL_STRICT=0",
            "html_fallback": "off by default for Magalu unless SEDA_MAGALU_DETAIL_HTML_FALLBACK=1",
            "blocked_circuit_breaker": "SEDA_MAGALU_DETAIL_403_ABORT_THRESHOLD",
        },
        "review": {
            "entrypoint": "seda.step08_detail_enrichment._merge_magalu_reviews",
            "primary": "seda.magalu.review_api.fetch_product_rating ProductRating via browser_graphql",
            "direct_fallback": "direct requests GraphQL only if SEDA_MAGALU_BROWSER_GRAPHQL_STRICT=0",
            "limit_rule": "max 20 bodies, capped by count_of_reviews when count is known",
            "blocked_circuit_breaker": "SEDA_MAGALU_REVIEW_403_ABORT_THRESHOLD",
        },
        "pdp_html_enrichment": {
            "entrypoint": "seda.step08_detail_enrichment._merge_magalu_pdp_html",
            "primary": "seda.magalu.browser_session.fetch_html",
            "used_for": ["summarized_review_content", "retailer_sku_name_similar"],
            "zenrows_fallback": "off unless SEDA_MAGALU_ZENROWS_PDP_FALLBACK=1",
        },
    }


def zenrows_doc_findings():
    return {
        "source_path": "C:/zenrows_doc",
        "docs_reviewed": [
            "llms.txt index",
            "first-steps/pricing.md",
            "universal-scraper-api/features/adaptive-stealth-mode.md",
            "universal-scraper-api/features/js-rendering.md",
            "universal-scraper-api/features/json-response.md",
            "universal-scraper-api/features/wait.md",
            "universal-scraper-api/features/wait-for.md",
            "universal-scraper-api/features/block-resources.md",
            "universal-scraper-api/features/css-extractor.md",
            "universal-scraper-api/features/proxy-country.md",
            "scraping-browser/*",
        ],
        "rules": [
            "mode=auto lets ZenRows escalate js_render/premium_proxy and bills only the successful configuration.",
            "js_render costs 5x; premium_proxy costs 10x; both together cost 25x for Universal Scraper API.",
            "json_response requires js_render and captures XHR/Fetch for endpoint discovery.",
            "wait and wait_for require js_render; wait_for is selector-specific and can fail if selector never appears.",
            "block_resources can reduce response size for js_render/json_response probes.",
            "proxy_country=br is the correct country-level option for Brazil; city-level Sao Paulo targeting is not supported in Universal Scraper API docs.",
            "X-Request-Cost and concurrency headers should be persisted when executing probes.",
        ],
        "test_policy": [
            "Default is dry-run so the script can be committed and inspected without cost.",
            "Paid calls require --execute and SEDA_ALLOW_ZENROWS=1 and ZENROWS_API_KEY.",
            "The default profile set is all: every profile in seda.magalu.zenrows_client.PROFILE_PARAMS is tested sequentially.",
            "Use --profile-set recommended for cheaper stage-specific smoke tests.",
            "Use xhr_discovery only as a reference capture in production, even though this test can run it for every stage.",
        ],
    }


def build_planned_matrix(args):
    rows = []
    urls = {
        "listing": listing_url(args),
        "detail": args.pdp_url,
        "review": args.pdp_url,
    }
    for stage in args.stages:
        profiles = selected_profiles(stage, args)
        for profile in profiles:
            params = dict(PROFILE_PARAMS.get(profile, {}))
            rows.append(
                {
                    "stage": stage,
                    "profile": profile,
                    "url": urls[stage],
                    "params": params,
                    "estimated_multiplier": estimated_multiplier(params),
                    "recommended_use": recommended_use(stage, profile),
                    "full_run_call_estimate": full_run_call_estimate(stage, args),
                }
            )
    return rows


def selected_profiles(stage, args):
    if args.profiles:
        if len(args.profiles) == 1 and args.profiles[0].lower() == "all":
            return ALL_UNIVERSAL_API_PROFILES[:]
        return args.profiles
    if args.profile_set == "recommended":
        return STAGE_RECOMMENDED_PROFILES[stage]
    return ALL_UNIVERSAL_API_PROFILES[:]


def recommended_use(stage, profile):
    if profile == "auto_html":
        return "first paid fallback candidate; cost is adaptive"
    if profile == "xhr_discovery":
        return "endpoint discovery only; not for every SKU"
    if "next_data" in profile:
        return "extract __NEXT_DATA__ with smaller body than full HTML"
    if "js_full" in profile or "listing_js_full" in profile:
        return "debug full rendered HTML when extractor is insufficient"
    return f"{stage} probe"


def full_run_call_estimate(stage, args):
    if stage == "listing":
        return args.listing_pages_full_run + args.bsr_pages_full_run
    if stage in {"detail", "review"}:
        return args.target_skus
    return 0


def execute_matrix(args, output_dir):
    results = []
    for row in build_planned_matrix(args):
        stage = row["stage"]
        profile = row["profile"]
        url = row["url"]
        started = time.monotonic()
        result = request_url(url, profile=profile, timeout=args.timeout)
        elapsed = round(time.monotonic() - started, 3)
        summary = summarize_result(stage, url, result.text or "")
        body_file = write_body(output_dir, stage, profile, result.text or "")
        results.append(
            {
                "stage": stage,
                "profile": profile,
                "url": url,
                "success": result.success,
                "status_code": result.status_code,
                "error": result.error,
                "elapsed_seconds": elapsed,
                "response_length": len(result.text or ""),
                "estimated_multiplier": result.estimated_multiplier,
                "params": result.params,
                "response_headers": result.headers,
                "summary": summary,
                "body_file": str(body_file) if body_file else "",
            }
        )
        print(
            f"[probe] {stage} profile={profile} status={result.status_code} "
            f"success={int(result.success)} seconds={elapsed} cost={result.estimated_multiplier}"
        )
    return results


def summarize_result(stage, url, text):
    summary = {
        "has_text": bool(text),
        "has_next_data": "__NEXT_DATA__" in text,
        "looks_blocked": any(marker in text.lower() for marker in ("access denied", "captcha", "akamai", "customdeny", "nao e possivel")),
    }
    if not text:
        return summary
    if stage == "listing":
        parse_text = text if "__NEXT_DATA__" in text else _result_to_next_data_html(text)
        try:
            rows = parse_listing(parse_text or text, "Magalu", BASE_URL, url, run_id="main")
        except Exception as exc:
            summary["parse_error"] = f"{type(exc).__name__}: {exc}"
        else:
            summary["parsed_rows"] = len(rows)
            summary["unique_urls"] = len({row.get("product_url", "") for row in rows if row.get("product_url")})
            summary["sample_names"] = [row.get("retailer_sku_name", "") for row in rows[:3]]
    elif stage == "detail":
        parse_text = text if "__NEXT_DATA__" in text else _result_to_next_data_html(text)
        try:
            detail = parse_detail(parse_text or text, "Magalu", BASE_URL, url)
        except Exception as exc:
            summary["parse_error"] = f"{type(exc).__name__}: {exc}"
        else:
            for key in (
                "sku",
                "screen_size",
                "model_year",
                "delivery_availability",
                "pick_up_availability",
                "summarized_review_content",
                "retailer_sku_name_similar",
            ):
                summary[f"has_{key}"] = bool(detail.get(key))
    elif stage == "review":
        try:
            parsed = json.loads(text)
        except ValueError:
            summary["json_response"] = False
        else:
            xhr = parsed.get("xhr") if isinstance(parsed, dict) else []
            summary["json_response"] = True
            summary["xhr_count"] = len(xhr or [])
            summary["graphql_count"] = len([item for item in (xhr or []) if "graphql" in str(item.get("url", "")).lower()])
            summary["sample_xhr_urls"] = [item.get("url", "") for item in (xhr or [])[:10] if isinstance(item, dict)]
    return summary


def listing_url(args):
    os.environ["SEDA_PRODUCT_LINE"] = args.product_line
    from seda.step00_config import RETAILERS

    return page_url(RETAILERS["magalu"], args.listing_page, run_id=args.listing_run_id)


def write_body(output_dir, stage, profile, text):
    if not text:
        return None
    suffix = ".json" if _looks_json(text) else ".html"
    body_dir = output_dir / "raw"
    body_dir.mkdir(parents=True, exist_ok=True)
    path = body_dir / f"{stage}_{profile}{suffix}"
    path.write_text(text, encoding="utf-8", errors="ignore")
    return path


def _looks_json(text):
    stripped = (text or "").lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _assert_paid_execution_allowed():
    if os.getenv("SEDA_ALLOW_ZENROWS", "0").lower() not in {"1", "true", "yes", "y"}:
        raise SystemExit("Refusing paid ZenRows test: set SEDA_ALLOW_ZENROWS=1.")
    if not os.getenv("ZENROWS_API_KEY", "").strip():
        raise SystemExit("Refusing paid ZenRows test: ZENROWS_API_KEY is not set.")


def _default_output_dir():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(DEFAULT_RUNS_BASE / "magalu" / run_date() / "stage_transport_matrix" / stamp)


if __name__ == "__main__":
    main()
