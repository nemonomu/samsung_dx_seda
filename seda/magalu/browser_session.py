import atexit
import html as html_module
import json
import os
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlparse

from ..parsers import extract_next_data, magalu_next_search_is_null
from .graphql_contract import (
    graphql_envelope_error,
    graphql_terminal_business_error,
)


_PAGE = None
_PAGE_CREATED_AT = 0.0
_PAGE_USE_COUNT = 0
_SEARCH_BROWSER_SKIP_REASON = ""


def _reset_search_browser_circuit():
    """Reset process-local listing recovery state (primarily for tests)."""
    global _SEARCH_BROWSER_SKIP_REASON
    _SEARCH_BROWSER_SKIP_REASON = ""


def _diag_enabled():
    return os.getenv("SEDA_MAGALU_SEARCH_DIAG_LOG", "1").lower() not in {"0", "false", "no", "n"}


def _diag_verbose():
    return os.getenv("SEDA_MAGALU_SEARCH_DIAG_VERBOSE", "0").lower() in {"1", "true", "yes", "y"}


def _diag_log(message):
    if not _diag_enabled():
        return
    text = str(message or "")
    if not _diag_verbose() and not text.startswith("stage="):
        return
    print(f"[seda][magalu-search] {text}", flush=True)


def _short_text(value, limit=160):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


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
    try:
        page = get_page()
        _PAGE_USE_COUNT += 1
        if _should_recycle_page(page):
            _restart_page(reason or "recycle")
            page = get_page()
            _PAGE_USE_COUNT = 1
        return page
    except Exception:
        # getting or probing the page failed (dead/detached driver) -> relaunch clean
        _restart_page(reason or "page_for_use_error")
        _PAGE_USE_COUNT = 1
        return get_page()


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
        "captcha",
        "customdeny",
        "bot detection",
        "chrome-error://",
        "chromewebdata",
    )
    if any(marker in haystack for marker in markers):
        return True
    return re.search(r"\brobot\b", haystack) is not None


def _is_magalu_search_login_redirect(url, browser_text=""):
    try:
        parsed = urlparse(str(url or ""))
        hostname = (parsed.hostname or "").lower()
    except (TypeError, ValueError):
        hostname = ""
        parsed = None
    location = ""
    if parsed is not None:
        location = _ascii_lower(f"{parsed.path}\n{parsed.fragment}")
    redirected = (
        hostname == "sacola.magazineluiza.com.br"
        and "/cliente/login" in location
    )
    bag_error = (
        hostname == "sacola.magazineluiza.com.br"
        and "ocorreu um erro ao recuperar a sacola"
        in _ascii_lower(browser_text)
    )
    return redirected or bag_error


def _restart_page(reason=""):
    close_page(force=True)
    sleep_seconds = _env_float("SEDA_MAGALU_BROWSER_RESTART_SLEEP_SECONDS", 3)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def _prepare_js_page(page, reason, context_url=None, allow_default_warmup=True):
    _stop_loading(page)
    url, html = _page_state(page)
    if context_url and not _same_page_path(url, context_url):
        return _warmup_page(page, reason, warmup_url=context_url)
    if _is_magalu_url(url) and not _is_bad_browser_state(url, html):
        return page
    if not allow_default_warmup:
        return page
    return _warmup_page(page, reason, warmup_url=context_url)


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


def _is_dead_page_error(exc):
    """True when an exception means the browser tab/driver is gone and the page
    object must be fully relaunched (not just re-navigated). Covers the DrissionPage
    'Not attached to an active page' CDP error and similar disconnects that used to
    hang the run instead of recovering."""
    text = f"{type(exc).__name__} {exc}".lower()
    markers = (
        "not attached", "active page", "target closed", "no target",
        "target crashed", "disconnected", "browser has been closed",
        "browser is closed", "browser already closed", "cdperror",
        "chrome-error", "page crash", "tab crash", "cannot access",
        "connection refused", "connection aborted", "connection reset",
        "websocket", "closed by",
    )
    return any(marker in text for marker in markers)


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


