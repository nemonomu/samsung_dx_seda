import atexit
import html as html_module
import json
import os
import time
import unicodedata
from urllib.parse import parse_qs, urlparse

from ..parsers import extract_next_data, magalu_next_search_is_null


_PAGE = None
_PAGE_CREATED_AT = 0.0
_PAGE_USE_COUNT = 0


def get_page():
    global _PAGE, _PAGE_CREATED_AT
    if _PAGE is not None:
        return _PAGE

    from DrissionPage import ChromiumOptions, ChromiumPage

    options = ChromiumOptions()
    address = os.getenv("SEDA_MAGALU_BROWSER_ADDRESS", "").strip()
    local_port = os.getenv("SEDA_MAGALU_BROWSER_LOCAL_PORT", "").strip()
    if address:
        options.set_address(address)
    elif local_port:
        options.set_local_port(int(local_port))

    if os.getenv("SEDA_MAGALU_BROWSER_USE_SYSTEM_PROFILE", "0").lower() in {"1", "true", "yes", "y"}:
        options.use_system_user_path()
    elif not address:
        profile = os.getenv("SEDA_MAGALU_BROWSER_PROFILE", "C:/tmp/seda_magalu_drission_profile")
        options.set_user_data_path(profile)

    options.set_load_mode(os.getenv("SEDA_MAGALU_BROWSER_LOAD_MODE", "eager"))
    options.set_timeouts(
        base=_env_float("SEDA_MAGALU_BROWSER_BASE_TIMEOUT", 30),
        page_load=_env_float("SEDA_MAGALU_BROWSER_PAGE_LOAD_TIMEOUT", 30),
        script=_env_float("SEDA_MAGALU_BROWSER_SCRIPT_TIMEOUT", 30),
    )
    _PAGE = ChromiumPage(options)
    _PAGE_CREATED_AT = time.time()
    return _PAGE


def _page_for_use(reason=""):
    global _PAGE_USE_COUNT
    page = get_page()
    _PAGE_USE_COUNT += 1
    if _should_recycle_page(page):
        _restart_page(reason or "recycle")
        page = get_page()
        _PAGE_USE_COUNT = 1
    return page


def _should_recycle_page(page):
    max_uses = _env_int("SEDA_MAGALU_BROWSER_MAX_USES", 80)
    max_age = _env_float("SEDA_MAGALU_BROWSER_MAX_AGE_SECONDS", 1800)
    if max_uses > 0 and _PAGE_USE_COUNT >= max_uses:
        return True
    if max_age > 0 and _PAGE_CREATED_AT and time.time() - _PAGE_CREATED_AT >= max_age:
        return True
    try:
        if _is_oom_state(page.url or "", ""):
            return True
    except Exception:
        return True
    return False


def _page_state(page):
    try:
        return page.url or "", page.html or ""
    except Exception:
        return "", ""


def _stop_loading(page):
    try:
        page.stop_loading()
    except Exception:
        pass


def _is_magalu_url(url):
    return "magazineluiza.com.br" in str(url or "")


