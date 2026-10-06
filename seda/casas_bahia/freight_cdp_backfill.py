import argparse
import asyncio
import csv
import json
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from seda.common.chrome_cdp import ensure_chrome_cdp, ensure_playwright_temp_dir
from seda.detail_publish import detail_consumer_guard
from seda.step00_config import run_root

from .detail_api import _freight_detail, _is_freight_calculation_error
from .diagnostics import Span, trace


DEFAULT_WARMUP_URL = (
    "https://www.casasbahia.com.br/"
    "smart-tv-32-fhd-tcl-32s5k-qled-dolby-audio-google-tv/p/55070945?frete=01010-010"
)
def default_input():
    return str(run_root() / "output" / "final_output_enriched.csv")


def default_output():
    return str(run_root() / "output" / "final_output_delivery_backfilled.csv")


@trace('freight_backfill')
async def run(args):
    rows, fieldnames = _read_csv(Path(args.input))
    targets = _targets(rows, args)
    if args.limit:
        targets = targets[: args.limit]

    stats = Counter(rows=len(rows), targets=len(targets))
    errors = []
    aborted_reason = ""
    terminal_404 = set()

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
                        if item.get('status') == 404:
                            terminal_404.add(item.get('row_index'))
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
                stats['native_skipped_404'] = sum(target['row_index'] in terminal_404 for target in native_targets)
                for target in native_targets:
                    if target['row_index'] in terminal_404:
                        with Span('native_skip', sku_id=target['sku_id'], seller_id=target['seller_id'], reason='skipped_404'):
                            pass
                native_targets = [target for target in native_targets if target['row_index'] not in terminal_404]
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
        updated_before = stats['updated']
        _merge_result(rows, item, stats, errors)
        if had_cdp_failure and stats['updated'] > updated_before:
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


@trace('native_freight_product')
async def _native_freight_item(page, target, args):
    expected = f"/sku/{target['sku_id']}/freight/seller/{target['seller_id']}/"
    product_url = target.get('product_url') or ''
    total_ms = max(1, int(getattr(args, 'native_total_timeout_ms', 40000)))
    failure = {key: target.get(key) for key in ('row_index', 'sku_id', 'seller_id')}
    failure.update(status=0, ok=False, content_type='', text='', error='')
    if not product_url:
        return {**failure, 'reason': 'missing_url', 'error': 'missing_url'}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_ms / 1000
    try:
        async with asyncio.timeout(total_ms / 1000):
            for attempt in range(2):
                remaining = deadline - loop.time()
                # Do not cancel a normal response early merely to reserve retry time.
                # A retry is possible only when the first error leaves time in the total budget.
                allowance = min(remaining, args.native_timeout_ms / 1000)
                with Span('native_freight_attempt', attempt=attempt + 1,
                          timeout_ms=round(allowance * 1000)) as span:
                    try:
                        async with asyncio.timeout(allowance):
                            item = await _native_attempt(page, target, args, expected, manual=bool(attempt))
                        span.observe(item)
                        if attempt == 0 and item.get('status') in {429, 500, 502, 503, 504}:
                            span.update(reason='transient_http')
                    except Exception as exc:
                        reason = 'retry_timeout' if 'timeout' in type(exc).__name__.lower() else 'retry_exception'
                        span.update(reason=reason, exception_type=type(exc).__name__, success=False)
                        failure.update(reason=reason, error=reason)
                        # Retry only timeout/network failures; closed page/input errors stop immediately.
                        retryable = 'timeout' in type(exc).__name__.lower() or isinstance(exc, ConnectionError)
                        if attempt == 0 and retryable:
                            continue
                        return failure
                status = item.get('status', 0)
                if attempt == 0 and status in {429, 500, 502, 503, 504}:
                    continue
                return item
    except TimeoutError:
        failure.update(reason='budget_exceeded', error='budget_exceeded')
    return failure


async def _native_attempt(page, target, args, expected, *, manual):
    loop = asyncio.get_running_loop()
    pending = loop.create_future()

    def received(response):
        if _native_response_matches(response.url, expected, args.zipcode) and not pending.done():
            pending.set_result(response)

    page.on('response', received)
    try:
        url = target['product_url'] if manual else _frete_url(target['product_url'], args.zipcode)
        with Span('native_navigation'):
            await page.goto(url, wait_until='domcontentloaded', timeout=args.timeout_ms)
        if await _native_out_of_stock(page, target):
            return {**{key: target.get(key) for key in ('row_index', 'sku_id', 'seller_id')},
                    'status': 0, 'ok': False, 'skipped': True, 'reason': 'out_of_stock',
                    'content_type': '', 'text': '', 'error': ''}
        if manual and not pending.done():
            with Span('native_postal_input'):
                await page.fill("#frete", args.zipcode, timeout=args.native_input_timeout_ms)
                await _click_freight_button(page, args)
        with Span('native_response_wait'):
            response = await pending
        return await _response_item(response, target)
    finally:
        page.remove_listener('response', received)
        if not pending.done():
            pending.cancel()


def _native_response_matches(url, expected, zipcode):
    # Automatic page requests can use a previously stored postal code.
    # Match the product/seller route AND its postal code before accepting data.
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return False
    match = re.search(re.escape(expected) + r'zipcode/([^/]+)/', path)
    requested = re.sub(r'\D', '', str(zipcode or ''))
    actual = re.sub(r'\D', '', match.group(1)) if match else ''
    return len(requested) == 8 and actual == requested


