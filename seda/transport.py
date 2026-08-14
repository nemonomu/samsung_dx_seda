import os
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import requests


_MAGALU_LISTING_DISABLED_ZENROWS_PROFILES = set()
_MAGALU_LISTING_TRANSIENT_REQUEST_ERRORS = {
    "request_error:ConnectionError",
    "request_error:Timeout",
}


def _reset_magalu_listing_recovery_state():
    """Reset process-local listing recovery state (primarily for tests)."""
    _MAGALU_LISTING_DISABLED_ZENROWS_PROFILES.clear()


@dataclass
class FetchResult:
    url: str
    text: str
    status_code: int = 0
    method: str = ""
    error: str = ""
    attempts: list = field(default_factory=list)


def _headers():
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
    }


def fetch_url(url, mode=None, timeout=None):
    mode = (mode or os.getenv("SEDA_FETCH_MODE", "uc_first")).strip().lower()
    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", os.getenv("ZENROWS_TIMEOUT", "180")))
    attempts = fetch_attempts(mode)
    trace = []
    last = FetchResult(url=url, text="", method=mode, error="not attempted")
    for attempt in attempts:
        if attempt == "browser":
            result = _fetch_browser(url, timeout)
        elif attempt == "uc":
            result = _fetch_uc(url, timeout)
        elif attempt == "graphql":
            result = _fetch_graphql(url, timeout)
        elif attempt == "zenrows":
            result = _fetch_zenrows(url, timeout)
        else:
            result = _fetch_requests(url, timeout)
        blocked = is_blocked_html(result.text, result.status_code)
        if result.text and blocked:
            result.error = result.error or "blocked_html"
        trace_item = {
            "method": result.method,
            "status_code": result.status_code,
            "length": len(result.text or ""),
            "blocked": blocked,
            "error": result.error,
        }
        if result.attempts:
            trace_item["inner_attempts"] = result.attempts
        trace.append(trace_item)
        result.attempts = trace[:]
        listing_error = bool(result.error) and _is_magalu_listing_url(url)
        if (
            result.text
            and len(result.text) > 500
            and not blocked
            and not listing_error
        ):
            return result
        last = result
        time.sleep(float(os.getenv("SEDA_RETRY_SLEEP_SECONDS", "1")))
    last.attempts = trace
    return last


def fetch_attempts(mode):
    if mode == "magalu_browser_first":
        attempts = ["browser", "graphql", "uc", "requests", "zenrows"]
    elif mode == "magalu_browser_zenrows":
        attempts = ["browser", "zenrows"]
    elif mode == "magalu_listing_graphql_zenrows":
        # Listing-only contract: ``graphql`` already performs native direct
        # GraphQL and then browser-context GraphQL. ZenRows remains the final
        # structured/HTML recovery ladder for that page.
        attempts = ["graphql", "zenrows"]
    elif mode in {"auto", "uc_first"} or mode.endswith("_uc_first"):
        attempts = ["uc", "graphql", "requests", "zenrows"]
    elif mode == "magalu_graphql_first":
        attempts = ["graphql", "browser"]
    elif mode == "graphql_first" or mode.endswith("_graphql_first"):
        attempts = ["graphql", "uc", "requests", "zenrows"]
    elif mode == "requests_first" or mode.endswith("_requests_first"):
        attempts = ["requests", "uc", "graphql", "zenrows"]
    elif mode == "zenrows_first" or mode.endswith("_zenrows_first"):
        attempts = ["zenrows", "uc", "graphql", "requests"]
    else:
        attempts = [mode]
    if os.getenv("SEDA_ALLOW_ZENROWS", "0").lower() not in {"1", "true", "yes", "y"}:
        attempts = [attempt for attempt in attempts if attempt != "zenrows"]
    return attempts


def _fetch_browser(url, timeout):
    if "magazineluiza.com.br" not in url:
        return FetchResult(url=url, text="", method="browser", error="browser_fetch_only_magalu")
    try:
        from .magalu.browser_session import fetch_page_html

        result = fetch_page_html(url)
    except Exception as exc:
        return FetchResult(url=url, text="", method="browser", error=f"{type(exc).__name__}: {exc}")
    if result.get("success"):
        return FetchResult(
            url=url,
            text=result.get("text", ""),
            status_code=200,
            method="browser",
            attempts=result.get("trace", []),
        )
    return FetchResult(
        url=url,
        text=result.get("text", ""),
        status_code=200 if result.get("text") else 0,
        method="browser",
        error=f"{result.get('error', 'browser_fetch_failed')}:{result.get('trace', [])}",
        attempts=result.get("trace", []),
    )


