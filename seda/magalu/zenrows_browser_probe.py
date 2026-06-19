import argparse
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from ..parsers import parse_detail, parse_listing, sku_from_url
from ..step00_config import DEFAULT_RUNS_BASE, run_date
from .review_api import PRODUCT_RATING_QUERY


BASE_URL = "https://www.magazineluiza.com.br"
DEFAULT_LISTING_URL = "https://www.magazineluiza.com.br/busca/tv/"
DEFAULT_PDP_URL = (
    "https://www.magazineluiza.com.br/smart-tv-50-tcl-4k-uhd-qled-50p7k-google-tv-aipq-google-assistente-3-hdmi/"
    "p/240144700/et/tves/?seller_id=magazineluiza"
)


def output_dir():
    override = os.getenv("SEDA_ZENROWS_BROWSER_PROBE_DIR", "").strip()
    if override:
        return Path(override)
    return DEFAULT_RUNS_BASE / "magalu" / run_date() / "zenrows_browser_probe"


def dry_run():
    return os.getenv("SEDA_ZENROWS_BROWSER_DRY_RUN", "1").lower() not in {"0", "false", "no", "n"}


def ensure_execute_allowed(execute):
    if not execute:
        os.environ.setdefault("SEDA_ZENROWS_BROWSER_DRY_RUN", "1")
        return
    os.environ["SEDA_ZENROWS_BROWSER_DRY_RUN"] = "0"
    if os.getenv("SEDA_ALLOW_ZENROWS", "0").lower() not in {"1", "true", "yes", "y"}:
        raise SystemExit("Refusing paid Scraping Browser probe: set SEDA_ALLOW_ZENROWS=1.")
    if not os.getenv("ZENROWS_API_KEY", "").strip():
        raise SystemExit("Refusing paid Scraping Browser probe: ZENROWS_API_KEY is not set.")


def connection_url():
    params = {
        "apikey": os.getenv("ZENROWS_API_KEY", "").strip(),
        "proxy_country": os.getenv("SEDA_ZENROWS_PROXY_COUNTRY", "br"),
        "session_ttl": os.getenv("SEDA_ZENROWS_BROWSER_SESSION_TTL", "2m"),
    }
    return "wss://browser.zenrows.com?" + urlencode(params)


def public_connection_params():
    return {
        "proxy_country": os.getenv("SEDA_ZENROWS_PROXY_COUNTRY", "br"),
        "session_ttl": os.getenv("SEDA_ZENROWS_BROWSER_SESSION_TTL", "2m"),
    }


async def block_route(route):
    blocked = {item.strip() for item in os.getenv("SEDA_ZENROWS_BROWSER_BLOCK", "image,media,font,stylesheet").split(",") if item.strip()}
    if route.request.resource_type in blocked:
        await route.abort()
    else:
        await route.continue_()


async def install_native_fetch_capture(page):
    await page.add_init_script("""
        (() => {
          if (!window.__sedaNativeFetch) {
            window.__sedaNativeFetch = window.fetch.bind(window);
          }
        })();
    """)


async def fetch_page(page, url):
    wait_ms = int(os.getenv("SEDA_ZENROWS_BROWSER_WAIT_MS", "5000"))
    timeout = int(os.getenv("SEDA_ZENROWS_BROWSER_TIMEOUT_MS", "90000"))
    started = time.time()
    response_status = 0
    error = ""
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        response_status = response.status if response else 0
        await page.wait_for_timeout(wait_ms)
        html = await page.content()
    except Exception as exc:
        html = ""
        error = f"{type(exc).__name__}: {exc}"
    return {
        "url": url,
        "final_url": page.url,
        "status_code": response_status,
        "elapsed_seconds": round(time.time() - started, 3),
        "error": error,
        "html": html,
    }