def _ascii_lower(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def _is_bad_browser_state(url, html):
    if _is_oom_state(url, html):
        return True
    haystack = _ascii_lower(f"{url}\n{html}")
    markers = (
        "nao e possivel acessar a pagina",
        "erro 403",
        "ops!",
        "alguma coisa deu errado",
        "access denied",
        "akamai",
        "captcha",
        "customdeny",
        "bot detection",
        "robot",
        "chrome-error://",
        "chromewebdata",
    )
    return any(marker in haystack for marker in markers)


def _restart_page(reason=""):
    close_page(force=True)
    sleep_seconds = _env_float("SEDA_MAGALU_BROWSER_RESTART_SLEEP_SECONDS", 3)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def _prepare_js_page(page, reason):
    _stop_loading(page)
    url, html = _page_state(page)
    if _is_magalu_url(url) and not _is_bad_browser_state(url, html):
        return page
    return _warmup_page(page, reason)


def _is_oom_state(url, html):
    haystack = f"{url}\n{html}".lower()
    markers = (
        "out of memory",
        "ran out of memory",
        "aw, snap",
        "aw snap",
        "status_breakpoint",
        "status_access_violation",
        "chrome-error://",
        "chromewebdata",
    )
    return any(marker in haystack for marker in markers)


def _trace_oom(trace, attempt, page):
    try:
        url = page.url or ""
        html = page.html or ""
    except Exception:
        url = ""
        html = ""
    if _is_oom_state(url, html):
        trace.append({"attempt": attempt, "length": len(html), "url": url, "error": "chrome_oom_or_crash_page"})
        return True
    return False


def _warmup_page(page, reason):
    warmup_url = os.getenv("SEDA_MAGALU_BROWSER_WARMUP_URL", "https://www.magazineluiza.com.br/busca/tv/")
    if str(reason or "").startswith("search_browser_graphql"):
        warmup_seconds = _env_float("SEDA_MAGALU_SEARCH_BROWSER_WARMUP_SECONDS", 1)
        nav_timeout = _env_float("SEDA_MAGALU_SEARCH_BROWSER_WARMUP_NAV_TIMEOUT", 4)
    else:
        warmup_seconds = _env_float("SEDA_MAGALU_BROWSER_WARMUP_SECONDS", 5)
        nav_timeout = _env_float("SEDA_MAGALU_BROWSER_WARMUP_NAV_TIMEOUT", 8)
    attempts = max(1, _env_int("SEDA_MAGALU_BROWSER_WARMUP_ATTEMPTS", 2))
    last_page = page
    for attempt in range(1, attempts + 1):
        try:
            last_page.get(warmup_url, timeout=nav_timeout)
            _stop_loading(last_page)
            time.sleep(warmup_seconds)
            url, html = _page_state(last_page)
            if not _is_bad_browser_state(url, html):
                return last_page
            _restart_page(f"{reason}_bad_state")
        except Exception:
            _stop_loading(last_page)
            _restart_page(f"{reason}_failed")
        last_page = _page_for_use(f"{reason}_retry")
    return last_page


def ensure_magalu_session(reason="ensure_magalu_session"):
    page = _page_for_use(reason)
    _stop_loading(page)
    url, html = _page_state(page)
    trace = [{"method": "browser_session_check", "url": url, "length": len(html)}]
    if _is_magalu_url(url) and not _is_bad_browser_state(url, html):
        trace[-1]["reused"] = True
        return {"success": True, "trace": trace}
    page = _warmup_page(page, reason)
    url, html = _page_state(page)
    ok = _is_magalu_url(url) and not _is_bad_browser_state(url, html)
    trace.append({"method": "browser_session_warmup", "url": url, "length": len(html), "success": ok})
    if not ok:
        _restart_page(f"{reason}_warmup_bad_state")
    return {"success": ok, "trace": trace}


def fetch_page_html(url, wait_seconds=None, attempts=None, validate_search_payload=True):
    search_recycle_attempts = _env_int("SEDA_MAGALU_BROWSER_SEARCH_RECYCLE_ATTEMPTS", 1)
    should_validate_search = validate_search_payload and _is_magalu_search_url(url)
    if should_validate_search:
        return _fetch_search_page_html(url, wait_seconds=wait_seconds, attempts=attempts, recycle_attempts=search_recycle_attempts)

    wait_seconds = float(wait_seconds) if wait_seconds is not None else _env_float("SEDA_MAGALU_BROWSER_WAIT_SECONDS", 5)
    attempts = max(1, int(attempts) if attempts is not None else _env_int("SEDA_MAGALU_BROWSER_ATTEMPTS", 3))
    nav_timeout = _env_float("SEDA_MAGALU_BROWSER_NAV_TIMEOUT", 20)
    max_cycles = 1
    page = _page_for_use("fetch_page_html")
    trace = []
    last_error = ""

    for cycle in range(1, max_cycles + 1):
        if cycle > 1:
            trace.append({"cycle": cycle, "error": "restart_after_search_payload_error", "previous_error": last_error})
            _restart_page("fetch_page_html_search_payload_error")
            page = _page_for_use("fetch_page_html_search_payload_retry")

        for attempt in range(1, attempts + 1):
            try:
                page.get(url, timeout=nav_timeout)
                _stop_loading(page)
                time.sleep(wait_seconds)
                if _trace_oom(trace, attempt, page):
                    _restart_page("fetch_page_html_oom")
                    page = _page_for_use("fetch_page_html_retry")
                    last_error = "chrome_oom_or_crash_page"
                    continue
                html = page.html or ""
                trace_item = {"cycle": cycle, "attempt": attempt, "length": len(html), "url": page.url}
                trace.append(trace_item)
                if "__NEXT_DATA__" in html or len(html) > 100000:
                    search_error = _magalu_search_payload_error(url, html) if should_validate_search else ""
                    if search_error:
                        last_error = search_error
                        trace_item["error"] = search_error
                        if attempt < attempts:
                            continue
                        break
                    return {"success": True, "text": html, "trace": trace, "url": page.url}
                last_error = "browser_html_missing_next_data"
            except Exception as exc:
                _stop_loading(page)
                last_error = f"{type(exc).__name__}: {exc}"
                trace.append({"cycle": cycle, "attempt": attempt, "length": 0, "error": last_error})
                time.sleep(wait_seconds)

    return {"success": False, "text": "", "error": last_error or "browser_fetch_failed", "trace": trace}


def _fetch_search_page_html(url, wait_seconds=None, attempts=None, recycle_attempts=None):
    wait_seconds = float(wait_seconds) if wait_seconds is not None else _env_float("SEDA_MAGALU_SEARCH_BROWSER_WAIT_SECONDS", 0.25)
    attempts = max(1, int(attempts) if attempts is not None else _env_int("SEDA_MAGALU_SEARCH_BROWSER_HTML_ATTEMPTS", 3))
    recycle_attempts = max(0, int(recycle_attempts) if recycle_attempts is not None else _env_int("SEDA_MAGALU_BROWSER_SEARCH_RECYCLE_ATTEMPTS", 1))
    ready_timeout = _env_float("SEDA_MAGALU_SEARCH_BROWSER_READY_TIMEOUT", 5)
    poll_seconds = _env_float("SEDA_MAGALU_SEARCH_BROWSER_POLL_SECONDS", 0.25)
    max_cycles = 1 + recycle_attempts
    page = _page_for_use("fetch_search_page_html")
    trace = []
    last_error = ""
    last_html = ""

    for cycle in range(1, max_cycles + 1):
        if cycle > 1:
            trace.append({"cycle": cycle, "method": "restart", "previous_error": last_error})
            _restart_page("fetch_search_page_payload_error")
            page = _page_for_use("fetch_search_page_retry")

        for attempt in range(1, attempts + 1):
            method, nav_error = _trigger_search_navigation(page, url, refresh=attempt > 1)

            wait_result = _wait_for_magalu_search_payload(page, url, ready_timeout, poll_seconds)
            html = wait_result.get("html", "")
            state = wait_result.get("state", {})
            last_html = html
            last_error = wait_result.get("error", "") or nav_error or "browser_html_search_payload_failed"
            trace_item = {
                "cycle": cycle,
                "attempt": attempt,
                "method": method,
                "navigation_error": nav_error,
                "url": state.get("url", ""),
                "length": len(html),
                "error": wait_result.get("error", ""),
                "has_next_data": bool(state.get("has_next_data")),
                "products": state.get("products", 0),
                "pagination_page": state.get("pagination_page", 0),
                "pagination_size": state.get("pagination_size", 0),
                "source": state.get("source", ""),
                "ready_state": state.get("ready_state", ""),
                "next_data_length": state.get("next_data_length", 0),
                "js_error": state.get("js_error", ""),
            }
            trace.append(trace_item)
            if wait_result.get("success"):
                _stop_loading(page)
                return {"success": True, "text": html, "trace": trace, "url": state.get("url", "")}

            if wait_seconds > 0:
                time.sleep(wait_seconds)
            if _trace_oom(trace, attempt, page):
                last_error = "chrome_oom_or_crash_page"
                break

        _stop_loading(page)

    return {"success": False, "text": last_html, "error": last_error or "browser_fetch_failed", "trace": trace}


def _wait_for_magalu_search_payload(page, expected_url, timeout_seconds, poll_seconds):
    deadline = time.perf_counter() + max(0.5, float(timeout_seconds or 0))
    poll_seconds = max(0.05, float(poll_seconds or 0.25))
    last_state = {}
    last_html = ""
    last_error = ""
    while time.perf_counter() <= deadline:
        snapshot = _read_search_next_data_snapshot(page)
        last_html = snapshot.get("html", "")
        state = _magalu_search_payload_state(
            expected_url,
            snapshot.get("url", ""),
            last_html,
            browser_text=snapshot.get("browser_text", ""),
        )
        state["source"] = snapshot.get("source", "")
        state["ready_state"] = snapshot.get("ready_state", "")
        state["title"] = snapshot.get("title", "")
        state["next_data_length"] = snapshot.get("next_data_length", 0)
        if snapshot.get("error"):
            state["js_error"] = snapshot["error"]
        last_state = state
        if state.get("valid"):
            return {"success": True, "html": last_html, "state": state, "error": ""}
        last_error = state.get("error") or snapshot.get("error") or last_error
        if state.get("blocked") or state.get("too_large"):
            break
        time.sleep(poll_seconds)
    return {"success": False, "html": last_html, "state": last_state, "error": last_error or "browser_html_search_payload_failed"}


def _read_search_next_data_snapshot(page):
    script = """
return (() => {
  try {
    const node = document.querySelector('script#__NEXT_DATA__');
    const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
    return JSON.stringify({
      href: location.href || '',
      title: document.title || '',
      readyState: document.readyState || '',
      nextData: node ? (node.textContent || '') : '',
      bodyText: body.slice(0, 3000)
    });
  } catch (error) {
    return JSON.stringify({error: String(error), href: location.href || '', title: document.title || '', readyState: document.readyState || '', nextData: '', bodyText: ''});
  }
})()
"""
    try:
        raw = page.run_js(script, timeout=_env_float("SEDA_MAGALU_SEARCH_NEXTDATA_JS_TIMEOUT", 2))
        payload = json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})
    except Exception as exc:
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    next_data = payload.get("nextData") if isinstance(payload, dict) else ""
    if not isinstance(next_data, str):
        next_data = ""
    html = _next_data_text_to_html(next_data) if next_data.strip() else ""
    browser_text = "\n".join(
        str(payload.get(key, "") or "") for key in ("title", "href", "bodyText", "error") if isinstance(payload, dict)
    )
    return {
        "url": str(payload.get("href", "") or "") if isinstance(payload, dict) else "",
        "title": str(payload.get("title", "") or "") if isinstance(payload, dict) else "",
        "ready_state": str(payload.get("readyState", "") or "") if isinstance(payload, dict) else "",
        "html": html,
        "browser_text": browser_text,
        "next_data_length": len(next_data),
        "source": "script_text" if next_data.strip() else "script_text_missing",
        "error": str(payload.get("error", "") or "") if isinstance(payload, dict) else "",
    }


