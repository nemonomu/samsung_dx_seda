import argparse
import asyncio
import csv
import json
import os
import re
from collections import Counter
from pathlib import Path

from seda.common.chrome_cdp import ensure_chrome_cdp, ensure_playwright_temp_dir
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
    aborted_reason = ""

    if not targets:
        _write_csv(Path(args.output), rows, fieldnames)
        return {"stats": dict(stats), "errors": errors, "output": args.output}

    ensure_playwright_temp_dir()

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
            if not args.native_only:
                for offset in range(0, len(targets), args.batch_size):
                    batch = targets[offset : offset + args.batch_size]
                    result = await _fetch_batch(page, batch, args)
                    status_counts = Counter(str(item.get("status", "unknown")) for item in result.get("items", []))
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
                        f"updated={stats['updated']} failed={stats['failed']} "
                        f"statuses={_status_summary(status_counts)}"
                    )
                    if args.fail_fast_failures and stats["updated"] == 0 and stats["failed"] >= args.fail_fast_failures:
                        if args.native_fallback:
                            print("[casas_freight_cdp] switching to native freight fallback", flush=True)
                        else:
                            aborted_reason = f"fail_fast_no_updates_after_{stats['failed']}_failures"
                            errors.append({"error": aborted_reason, "status_counts": dict(status_counts)})
                            print(f"[casas_freight_cdp] aborted {aborted_reason}", flush=True)
                        break
            if args.native_fallback and not aborted_reason:
                if args.native_only and args.force:
                    native_targets = targets
                else:
                    native_targets = [
                        target for target in targets
                        if not str(rows[target["row_index"]].get("delivery_availability") or "").strip()
                    ]
                if native_targets:
                    await _native_backfill(page, rows, native_targets, args, stats, errors)
        finally:
            await page.close()
            if args.close_browser or (not args.no_auto_start_cdp and not args.keep_browser):
                try:
                    await browser.close()
                except Exception as exc:
                    print(f"[casas_freight_cdp] warning: Chrome CDP close failed: {exc}", flush=True)

    _write_csv(Path(args.output), rows, fieldnames)
    manifest = {
        "input": args.input,
        "output": args.output,
        "stats": dict(stats),
        "errors": errors[:100],
        "aborted": bool(aborted_reason),
        "aborted_reason": aborted_reason,
    }
    manifest_path = Path(args.output).with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


async def _native_backfill(page, rows, targets, args, stats, errors):
    total = len(targets)
    if args.native_limit:
        targets = targets[: args.native_limit]
        total = len(targets)
    for position, target in enumerate(targets, start=1):
        item = await _native_freight_item(page, target, args)
        row = rows[target["row_index"]]
        had_cdp_failure = "freight_cdp_failed:" in str(row.get("parse_status") or "")
        _merge_result(rows, item, stats, errors)
        if item.get("ok") and had_cdp_failure:
            stats["failed"] = max(0, stats["failed"] - 1)
            _drop_row_errors(errors, target.get("row_index"))
            row["parse_status"] = _drop_status_prefix(row.get("parse_status", ""), "freight_cdp_failed:")
        print(
            "[casas_freight_cdp_native] "
            f"{position}/{total} sku={target.get('sku_id')} seller={target.get('seller_id')} "
            f"status={item.get('status')} ok={int(bool(item.get('ok')))} "
            f"updated={stats['updated']} failed={stats['failed']}",
            flush=True,
        )


