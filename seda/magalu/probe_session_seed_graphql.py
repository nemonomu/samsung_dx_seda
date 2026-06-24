import argparse
import csv
import json
import os
import time
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path

import requests

from seda.magalu import search_api
from seda.parsers import parse_listing
from seda.step00_config import DEFAULT_RUNS_BASE, RETAILERS, page_url, run_date


CSV_COLUMNS = [
    "run_id",
    "page",
    "profile",
    "client",
    "endpoint_profile",
    "cookie_mode",
    "url",
    "status_code",
    "seconds",
    "bytes",
    "products",
    "parsed_rows",
    "pagination_page",
    "pagination_size",
    "error",
    "bootstrap_url",
    "bootstrap_success",
    "browser_url",
    "browser_title",
    "browser_ready_state",
    "cookie_count",
    "cookie_names",
    "preview",
]


def main():
    args = parse_args()
    os.environ["SEDA_PRODUCT_LINE"] = args.product_line
    if args.postal_code:
        os.environ["SEDA_POSTAL_CODE"] = args.postal_code

    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_RUNS_BASE / "magalu" / run_date() / "session_seed_graphql"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"session_seed_graphql_{args.product_line}_{stamp}.csv"
    json_path = out_dir / f"session_seed_graphql_{args.product_line}_{stamp}.json"

    bootstrap_url = args.bootstrap_url or page_url(RETAILERS["magalu"], 1, run_id="main")
    seed = seed_browser_session(bootstrap_url, args.bootstrap_wait_seconds, args.browser_timeout)
    print(
        "[session_seed] "
        f"bootstrap_success={int(seed['success'])} cookies={len(seed['cookies'])} "
        f"url={seed['browser_url'] or '-'} error={seed['error'] or '-'}",
        flush=True,
    )

    rows = []
    for run_id in ("main", "bsr"):
        for page in parse_pages(args.pages):
            url = page_url(RETAILERS["magalu"], page, run_id=run_id)
            payload = search_api._payload(url, args.page_size)
            for profile in profiles(args):
                print(f"[session_seed] {run_id} page={page} profile={profile['name']} start", flush=True)
                row = run_profile(profile, seed, url, payload, run_id, page, args.timeout)
                rows.append(row)
                write_outputs(csv_path, json_path, seed, rows)
                print(
                    f"[session_seed] {run_id} page={page} profile={profile['name']} "
                    f"status={row['status_code']} products={row['products']} parsed={row['parsed_rows']} "
                    f"error={row['error'] or '-'} seconds={row['seconds']}",
                    flush=True,
                )

    if args.close_browser:
        close_browser()

    print(f"[session_seed] wrote {json_path}")
    print(f"[session_seed] wrote {csv_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Probe Magalu direct GraphQL after one browser session/cookie seed. No browser per page."
    )
    parser.add_argument("product_line", nargs="?", default=os.getenv("SEDA_PRODUCT_LINE", "TV").upper(), choices=["TV", "REF", "LDY"])
    parser.add_argument("--pages", default=os.getenv("SEDA_MAGALU_SESSION_SEED_PAGES", "1,2,3"))
    parser.add_argument("--page-size", type=int, default=int(os.getenv("SEDA_MAGALU_SEARCH_PAGE_SIZE", "60")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SEDA_TIMEOUT", "60")))
    parser.add_argument("--browser-timeout", type=float, default=float(os.getenv("SEDA_MAGALU_SESSION_SEED_BROWSER_TIMEOUT", "30")))
    parser.add_argument("--bootstrap-wait-seconds", type=float, default=float(os.getenv("SEDA_MAGALU_SESSION_SEED_WAIT_SECONDS", "5")))
    parser.add_argument("--bootstrap-url", default=os.getenv("SEDA_MAGALU_SESSION_SEED_BOOTSTRAP_URL", ""))
    parser.add_argument("--postal-code", default=os.getenv("SEDA_POSTAL_CODE", "01001-001"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--keep-browser-open",
        dest="close_browser",
        action="store_false",
        default=os.getenv("SEDA_MAGALU_SESSION_SEED_CLOSE_BROWSER", "1").lower() not in {"0", "false", "no", "n"},
    )
    parser.add_argument("--no-curl-cffi", action="store_true")
    return parser.parse_args()


def seed_browser_session(url, wait_seconds, timeout_seconds):
    started = time.perf_counter()
    seed = {
        "success": False,
        "error": "",
        "bootstrap_url": url,
        "browser_url": "",
        "browser_title": "",
        "browser_ready_state": "",
        "user_agent": "",
        "cookies": [],
        "cookie_header": "",
        "seconds": 0,
    }
    try:
        from seda.magalu.browser_session import get_page

        page = get_page()
        try:
            page.set.timeouts(page_load=timeout_seconds, script=timeout_seconds)
        except Exception:
            pass
        page.get(url)
        _wait_doc_loaded(page, timeout_seconds)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        seed.update(read_browser_context(page))
        seed["success"] = bool(seed["cookies"]) and seed["browser_url"].startswith("https://www.magazineluiza.com.br")
        if not seed["success"] and not seed["error"]:
            seed["error"] = "missing_magalu_browser_context"
    except Exception as exc:
        seed["error"] = f"{type(exc).__name__}: {exc}"
    seed["seconds"] = round(time.perf_counter() - started, 3)
    return seed


def _wait_doc_loaded(page, timeout_seconds):
    try:
        page.wait.doc_loaded(timeout=timeout_seconds, raise_err=False)
    except Exception:
        pass


def read_browser_context(page):
    context = {
        "browser_url": "",
        "browser_title": "",
        "browser_ready_state": "",
        "user_agent": "",
        "cookies": [],
        "cookie_header": "",
        "error": "",
    }
    errors = []
    try:
        target_id = getattr(page, "_target_id", None)
        target = page._run_cdp("Target.getTargetInfo", targetId=target_id) if target_id else {}
        info = target.get("targetInfo") or {}
        context["browser_url"] = str(info.get("url") or "")
        context["browser_title"] = str(info.get("title") or "")
    except Exception as exc:
        errors.append(f"target:{type(exc).__name__}: {exc}")
        try:
            context["browser_url"] = str(page.url or "")
        except Exception:
            pass

    try:
        context["browser_ready_state"] = str(page.run_js("return document.readyState;", timeout=5) or "")
    except Exception as exc:
        errors.append(f"ready:{type(exc).__name__}: {exc}")

    try:
        context["user_agent"] = str(page.run_js("return navigator.userAgent;", timeout=5) or "")
    except Exception as exc:
        errors.append(f"ua:{type(exc).__name__}: {exc}")

    cookies = read_cdp_cookies(page)
    if not cookies:
        cookies = read_document_cookies(page)
    context["cookies"] = cookies
    context["cookie_header"] = cookie_header(cookies)
    context["error"] = "; ".join(errors)
    return context


def read_cdp_cookies(page):
    try:
        try:
            page._run_cdp("Network.enable")
        except Exception:
            pass
        result = page._run_cdp("Network.getAllCookies") or {}
    except Exception:
        return []
    cookies = []
    for item in result.get("cookies") or []:
        domain = str(item.get("domain") or "")
        if "magazineluiza.com.br" not in domain:
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain or ".magazineluiza.com.br",
                "path": str(item.get("path") or "/"),
            }
        )
    return dedupe_cookies(cookies)


def read_document_cookies(page):
    try:
        raw = str(page.run_js("return document.cookie || '';", timeout=5) or "")
    except Exception:
        raw = ""
    parsed = SimpleCookie()
    try:
        parsed.load(raw)
    except Exception:
        return []
    cookies = []
    for name, morsel in parsed.items():
        cookies.append({"name": name, "value": morsel.value, "domain": ".magazineluiza.com.br", "path": "/"})
    return dedupe_cookies(cookies)


def dedupe_cookies(cookies):
    seen = set()
    deduped = []
    for cookie in cookies:
        key = (cookie.get("name"), cookie.get("domain"), cookie.get("path"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cookie)
    return deduped


def cookie_header(cookies):
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies if cookie.get("name"))


def profiles(args):
    values = [
        {
            "name": "requests_seed_cookiejar_op",
            "client": "requests",
            "endpoint": "operation",
            "cookie_mode": "jar",
        },
        {
            "name": "requests_seed_cookie_header_op",
            "client": "requests",
            "endpoint": "operation",
            "cookie_mode": "header",
        },
        {
            "name": "requests_seed_cookie_header_plain",
            "client": "requests",
            "endpoint": "plain",
            "cookie_mode": "header",
        },
    ]
    if not args.no_curl_cffi and curl_cffi_available():
        values.append(
            {
                "name": "curl_cffi_seed_cookie_header_op",
                "client": "curl_cffi",
                "endpoint": "operation",
                "cookie_mode": "header",
            }
        )
    return values


def curl_cffi_available():
    try:
        import curl_cffi  # noqa: F401
    except Exception:
        return False
    return True


def run_profile(profile, seed, listing_url, payload, run_id, page, timeout):
    started = time.perf_counter()
    text = ""
    status_code = 0
    error = ""
    try:
        response = post_graphql(profile, seed, listing_url, payload, timeout)
        status_code = int(response.status_code)
        text = response.text or ""
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    summary = summarize_response(text, listing_url, run_id, page)
    if status_code != 200:
        error = error or f"http_{status_code}"
    elif summary["json_error"]:
        error = error or summary["json_error"]
    elif summary["pagination_page"] and summary["pagination_page"] != page:
        error = error or f"page_mismatch:{summary['pagination_page']}!={page}"
    elif not summary["products"]:
        error = error or "empty_products"

    return {
        "run_id": run_id,
        "page": page,
        "profile": profile["name"],
        "client": profile["client"],
        "endpoint_profile": profile["endpoint"],
        "cookie_mode": profile["cookie_mode"],
        "url": listing_url,
        "status_code": status_code,
        "seconds": round(time.perf_counter() - started, 3),
        "bytes": len(text),
        "products": summary["products"],
        "parsed_rows": summary["parsed_rows"],
        "pagination_page": summary["pagination_page"],
        "pagination_size": summary["pagination_size"],
        "error": error,
        "bootstrap_url": seed["bootstrap_url"],
        "bootstrap_success": int(bool(seed["success"])),
        "browser_url": seed["browser_url"],
        "browser_title": seed["browser_title"],
        "browser_ready_state": seed["browser_ready_state"],
        "cookie_count": len(seed["cookies"]),
        "cookie_names": ",".join(cookie.get("name", "") for cookie in seed["cookies"][:25]),
        "preview": text[:180].replace("\r", " ").replace("\n", " "),
    }


def post_graphql(profile, seed, listing_url, payload, timeout):
    endpoint = endpoint_url(profile["endpoint"], payload.get("operationName", ""))
    headers = request_headers(listing_url, seed, cookie_header_mode=profile["cookie_mode"] == "header")
    if profile["client"] == "curl_cffi":
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate=os.getenv("SEDA_MAGALU_CURL_IMPERSONATE", "chrome136"))
    else:
        session = requests.Session()
    if profile["cookie_mode"] == "jar":
        attach_cookies(session, seed["cookies"])
    return session.post(endpoint, json=payload, headers=headers, timeout=timeout)