def _next_data_text_to_html(next_data_text):
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + html_module.escape(str(next_data_text or ""), quote=False)
        + "</script>"
    )


def _magalu_search_payload_state(expected_url, actual_url, html, browser_text=""):
    state = {
        "valid": False,
        "error": "",
        "url": actual_url or "",
        "has_next_data": "__NEXT_DATA__" in (html or ""),
        "products": 0,
        "pagination_page": 0,
        "pagination_size": 0,
        "blocked": False,
        "too_large": False,
    }
    data = extract_next_data(html)
    if not data:
        if _is_bad_browser_state(actual_url, f"{browser_text}\n{html}"):
            state["blocked"] = True
            state["error"] = "browser_html_blocked_or_error_page"
            return state
        state["error"] = "browser_html_missing_next_data"
        return state
    props = data.get("props") if isinstance(data, dict) else {}
    page_props = props.get("pageProps") if isinstance(props, dict) else {}
    page_data = page_props.get("data") if isinstance(page_props, dict) else {}
    if isinstance(page_data, dict) and "search" in page_data and page_data.get("search") is None:
        state["error"] = "browser_html_search_null"
        return state
    search = page_data.get("search") if isinstance(page_data, dict) else {}
    if not isinstance(search, dict):
        if _is_bad_browser_state(actual_url, f"{browser_text}\n{html}"):
            state["blocked"] = True
            state["error"] = "browser_html_blocked_or_error_page"
            return state
        state["error"] = "browser_html_search_missing"
        return state
    products = search.get("products")
    if not isinstance(products, list) or not products:
        state["error"] = "browser_html_products_missing"
        return state
    pagination = search.get("pagination") if isinstance(search.get("pagination"), dict) else {}
    payload_page = _safe_int(pagination.get("page"), 0)
    payload_size = _safe_int(pagination.get("size"), 0)
    state["products"] = len(products)
    state["pagination_page"] = payload_page
    state["pagination_size"] = payload_size
    requested_page = _requested_search_page(expected_url)
    if payload_page != requested_page:
        state["error"] = f"browser_html_page_mismatch:{payload_page}!={requested_page}"
        return state
    max_products = _env_int("SEDA_MAGALU_SEARCH_BROWSER_MAX_PRODUCTS", 120)
    if max_products > 0 and (len(products) > max_products or payload_size > max_products):
        state["too_large"] = True
        state["error"] = f"browser_html_too_many_products:{max(len(products), payload_size)}"
        return state
    state["valid"] = True
    return state