def summarize_listing(html, url):
    summary = {"has_next_data": "__NEXT_DATA__" in (html or "")}
    try:
        rows = parse_listing(html or "", "Magalu", BASE_URL, url, run_id="main")
    except Exception as exc:
        summary["parse_error"] = f"{type(exc).__name__}: {exc}"
        return summary
    summary["parsed_rows"] = len(rows)
    summary["unique_urls"] = len({row.get("product_url", "") for row in rows if row.get("product_url")})
    summary["sample_names"] = [row.get("retailer_sku_name", "") for row in rows[:3]]
    return summary


def summarize_pdp(html, url):
    summary = {"has_next_data": "__NEXT_DATA__" in (html or "")}
    try:
        detail = parse_detail(html or "", "Magalu", BASE_URL, url)
    except Exception as exc:
        summary["parse_error"] = f"{type(exc).__name__}: {exc}"
        return summary
    for key in ("sku", "screen_size", "model_year", "summarized_review_content", "retailer_sku_name_similar"):
        value = detail.get(key, "")
        summary[f"has_{key}"] = bool(value)
        if value and key in {"sku", "screen_size", "model_year"}:
            summary[key] = value
    return summary


def review_payload(variation_id, page=1, page_size=16):
    return {
        "operationName": "ProductRating",
        "variables": {
            "variationId": variation_id,
            "filters": None,
            "includeUserReviews": True,
            "page": page,
            "pageSize": page_size,
            "sortType": "MORE_RELEVANT",
            "hasTag": True,
        },
        "query": PRODUCT_RATING_QUERY,
    }


async def fetch_product_rating_in_page(page, pdp_url):
    variation_id = sku_from_url(pdp_url)
    payload = review_payload(variation_id)
    started = time.time()
    page_fetch_error = ""
    try:
        result = await page.evaluate(
            """
            async ({ payload }) => {
              const nativeFetch = window.__sedaNativeFetch || window.fetch.bind(window);
              const response = await nativeFetch('https://federation.magazineluiza.com.br/graphql', {
                method: 'POST',
                credentials: 'include',
                headers: {
                  'accept': 'application/json',
                  'content-type': 'application/json'
                },
                body: JSON.stringify(payload)
              });
              const text = await response.text();
              let json = null;
              try { json = JSON.parse(text); } catch (error) {}
              return {
                method: window.__sedaNativeFetch ? 'page_evaluate_native_fetch' : 'page_evaluate_fetch',
                status_code: response.status,
                content_type: response.headers.get('content-type') || '',
                text_length: text.length,
                text_preview: text.slice(0, 500),
                json
              };
            }
            """,
            {"payload": payload},
        )
    except Exception as exc:
        page_fetch_error = f"{type(exc).__name__}: {exc}"
        result = {
            "method": "page_evaluate_fetch",
            "status_code": 0,
            "content_type": "",
            "text_length": 0,
            "text_preview": "",
            "json": None,
            "error": page_fetch_error,
        }

    result["elapsed_seconds"] = round(time.time() - started, 3)
    result["variation_id"] = variation_id
    result["summary"] = summarize_review_graphql(result.get("json"))
    if not result["summary"].get("success"):
        fallback = await fetch_product_rating_with_context_request(page, payload, pdp_url, variation_id)
        fallback["page_fetch_error"] = page_fetch_error or result.get("error", "")
        return fallback
    return result