def attach_cookies(session, cookies):
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name:
            continue
        domain = cookie.get("domain") or ".magazineluiza.com.br"
        path = cookie.get("path") or "/"
        session.cookies.set(name, value, domain=domain, path=path)
        if domain.startswith("www."):
            session.cookies.set(name, value, domain=".magazineluiza.com.br", path=path)


def request_headers(url, seed, cookie_header_mode=False):
    headers = search_api._headers(url)
    if seed.get("user_agent"):
        headers["user-agent"] = seed["user_agent"]
    headers.update(
        {
            "accept": "application/json",
            "origin": "https://www.magazineluiza.com.br",
            "referer": url,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }
    )
    if cookie_header_mode and seed.get("cookie_header"):
        headers["cookie"] = seed["cookie_header"]
    return headers


def endpoint_url(profile, operation):
    if profile == "operation" and operation:
        return f"{search_api.GRAPHQL_URL}?operationName={operation}"
    return search_api.GRAPHQL_URL


def summarize_response(text, url, run_id, page):
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {"json_error": "invalid_json", "products": 0, "parsed_rows": 0, "pagination_page": 0, "pagination_size": 0}
    search = ((data.get("data") or {}).get("search") or {}) if isinstance(data, dict) else {}
    if not isinstance(search, dict) or not search:
        return {"json_error": "missing_search", "products": 0, "parsed_rows": 0, "pagination_page": 0, "pagination_size": 0}
    products = search.get("products") or []
    pagination = search.get("pagination") or {}
    html_text = search_api._as_next_data_html(search, url)
    parsed_rows = len(parse_listing(html_text, "Magalu", RETAILERS["magalu"].base_url, url, run_id=run_id))
    return {
        "json_error": "",
        "products": len(products) if isinstance(products, list) else 0,
        "parsed_rows": parsed_rows,
        "pagination_page": as_int(pagination.get("page"), 0),
        "pagination_size": as_int(pagination.get("size"), 0),
    }


def parse_pages(raw):
    pages = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if item:
            pages.append(int(item))
    return pages or [1, 2, 3]


def as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def write_outputs(csv_path, json_path, seed, rows):
    redacted_seed = {
        key: value
        for key, value in seed.items()
        if key not in {"cookie_header", "cookies"}
    }
    redacted_seed["cookie_count"] = len(seed.get("cookies") or [])
    redacted_seed["cookie_names"] = [cookie.get("name", "") for cookie in (seed.get("cookies") or [])]
    payload = {
        "seed": redacted_seed,
        "results": rows,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def close_browser():
    try:
        from seda.magalu.browser_session import close_page

        close_page(force=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
