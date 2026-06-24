import atexit
import json
import os
import time

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


def _restart_page(reason=""):
    close_page(force=True)
    sleep_seconds = _env_float("SEDA_MAGALU_BROWSER_RESTART_SLEEP_SECONDS", 3)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


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
    warmup_seconds = _env_float("SEDA_MAGALU_BROWSER_WARMUP_SECONDS", 5)
    try:
        page.get(warmup_url)
        time.sleep(warmup_seconds)
        if _is_oom_state(page.url or "", page.html or ""):
            _restart_page(f"{reason}_oom")
            return _page_for_use(f"{reason}_retry")
        return page
    except Exception:
        _restart_page(f"{reason}_failed")
        return _page_for_use(f"{reason}_retry")


def fetch_page_html(url, wait_seconds=None, attempts=None):
    wait_seconds = float(wait_seconds) if wait_seconds is not None else _env_float("SEDA_MAGALU_BROWSER_WAIT_SECONDS", 5)
    attempts = int(attempts) if attempts is not None else _env_int("SEDA_MAGALU_BROWSER_ATTEMPTS", 3)
    search_recycle_attempts = _env_int("SEDA_MAGALU_BROWSER_SEARCH_RECYCLE_ATTEMPTS", 1)
    max_cycles = 1 + search_recycle_attempts if _is_magalu_search_url(url) else 1
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
                page.get(url)
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
                    search_error = _magalu_search_payload_error(url, html)
                    if search_error:
                        last_error = search_error
                        trace_item["error"] = search_error
                        if attempt < attempts:
                            continue
                        break
                    return {"success": True, "text": html, "trace": trace, "url": page.url}
                last_error = "browser_html_missing_next_data"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                trace.append({"cycle": cycle, "attempt": attempt, "length": 0, "error": last_error})
                time.sleep(wait_seconds)

    return {"success": False, "text": "", "error": last_error or "browser_fetch_failed", "trace": trace}


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
    return "magazineluiza.com.br" in str(url or "") and "/busca/" in str(url or "")


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
    if not str(page.url or "").startswith("https://www.magazineluiza.com.br"):
        page = _warmup_page(page, "graphql_post_warmup")

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
            page = _warmup_page(page, "graphql_post_warmup")
    return last


def _graphql_result_blocked(result):
    status = int(result.get("status_code") or 0)
    text = (result.get("text") or "").lower()
    if status in {401, 403, 429}:
        return True
    return any(marker in text for marker in ("akamai", "captcha", "access denied", "não é possível", "nao e possivel"))


def graphql_post_raw(payload, timeout=None, endpoint=None):
    page = _page_for_use("graphql_post_raw")
    timeout = int(timeout) if timeout is not None else _env_int("SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT", 60)
    if not str(page.url or "").startswith("https://www.magazineluiza.com.br"):
        page = _warmup_page(page, "graphql_post_raw_warmup")

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
    if not str(page.url or "").startswith("https://www.magazineluiza.com.br"):
        page = _warmup_page(page, "fetch_html_warmup")
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
            page.get(url)
            nav_wait_seconds = _env_float("SEDA_MAGALU_PDP_NAV_WAIT_SECONDS", 5)
            time.sleep(nav_wait_seconds)
            if _trace_oom(trace, len(trace) + 1, page):
                _restart_page("fetch_html_navigation_oom")
                page = _page_for_use("fetch_html_navigation_retry")
                page.get(url)
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
