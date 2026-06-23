import argparse
import csv
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from seda.common.retailer_runner import configure_retailer
from seda.step00_config import run_root, write_json

from .detail_api import _cvip_header, _freight_detail
from .freight_har_replay_probe import DEFAULT_HAR, _first_success_har_entry, _read_har


def default_input():
    return str(run_root() / "output" / "seda_final_targets.csv")


def default_output():
    return str(run_root() / "output" / "freight_browser_batch_probe.json")


def _sku_id(row):
    match = re.search(r"/p/(\d+)", str(row.get("product_url") or ""))
    if match:
        return match.group(1)
    item = str(row.get("item") or "").strip()
    return item if re.fullmatch(r"\d+", item) else ""


def _seller_id(row):
    return re.sub(r"\D+", "", str(row.get("seller_id") or ""))


def _load_targets(path, limit):
    targets = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            sku_id = _sku_id(row)
            seller_id = _seller_id(row)
            if not sku_id or not seller_id:
                continue
            targets.append(
                {
                    "index": index,
                    "sku_id": sku_id,
                    "seller_id": seller_id,
                    "product_url": row.get("product_url", ""),
                    "retailer_sku_name": row.get("retailer_sku_name", ""),
                }
            )
            if limit and len(targets) >= limit:
                break
    return targets


def _replace_freight_identity(url, sku_id, seller_id, zipcode=None):
    parsed = urlsplit(url)
    path = re.sub(
        r"/sku/[^/]+/freight/seller/[^/]+/zipcode/[^/]+/",
        f"/sku/{sku_id}/freight/seller/{seller_id}/zipcode/{zipcode or os.getenv('SEDA_POSTAL_CODE', '01010-010')}/",
        parsed.path,
    )
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    if not query:
        query = [("channel", "DESKTOP"), ("orderby", "price")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), parsed.fragment))


def _har_cvip(har_entry):
    return str((har_entry.get("headers") or {}).get("x-cvip") or "").strip()


def _cvip_for_mode(mode, har_entry, zipcode=None):
    if mode in {"browser", "none"}:
        return ""
    if mode == "har":
        return _har_cvip(har_entry)
    if mode == "generated":
        return _cvip_header(zipcode)
    if mode == "env":
        return os.getenv("SEDA_CASAS_BAHIA_X_CVIP", "").strip()
    return ""


def _create_driver(args):
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.set_capability("pageLoadStrategy", args.page_load_strategy)
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-notifications")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--no-first-run")
    user_data_dir = os.getenv("SEDA_CASAS_BAHIA_BROWSER_USER_DATA_DIR", "").strip()
    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
    version_main = _uc_version_main()
    kwargs = {"options": options}
    if version_main:
        kwargs["version_main"] = version_main
    return uc.Chrome(**kwargs)


def _uc_version_main():
    raw = os.getenv("SEDA_UC_VERSION_MAIN") or os.getenv("SEDA_UC_DEFAULT_VERSION_MAIN") or ""
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _batch_fetch(driver, requests, concurrency, timeout_seconds):
    script = """
const done = arguments[arguments.length - 1];
const items = arguments[0] || [];
const concurrency = Math.max(1, arguments[1] || 3);
const timeoutMs = Math.max(1000, (arguments[2] || 10) * 1000);
let nextIndex = 0;
let active = 0;
const results = new Array(items.length);

function finishIfDone() {
  if (nextIndex >= items.length && active === 0) {
    done({ok: true, results});
  }
}

function runOne(item, index) {
  active += 1;
  const xhr = new XMLHttpRequest();
  let finished = false;
  function complete(payload) {
    if (finished) return;
    finished = true;
    active -= 1;
    results[index] = Object.assign({
      index,
      case: item.case || "",
      cvipMode: item.cvipMode || "",
      skuId: item.skuId || "",
      sellerId: item.sellerId || "",
      productUrl: item.productUrl || "",
      url: item.url || ""
    }, payload || {});
    schedule();
    finishIfDone();
  }
  try {
    xhr.open("GET", item.url, true);
    xhr.timeout = timeoutMs;
    xhr.setRequestHeader("accept", "application/json, text/plain, */*");
    if (item.setContentType) xhr.setRequestHeader("content-type", "application/json");
    if (item.cvip) xhr.setRequestHeader("x-cvip", item.cvip);
    xhr.onload = () => complete({
      ok: xhr.status >= 200 && xhr.status < 300,
      status: xhr.status,
      contentType: xhr.getResponseHeader("content-type") || "",
      text: xhr.responseText || ""
    });
    xhr.onerror = () => complete({
      ok: false,
      status: xhr.status || 0,
      contentType: xhr.getResponseHeader("content-type") || "",
      text: xhr.responseText || "",
      error: "xhr_error"
    });
    xhr.ontimeout = () => complete({
      ok: false,
      status: xhr.status || 0,
      contentType: xhr.getResponseHeader("content-type") || "",
      text: xhr.responseText || "",
      error: "xhr_timeout"
    });
    xhr.send();
  } catch (error) {
    complete({
      ok: false,
      status: 0,
      contentType: "",
      text: "",
      error: String(error && error.message ? error.message : error)
    });
  }
}

function schedule() {
  while (active < concurrency && nextIndex < items.length) {
    const index = nextIndex;
    nextIndex += 1;
    runOne(items[index], index);
  }
}

if (!items.length) {
  done({ok: true, results: []});
} else {
  schedule();
}
"""
    try:
        return driver.execute_async_script(script, requests, concurrency, timeout_seconds)
    except Exception as exc:
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "results": []}