def _trigger_search_navigation(page, url, refresh=False):
    if refresh:
        try:
            page.run_cdp("Page.reload", ignoreCache=True)
            return "cdp_reload", ""
        except Exception as exc:
            reload_error = f"{type(exc).__name__}: {exc}"
    else:
        reload_error = ""

    try:
        page.run_cdp("Page.navigate", url=url)
        return "cdp_navigate", reload_error
    except Exception as exc:
        cdp_error = f"{type(exc).__name__}: {exc}"

    try:
        page.get(url, retry=0, interval=0, timeout=_env_float("SEDA_MAGALU_SEARCH_BROWSER_NAV_TIMEOUT", 6))
        return "get", reload_error or cdp_error
    except Exception as exc:
        get_error = f"{type(exc).__name__}: {exc}"
        return "navigation_failed", "; ".join(item for item in (reload_error, cdp_error, get_error) if item)


def _requested_search_page(url):
    query = parse_qs(urlparse(str(url or "")).query)
    raw = (query.get("page") or ["1"])[0]
    return _safe_int(raw, 1)


def _safe_int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _is_magalu_search_url(url):
    return _is_magalu_url(url) and "/busca/" in str(url or "")


def _magalu_search_payload_error(url, html):
    if not _is_magalu_search_url(url):
        return ""
    if magalu_next_search_is_null(html):
        return "browser_html_search_null"
    data = extract_next_data(html)
    props = data.get("props") if isinstance(data, dict) else {}
    page_props = props.get("pageProps") if isinstance(props, dict) else {}
    page_data = page_props.get("data") if isinstance(page_props, dict) else {}
    search = page_data.get("search") if isinstance(page_data, dict) else {}
    if not isinstance(search, dict):
        return "browser_html_search_missing"
    products = search.get("products") or []
    if not isinstance(products, list) or not products:
        return "browser_html_products_missing"
    return ""


