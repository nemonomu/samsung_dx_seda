import argparse
import asyncio
import csv
import json
import re
from collections import Counter
from pathlib import Path

from seda.common.chrome_cdp import ensure_chrome_cdp
from seda.step00_config import run_root

from .detail_api import _freight_detail


DEFAULT_WARMUP_URL = (
    "https://www.casasbahia.com.br/"
    "smart-tv-32-fhd-tcl-32s5k-qled-dolby-audio-google-tv/p/55070945?frete=01010-010"
)
def default_input():
    return str(run_root() / "output" / "final_output_enriched.csv")


def default_output():
    return str(run_root() / "output" / "final_output_delivery_backfilled.csv")


async def run(args):
    rows, fieldnames = _read_csv(Path(args.input))
    targets = _targets(rows, args)
    if args.limit:
        targets = targets[: args.limit]

    stats = Counter(rows=len(rows), targets=len(targets))
    errors = []

    if not targets:
        _write_csv(Path(args.output), rows, fieldnames)
        return {"stats": dict(stats), "errors": errors, "output": args.output}

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        cdp_status = ensure_chrome_cdp(
            args.cdp_url,
            timeout_seconds=args.cdp_start_timeout,
            auto_start=not args.no_auto_start_cdp,
        )
        if cdp_status.get("started"):
            print(
                "[casas_freight_cdp] "
                f"started Chrome CDP url={args.cdp_url} user_data_dir={cdp_status.get('user_data_dir', '')}",
                flush=True,
            )
        browser = await p.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
        page = await context.new_page()
        try:
            await page.goto(args.warmup_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            await page.wait_for_timeout(args.wait_ms)
            for offset in range(0, len(targets), args.batch_size):
                batch = targets[offset : offset + args.batch_size]
                result = await _fetch_batch(page, batch, args)
                stats.update(
                    cdp_calls=1,
                    cdp_rows=len(batch),
                    cdp_has_cvip=int(bool(result.get("hasCvip"))),
                    cdp_has_cvip_cep=int(bool(result.get("hasCvipCep"))),
                )
                for item in result.get("items", []):
                    _merge_result(rows, item, stats, errors)
                print(
                    "[casas_freight_cdp] "
                    f"{min(offset + len(batch), len(targets))}/{len(targets)} "
                    f"updated={stats['updated']} failed={stats['failed']}"
                )
        finally:
            await page.close()
            if args.close_browser:
                await browser.close()

    _write_csv(Path(args.output), rows, fieldnames)
    manifest = {
        "input": args.input,
        "output": args.output,
        "stats": dict(stats),
        "errors": errors[:100],
    }
    manifest_path = Path(args.output).with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


async def _fetch_batch(page, batch, args):
    script = """
    async ({ items, zipDigits, zipcode, concurrency }) => {
      const cookie = document.cookie || "";
      const match = cookie.match(/(?:^|;\\s*)IPI-CasasBahia=([^;]+)/);
      let cvip = match ? `IPI-CasasBahia=${decodeURIComponent(match[1])}` : "";
      if (!cvip) {
        const guid = crypto.randomUUID ? crypto.randomUUID() : "00000000-0000-4000-8000-000000000000";
        cvip = `IPI-CasasBahia=UsuarioGUID=${guid}`;
      }
      if (zipDigits && !cvip.includes("cepClienteProvavel=")) {
        cvip = `${cvip}&cepClienteProvavel=${zipDigits}`;
      }
      const params = new URLSearchParams({channel: "DESKTOP", orderby: "price"});
      const headers = {"content-type": "application/json", "x-cvip": cvip};
      async function fetchOne(item) {
        const url = `https://pdp-api.casasbahia.com.br/api/v2/sku/${item.sku_id}/freight/seller/${item.seller_id}/zipcode/${zipcode}/source/CB?${params}`;
        try {
          const response = await fetch(url, {
            method: "GET",
            headers,
            credentials: "include",
            cache: "no-cache"
          });
          const text = await response.text();
          return {
            row_index: item.row_index,
            sku_id: item.sku_id,
            seller_id: item.seller_id,
            status: response.status,
            ok: response.ok,
            content_type: response.headers.get("content-type") || "",
            text: response.ok ? text : text.slice(0, 200)
          };
        } catch (error) {
          return {
            row_index: item.row_index,
            sku_id: item.sku_id,
            seller_id: item.seller_id,
            status: 0,
            ok: false,
            content_type: "",
            error: String(error),
            text: ""
          };
        }
      }
      const results = [];
      let cursor = 0;
      async function worker() {
        while (cursor < items.length) {
          const item = items[cursor++];
          results.push(await fetchOne(item));
        }
      }
      const workers = Array.from({length: Math.max(1, concurrency)}, worker);
      await Promise.all(workers);
      return {
        hasCvip: Boolean(cvip),
        hasCvipCep: Boolean(cvip && cvip.includes("cepClienteProvavel=")),
        items: results
      };
    }
    """
    zip_digits = re.sub(r"\D+", "", args.zipcode)
    return await page.evaluate(
        script,
        {
            "items": batch,
            "zipDigits": zip_digits,
            "zipcode": args.zipcode,
            "concurrency": args.concurrency,
        },
    )


def _targets(rows, args):
    targets = []
    for index, row in enumerate(rows):
        if not args.force and str(row.get("delivery_availability") or "").strip():
            continue
        sku_id = _sku_id_from_url(row.get("product_url", "")) or _numeric(row.get("item", ""))
        seller_id = _numeric(row.get("seller_id", ""))
        if not sku_id or not seller_id:
            continue
        targets.append({"row_index": index, "sku_id": sku_id, "seller_id": seller_id})
    return targets


def _merge_result(rows, item, stats, errors):
    index = item.get("row_index")
    row = rows[index]
    status = item.get("status")
    if not item.get("ok"):
        detail = _detail_from_text(item.get("text") or "")
        if detail.get("delivery_availability"):
            _merge_detail(row, detail)
            _append_status(row, f"freight_cdp_unavailable:status_{status}")
            stats.update(updated=1, unavailable=1)
            return
        stats.update(failed=1)
        _append_status(row, f"freight_cdp_failed:status_{status}")
        errors.append(
            {
                "row_index": index,
                "sku_id": item.get("sku_id"),
                "seller_id": item.get("seller_id"),
                "status": status,
                "error": item.get("error", ""),
            }
        )
        return
    detail = _detail_from_text(item.get("text") or "")
    if not detail:
        stats.update(failed=1)
        _append_status(row, "freight_cdp_failed:invalid_json")
        return
    if not detail.get("delivery_availability"):
        stats.update(empty=1)
        _append_status(row, "freight_cdp_empty_delivery")
        return
    _merge_detail(row, detail)
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_freight_cdp")
    _append_status(row, "freight_cdp_ok")
    stats.update(updated=1)


def _detail_from_text(text):
    try:
        return _freight_detail(json.loads(text or "{}"))
    except ValueError:
        return {}


def _merge_detail(row, detail):
    for key, value in detail.items():
        if value:
            row[key] = value
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_freight_cdp")


def _append_status(row, token):
    row["parse_status"] = _append_token(row.get("parse_status", ""), token)


def _append_token(value, token):
    tokens = [part for part in str(value or "").split("+") if part]
    if token not in tokens:
        tokens.append(token)
    return "+".join(tokens)


def _sku_id_from_url(url):
    match = re.search(r"/p/(\d+)", str(url or ""))
    return match.group(1) if match else ""


def _numeric(value):
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d+", text) else ""


def _read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def _write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Backfill Casas Bahia freight fields through an existing Chrome CDP session.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--cdp-start-timeout", type=int, default=20)
    parser.add_argument("--no-auto-start-cdp", action="store_true")
    parser.add_argument("--warmup-url", default=DEFAULT_WARMUP_URL)
    parser.add_argument("--zipcode", default="01010-010")
    parser.add_argument("--input", default=default_input())
    parser.add_argument("--output", default=default_output())
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--wait-ms", type=int, default=5000)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--close-browser", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({"stats": result.get("stats", {}), "output": result.get("output", args.output)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