async def _native_freight_item(page, target, args):
    expected = f"/sku/{target['sku_id']}/freight/seller/{target['seller_id']}/"
    product_url = target.get("product_url") or ""
    first_error = ""
    if product_url:
        try:
            async with page.expect_response(lambda response: expected in response.url, timeout=args.native_timeout_ms) as response_info:
                await page.goto(_frete_url(product_url, args.zipcode), wait_until="domcontentloaded", timeout=args.timeout_ms)
            return await _response_item(await response_info.value, target)
        except Exception as exc:
            first_error = f"{type(exc).__name__}: {exc}"
    try:
        if product_url:
            await page.goto(product_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        await page.fill("#frete", args.zipcode, timeout=args.native_input_timeout_ms)
        async with page.expect_response(lambda response: expected in response.url, timeout=args.native_timeout_ms) as response_info:
            await _click_freight_button(page, args)
        return await _response_item(await response_info.value, target)
    except Exception as exc:
        error = f"native_failed:{first_error}; {type(exc).__name__}: {exc}"
        return {
            "row_index": target.get("row_index"),
            "sku_id": target.get("sku_id"),
            "seller_id": target.get("seller_id"),
            "status": 0,
            "ok": False,
            "content_type": "",
            "error": error[:500],
            "text": "",
        }


def _drop_row_errors(errors, row_index):
    errors[:] = [error for error in errors if error.get("row_index") != row_index]


def _drop_status_prefix(value, prefix):
    tokens = [part for part in str(value or "").split("+") if part and not part.startswith(prefix)]
    return "+".join(tokens)


async def _click_freight_button(page, args):
    selectors = [
        'button[data-testid="calcular-frete"]',
        'button:has-text("Consultar")',
        'button:has-text("Calcular")',
    ]
    last_error = None
    for selector in selectors:
        try:
            await page.click(selector, timeout=args.native_input_timeout_ms)
            return
        except Exception as exc:
            last_error = exc
    try:
        await page.press("#frete", "Enter", timeout=args.native_input_timeout_ms)
    except Exception:
        if last_error:
            raise last_error
        raise


async def _response_item(response, target):
    text = ""
    try:
        text = await response.text()
    except Exception as exc:
        return {
            "row_index": target.get("row_index"),
            "sku_id": target.get("sku_id"),
            "seller_id": target.get("seller_id"),
            "status": 0,
            "ok": False,
            "content_type": "",
            "error": f"response_text_failed:{type(exc).__name__}: {exc}",
            "text": "",
        }
    return {
        "row_index": target.get("row_index"),
        "sku_id": target.get("sku_id"),
        "seller_id": target.get("seller_id"),
        "status": response.status,
        "ok": response.ok,
        "content_type": response.headers.get("content-type", ""),
        "text": text,
    }


def _frete_url(url, zipcode):
    if not url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}frete={zipcode}"


def _status_summary(status_counts):
    if not status_counts:
        return "-"
    return ",".join(f"{status}:{count}" for status, count in sorted(status_counts.items()))


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
    payload = {
        "items": batch,
        "zipDigits": zip_digits,
        "zipcode": args.zipcode,
        "concurrency": args.concurrency,
    }
    last_error = ""
    for attempt in range(args.evaluate_retries + 1):
        try:
            return await page.evaluate(script, payload)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if not _retryable_evaluate_error(last_error) or attempt >= args.evaluate_retries:
                break
            print(
                "[casas_freight_cdp] "
                f"retry evaluate after navigation/context reset attempt={attempt + 1}/{args.evaluate_retries}",
                flush=True,
            )
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=args.timeout_ms)
            except Exception:
                pass
            await page.wait_for_timeout(args.evaluate_retry_wait_ms)
    return _failed_batch(batch, last_error)


def _retryable_evaluate_error(message):
    text = str(message or "").lower()
    return (
        "execution context was destroyed" in text
        or "most likely because of a navigation" in text
        or "frame was detached" in text
    )


def _failed_batch(batch, error):
    return {
        "hasCvip": False,
        "hasCvipCep": False,
        "items": [
            {
                "row_index": item.get("row_index"),
                "sku_id": item.get("sku_id"),
                "seller_id": item.get("seller_id"),
                "status": 0,
                "ok": False,
                "content_type": "",
                "error": error,
                "text": "",
            }
            for item in batch
        ],
    }


def _targets(rows, args):
    targets = []
    for index, row in enumerate(rows):
        if not args.force and str(row.get("delivery_availability") or "").strip():
            continue
        sku_id = _sku_id_from_url(row.get("product_url", "")) or _numeric(row.get("item", ""))
        seller_id = _numeric(row.get("seller_id", ""))
        if not sku_id or not seller_id:
            continue
        targets.append(
            {
                "row_index": index,
                "sku_id": sku_id,
                "seller_id": seller_id,
                "product_url": row.get("product_url", ""),
            }
        )
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
                "content_type": item.get("content_type", ""),
                "text_preview": str(item.get("text") or "")[:200],
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
    parser.add_argument("--fail-fast-failures", type=int, default=50)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--wait-ms", type=int, default=5000)
    parser.add_argument("--evaluate-retries", type=int, default=3)
    parser.add_argument("--evaluate-retry-wait-ms", type=int, default=2000)
    parser.add_argument(
        "--native-fallback",
        action="store_true",
        default=os.getenv("SEDA_CASAS_BAHIA_FREIGHT_NATIVE_FALLBACK", "1").lower() in {"1", "true", "yes", "y"},
    )
    parser.add_argument("--native-timeout-ms", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_FREIGHT_NATIVE_TIMEOUT_MS", "25000")))
    parser.add_argument("--native-input-timeout-ms", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_FREIGHT_NATIVE_INPUT_TIMEOUT_MS", "8000")))
    parser.add_argument("--native-limit", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_FREIGHT_NATIVE_LIMIT", "0")))
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--close-browser", action="store_true")
    parser.add_argument("--keep-browser", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({"stats": result.get("stats", {}), "output": result.get("output", args.output)}, ensure_ascii=True))
    if result.get("aborted"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