def _parse_batch_items(raw_items):
    parsed = []
    for item in raw_items or []:
        text = str(item.get("text") or "")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        detail = _freight_detail(data) if data is not None else {}
        delivery = detail.get("delivery_availability", "")
        pickup = detail.get("pick_up_availability", "")
        parsed.append(
            {
                "case": item.get("case", ""),
                "cvip_mode": item.get("cvipMode", ""),
                "sku_id": item.get("skuId", ""),
                "seller_id": item.get("sellerId", ""),
                "product_url": item.get("productUrl", ""),
                "url": item.get("url", ""),
                "ok": bool(item.get("ok")),
                "success": bool(item.get("ok") and data is not None and (delivery or pickup)),
                "status_code": int(item.get("status") or 0),
                "content_type": item.get("contentType", ""),
                "text_length": len(text),
                "json": data is not None,
                "delivery_availability": delivery,
                "pick_up_availability": pickup,
                "error": item.get("error", ""),
            }
        )
    return parsed


def _request_items(har_entry, targets, args):
    items = []
    modes = [item.strip().lower() for item in args.cvip_modes.split(",") if item.strip()]
    if args.include_har_url:
        for mode in modes:
            items.append(
                {
                    "case": "har_url",
                    "url": har_entry["url"],
                    "cvipMode": mode,
                    "cvip": _cvip_for_mode(mode, har_entry, args.zipcode),
                    "setContentType": args.set_content_type,
                }
            )
    for target in targets:
        url = _replace_freight_identity(har_entry["url"], target["sku_id"], target["seller_id"], zipcode=args.zipcode)
        for mode in modes:
            items.append(
                {
                    "case": "target",
                    "url": url,
                    "cvipMode": mode,
                    "cvip": _cvip_for_mode(mode, har_entry, args.zipcode),
                    "skuId": target["sku_id"],
                    "sellerId": target["seller_id"],
                    "productUrl": target.get("product_url", ""),
                    "setContentType": args.set_content_type,
                }
            )
    return items


def _open_bootstrap(driver, args):
    bootstrap_error = ""
    try:
        driver.get(args.bootstrap_url)
    except Exception as exc:
        bootstrap_error = f"{type(exc).__name__}: {exc}"
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
    time.sleep(args.wait_seconds)
    return {
        "bootstrap_url": args.bootstrap_url,
        "bootstrap_error": bootstrap_error,
        "browser_current_url": _safe_current_url(driver),
        "bootstrap_blocked": _blocked_page(driver),
    }


def _safe_current_url(driver):
    try:
        return driver.current_url
    except Exception:
        return ""


def _blocked_page(driver):
    try:
        source = (driver.page_source or "").lower()
    except Exception:
        return False
    if any(marker in _normalize_ascii(source) for marker in ("erro 403", "nao e possivel acessar", "customdeny")):
        return True
    return any(marker in source for marker in ("erro 403", "nao e possivel acessar", "n찾o 챕 poss챠vel acessar", "customdeny"))