def _warmup_page(page, reason, warmup_url=None):
    warmup_url = warmup_url or os.getenv("SEDA_MAGALU_BROWSER_WARMUP_URL", "https://www.magazineluiza.com.br/busca/tv/")
    _diag_log(f"warmup start reason={reason} target={'search' if _is_magalu_search_url(warmup_url) else 'context'}")
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
        if _SEARCH_BROWSER_SKIP_REASON:
            requested_page = _requested_search_page(url)
            error = "browser_html_search_login_redirect_circuit_open"
            _diag_log(
                "stage=listing "
                f"page={requested_page} action=browser status=skipped "
                f"reason={_SEARCH_BROWSER_SKIP_REASON}"
            )
            return {
                "success": False,
                "text": "",
                "error": error,
                "trace": [
                    {
                        "method": "browser_skip",
                        "error": error,
                        "reason": _SEARCH_BROWSER_SKIP_REASON,
                        "circuit_open": True,
                    }
                ],
            }
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
    global _SEARCH_BROWSER_SKIP_REASON
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
    terminal_redirects = 0
    requested_page = _requested_search_page(url)
    expected_sort_type, expected_sort_orientation = _expected_search_sort(url)

    _diag_log(
        "fetch start "
        f"attempts={attempts} recycle_attempts={recycle_attempts} "
        f"ready_timeout={ready_timeout}s poll={poll_seconds}s"
    )
    _diag_log(
        "stage=listing "
        f"page={requested_page} sort={expected_sort_type}:{expected_sort_orientation} "
        f"action=next_data status=start attempts={attempts} timeout={ready_timeout:g}s"
    )
    for cycle in range(1, max_cycles + 1):
        if cycle > 1:
            trace.append({"cycle": cycle, "method": "restart", "previous_error": last_error})
            _diag_log(f"restart cycle={cycle} previous_error={_short_text(last_error)}")
            _diag_log(
                "stage=listing "
                f"page={requested_page} action=browser_restart status=retry "
                f"cycle={cycle}/{max_cycles} previous={_short_text(last_error, 80)}"
            )
            _restart_page("fetch_search_page_payload_error")
            page = _page_for_use("fetch_search_page_retry")

        for attempt in range(1, attempts + 1):
            _diag_log(f"navigation start cycle={cycle} attempt={attempt} refresh={int(attempt > 1)}")
            _diag_log(
                "stage=listing "
                f"page={requested_page} action=next_data status=trying "
                f"attempt={attempt}/{attempts} cycle={cycle}/{max_cycles}"
            )
            method, nav_error = _trigger_search_navigation(page, url, refresh=attempt > 1)
            _diag_log(
                f"navigation triggered cycle={cycle} attempt={attempt} "
                f"method={method} nav_error={_short_text(nav_error)}"
            )

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
                "selected_sort_type": state.get("selected_sort_type", ""),
                "selected_sort_orientation": state.get("selected_sort_orientation", ""),
                "source": state.get("source", ""),
                "ready_state": state.get("ready_state", ""),
                "next_data_length": state.get("next_data_length", 0),
                "js_error": state.get("js_error", ""),
                "cdp_success": state.get("cdp_success", 0),
                "cdp_empty": state.get("cdp_empty", 0),
                "cdp_error": state.get("cdp_error", ""),
                "fallback_used": state.get("fallback_used", 0),
                "fallback_success": state.get("fallback_success", 0),
                "fallback_empty": state.get("fallback_empty", 0),
                "fallback_error": state.get("fallback_error", ""),
                "terminal_redirect": bool(state.get("terminal_redirect")),
            }
            trace.append(trace_item)
            if wait_result.get("success"):
                _stop_loading(page)
                _diag_log(
                    "fetch success "
                    f"cycle={cycle} attempt={attempt} products={state.get('products', 0)} "
                    f"page={state.get('pagination_page', 0)} "
                    f"sort={state.get('selected_sort_type', '')}:{state.get('selected_sort_orientation', '')} "
                    f"source={state.get('source', '')}"
                )
                _diag_log(
                    "stage=listing "
                    f"page={state.get('pagination_page', requested_page)} action=next_data status=success "
                    f"attempt={attempt}/{attempts} products={state.get('products', 0)} "
                    f"sort={state.get('selected_sort_type', '')}:{state.get('selected_sort_orientation', '')} "
                    f"source={state.get('source', '')}"
                )
                return {"success": True, "text": html, "trace": trace, "url": state.get("url", "")}

            _diag_log(
                "attempt failed "
                f"cycle={cycle} attempt={attempt} error={_short_text(last_error)} "
                f"url={_short_text(state.get('url', ''))} ready={state.get('ready_state', '')} "
                f"next_len={state.get('next_data_length', 0)} products={state.get('products', 0)} "
                f"sort={state.get('selected_sort_type', '')}:{state.get('selected_sort_orientation', '')} "
                f"source={state.get('source', '')}"
            )
            is_terminal_redirect = bool(state.get("terminal_redirect"))
            if is_terminal_redirect:
                terminal_redirects += 1
            terminal_redirect_exhausted = (
                is_terminal_redirect and terminal_redirects >= 2
            )
            retry_left = (
                not terminal_redirect_exhausted
                and (attempt < attempts or cycle < max_cycles)
            )
            terminal_suffix = (
                f" terminal_redirects={terminal_redirects}"
                if is_terminal_redirect
                else ""
            )
            _diag_log(
                "stage=listing "
                f"page={requested_page} action=next_data status={'retry' if retry_left else 'failed'} "
                f"attempt={attempt}/{attempts} products={state.get('products', 0)} "
                f"error={_short_text(last_error, 100)}{terminal_suffix}"
            )
            if is_terminal_redirect:
                if terminal_redirect_exhausted:
                    _stop_loading(page)
                    _SEARCH_BROWSER_SKIP_REASON = "browser_html_search_login_redirect"
                    _diag_log(
                        "fetch failed terminal_redirect "
                        f"count={terminal_redirects} error={_short_text(last_error)}"
                    )
                    return {
                        "success": False,
                        "text": last_html,
                        "error": last_error,
                        "trace": trace,
                    }
                continue
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            if _trace_oom(trace, attempt, page):
                last_error = "chrome_oom_or_crash_page"
                _diag_log(f"oom/crash detected cycle={cycle} attempt={attempt}")
                break

        _stop_loading(page)

    _diag_log(f"fetch failed final_error={_short_text(last_error)}")
    _diag_log(
        "stage=listing "
        f"page={requested_page} action=next_data status=failed error={_short_text(last_error, 100)}"
    )
    return {"success": False, "text": last_html, "error": last_error or "browser_fetch_failed", "trace": trace}