def graphql_post(payload, timeout=None):
    page = _page_for_use("graphql_post")
    timeout = int(timeout) if timeout is not None else _env_int("SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT", 60)
    page = _prepare_js_page(page, "graphql_post_warmup")

    operation = payload.get("operationName") or ""
    attempts = _env_int("SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS", 2)
    script = """
return (async () => {
  try {
    const payload = arguments[0];
    const operation = payload.operationName || '';
    const response = await fetch(
      'https://federation.magazineluiza.com.br/graphql?operationName=' + encodeURIComponent(operation),
      {
        method: 'POST',
        headers: {
          'accept': 'application/json',
          'content-type': 'application/json',
          'x-channel-id': '45',
          'x-channel-name': 'mixer-desk.magazineluiza.com.br'
        },
        body: JSON.stringify(payload)
      }
    );
    return JSON.stringify({status: response.status, text: await response.text()});
  } catch (error) {
    return JSON.stringify({status: 0, error: String(error), text: ''});
  }
})()
"""
    last = {}
    for attempt in range(1, attempts + 1):
        try:
            raw_result = page.run_js(script, payload, timeout=timeout) or "{}"
        except Exception as exc:
            if _trace_oom([], attempt, page):
                _restart_page("graphql_post_run_js_oom")
                page = _page_for_use("graphql_post_retry")
            result = {"status": 0, "error": f"{type(exc).__name__}: {exc}", "text": ""}
        else:
            try:
                result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            except ValueError:
                result = {"status": 0, "error": "invalid_js_result", "text": str(raw_result)}
        text = result.get("text") or ""
        data = {}
        error = result.get("error") or ""
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                error = error or "invalid_json"
        last = {
            "status_code": result.get("status") or 0,
            "text": text,
            "data": data,
            "error": error,
            "operation": operation,
            "attempt": attempt,
        }
        blocked = _graphql_result_blocked(last)
        if not blocked and last["status_code"] == 200:
            return last
        if attempt < attempts:
            page = _prepare_js_page(page, "graphql_post_warmup")
    return last


