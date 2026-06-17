import argparse
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from ..step00_config import DEFAULT_RUNS_BASE, run_date


DEFAULT_PDP_URL = (
    "https://www.casasbahia.com.br/"
    "smart-tv-43-aoc-43s5155-78g-full-hd-led-wi-fi-roku-tv-dolby-audio/p/55071718?frete=01010-010"
)
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "apikey"}
KEYWORDS = ("consumo", "energia", "especifica", "caracteristica", "tamanho da tela", "spec")


def output_dir():
    override = os.getenv("SEDA_CASAS_BAHIA_ZENROWS_BROWSER_DIR", "").strip()
    if override:
        return Path(override)
    return DEFAULT_RUNS_BASE / "casas_bahia" / run_date() / "zenrows_browser_capture"


def ensure_execute_allowed(execute):
    if not execute:
        return
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
    blocked = {
        item.strip()
        for item in os.getenv("SEDA_ZENROWS_BROWSER_BLOCK", "image,media,font,stylesheet").split(",")
        if item.strip()
    }
    if route.request.resource_type in blocked:
        await route.abort()
    else:
        await route.continue_()


def _safe_headers(headers):
    return {
        key: ("[redacted]" if key.lower() in SENSITIVE_HEADERS else value)
        for key, value in (headers or {}).items()
    }


async def _capture_response(response, captured):
    request = response.request
    url = response.url
    resource_type = request.resource_type
    if resource_type not in {"xhr", "fetch"} and not any(token in url.lower() for token in ("api", "graphql", "recommend", "review", "freight")):
        return
    body = ""
    body_error = ""
    try:
        body = await response.text()
    except Exception as exc:
        body_error = f"{type(exc).__name__}: {exc}"
    captured.append(
        {
            "url": url,
            "endpoint": _endpoint(url),
            "method": request.method,
            "status_code": response.status,
            "resource_type": resource_type,
            "request_headers": _safe_headers(request.headers),
            "request_post_data": request.post_data or "",
            "response_headers": _safe_headers(response.headers),
            "body": body,
            "body_error": body_error,
        }
    )


async def _click_by_text(page, text):
    locator = page.get_by_text(text, exact=False).first
    try:
        await locator.scroll_into_view_if_needed(timeout=3000)
        await locator.click(timeout=3000)
        return True
    except Exception:
        return False


async def execute_capture(args):
    if not args.execute:
        return {
            "dry_run": True,
            "url": args.url,
            "connection": public_connection_params(),
            "planned_click_text": args.click_text,
        }

    from playwright.async_api import async_playwright

    captured = []
    started = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(connection_url())
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
            page = await context.new_page()
            page.on("response", lambda response: asyncio.create_task(_capture_response(response, captured)))
            await page.route("**/*", block_route)
            response = await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            await page.wait_for_timeout(args.wait_ms)
            for _ in range(args.scrolls):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(args.scroll_wait_ms)
            for text in args.click_text:
                if await _click_by_text(page, text):
                    await page.wait_for_timeout(args.after_click_wait_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            html = await page.content()
            title = await page.title()
            final_url = page.url
            status_code = response.status if response else 0
        finally:
            await browser.close()
    return {
        "dry_run": False,
        "url": args.url,
        "final_url": final_url,
        "title": title,
        "status_code": status_code,
        "elapsed_seconds": round(time.time() - started, 3),
        "connection": public_connection_params(),
        "looks_blocked": _looks_blocked(html),
        "html_length": len(html or ""),
        "html_keyword_hits": _keyword_hits(html),
        "requests": captured,
        "summary": _summarize_requests(captured),
    }


def _endpoint(url):
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}"


def _looks_blocked(html):
    text = (html or "").lower()
    return any(marker in text for marker in ("akamai", "ops! algo deu errado", "access denied", "captcha", "customdeny"))


def _keyword_hits(text):
    haystack = (text or "").lower()
    return [keyword for keyword in KEYWORDS if keyword in haystack]


def _summarize_requests(requests):
    rows = []
    for index, item in enumerate(requests):
        body = item.get("body") or ""
        rows.append(
            {
                "index": index,
                "method": item.get("method", ""),
                "status_code": item.get("status_code", ""),
                "endpoint": item.get("endpoint", ""),
                "body_length": len(body),
                "keyword_hits": _keyword_hits(body),
            }
        )
    return rows


def write_result(result, directory):
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = directory / f"{timestamp}_capture.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description="Casas Bahia ZenRows Scraping Browser network capture.")
    parser.add_argument("--url", default=DEFAULT_PDP_URL)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--execute", action="store_true", help="Execute paid Scraping Browser session.")
    parser.add_argument("--wait-ms", type=int, default=int(os.getenv("SEDA_ZENROWS_BROWSER_WAIT_MS", "7000")))
    parser.add_argument("--scrolls", type=int, default=int(os.getenv("SEDA_ZENROWS_BROWSER_SCROLLS", "3")))
    parser.add_argument("--scroll-wait-ms", type=int, default=int(os.getenv("SEDA_ZENROWS_BROWSER_SCROLL_WAIT_MS", "1500")))
    parser.add_argument("--after-click-wait-ms", type=int, default=int(os.getenv("SEDA_ZENROWS_BROWSER_AFTER_CLICK_WAIT_MS", "4000")))
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("SEDA_ZENROWS_BROWSER_TIMEOUT_MS", "90000")))
    parser.add_argument("--click-text", action="append", default=["Ver mais", "Características", "Especificações Técnicas"])
    args = parser.parse_args()

    ensure_execute_allowed(args.execute)
    result = asyncio.run(execute_capture(args))
    path = write_result(result, Path(args.output_dir) if args.output_dir else output_dir())
    print(f"[casas_zenrows_browser_capture] wrote {path}")


if __name__ == "__main__":
    main()