def _wait_for_magalu_search_payload(page, expected_url, timeout_seconds, poll_seconds):
    timeout_seconds = max(0.5, float(timeout_seconds or 0))
    deadline = time.perf_counter() + timeout_seconds
    poll_seconds = max(0.05, float(poll_seconds or 0.25))
    started = time.perf_counter()
    heartbeat_seconds = max(1.0, _env_float("SEDA_MAGALU_SEARCH_DIAG_HEARTBEAT_SECONDS", 5))
    next_heartbeat = started
    last_state = {}
    last_html = ""
    last_error = ""
    _diag_log(f"wait start timeout={timeout_seconds}s poll={poll_seconds}s")
    while time.perf_counter() <= deadline:
        snapshot = _read_search_next_data_snapshot(page)
        last_html = snapshot.get("html", "")
        actual_url = snapshot.get("url", "")
        if not actual_url:
            try:
                actual_url = str(page.url or "")
            except Exception:
                actual_url = ""
        state = _magalu_search_payload_state(
            expected_url,
            actual_url,
            last_html,
            browser_text=snapshot.get("browser_text", ""),
        )
        state["source"] = snapshot.get("source", "")
        state["ready_state"] = snapshot.get("ready_state", "")
        state["title"] = snapshot.get("title", "")
        state["next_data_length"] = snapshot.get("next_data_length", 0)
        state["cdp_source"] = snapshot.get("cdp_source", "")
        state["cdp_success"] = snapshot.get("cdp_success", 0)
        state["cdp_empty"] = snapshot.get("cdp_empty", 0)
        state["cdp_error"] = snapshot.get("cdp_error", "")
        state["fallback_used"] = snapshot.get("fallback_used", 0)
        state["fallback_source"] = snapshot.get("fallback_source", "")
        state["fallback_success"] = snapshot.get("fallback_success", 0)
        state["fallback_empty"] = snapshot.get("fallback_empty", 0)
        state["fallback_error"] = snapshot.get("fallback_error", "")
        if snapshot.get("error"):
            state["js_error"] = snapshot["error"]
        last_state = state
        now = time.perf_counter()
        if now >= next_heartbeat:
            elapsed = now - started
            _diag_log(
                "wait heartbeat "
                f"elapsed={elapsed:.1f}s/{timeout_seconds:.1f}s "
                f"url={_short_text(state.get('url', ''))} title={_short_text(state.get('title', ''))} "
                f"ready={state.get('ready_state', '')} next_len={state.get('next_data_length', 0)} "
                f"products={state.get('products', 0)} source={state.get('source', '')} "
                f"error={_short_text(state.get('error') or snapshot.get('error') or '')} "
                f"cdp_success={state.get('cdp_success', 0)} fallback_success={state.get('fallback_success', 0)}"
            )
            next_heartbeat = now + heartbeat_seconds
        if state.get("valid"):
            _diag_log(
                "wait success "
                f"elapsed={time.perf_counter() - started:.1f}s products={state.get('products', 0)} "
                f"page={state.get('pagination_page', 0)} next_len={state.get('next_data_length', 0)}"
            )
            return {"success": True, "html": last_html, "state": state, "error": ""}
        last_error = state.get("error") or snapshot.get("error") or last_error
        if (
            state.get("blocked")
            or state.get("too_large")
            or state.get("terminal_redirect")
        ):
            _diag_log(f"wait break error={_short_text(last_error)}")
            break
        time.sleep(poll_seconds)
    _diag_log(f"wait end error={_short_text(last_error)}")
    return {"success": False, "html": last_html, "state": last_state, "error": last_error or "browser_html_search_payload_failed"}


