import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

from .detail_api import _freight_detail


DEFAULT_PAGE_URL = (
    "https://www.casasbahia.com.br/"
    "smart-tv-32-fhd-tcl-32s5k-qled-dolby-audio-google-tv/p/55070945?frete=01010-010"
)


async def run(args):
    from playwright.async_api import async_playwright

    freight_url = _freight_url(args.sku_id, args.seller_id, args.zipcode)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(args.cdp_url)
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
            page = None
            if args.new_page:
                page = await context.new_page()
            else:
                page = _active_page(context)
                if page is None:
                    page = await context.new_page()
            if args.page_url:
                await page.goto(args.page_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                await page.wait_for_timeout(args.wait_ms)
            result = await _evaluate_freight(page, freight_url, args)
            if args.new_page:
                await page.close()
        finally:
            if args.close_browser:
                await browser.close()

    payload = _public_result(result, include_text=args.include_text)
    if result.get("ok"):
        try:
            payload["detail"] = _freight_detail(json.loads(result.get("text") or "{}"))
        except ValueError:
            payload["parse_error"] = "invalid_json"
    return payload


def _active_page(context):
    for page in context.pages:
        if "casasbahia.com.br" in page.url:
            return page
    return context.pages[0] if context.pages else None


async def _evaluate_freight(page, freight_url, args):
    script = """
    async ({ freightUrl, zipDigits }) => {
      const cookie = document.cookie || "";
      const match = cookie.match(/(?:^|;\\s*)IPI-CasasBahia=([^;]+)/);
      let cvip = match ? `IPI-CasasBahia=${decodeURIComponent(match[1])}` : "";
      if (cvip && zipDigits && !cvip.includes("cepClienteProvavel=")) {
        cvip = `${cvip}&cepClienteProvavel=${zipDigits}`;
      }
      const headers = {"content-type": "application/json"};
      if (cvip) headers["x-cvip"] = cvip;
      try {
        const response = await fetch(freightUrl, {
          method: "GET",
          headers,
          credentials: "include",
        });
        const text = await response.text();
        return {
          status: response.status,
          ok: response.ok,
          contentType: response.headers.get("content-type") || "",
          hasCvip: Boolean(cvip),
          hasCvipCep: Boolean(cvip && cvip.includes("cepClienteProvavel=")),
          text,
        };
      } catch (error) {
        return {
          status: 0,
          ok: false,
          contentType: "",
          hasCvip: Boolean(cvip),
          hasCvipCep: Boolean(cvip && cvip.includes("cepClienteProvavel=")),
          error: String(error),
          text: ""
        };
      }
    }
    """
    last_error = ""
    for _ in range(max(1, args.evaluate_attempts)):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=args.timeout_ms)
            await page.wait_for_timeout(args.evaluate_wait_ms)
            zip_digits = "".join(ch for ch in args.zipcode if ch.isdigit())
            return await page.evaluate(script, {"freightUrl": freight_url, "zipDigits": zip_digits})
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            await page.wait_for_timeout(args.evaluate_wait_ms)
    return {"status": 0, "ok": False, "contentType": "", "hasCvip": False, "error": last_error, "text": ""}


def _freight_url(sku_id, seller_id, zipcode):
    path = f"https://pdp-api.casasbahia.com.br/api/v2/sku/{sku_id}/freight/seller/{seller_id}/zipcode/{zipcode}/source/CB"
    return path + "?" + urlencode({"channel": "DESKTOP", "orderby": "price"})


def _public_result(result, include_text=False):
    text = result.get("text") or ""
    payload = {
        "status": result.get("status", 0),
        "ok": bool(result.get("ok")),
        "content_type": result.get("contentType", ""),
        "has_cvip": bool(result.get("hasCvip")),
        "has_cvip_cep": bool(result.get("hasCvipCep")),
        "json_like": text.strip().startswith(("{", "[")),
        "body_prefix": text[:200] if not result.get("ok") else "",
        "error": result.get("error", ""),
    }
    if include_text:
        payload["text"] = text
    return payload


def write_result(result, path):
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Probe Casas Bahia freight API from an existing Chrome CDP session.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--page-url", default=DEFAULT_PAGE_URL)
    parser.add_argument("--sku-id", default="55070945")
    parser.add_argument("--seller-id", default="10037")
    parser.add_argument("--zipcode", default="01010-010")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--wait-ms", type=int, default=5000)
    parser.add_argument("--evaluate-wait-ms", type=int, default=1500)
    parser.add_argument("--evaluate-attempts", type=int, default=3)
    parser.add_argument("--output", default="")
    parser.add_argument("--new-page", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--close-browser", action="store_true")
    parser.add_argument("--include-text", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    write_result(result, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