def _print_row(row):
    delivery = _ascii(row.get("delivery_availability", ""))
    pickup = _ascii(row.get("pick_up_availability", ""))
    sku = row.get("sku_id", "")
    seller = row.get("seller_id", "")
    target = f" sku={sku} seller={seller}" if sku or seller else ""
    print(
        "[casas_freight_browser_batch] "
        f"{row.get('case')}{target} cvip={row.get('cvip_mode')} success={int(bool(row.get('success')))} "
        f"status={row.get('status_code')} json={int(bool(row.get('json')))} "
        f"len={row.get('text_length')} error={row.get('error')} delivery={delivery} pickup={pickup}",
        flush=True,
    )


def _ascii(value):
    return str(value or "").encode("ascii", "backslashreplace").decode("ascii")[:160]


def _normalize_ascii(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def run(args):
    configure_retailer("casas_bahia")
    har_entry = _first_success_har_entry(_read_har(args.har))
    targets = _load_targets(args.input, args.limit)
    items = _request_items(har_entry, targets, args)
    script_timeout = max(args.script_timeout, args.fetch_timeout * ((len(items) + max(1, args.concurrency) - 1) // max(1, args.concurrency)) + 10)
    results = {
        "har": str(args.har),
        "input": args.input,
        "target_count": len(targets),
        "request_count": len(items),
        "concurrency": args.concurrency,
        "fetch_timeout": args.fetch_timeout,
        "script_timeout": script_timeout,
        "rows": [],
    }

    driver = _create_driver(args)
    try:
        driver.set_page_load_timeout(args.page_timeout)
        driver.set_script_timeout(script_timeout)
        print(f"[casas_freight_browser_batch] opening bootstrap={args.bootstrap_url}", flush=True)
        results.update(_open_bootstrap(driver, args))
        raw = _batch_fetch(driver, items, args.concurrency, args.fetch_timeout)
        results["raw_ok"] = bool(raw.get("ok")) if isinstance(raw, dict) else False
        results["raw_error"] = raw.get("error", "") if isinstance(raw, dict) else ""
        parsed = _parse_batch_items((raw or {}).get("results") if isinstance(raw, dict) else [])
        for row in parsed:
            _print_row(row)
        results["rows"] = parsed
    finally:
        if args.close_browser:
            try:
                driver.quit()
            except Exception:
                pass

    results["ok"] = sum(1 for row in results["rows"] if row.get("success"))
    results["fail"] = len(results["rows"]) - results["ok"]
    write_json(args.output, results)
    print(json.dumps({"ok": results["ok"], "fail": results["fail"], "output": str(args.output)}, ensure_ascii=False))
    return results


def main():
    configure_retailer("casas_bahia")
    parser = argparse.ArgumentParser(description="Batch probe Casas Bahia freight endpoint through one browser origin.")
    parser.add_argument("--har", default=str(DEFAULT_HAR))
    parser.add_argument("--input", default=default_input())
    parser.add_argument("--output", default=default_output())
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--zipcode", default=None)
    parser.add_argument("--cvip-modes", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_BATCH_CVIP_MODES", "browser,generated,har"))
    parser.add_argument("--include-har-url", action="store_true", default=True)
    parser.add_argument("--no-har-url", action="store_false", dest="include_har_url")
    parser.add_argument("--set-content-type", action="store_true", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_BATCH_CONTENT_TYPE", "0").lower() in {"1", "true", "yes", "y"})
    parser.add_argument("--bootstrap-url", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_BOOTSTRAP_URL", "https://www.casasbahia.com.br/"))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_BROWSER_BATCH_CONCURRENCY", "3")))
    parser.add_argument("--wait-seconds", type=float, default=float(os.getenv("SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS", "3")))
    parser.add_argument("--page-timeout", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_BROWSER_PAGE_TIMEOUT", "15")))
    parser.add_argument("--fetch-timeout", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_BROWSER_FETCH_TIMEOUT", "8")))
    parser.add_argument("--script-timeout", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_BROWSER_SCRIPT_TIMEOUT", "30")))
    parser.add_argument("--page-load-strategy", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_PAGE_LOAD_STRATEGY", "none"), choices=["normal", "eager", "none"])
    parser.add_argument("--headless", action="store_true", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_HEADLESS", "0").lower() in {"1", "true", "yes", "y"})
    parser.add_argument("--close-browser", action="store_true", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_CLOSE", "1").lower() in {"1", "true", "yes", "y"})
    args = parser.parse_args()
    args.har = Path(args.har)
    args.output = Path(args.output)
    run(args)


if __name__ == "__main__":
    main()