def _read_search_next_data_snapshot(page):
    return _read_next_data_snapshot(page)


def _read_next_data_snapshot(page):
    cdp_payload, cdp_source, cdp_error = _read_search_next_data_with_cdp(page)
    cdp_has_next_data = _payload_has_next_data(cdp_payload)
    cdp_has_payload = bool(cdp_payload)
    fallback_used = not cdp_has_next_data
    payload = cdp_payload
    source = cdp_source
    error = cdp_error
    fallback_source = ""
    fallback_error = ""
    fallback_has_next_data = False
    fallback_has_payload = False
    if fallback_used:
        fallback_payload, fallback_source, fallback_error = _read_search_next_data_with_run_js(page)
        fallback_has_payload = bool(fallback_payload)
        fallback_has_next_data = _payload_has_next_data(fallback_payload)
        payload = fallback_payload
        source = fallback_source
        error = fallback_error
    if not payload:
        payload = {"error": error}
        source = source or "script_text_missing"

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
        "source": source if next_data.strip() else f"{source}_missing",
        "cdp_source": cdp_source,
        "cdp_success": int(cdp_has_next_data),
        "cdp_empty": int(cdp_has_payload and not cdp_has_next_data),
        "cdp_error": cdp_error,
        "fallback_used": int(fallback_used),
        "fallback_source": fallback_source,
        "fallback_success": int(fallback_has_next_data),
        "fallback_empty": int(fallback_has_payload and not fallback_has_next_data),
        "fallback_error": fallback_error,
        "error": str(payload.get("error", "") or error or "") if isinstance(payload, dict) else str(error or ""),
    }


def _payload_has_next_data(payload):
    if not isinstance(payload, dict):
        return False
    next_data = payload.get("nextData")
    return isinstance(next_data, str) and bool(next_data.strip())