async def fetch_product_rating_with_context_request(page, payload, pdp_url, variation_id):
    started = time.time()
    try:
        response = await page.context.request.post(
            "https://federation.magazineluiza.com.br/graphql",
            data=json.dumps(payload),
            headers={
                "accept": "application/json",
                "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "content-type": "application/json",
                "origin": "https://www.magazineluiza.com.br",
                "referer": pdp_url,
            },
        )
        text = await response.text()
        parsed = None
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
        result = {
            "method": "context_request_post",
            "status_code": response.status,
            "content_type": response.headers.get("content-type", ""),
            "text_length": len(text),
            "text_preview": text[:500],
            "json": parsed,
        }
    except Exception as exc:
        result = {
            "method": "context_request_post",
            "status_code": 0,
            "content_type": "",
            "text_length": 0,
            "text_preview": "",
            "json": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    result["elapsed_seconds"] = round(time.time() - started, 3)
    result["variation_id"] = variation_id
    result["summary"] = summarize_review_graphql(result.get("json"))
    return result


def summarize_review_graphql(parsed):
    product_rating = (((parsed or {}).get("data") or {}).get("productRating") or {}) if isinstance(parsed, dict) else {}
    general = product_rating.get("general") if isinstance(product_rating.get("general"), dict) else {}
    user_reviews = product_rating.get("userReviews") if isinstance(product_rating.get("userReviews"), dict) else {}
    items = user_reviews.get("items") if isinstance(user_reviews.get("items"), list) else []
    descriptions = [str(item.get("description") or "").strip() for item in items if isinstance(item, dict) and str(item.get("description") or "").strip()]
    return {
        "has_product_rating": bool(product_rating),
        "rating": general.get("rating", ""),
        "reviewCount": general.get("reviewCount", ""),
        "commentCount": general.get("commentCount", ""),
        "review_items": len(items),
        "review_descriptions": len(descriptions),
        "sample_reviews": descriptions[:3],
        "success": bool(product_rating and (general or descriptions)),
    }


async def execute_probe(probe, listing_url, pdp_url):
    if dry_run():
        return {
            "probe": probe,
            "dry_run": True,
            "connection": public_connection_params(),
            "planned_urls": {"listing": listing_url, "pdp": pdp_url},
            "results": [],
        }

    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: playwright. Install project dependencies with: "
            "python -m pip install -r requirements.txt"
        ) from exc

    started_at = datetime.now().isoformat(timespec="seconds")
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(connection_url())
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
            page = await context.new_page()
            await install_native_fetch_capture(page)
            await page.route("**/*", block_route)
            if probe in {"listing", "all"}:
                page_result = await fetch_page(page, listing_url)
                page_result["summary"] = summarize_listing(page_result.get("html", ""), listing_url)
                results.append({"kind": "listing", **page_result})
            if probe in {"pdp", "review", "all"}:
                page_result = await fetch_page(page, pdp_url)
                page_result["summary"] = summarize_pdp(page_result.get("html", ""), pdp_url)
                if probe in {"pdp", "all"}:
                    results.append({"kind": "pdp", **page_result})
            if probe in {"review", "all"}:
                review_result = await fetch_product_rating_in_page(page, pdp_url)
                results.append({"kind": "review", "url": pdp_url, **review_result})
        finally:
            await browser.close()
    return {
        "probe": probe,
        "dry_run": False,
        "started_at": started_at,
        "ended_at": datetime.now().isoformat(timespec="seconds"),
        "connection": public_connection_params(),
        "results": results,
    }


def write_probe(result):
    directory = output_dir()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for item in result.get("results", []):
        html = item.pop("html", "")
        if html:
            body_path = directory / f"{timestamp}_{item['kind']}.html"
            body_path.write_text(html, encoding="utf-8", errors="ignore")
            item["body_file"] = str(body_path)
    meta_path = directory / f"{timestamp}_{result['probe']}.meta.json"
    meta_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta_path


def main():
    parser = argparse.ArgumentParser(description="ZenRows Scraping Browser probe for Magalu")
    parser.add_argument("probe", choices=["listing", "pdp", "review", "all"])
    parser.add_argument("--listing-url", default=DEFAULT_LISTING_URL)
    parser.add_argument("--pdp-url", default=DEFAULT_PDP_URL)
    parser.add_argument("--execute", action="store_true", help="Execute paid Scraping Browser session. Requires SEDA_ALLOW_ZENROWS=1 and API key.")
    args = parser.parse_args()

    ensure_execute_allowed(args.execute)
    result = asyncio.run(execute_probe(args.probe, args.listing_url, args.pdp_url))
    meta_path = write_probe(result)
    print(f"[zenrows_browser_probe] wrote {meta_path}")


if __name__ == "__main__":
    main()