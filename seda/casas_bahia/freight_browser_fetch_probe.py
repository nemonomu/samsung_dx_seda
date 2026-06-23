import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from seda.common.retailer_runner import configure_retailer
from seda.step00_config import run_root, write_json

from .detail_api import _cvip_header, _freight_detail
from .freight_har_replay_probe import DEFAULT_HAR, _first_success_har_entry, _read_har


def default_input():
    return str(run_root() / "output" / "seda_final_targets.csv")


def default_output():
    return str(run_root() / "output" / "freight_browser_fetch_probe.json")


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
    headers = har_entry.get("headers") or {}
    return str(headers.get("x-cvip") or "").strip()


def _cvip_for_mode(mode, har_entry, zipcode=None):
    if mode == "none":
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


def _browser_fetch(driver, url, cvip, timeout_seconds):
    script = """
const done = arguments[arguments.length - 1];
const url = arguments[0];
const cvip = arguments[1];
const timeoutMs = arguments[2] * 1000;
try {
  const xhr = new XMLHttpRequest();
  xhr.open("GET", url, true);
  xhr.timeout = timeoutMs;
  xhr.setRequestHeader("accept", "application/json, text/plain, */*");
  xhr.setRequestHeader("content-type", "application/json");
  if (cvip) xhr.setRequestHeader("x-cvip", cvip);
  xhr.onload = () => {
    done({
      ok: xhr.status >= 200 && xhr.status < 300,
      status: xhr.status,
      contentType: xhr.getResponseHeader("content-type") || "",
      text: xhr.responseText || ""
    });
  };
  xhr.onerror = () => {
    done({
      ok: false,
      status: xhr.status || 0,
      contentType: xhr.getResponseHeader("content-type") || "",
      text: xhr.responseText || "",
      error: "xhr_error"
    });
  };
  xhr.ontimeout = () => {
    done({
      ok: false,
      status: xhr.status || 0,
      contentType: xhr.getResponseHeader("content-type") || "",
      text: xhr.responseText || "",
      error: "xhr_timeout"
    });
  };
  xhr.send();
} catch (error) {
  done({
    ok: false,
    status: 0,
    contentType: "",
    text: "",
    error: String(error && error.message ? error.message : error)
  });
}
"""
    try:
        return driver.execute_async_script(script, url, cvip, timeout_seconds)
    except Exception as exc:
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        return {
            "ok": False,
            "status": 0,
            "contentType": "",
            "text": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _parse_result(raw):
    text = str((raw or {}).get("text") or "")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    detail = _freight_detail(data) if data is not None else {}
    return {
        "status_code": int((raw or {}).get("status") or 0),
        "ok": bool((raw or {}).get("ok")),
        "content_type": (raw or {}).get("contentType", ""),
        "text_length": len(text),
        "json": data is not None,
        "delivery_availability": detail.get("delivery_availability", ""),
        "pick_up_availability": detail.get("pick_up_availability", ""),
        "error": (raw or {}).get("error", ""),
    }


def _is_blocked_page(driver):
    source = (driver.page_source or "").lower()
    return any(marker in source for marker in ("erro 403", "nao e possivel acessar", "não é possível acessar", "customdeny"))


def _print_result(label, target, result, cvip_mode):
    target_text = f" sku={target.get('sku_id')} seller={target.get('seller_id')}" if target else ""
    delivery = _ascii(result.get("delivery_availability", ""))
    pickup = _ascii(result.get("pick_up_availability", ""))
    print(
        "[casas_freight_browser_probe] "
        f"{label}{target_text} cvip={cvip_mode} success={int(bool(result.get('success')))} "
        f"status={result.get('status_code')} json={int(bool(result.get('json')))} "
        f"len={result.get('text_length')} error={result.get('error')} "
        f"delivery={delivery} pickup={pickup}",
        flush=True,
    )


def _ascii(value):
    return str(value or "").encode("ascii", "backslashreplace").decode("ascii")[:160]


def run(args):
    configure_retailer("casas_bahia")
    har_entry = _first_success_har_entry(_read_har(args.har))
    targets = _load_targets(args.input, args.limit)
    cvip_modes = [item.strip().lower() for item in args.cvip_modes.split(",") if item.strip()]
    results = {
        "har": str(args.har),
        "input": args.input,
        "target_count": len(targets),
        "cvip_modes": cvip_modes,
        "rows": [],
    }

    driver = _create_driver(args)
    try:
        driver.set_page_load_timeout(args.page_timeout)
        driver.set_script_timeout(args.fetch_timeout + 5)
        bootstrap_url = args.bootstrap_url or "https://www.casasbahia.com.br/"
        print(f"[casas_freight_browser_probe] opening bootstrap={bootstrap_url}", flush=True)
        bootstrap_error = ""
        try:
            driver.get(bootstrap_url)
        except Exception as exc:
            bootstrap_error = f"{type(exc).__name__}: {exc}"
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        time.sleep(args.wait_seconds)
        results["bootstrap_url"] = bootstrap_url
        results["bootstrap_error"] = bootstrap_error
        results["bootstrap_blocked"] = _is_blocked_page(driver)
        results["browser_current_url"] = driver.current_url

        for cvip_mode in cvip_modes:
            cvip = _cvip_for_mode(cvip_mode, har_entry, args.zipcode)
            raw = _browser_fetch(driver, har_entry["url"], cvip, args.fetch_timeout)
            result = _parse_result(raw)
            result.update(
                {
                    "case": "har_url",
                    "success": bool(result["ok"] and result["json"] and (result["delivery_availability"] or result["pick_up_availability"])),
                    "cvip_mode": cvip_mode,
                    "url": har_entry["url"],
                    "raw_error": raw.get("error", "") if isinstance(raw, dict) else "",
                }
            )
            _print_result("har_url", {}, result, cvip_mode)
            results["rows"].append(result)

        for pos, target in enumerate(targets, start=1):
            url = _replace_freight_identity(har_entry["url"], target["sku_id"], target["seller_id"], zipcode=args.zipcode)
            if args.navigate_each_product and target.get("product_url"):
                try:
                    driver.get(target["product_url"])
                    time.sleep(args.wait_seconds)
                except Exception:
                    try:
                        driver.execute_script("window.stop();")
                    except Exception:
                        pass
            for cvip_mode in cvip_modes:
                cvip = _cvip_for_mode(cvip_mode, har_entry, args.zipcode)
                raw = _browser_fetch(driver, url, cvip, args.fetch_timeout)
                result = _parse_result(raw)
                result.update(
                    {
                        "case": "target",
                        "success": bool(result["ok"] and result["json"] and (result["delivery_availability"] or result["pick_up_availability"])),
                        "cvip_mode": cvip_mode,
                        "url": url,
                        "target_index": pos,
                        "raw_error": raw.get("error", "") if isinstance(raw, dict) else "",
                        **target,
                    }
                )
                _print_result("target", target, result, cvip_mode)
                results["rows"].append(result)
    finally:
        if args.close_browser:
            try:
                driver.quit()
            except Exception:
                pass

    ok = sum(1 for row in results["rows"] if row.get("success"))
    results["ok"] = ok
    results["fail"] = len(results["rows"]) - ok
    write_json(args.output, results)
    print(json.dumps({"ok": ok, "fail": results["fail"], "output": str(args.output)}, ensure_ascii=False))
    return results


def main():
    configure_retailer("casas_bahia")
    parser = argparse.ArgumentParser(description="Probe Casas Bahia freight endpoint using in-browser fetch from a real Chrome context.")
    parser.add_argument("--har", default=str(DEFAULT_HAR))
    parser.add_argument("--input", default=default_input())
    parser.add_argument("--output", default=default_output())
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--zipcode", default=None)
    parser.add_argument("--cvip-modes", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_FREIGHT_CVIP_MODES", "har,generated,none"))
    parser.add_argument("--bootstrap-url", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_BOOTSTRAP_URL", "https://www.casasbahia.com.br/"))
    parser.add_argument("--navigate-each-product", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=float(os.getenv("SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS", "3")))
    parser.add_argument("--page-timeout", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_BROWSER_PAGE_TIMEOUT", "45")))
    parser.add_argument("--fetch-timeout", type=int, default=int(os.getenv("SEDA_CASAS_BAHIA_BROWSER_FETCH_TIMEOUT", "30")))
    parser.add_argument("--page-load-strategy", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_PAGE_LOAD_STRATEGY", "eager"), choices=["normal", "eager", "none"])
    parser.add_argument("--headless", action="store_true", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_HEADLESS", "0").lower() in {"1", "true", "yes", "y"})
    parser.add_argument("--close-browser", action="store_true", default=os.getenv("SEDA_CASAS_BAHIA_BROWSER_CLOSE", "1").lower() in {"1", "true", "yes", "y"})
    args = parser.parse_args()
    args.har = Path(args.har)
    args.output = Path(args.output)
    run(args)


if __name__ == "__main__":
    main()