def _graphql_result_blocked(result):
    status = int(result.get("status_code") or 0)
    text = _ascii_lower(result.get("text") or "")
    if status in {401, 403, 429}:
        return True
    return any(marker in text for marker in ("akamai", "captcha", "access denied", "nao e possivel", "erro 403", "oops", "ops!"))


def graphql_post_raw(payload, timeout=None, endpoint=None):
    page = _page_for_use("graphql_post_raw")
    timeout = int(timeout) if timeout is not None else _env_int("SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT", 60)
    page = _prepare_js_page(page, "graphql_post_raw_warmup")

    endpoint = endpoint or os.getenv("SEDA_MAGALU_GRAPHQL_ENDPOINT", "https://federation.magazineluiza.com.br/graphql")
    payload_text = json.dumps(payload, ensure_ascii=False)
    script = """
return (async () => {
  try {
    const endpoint = arguments[0];
    const payload = JSON.parse(arguments[1]);
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'accept': 'application/json',
        'content-type': 'application/json',
        'x-channel-id': '45',
        'x-channel-name': 'mixer-desk.magazineluiza.com.br'
      },
      body: JSON.stringify(payload)
    });
    return JSON.stringify({status: response.status, text: await response.text()});
  } catch (error) {
    return JSON.stringify({status: 0, error: String(error), text: ''});
  }
})()
"""
    try:
        raw_result = page.run_js(script, endpoint, payload_text, timeout=timeout) or "{}"
    except Exception as exc:
        if _is_oom_state(page.url or "", page.html or ""):
            _restart_page("graphql_post_raw_run_js_oom")
        result = {"status": 0, "error": f"{type(exc).__name__}: {exc}", "text": ""}
    else:
        try:
            result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except ValueError:
            result = {"status": 0, "error": "invalid_js_result", "text": str(raw_result)}
    text = result.get("text") or ""
    data = {}
    error = result.get("error") or ""
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            error = error or "invalid_json"
    return {
        "status_code": result.get("status") or 0,
        "text": text,
        "data": data,
        "error": error,
        "endpoint": endpoint,
        "payload_count": len(payload) if isinstance(payload, list) else 1,
    }