async def _native_out_of_stock(page, target):
    # Require the requested product page plus an explicit, visible stock message.
    if _sku_id_from_url(page.url) != str(target.get('sku_id') or ''):
        return False
    message = page.get_by_text(re.compile(
        r'Infelizmente\s+n[aã]o\s+temos\s+estoque\s+do\s+produto', re.IGNORECASE))
    return await message.first.is_visible()


@trace('native_response_body')
async def _response_item(response, target):
    result = {key: target.get(key) for key in ('row_index', 'sku_id', 'seller_id')}
    result.update(status=response.status, ok=response.ok,
                  content_type=response.headers.get('content-type', ''), text='')
    try:
        result['text'] = await response.text()
    except Exception:
        return {**result, 'ok': False, 'error': 'body_read_error', 'reason': 'body_read_error'}
    detail = _detail_from_text(result['text'])
    if detail.get('delivery_availability'):
        result['reason'] = 'completed'
    elif response.status == 404:
        result['reason'] = 'http_404'
    elif not response.ok:
        result['reason'] = 'http_error'
    else:
        try:
            data = json.loads(result['text'])
            error = data.get('error') if isinstance(data, dict) else None
            message = error.get('message', '') if isinstance(error, dict) else ''
            result['reason'] = 'calculation_error' if _is_freight_calculation_error(message) else 'empty_delivery'
        except ValueError:
            result['reason'] = 'invalid_json'
    return result


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
            await page.click(selector, timeout=min(args.native_input_timeout_ms, 1000))
            return
        except Exception as exc:
            last_error = exc
    try:
        await page.press("#frete", "Enter", timeout=min(args.native_input_timeout_ms, 1000))
    except Exception:
        if last_error:
            raise last_error
        raise





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
    script = r"""
    async ({ items, zipDigits, zipcode, concurrency, requestTimeoutMs }) => {
      const cookie = document.cookie || "";
      const match = cookie.match(/(?:^|;\s*)IPI-CasasBahia=([^;]+)/);
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
        const controller = new AbortController();
        const startedUtc = new Date().toISOString();
        const started = performance.now();
        const timer = setTimeout(() => controller.abort(), requestTimeoutMs);
        const url = `https://pdp-api.casasbahia.com.br/api/v2/sku/${item.sku_id}/freight/seller/${item.seller_id}/zipcode/${zipcode}/source/CB?${params}`;
        try {
          const response = await fetch(url, { signal: controller.signal,
            method: "GET",
            headers,
            credentials: "include",
            cache: "no-cache"
          });
          const text = await response.text();
          return {
            started_at_utc: startedUtc,
            finished_at_utc: new Date().toISOString(),
            elapsed_ms: performance.now() - started,
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
            started_at_utc: startedUtc,
            finished_at_utc: new Date().toISOString(),
            elapsed_ms: performance.now() - started,
            row_index: item.row_index,
            sku_id: item.sku_id,
            seller_id: item.seller_id,
            status: 0,
            ok: false,
            content_type: "",
            error: controller.signal.aborted ? 'request_timeout' : 'request_error',
            text: ""
          };
        } finally {
          clearTimeout(timer);
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
    payload['requestTimeoutMs'] = max(1, getattr(args, 'request_timeout_ms', 30000))
    last_error = ""
    for attempt in range(args.evaluate_retries + 1):
        try:
            waves = (len(batch) + max(1, args.concurrency) - 1) // max(1, args.concurrency)
            batch_timeout = waves * payload['requestTimeoutMs'] / 1000 + 5
            with Span('cdp_batch_attempt', attempt=attempt + 1, batch_size=len(batch)):
                result = await asyncio.wait_for(page.evaluate(script, payload), batch_timeout)
            for item in result.get('items', []):
                with Span('cdp_freight_response', sku_id=item.get('sku_id'), seller_id=item.get('seller_id'),
                          row_index=item.get('row_index')) as span:
                    span.observe(item)
                    # Browser timing covers fetch AND body reading for this product.
                    span.update(http_elapsed_ms=item.get('elapsed_ms', 0),
                                http_started_at_utc=item.get('started_at_utc'),
                                http_finished_at_utc=item.get('finished_at_utc'))
            return result
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
    if item.get('skipped'):
        stats.update(native_skipped=1)
        _append_status(rows[item['row_index']], 'freight_native_skipped_out_of_stock')
        return
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
    parser.add_argument("--zipcode", default=os.getenv("SEDA_POSTAL_CODE", "01010-010"))
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
    parser.add_argument("--native-timeout-ms", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_FREIGHT_NATIVE_TIMEOUT_MS", "40000")))
    parser.add_argument("--native-input-timeout-ms", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_FREIGHT_NATIVE_INPUT_TIMEOUT_MS", "8000")))
    parser.add_argument("--native-limit", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_FREIGHT_NATIVE_LIMIT", "0")))
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--close-browser", action="store_true")
    parser.add_argument("--keep-browser", action="store_true")
    parser.add_argument('--native-total-timeout-ms', type=int, default=int(os.getenv('SEDA_CASAS_BAHIA_FREIGHT_NATIVE_TOTAL_TIMEOUT_MS', "40000")))
    parser.add_argument('--request-timeout-ms', type=int, default=int(os.getenv('SEDA_CASAS_BAHIA_FREIGHT_REQUEST_TIMEOUT_MS', '30000')))
    args = parser.parse_args()
    if args.native_total_timeout_ms <= 0 or args.request_timeout_ms <= 0:
        parser.error('Freight total/request timeouts must be positive')
    root = run_root()
    with detail_consumer_guard(root):
        result = asyncio.run(run(args))
    print(json.dumps({"stats": result.get("stats", {}), "output": result.get("output", args.output)}, ensure_ascii=True))
    if result.get("aborted"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