def _next_data_reader_script():
    return """
(() => {
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


def _read_search_next_data_with_cdp(page):
    verbose = os.getenv("SEDA_MAGALU_SEARCH_DIAG_VERBOSE", "0").lower() in {"1", "true", "yes", "y"}
    if verbose:
        _diag_log("cdp read start")
    try:
        result = page.run_cdp(
            "Runtime.evaluate",
            expression=_next_data_reader_script(),
            returnByValue=True,
            awaitPromise=False,
        )
        raw = ((result or {}).get("result") or {}).get("value") or ""
        payload = json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})
        if verbose:
            _diag_log(f"cdp read end next_len={len(str(payload.get('nextData') or ''))} error={_short_text(payload.get('error', ''))}")
        return payload, "script_text_cdp", str(payload.get("error", "") or "")
    except Exception as exc:
        if verbose:
            _diag_log(f"cdp read error {type(exc).__name__}: {_short_text(exc)}")
        return {}, "script_text_cdp", f"{type(exc).__name__}: {exc}"


def _read_search_next_data_with_run_js(page):
    verbose = os.getenv("SEDA_MAGALU_SEARCH_DIAG_VERBOSE", "0").lower() in {"1", "true", "yes", "y"}
    if verbose:
        _diag_log("js read start")
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
        if verbose:
            _diag_log(f"js read end next_len={len(str(payload.get('nextData') or ''))} error={_short_text(payload.get('error', ''))}")
        return payload, "script_text_js", str(payload.get("error", "") or "")
    except Exception as exc:
        if verbose:
            _diag_log(f"js read error {type(exc).__name__}: {_short_text(exc)}")
        return {}, "script_text_js", f"{type(exc).__name__}: {exc}"


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
        "selected_sort_type": "",
        "selected_sort_orientation": "",
        "blocked": False,
        "too_large": False,
        "terminal_redirect": False,
    }
    if _is_magalu_search_login_redirect(actual_url, browser_text):
        state["terminal_redirect"] = True
        state["error"] = "browser_html_search_login_redirect"
        return state
    data = extract_next_data(html)
    if not data:
        if _is_bad_browser_state(actual_url, browser_text):
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
        if _is_bad_browser_state(actual_url, browser_text):
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
    selected_sort = _selected_search_sort(search)
    state["selected_sort_type"] = selected_sort.get("type", "")
    state["selected_sort_orientation"] = selected_sort.get("orientation", "")
    requested_page = _requested_search_page(expected_url)
    if payload_page != requested_page:
        state["error"] = f"browser_html_page_mismatch:{payload_page}!={requested_page}"
        return state
    expected_type, expected_orientation = _expected_search_sort(expected_url)
    if state["selected_sort_type"] != expected_type or state["selected_sort_orientation"] != expected_orientation:
        state["error"] = (
            "browser_html_sort_mismatch:"
            f"{state['selected_sort_type'] or 'missing'}:{state['selected_sort_orientation'] or 'missing'}"
            f"!={expected_type}:{expected_orientation}"
        )
        return state
    max_products = _env_int("SEDA_MAGALU_SEARCH_BROWSER_MAX_PRODUCTS", 120)
    if max_products > 0 and (len(products) > max_products or payload_size > max_products):
        state["too_large"] = True
        state["error"] = f"browser_html_too_many_products:{max(len(products), payload_size)}"
        return state
    state["valid"] = True
    return state


def _trigger_search_navigation(page, url, refresh=False):
    current_url = ""
    try:
        current_url = str(page.url or "")
    except Exception:
        pass
    if refresh and _same_magalu_search_request(current_url, url):
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


def _expected_search_sort(url):
    query = parse_qs(urlparse(str(url or "")).query)
    sort_type = (query.get("sortType") or ["score"])[0] or "score"
    orientation = (query.get("sortOrientation") or ["desc"])[0] or "desc"
    return str(sort_type).strip(), str(orientation).strip()


def _selected_search_sort(search):
    sorts = search.get("sorts") if isinstance(search, dict) else []
    if not isinstance(sorts, list):
        return {}
    for item in sorts:
        if isinstance(item, dict) and item.get("selected"):
            return {
                "type": str(item.get("type") or "").strip(),
                "orientation": str(item.get("orientation") or "").strip(),
            }
    return {}


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


def graphql_post(payload, timeout=None, context_url=None):
    page = _page_for_use("graphql_post")
    timeout = int(timeout) if timeout is not None else _env_int("SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT", 60)
    # The federation GraphQL fetch only needs a warm magalu origin (cookies/CORS), not
    # the specific product page. Reuse the current warm page instead of navigating to
    # context_url per product (each nav costs a page load + warmup sleep). context_url
    # is kept only as a recovery target for retries after a blocked/failed attempt.
    reuse_warm_page = os.getenv("SEDA_MAGALU_GRAPHQL_REUSE_PAGE", "1").lower() not in {"0", "false", "no", "n"}
    initial_context = None if reuse_warm_page else context_url
    try:
        page = _prepare_js_page(page, "graphql_post_warmup", context_url=initial_context, allow_default_warmup=True)
    except Exception:
        _restart_page("graphql_post_prepare_failed")
        page = _page_for_use("graphql_post_prepare_retry")
        page = _prepare_js_page(page, "graphql_post_warmup", context_url=None, allow_default_warmup=True)

    operation = payload.get("operationName") or ""
    payload_text = json.dumps(payload, ensure_ascii=False)
    configured_attempts = _env_int("SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS", 2)
    # Showcase gets one *conditional* bonus slot.  It is reachable only when
    # two consecutive Failed-to-fetch results exhaust the configured budget
    # and a clean-browser verification is still required.
    loop_attempts = configured_attempts
    if operation == "showcaseQuery" and configured_attempts >= 2:
        loop_attempts += 1
    script = """