def is_blocked_html(text, status_code=0):
    return bool(blocked_html_reason(text, status_code))


def blocked_html_reason(text, status_code=0):
    haystack = _ascii_lower(text or "")
    if "__next_data__" in haystack and "pageprops" in haystack:
        return ""
    if status_code in {401, 403, 429}:
        return f"status_{status_code}"
    blocked_markers = [
        "akamai-bot",
        "customdeny",
        "error-code\">403",
        "ops! algo deu errado",
        "oops",
        "nao e possivel acessar a pagina",
        "alguma coisa deu errado",
        "erro 403",
        "access denied",
        "captcha",
        "bot detection",
        "robot",
    ]
    for marker in blocked_markers:
        if marker in haystack:
            return marker
    return ""


def _ascii_lower(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def _fetch_requests(url, timeout):
    try:
        response = requests.get(url, headers=_headers(), timeout=timeout)
        return FetchResult(url=url, text=response.text, status_code=response.status_code, method="requests")
    except Exception as exc:
        return FetchResult(url=url, text="", method="requests", error=f"{type(exc).__name__}: {exc}")


def _fetch_graphql(url, timeout):
    if "magazineluiza.com.br" in url and "/busca/" in url:
        try:
            from .magalu.search_api import fetch_search_listing

            result = fetch_search_listing(url, timeout=timeout)
        except Exception as exc:
            return FetchResult(url=url, text="", method="graphql", error=f"{type(exc).__name__}: {exc}")
        if result.get("text"):
            return FetchResult(
                url=url,
                text=result["text"],
                status_code=200,
                method=result.get("method") or "graphql",
                attempts=result.get("trace") or [],
            )
        return FetchResult(
            url=url,
            text="",
            method=result.get("method") or "graphql",
            error=f"{result.get('error', 'graphql_failed')}:{result.get('trace', [])}",
            attempts=result.get("trace") or [],
        )
    if "casasbahia.com.br" in url:
        try:
            from .casas_bahia.search_api import fetch_search_listing

            result = fetch_search_listing(url, timeout=timeout)
        except Exception as exc:
            return FetchResult(url=url, text="", method="api_partner", error=f"{type(exc).__name__}: {exc}")
        if result.get("text"):
            return FetchResult(url=url, text=result["text"], status_code=200, method="api_partner")
        return FetchResult(
            url=url,
            text="",
            method="api_partner",
            error=f"{result.get('error', 'api_partner_failed')}:{result.get('trace', [])}",
        )
    return FetchResult(
        url=url,
        text="",
        method="graphql",
        error=(
            "graphql_probe_requires_har_or_captured_payload:"
            " provide a browser HAR if UC page source does not expose listing data"
        ),
    )


def _fetch_zenrows(url, timeout):
    zenrows_timeout = int(os.getenv("SEDA_ZENROWS_TIMEOUT", os.getenv("ZENROWS_TIMEOUT", str(timeout))))
    is_magalu_listing = _is_magalu_listing_url(url)
    if is_magalu_listing:
        profile_hint = os.getenv("SEDA_ZENROWS_LISTING_PROFILE", os.getenv("SEDA_ZENROWS_PROFILE", "listing_js_full"))
    else:
        profile_hint = os.getenv("SEDA_ZENROWS_HTML_PROFILE", os.getenv("SEDA_ZENROWS_PROFILE", "auto_html"))
    profiles = _zenrows_attempt_profiles(url, profile_hint)
    if is_magalu_listing and _MAGALU_LISTING_DISABLED_ZENROWS_PROFILES:
        active_profiles = [
            profile
            for profile in profiles
            if profile not in _MAGALU_LISTING_DISABLED_ZENROWS_PROFILES
        ]
        if active_profiles:
            skipped_profiles = [
                profile
                for profile in profiles
                if profile in _MAGALU_LISTING_DISABLED_ZENROWS_PROFILES
            ]
            profiles = active_profiles
            print(
                "[seda] zenrows listing profiles skipped "
                f"profiles={','.join(skipped_profiles)} reason=semantic_invalid_circuit_open",
                flush=True,
            )
    last = None
    attempt_trace = []
    graphql_first = (
        is_magalu_listing
        and os.getenv(
            "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_FIRST",
            "0",
        ).lower()
        in {"1", "true", "yes", "y"}
    )
    attempt_offset = 0
    max_attempts = len(profiles) + int(graphql_first)
    if graphql_first:
        graphql_profile = _magalu_listing_graphql_profile()
        result = _fetch_zenrows_graphql_once(
            url,
            zenrows_timeout,
            1,
            max_attempts,
        )
        failure_reason, _ = _zenrows_failure_reason(url, result)
        _extend_zenrows_attempt_trace(
            attempt_trace,
            result,
            profile=graphql_profile,
            attempt=1,
            max_attempts=max_attempts,
            failure_reason=failure_reason,
        )
        if not failure_reason:
            result.attempts = attempt_trace
            return result
        last = result
        attempt_offset = 1
        if profiles:
            sleep_seconds = float(
                os.getenv(
                    "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS",
                    "2",
                )
            )
            print(
                "[seda] zenrows graphql fallback "
                f"failure_reason={failure_reason} "
                f"next_profile={profiles[0]} sleep={sleep_seconds}",
                flush=True,
            )
            time.sleep(sleep_seconds)

    for profile_index, profile in enumerate(profiles, start=1):
        attempt = profile_index + attempt_offset
        result = _fetch_zenrows_once(url, zenrows_timeout, profile, attempt, max_attempts)
        failure_reason, semantic_invalid = _zenrows_failure_reason(url, result)
        _extend_zenrows_attempt_trace(
            attempt_trace,
            result,
            profile=profile,
            attempt=attempt,
            max_attempts=max_attempts,
            failure_reason=failure_reason,
        )
        if not failure_reason:
            result.attempts = attempt_trace
            return result
        last = result

        has_later_profile = any(
            candidate != profile for candidate in profiles[profile_index:]
        )
        if (
            is_magalu_listing
            and profile == "premium_html"
            and semantic_invalid
            and has_later_profile
        ):
            _MAGALU_LISTING_DISABLED_ZENROWS_PROFILES.add(profile)
            print(
                "[seda] zenrows listing profile circuit opened "
                f"profile={profile} reason={failure_reason}",
                flush=True,
            )

        if (
            is_magalu_listing
            and profile == "listing_next_data_js_wait"
        ):
            if failure_reason in _MAGALU_LISTING_TRANSIENT_REQUEST_ERRORS:
                sleep_seconds = float(
                    os.getenv(
                        "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS",
                        "2",
                    )
                )
                print(
                    "[seda] zenrows fetch transient retry "
                    f"failure_reason={failure_reason} profile={profile} "
                    f"retry=1/1 sleep={sleep_seconds}",
                    flush=True,
                )
                time.sleep(sleep_seconds)
                result = _fetch_zenrows_once(
                    url,
                    zenrows_timeout,
                    profile,
                    attempt,
                    max_attempts,
                )
                failure_reason, _ = _zenrows_failure_reason(url, result)
                _extend_zenrows_attempt_trace(
                    attempt_trace,
                    result,
                    profile=profile,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    failure_reason=failure_reason,
                )
                if not failure_reason:
                    result.attempts = attempt_trace
                    return result
                last = result
            # The HTML listing ladder ends at this exact 25x profile. Ignore
            # stale extra profiles inherited from a parent process.
            last.attempts = attempt_trace
            return last

        if profile_index < len(profiles):
            sleep_seconds = float(os.getenv("SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS", "2"))
            print(
                f"[seda] zenrows fetch fallback failure_reason={failure_reason} "
                f"attempt={attempt}/{max_attempts} profile={profile} "
                f"next_profile={profiles[profile_index]} "
                f"sleep={sleep_seconds}",
                flush=True,
            )
            time.sleep(sleep_seconds)
    if last is not None:
        last.attempts = attempt_trace
        return last
    return FetchResult(
        url=url,
        text="",
        method="zenrows",
        error="zenrows_not_attempted",
        attempts=attempt_trace,
    )


def _fetch_zenrows_graphql_once(
    url,
    zenrows_timeout,
    attempt=1,
    max_attempts=1,
):
    profile = _magalu_listing_graphql_profile()
    print(
        "[seda] zenrows graphql fetch start "
        f"attempt={attempt}/{max_attempts} profile={profile}",
        flush=True,
    )
    try:
        from .magalu.search_api import fetch_search_listing_via_zenrows

        result = fetch_search_listing_via_zenrows(
            url,
            timeout=zenrows_timeout,
            profile=profile,
        )
    except Exception as exc:
        return FetchResult(
            url=url,
            text="",
            method="zenrows_graphql_search",
            error=f"zenrows_graphql_error:{type(exc).__name__}",
        )
    metadata = result.get("zenrows") if isinstance(result, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    trace = result.get("trace") if isinstance(result, dict) else []
    trace = trace if isinstance(trace, list) else []
    status_code = int(metadata.get("status_code") or 0)
    text = result.get("text", "") if isinstance(result, dict) else ""
    error = result.get("error", "") if isinstance(result, dict) else ""
    success = bool(result.get("success")) if isinstance(result, dict) else False
    print(
        "[seda] zenrows graphql fetch done "
        f"status={status_code} length={len(text or '')} "
        f"method=zenrows_graphql_search profile={metadata.get('profile', profile)} "
        f"cost={metadata.get('estimated_multiplier', '') or 'unknown'} "
        f"error={error or ''}",
        flush=True,
    )
    return FetchResult(
        url=url,
        text=text if success else "",
        status_code=status_code,
        method="zenrows_graphql_search",
        error="" if success else (error or "zenrows_graphql_failed"),
        attempts=trace,
    )


def _magalu_listing_graphql_profile():
    return (
        os.getenv("SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_PROFILE", "").strip()
        or "premium_html"
    )


def _extend_zenrows_attempt_trace(
    history,
    result,
    *,
    profile,
    attempt,
    max_attempts,
    failure_reason="",
):
    nested = result.attempts if isinstance(result.attempts, list) else []
    appended = False
    for raw in nested:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item.setdefault("method", result.method or "zenrows")
        item.setdefault("profile", profile)
        item.setdefault("status_code", result.status_code)
        item.setdefault("length", len(result.text or ""))
        item["attempt"] = attempt
        item["max_attempts"] = max_attempts
        item["request_number"] = len(history) + 1
        if failure_reason:
            item["error"] = failure_reason
        else:
            item.setdefault("error", result.error or "")
        history.append(item)
        appended = True
    if appended:
        return
    history.append(
        {
            "method": result.method or "zenrows",
            "profile": profile,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "request_number": len(history) + 1,
            "status_code": result.status_code,
            "length": len(result.text or ""),
            "error": failure_reason or result.error or "",
        }
    )


def _zenrows_failure_reason(url, result):
    blocked_reason = blocked_html_reason(result.text, result.status_code)
    if blocked_reason:
        failure_reason = f"blocked_html:{blocked_reason}"
        result.error = result.error or failure_reason
        return failure_reason, False

    if not _is_magalu_listing_url(url):
        return "", False

    if result.error:
        return result.error, False

    if result.status_code and not 200 <= result.status_code < 300:
        failure_reason = f"http_{result.status_code}"
        result.error = failure_reason
        return failure_reason, False

    payload_error = _magalu_listing_payload_error(url, result.text)
    if payload_error:
        failure_reason = f"invalid_listing_payload:{payload_error}"
        result.error = failure_reason
        return failure_reason, True

    return "", False


def _zenrows_attempt_profiles(url, profile_hint):
    if _is_magalu_listing_url(url):
        fallback_raw = os.getenv("SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES", "").strip()
        if fallback_raw:
            return _unique_profiles([profile_hint] + _split_profiles(fallback_raw))
        retries = int(os.getenv("SEDA_MAGALU_LISTING_ZENROWS_RETRIES", "0"))
    else:
        retries = int(os.getenv("SEDA_ZENROWS_RETRIES", "0"))
    return [profile_hint] * max(1, retries + 1)


def _split_profiles(raw):
    return [item.strip() for item in str(raw or "").replace(";", ",").split(",") if item.strip()]


def _unique_profiles(profiles):
    unique = []
    seen = set()
    for profile in profiles:
        if profile in seen:
            continue
        unique.append(profile)
        seen.add(profile)
    return unique or ["auto_html"]


def _fetch_zenrows_once(url, zenrows_timeout, profile_hint, attempt=1, max_attempts=1):
    print(
        f"[seda] zenrows fetch start attempt={attempt}/{max_attempts} profile={profile_hint}",
        flush=True,
    )
    try:
        if _is_magalu_listing_url(url):
            from .magalu.zenrows_client import fetch_listing_next_data_html

            result = fetch_listing_next_data_html(url, profile=profile_hint, timeout=zenrows_timeout)
            method_prefix = "zenrows_listing_next_data"
        else:
            from .magalu.zenrows_client import fetch_html

            result = fetch_html(url, profile=profile_hint, timeout=zenrows_timeout)
            method_prefix = "zenrows"
    except Exception as exc:
        return FetchResult(url=url, text="", method="zenrows", error=f"{type(exc).__name__}: {exc}")
    blocked_reason = blocked_html_reason(result.text, result.status_code)
    print(
        f"[seda] zenrows fetch done status={result.status_code} length={len(result.text or '')} "
        f"method=zenrows profile={result.profile} cost={result.estimated_multiplier} "
        f"blocked={blocked_reason or 0} error={result.error or ''}",
        flush=True,
    )
    if result.success:
        return FetchResult(url=url, text=result.text, status_code=result.status_code, method="zenrows")
    error = result.error or "zenrows_failed"
    if result.headers:
        error = f"{error}:{result.headers}"
    return FetchResult(url=url, text=result.text, status_code=result.status_code, method="zenrows", error=error)


def _is_magalu_listing_url(url):
    return "magazineluiza.com.br" in str(url or "") and "/busca/" in str(url or "")


def _magalu_listing_payload_error(url, text):
    if not _is_magalu_listing_url(url):
        return ""
    if not str(text or "").strip():
        return "empty_html"
    try:
        from .magalu.search_api import (
            MAGALU_LISTING_PAGE_SIZE,
            _strict_search_payload_error,
        )
        from .parsers import extract_next_data

        data = extract_next_data(text)
        props = data.get("props") if isinstance(data, dict) else {}
        page_props = props.get("pageProps") if isinstance(props, dict) else {}
        page_data = page_props.get("data") if isinstance(page_props, dict) else {}
        search = page_data.get("search") if isinstance(page_data, dict) else None
        return _strict_search_payload_error(
            url,
            search,
            MAGALU_LISTING_PAGE_SIZE,
        )
    except Exception as exc:
        return f"validator_error:{type(exc).__name__}"


def _fetch_uc(url, timeout):
    try:
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        if os.getenv("SEDA_UC_HEADLESS", "1").lower() in {"1", "true", "yes", "y"}:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        version_main = uc_version_main()
        driver = uc.Chrome(options=options, version_main=version_main)
        try:
            driver.set_page_load_timeout(timeout)
            driver.get(url)
            time.sleep(float(os.getenv("SEDA_UC_WAIT_SECONDS", "5")))
            text = driver.page_source
        finally:
            driver.quit()
        return FetchResult(url=url, text=text, status_code=200 if text else 0, method="uc")
    except Exception as exc:
        return FetchResult(url=url, text="", method="uc", error=f"{type(exc).__name__}: {exc}")


def uc_version_main():
    override = os.getenv("SEDA_UC_VERSION_MAIN", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            return None
    if os.getenv("SEDA_UC_AUTO_DETECT_VERSION", "0").lower() not in {"1", "true", "yes", "y"}:
        default = os.getenv("SEDA_UC_DEFAULT_VERSION_MAIN", "148").strip()
        try:
            return int(default)
        except ValueError:
            return None
    detected = detect_chrome_major()
    return detected


def detect_chrome_major():
    candidates = [
        os.getenv("SEDA_CHROME_PATH", "").strip(),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            output = subprocess.check_output([candidate, "--version"], text=True, stderr=subprocess.STDOUT, timeout=10)
        except Exception:
            continue
        match = re.search(r"(\d+)\.", output)
        if match:
            return int(match.group(1))
    return None

