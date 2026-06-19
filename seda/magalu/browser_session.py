import json
import os
import time


_PAGE = None


def get_page():
    global _PAGE
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
        base=float(os.getenv("SEDA_MAGALU_BROWSER_BASE_TIMEOUT", "30")),
        page_load=float(os.getenv("SEDA_MAGALU_BROWSER_PAGE_LOAD_TIMEOUT", "30")),
        script=float(os.getenv("SEDA_MAGALU_BROWSER_SCRIPT_TIMEOUT", "30")),
    )
    _PAGE = ChromiumPage(options)
    return _PAGE


def fetch_page_html(url, wait_seconds=None, attempts=None):
    wait_seconds = float(wait_seconds or os.getenv("SEDA_MAGALU_BROWSER_WAIT_SECONDS", "5"))
    attempts = int(attempts or os.getenv("SEDA_MAGALU_BROWSER_ATTEMPTS", "3"))
    page = get_page()
    trace = []
    last_error = ""

    for attempt in range(1, attempts + 1):
        try:
            page.get(url)
            time.sleep(wait_seconds)
            html = page.html or ""
            trace.append({"attempt": attempt, "length": len(html), "url": page.url})
            if "__NEXT_DATA__" in html or len(html) > 100000:
                return {"success": True, "text": html, "trace": trace, "url": page.url}
            last_error = "browser_html_missing_next_data"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            trace.append({"attempt": attempt, "length": 0, "error": last_error})
            time.sleep(wait_seconds)

    return {"success": False, "text": "", "error": last_error or "browser_fetch_failed", "trace": trace}


def graphql_post(payload, timeout=None):
    page = get_page()
    timeout = int(timeout or os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT", "60"))
    if not str(page.url or "").startswith("https://www.magazineluiza.com.br"):
        page.get(os.getenv("SEDA_MAGALU_BROWSER_WARMUP_URL", "https://www.magazineluiza.com.br/busca/tv/"))
        time.sleep(float(os.getenv("SEDA_MAGALU_BROWSER_WARMUP_SECONDS", "5")))

    operation = payload.get("operationName") or ""
    attempts = int(os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS", "2"))
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
        raw_result = page.run_js(script, payload, timeout=timeout) or "{}"
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
            warmup_url = os.getenv("SEDA_MAGALU_BROWSER_WARMUP_URL", "https://www.magazineluiza.com.br/busca/tv/")
            page.get(warmup_url)
            time.sleep(float(os.getenv("SEDA_MAGALU_BROWSER_WARMUP_SECONDS", "5")))
    return last


def _graphql_result_blocked(result):
    status = int(result.get("status_code") or 0)
    text = (result.get("text") or "").lower()
    if status in {401, 403, 429}:
        return True
    return any(marker in text for marker in ("akamai", "captcha", "access denied", "não é possível", "nao e possivel"))


def graphql_post_raw(payload, timeout=None, endpoint=None):
    page = get_page()
    timeout = int(timeout or os.getenv("SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT", "60"))
    if not str(page.url or "").startswith("https://www.magazineluiza.com.br"):
        page.get(os.getenv("SEDA_MAGALU_BROWSER_WARMUP_URL", "https://www.magazineluiza.com.br/busca/tv/"))
        time.sleep(float(os.getenv("SEDA_MAGALU_BROWSER_WARMUP_SECONDS", "5")))

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
    raw_result = page.run_js(script, endpoint, payload_text, timeout=timeout) or "{}"
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
    page = get_page()
    timeout = int(timeout or os.getenv("SEDA_MAGALU_BROWSER_HTML_TIMEOUT", "90"))
    attempts = int(os.getenv("SEDA_MAGALU_BROWSER_HTML_ATTEMPTS", "2"))
    warmup_url = os.getenv("SEDA_MAGALU_BROWSER_WARMUP_URL", "https://www.magazineluiza.com.br/busca/tv/")
    warmup_seconds = float(os.getenv("SEDA_MAGALU_BROWSER_WARMUP_SECONDS", "5"))
    if not str(page.url or "").startswith("https://www.magazineluiza.com.br"):
        page.get(warmup_url)
        time.sleep(warmup_seconds)
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
        page.get(warmup_url)
        time.sleep(warmup_seconds)

    if os.getenv("SEDA_MAGALU_PDP_NAV_FALLBACK", "1").lower() not in {"0", "false", "no", "n"}:
        try:
            page.get(url)
            time.sleep(float(os.getenv("SEDA_MAGALU_PDP_NAV_WAIT_SECONDS", "5")))
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


def close_page():
    global _PAGE
    if _PAGE is None:
        return
    try:
        _PAGE.quit()
    finally:
        _PAGE = None