return (async () => {
  try {
    const payload = JSON.parse(arguments[0]);
    const operation = payload.operationName || '';
    const response = await fetch(
      'https://federation.magazineluiza.com.br/graphql?operationName=' + encodeURIComponent(operation),
      {
        method: 'POST',
        mode: 'cors',
        cache: 'no-cache',
        credentials: 'include',
        headers: {
          'accept': 'application/json',
          'content-type': 'application/json',
          'x-channel-id': '45',
          'x-channel-name': 'mixer-desk.magazineluiza.com.br'
        },
        body: JSON.stringify(payload)
      }
    );
    return JSON.stringify({
      status: response.status,
      contentType: response.headers.get('content-type') || '',
      text: await response.text()
    });
  } catch (error) {
    return JSON.stringify({status: 0, error: String(error), text: ''});
  }
})()
"""
    last = {}
    attempt_trace = []
    showcase_fetch_failure_streak = 0
    showcase_restart_attempted = False
    for attempt in range(1, loop_attempts + 1):
        try:
            raw_result = page.run_js(script, payload_text, timeout=timeout) or "{}"
        except Exception as exc:
            if _trace_oom([], attempt, page) or _is_dead_page_error(exc):
                # dead/crashed tab: fully relaunch and re-warm so the next attempt
                # (and the next product) runs on a live page instead of hanging.
                _restart_page("graphql_post_dead_page")
                page = _page_for_use("graphql_post_retry")
                try:
                    page = _prepare_js_page(page, "graphql_post_warmup", context_url=context_url, allow_default_warmup=True)
                except Exception:
                    pass
            result = {"status": 0, "error": f"{type(exc).__name__}: {exc}", "text": ""}
        else:
            try:
                result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            except ValueError:
                result = {"status": 0, "error": "invalid_js_result", "text": str(raw_result)}
        if not isinstance(result, dict):
            result = {
                "status": 0,
                "error": "invalid_js_result",
                "text": str(raw_result),
            }
        text = result.get("text") or ""
        data = {}
        error = result.get("error") or ""
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                error = error or "invalid_json"
        status_code = result.get("status") or 0
        content_type = result.get("contentType") or ""
        semantic_error = ""
        terminal_business_error = ""
        if not error and status_code == 200:
            terminal_business_error = graphql_terminal_business_error(
                operation,
                data,
            )
            semantic_error = _graphql_semantic_error(operation, data)
            error = semantic_error or error
        last = {
            "status_code": status_code,
            "content_type": content_type,
            "text": text,
            "data": data,
            "error": error,
            "operation": operation,
            "attempt": attempt,
            "terminal_business_error": terminal_business_error,
        }
        blocked = _graphql_result_blocked(last)
        if blocked:
            error = "blocked_response"
        elif status_code != 200 and not error:
            error = f"http_status_{status_code}"
        last["error"] = error
        graphql_errors = data.get("errors") if isinstance(data, dict) else None
        item_present = ""
        if operation == "itemQuery":
            response_data = data.get("data") if isinstance(data, dict) else {}
            item_present = int(
                isinstance(response_data, dict) and bool(response_data.get("item"))
            )
        trace_item = {
            "operation": operation,
            "attempt": attempt,
            "method": "browser_graphql",
            "status_code": status_code,
            "content_type": content_type,
            "length": len(text),
            "error": error,
            "item_present": item_present,
        }
        if terminal_business_error:
            trace_item["terminal_business_error"] = terminal_business_error
        if graphql_errors:
            trace_item["graphql_errors"] = graphql_errors
        if error and text:
            trace_item["response_preview"] = text[:500]
        showcase_failed_fetch = (
            operation == "showcaseQuery" and _is_failed_to_fetch_error(error)
        )
        if showcase_failed_fetch:
            showcase_fetch_failure_streak += 1
        else:
            showcase_fetch_failure_streak = 0
        showcase_circuit_open = (
            showcase_restart_attempted and showcase_failed_fetch
        )
        if showcase_circuit_open:
            trace_item["showcase_failed_fetch_circuit_open"] = True
            last["showcase_failed_fetch_circuit_open"] = True
        attempt_trace.append(trace_item)
        last["trace"] = attempt_trace[:]
        last["graphql_errors"] = graphql_errors or []
        last["item_present"] = item_present
        if error:
            # Invalid/failed envelopes remain available as raw text and trace
            # previews, but must never reach operation-specific consumers.
            last["data"] = {}
        if terminal_business_error or showcase_circuit_open:
            return last
        if not blocked and status_code == 200 and not error:
            return last
        should_restart_showcase = (
            operation == "showcaseQuery"
            and showcase_fetch_failure_streak >= 2
            and not showcase_restart_attempted
        )
        if should_restart_showcase:
            # A JS fetch failure is returned as a status=0 result, not a Python
            # exception, so the normal dead-page recovery cannot see it.  Restart
            # once and spend one bounded attempt on a clean browser.  If the
            # configured budget ended here, loop_attempts contains exactly one
            # conditional bonus slot for this verification.
            showcase_restart_attempted = True
            trace_item["recovery"] = "browser_restart_after_failed_fetch"
            try:
                _restart_page("graphql_post_showcase_failed_fetch")
                page = _page_for_use("graphql_post_showcase_retry")
                page = _prepare_js_page(
                    page,
                    "graphql_post_warmup",
                    context_url=context_url,
                    allow_default_warmup=True,
                )
            except Exception as exc:
                trace_item["recovery_error"] = f"{type(exc).__name__}: {exc}"
                trace_item["showcase_failed_fetch_circuit_open"] = True
                last["showcase_failed_fetch_circuit_open"] = True
                last["trace"] = attempt_trace[:]
                return last
            last["trace"] = attempt_trace[:]
            continue
        if attempt >= configured_attempts:
            return last
        # recovery: on a blocked/failed attempt, escalate to navigating to the
        # product page (context_url) before retrying.
        try:
            page = _prepare_js_page(page, "graphql_post_warmup", context_url=context_url, allow_default_warmup=True)
        except Exception:
            _restart_page("graphql_post_retry_prepare_failed")
            page = _page_for_use("graphql_post_retry")
    return last


def _graphql_semantic_error(operation, data):
    return graphql_envelope_error(data, require_item=operation == "itemQuery")


def _is_failed_to_fetch_error(error):
    normalized = re.sub(r"\s+", " ", str(error or "").strip()).casefold()
    return normalized == "typeerror: failed to fetch"


def _graphql_result_blocked(result):
    status = int(result.get("status_code") or 0)
    if status in {401, 403, 429}:
        return True

    payload = result.get("data")
    if isinstance(payload, dict) and ("data" in payload or "errors" in payload):
        errors = payload.get("errors")
        return bool(
            isinstance(errors, list)
            and any(_graphql_error_is_blocked(error) for error in errors)
        )

    # Normal GraphQL data can contain arbitrary customer review text. Only
    # inspect explicit top-level error/message values for a decoded non-envelope
    # JSON response. Never scan arbitrary JSON values or normal GraphQL data.
    if result.get("error") != "invalid_json" and "json" in str(
        result.get("content_type") or ""
    ).casefold():
        return _top_level_json_error_is_blocked(payload)
    text = _ascii_lower(result.get("text") or "")
    return any(
        marker in text
        for marker in (
            "akamai",
            "captcha",
            "access denied",
            "erro 403",
            "too many requests",
        )
    )


def _top_level_json_error_is_blocked(payload):
    if not isinstance(payload, dict):
        return False
    values = []
    for key in ("error", "message"):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = _ascii_lower(value).strip()
            if text:
                values.append(text)
    if not values:
        return False
    exact_codes = {
        "401",
        "403",
        "429",
        "forbidden",
        "unauthenticated",
        "unauthorized",
        "rate_limited",
        "too_many_requests",
    }
    strong_markers = (
        "akamai",
        "captcha",
        "access denied",
        "forbidden",
        "unauthorized",
        "unauthenticated",
        "rate limit",
        "too many requests",
        "erro 403",
        "http 403",
    )
    return any(
        text in exact_codes or any(marker in text for marker in strong_markers)
        for text in values
    )


def _graphql_error_is_blocked(error):
    if not isinstance(error, dict):
        return False
    extensions = error.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    codes = {
        str(extensions.get(key) or "").strip().casefold()
        for key in ("code", "status", "statusCode", "errorType")
    }
    codes.discard("")
    if codes.intersection(
        {
            "401",
            "403",
            "429",
            "forbidden",
            "unauthenticated",
            "unauthorized",
            "rate_limited",
            "too_many_requests",
        }
    ):
        return True
    message = _ascii_lower(error.get("message") or "")
    return any(
        marker in message
        for marker in (
            "akamai",
            "captcha",
            "access denied",
            "forbidden",
            "unauthorized",
            "unauthenticated",
            "rate limit",
            "too many requests",
            "erro 403",
        )
    )


def graphql_post_raw(payload, timeout=None, endpoint=None):
    page = _page_for_use("graphql_post_raw")
    timeout = int(timeout) if timeout is not None else _env_int("SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT", 60)
    page = _prepare_js_page(page, "graphql_post_raw_warmup", allow_default_warmup=False)

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
    if not isinstance(result, dict):
        result = {
            "status": 0,
            "error": "invalid_js_result",
            "text": str(raw_result),
        }
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
    trace = []
    current_url, _ = _page_state(page)
    nav_first = os.getenv("SEDA_MAGALU_HTML_NAV_FIRST", "1").lower() not in {"0", "false", "no", "n"}
    if nav_first or not _same_page_path(current_url, url):
        nav_result = _navigate_html_page(page, url, timeout, "browser_navigation_context")
        trace.extend(nav_result.get("trace") or [])
        if nav_result.get("text") and "__NEXT_DATA__" in nav_result.get("text", ""):
            return nav_result
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
        if attempt < attempts:
            time.sleep(_env_float("SEDA_MAGALU_BROWSER_HTML_RETRY_SLEEP_SECONDS", 0.5))

    if os.getenv("SEDA_MAGALU_PDP_NAV_FALLBACK", "0").lower() not in {"0", "false", "no", "n"}:
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


def _navigate_html_page(page, url, timeout, method):
    trace = []
    navigation_error = ""
    try:
        nav_timeout = _env_float("SEDA_MAGALU_PDP_NAV_TIMEOUT", min(timeout, 30))
        page.get(url, timeout=nav_timeout)
    except Exception as exc:
        navigation_error = f"{type(exc).__name__}: {exc}"
        _stop_loading(page)
        trace.append(
            {
                "attempt": 1,
                "method": method,
                "status_code": 0,
                "length": 0,
                "error": navigation_error,
            }
        )

    ready_timeout = _env_float("SEDA_MAGALU_PDP_NEXTDATA_READY_TIMEOUT", max(5, min(timeout, 30)))
    poll_seconds = max(0.05, _env_float("SEDA_MAGALU_PDP_NEXTDATA_POLL_SECONDS", 0.25))
    deadline = time.perf_counter() + ready_timeout
    last_snapshot = {}
    last_error = navigation_error
    attempt = 1
    while time.perf_counter() <= deadline:
        snapshot = _read_next_data_snapshot(page)
        last_snapshot = snapshot
        text = snapshot.get("html", "")
        has_next_data = "__NEXT_DATA__" in text
        browser_text = snapshot.get("browser_text", "")
        actual_url = snapshot.get("url", "") or getattr(page, "url", "")
        blocked = _is_bad_browser_state(actual_url, f"{browser_text}\n{text}")
        trace_item = {
            "attempt": attempt,
            "method": method,
            "status_code": 200 if text else 0,
            "length": len(text),
            "has_next_data": has_next_data,
            "url": actual_url,
            "source": snapshot.get("source", ""),
            "ready_state": snapshot.get("ready_state", ""),
            "next_data_length": snapshot.get("next_data_length", 0),
            "cdp_success": snapshot.get("cdp_success", 0),
            "fallback_used": snapshot.get("fallback_used", 0),
            "fallback_success": snapshot.get("fallback_success", 0),
            "error": snapshot.get("error", "") or last_error,
        }
        trace.append(trace_item)
        if has_next_data and not blocked:
            _stop_loading(page)
            return {"status_code": 200, "text": text, "error": "", "trace": trace}
        if blocked:
            last_error = "browser_html_blocked_or_error_page"
            break
        last_error = snapshot.get("error", "") or "navigation_missing_next_data"
        attempt += 1
        time.sleep(poll_seconds)
    _stop_loading(page)
    text = last_snapshot.get("html", "") if isinstance(last_snapshot, dict) else ""
    return {
        "status_code": 200 if text else 0,
        "text": text,
        "error": last_error or "navigation_missing_next_data",
        "trace": trace,
    }


def _same_page_path(left, right):
    try:
        left_parsed = urlparse(str(left or ""))
        right_parsed = urlparse(str(right or ""))
    except Exception:
        return False
    if not left_parsed.netloc or not right_parsed.netloc:
        return False
    return (
        left_parsed.netloc.lower() == right_parsed.netloc.lower()
        and left_parsed.path.rstrip("/") == right_parsed.path.rstrip("/")
    )


def _same_magalu_search_request(left, right):
    if not _same_page_path(left, right):
        return False
    return (
        _requested_search_page(left) == _requested_search_page(right)
        and _expected_search_sort(left) == _expected_search_sort(right)
    )


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