def fetch_html(url, timeout=None):
    page = _page_for_use("fetch_html")
    timeout = int(timeout) if timeout is not None else _env_int("SEDA_MAGALU_BROWSER_HTML_TIMEOUT", 90)
    attempts = _env_int("SEDA_MAGALU_BROWSER_HTML_ATTEMPTS", 2)
    page = _prepare_js_page(page, "fetch_html_warmup")
    script = """
return (async () => {
  try {
    const response = await fetch(arguments[0], {
      method: 'GET',
      headers: {'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
    });
    return JSON.stringify({status: response.status, text: await response.text()});
  } catch (error) {
    return JSON.stringify({status: 0, error: String(error), text: ''});
  }
})()
"""
    trace = []
    last = {"status_code": 0, "text": "", "error": "not_attempted"}
    for attempt in range(1, attempts + 1):
        try:
            raw_result = page.run_js(script, url, timeout=timeout) or "{}"
            result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except Exception as exc:
            result = {"status": 0, "text": "", "error": f"{type(exc).__name__}: {exc}"}
        text = result.get("text") or ""
        status_code = result.get("status") or 0
        error = result.get("error") or ""
        has_next_data = "__NEXT_DATA__" in text
        trace.append(
            {
                "attempt": attempt,
                "method": "browser_fetch",
                "status_code": status_code,
                "length": len(text),
                "has_next_data": has_next_data,
                "error": error,
            }
        )
        last = {"status_code": status_code, "text": text, "error": error, "trace": trace[:]}
        if status_code == 200 and has_next_data:
            return last
        page = _warmup_page(page, "fetch_html_retry_warmup")

    if os.getenv("SEDA_MAGALU_PDP_NAV_FALLBACK", "1").lower() not in {"0", "false", "no", "n"}:
        try:
            nav_timeout = _env_float("SEDA_MAGALU_PDP_NAV_TIMEOUT", min(timeout, 30))
            page.get(url, timeout=nav_timeout)
            _stop_loading(page)
            nav_wait_seconds = _env_float("SEDA_MAGALU_PDP_NAV_WAIT_SECONDS", 5)
            time.sleep(nav_wait_seconds)
            if _trace_oom(trace, len(trace) + 1, page):
                _restart_page("fetch_html_navigation_oom")
                page = _page_for_use("fetch_html_navigation_retry")
                page.get(url, timeout=nav_timeout)
                _stop_loading(page)
                time.sleep(nav_wait_seconds)
            text = page.html or ""
            trace.append(
                {
                    "attempt": len(trace) + 1,
                    "method": "browser_navigation",
                    "status_code": 200 if text else 0,
                    "length": len(text),
                    "has_next_data": "__NEXT_DATA__" in text,
                    "url": page.url,
                }
            )
            return {
                "status_code": 200 if text else 0,
                "text": text,
                "error": "" if "__NEXT_DATA__" in text else "navigation_missing_next_data",
                "trace": trace,
            }
        except Exception as exc:
            _stop_loading(page)
            trace.append(
                {
                    "attempt": len(trace) + 1,
                    "method": "browser_navigation",
                    "status_code": 0,
                    "length": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    last["trace"] = trace
    return last


def close_page(force=False):
    global _PAGE, _PAGE_CREATED_AT, _PAGE_USE_COUNT
    if _PAGE is None:
        return
    if not force and not _should_close_on_exit():
        _PAGE = None
        _PAGE_CREATED_AT = 0.0
        _PAGE_USE_COUNT = 0
        return
    try:
        _PAGE.quit()
    except Exception:
        pass
    finally:
        _PAGE = None
        _PAGE_CREATED_AT = 0.0
        _PAGE_USE_COUNT = 0


def _should_close_on_exit():
    explicit = os.getenv("SEDA_MAGALU_BROWSER_CLOSE_ON_EXIT", "").strip().lower()
    if explicit:
        return explicit in {"1", "true", "yes", "y"}
    if os.getenv("SEDA_MAGALU_BROWSER_ADDRESS", "").strip():
        return False
    if os.getenv("SEDA_MAGALU_BROWSER_USE_SYSTEM_PROFILE", "0").lower() in {"1", "true", "yes", "y"}:
        return False
    return True


atexit.register(close_page)
