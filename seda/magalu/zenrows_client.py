import html
import json
import os
from dataclasses import dataclass, field

import requests


ZENROWS_API_URL = "https://api.zenrows.com/v1/"


PROFILE_PARAMS = {
    # Plain API request. Cheapest baseline; expected to fail on protected Magalu pages but useful for comparison.
    "basic_html": {},
    # Cheapest adaptive probe for listing/detail HTML. ZenRows decides whether JS/proxy is needed.
    "auto_html": {
        "mode": "auto",
        "proxy_country": "br",
    },
    # Adaptive mode plus browser-like headers. Useful when the site expects referer/language context.
    "auto_custom_headers": {
        "mode": "auto",
        "proxy_country": "br",
        "custom_headers": "true",
    },
    # Browser rendering without residential proxy. Confirms whether JavaScript alone is enough.
    "js_html": {
        "js_render": "true",
        "wait": "5000",
        "block_resources": "image,media,font,stylesheet",
    },
    # Brazil residential IP, no browser rendering. Good first attempt for SSR __NEXT_DATA__.
    "premium_html": {
        "premium_proxy": "true",
        "proxy_country": "br",
    },
    # Protected-page baseline: browser rendering plus Brazil residential proxy.
    "js_premium_html": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "wait": "5000",
        "block_resources": "image,media,font,stylesheet",
    },
    # Same as protected baseline but asks ZenRows to expose original target status for diagnostics.
    "js_premium_original_status": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "wait": "5000",
        "original_status": "true",
        "block_resources": "image,media,font,stylesheet",
    },
    # JSON response without premium proxy. Lower-cost XHR check when IP reputation is not the issue.
    "json_response_light": {
        "js_render": "true",
        "json_response": "true",
        "wait": "3000",
        "block_resources": "image,media,font,stylesheet",
    },
    # Browser-rendered full PDP HTML. Use for diagnosing AI summary / __NEXT_DATA__ availability.
    "pdp_js_full": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "wait": "8000",
        "block_resources": "image,media,font,stylesheet",
    },
    # Extract only the Next.js data script. Lower response size than full PDP HTML.
    "pdp_next_data": {
        "premium_proxy": "true",
        "proxy_country": "br",
        "css_extractor": json.dumps({"next_data": "script#__NEXT_DATA__"}, separators=(",", ":")),
    },
    # Browser-rendered full listing HTML. Use for diagnosing whether Magalu renders Next data or a challenge page.
    "listing_js_full": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "wait": "8000",
        "block_resources": "image,media,font,stylesheet",
    },
    # Browser-rendered listing extraction with fixed wait. Faster failure than wait_for on protected pages.
    "listing_next_data_js_wait": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "wait": "8000",
        "block_resources": "image,media,font,stylesheet",
        "css_extractor": json.dumps({"next_data": "script#__NEXT_DATA__"}, separators=(",", ":")),
    },
    # Browser-rendered listing extraction. Use when HTML-only listing returns an Akamai/challenge page.
    "listing_next_data_js": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "wait_for": "script#__NEXT_DATA__",
        "block_resources": "image,media,font,stylesheet",
        "css_extractor": json.dumps({"next_data": "script#__NEXT_DATA__"}, separators=(",", ":")),
    },
    # Browser-rendered fallback for PDP pages when premium-only SSR is blocked/incomplete.
    "pdp_next_data_js": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "wait_for": "script#__NEXT_DATA__",
        "block_resources": "image,media,font,stylesheet",
        "css_extractor": json.dumps({"next_data": "script#__NEXT_DATA__"}, separators=(",", ":")),
    },
    # Expensive discovery mode. Use only for 1-2 reference captures.
    "xhr_discovery": {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "br",
        "json_response": "true",
        "wait": "3000",
        "block_resources": "image,media,font,stylesheet",
    },
}


@dataclass
class ZenRowsResult:
    success: bool
    url: str
    profile: str
    status_code: int = 0
    text: str = ""
    error: str = ""
    params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    estimated_multiplier: str = ""


def enabled():
    return os.getenv("SEDA_ALLOW_ZENROWS", "0").lower() in {"1", "true", "yes", "y"}


def dry_run():
    return os.getenv("SEDA_ZENROWS_DRY_RUN", "1").lower() not in {"0", "false", "no", "n"}


def api_key():
    return os.getenv("ZENROWS_API_KEY", "").strip()


def build_params(profile, extra=None):
    profile = profile or os.getenv("SEDA_ZENROWS_PROFILE", "auto_html")
    params = dict(PROFILE_PARAMS.get(profile, PROFILE_PARAMS["auto_html"]))
    proxy_country = os.getenv("SEDA_ZENROWS_PROXY_COUNTRY", params.get("proxy_country", "br"))
    if _supports_proxy_country(params):
        params["proxy_country"] = proxy_country
    session_id = os.getenv("SEDA_ZENROWS_SESSION_ID", "").strip()
    if session_id:
        params["session_id"] = session_id
    if os.getenv("SEDA_ZENROWS_CUSTOM_HEADERS", "0").lower() in {"1", "true", "yes", "y"}:
        params["custom_headers"] = "true"
    if extra:
        params.update({key: value for key, value in extra.items() if value not in (None, "")})
    return params


def _supports_proxy_country(params):
    if str(params.get("mode", "")).lower() == "auto":
        return True
    return str(params.get("premium_proxy", "")).lower() == "true"


def estimated_multiplier(params):
    if params.get("mode") == "auto":
        return "auto:1x/5x/10x/25x_success_only"
    js = str(params.get("js_render", "")).lower() == "true"
    premium = str(params.get("premium_proxy", "")).lower() == "true"
    if js and premium:
        return "25x"
    if premium:
        return "10x"
    if js:
        return "5x"
    return "1x"


def request_url(url, profile=None, timeout=None, extra=None):
    profile = profile or os.getenv("SEDA_ZENROWS_PROFILE", "auto_html")
    params = build_params(profile, extra=extra)
    params["url"] = url
    multiplier = estimated_multiplier(params)
    public_params = {key: value for key, value in params.items() if key != "apikey"}

    if not enabled():
        return ZenRowsResult(False, url, profile, error="zenrows_disabled", params=public_params, estimated_multiplier=multiplier)
    if dry_run():
        return ZenRowsResult(False, url, profile, error="zenrows_dry_run", params=public_params, estimated_multiplier=multiplier)
    key = api_key()
    if not key:
        return ZenRowsResult(False, url, profile, error="ZENROWS_API_KEY is not set", params=public_params, estimated_multiplier=multiplier)

    params["apikey"] = key
    timeout = int(timeout or os.getenv("SEDA_ZENROWS_TIMEOUT", os.getenv("ZENROWS_TIMEOUT", "180")))
    headers = {}
    if params.get("custom_headers") == "true":
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "referer": "https://www.magazineluiza.com.br/",
        }
    try:
        response = requests.get(ZENROWS_API_URL, params=params, headers=headers, timeout=timeout)
    except Exception as exc:
        return ZenRowsResult(False, url, profile, error=f"{type(exc).__name__}: {exc}", params=public_params, estimated_multiplier=multiplier)

    tracked_headers = {
        key: response.headers.get(key, "")
        for key in ("X-Request-Cost", "Concurrency-Limit", "Concurrency-Remaining", "X-Request-Id")
        if response.headers.get(key, "")
    }
    return ZenRowsResult(
        success=response.ok and bool(response.text),
        url=url,
        profile=profile,
        status_code=response.status_code,
        text=response.text or "",
        error="" if response.ok else f"http_{response.status_code}",
        params=public_params,
        headers=tracked_headers,
        estimated_multiplier=multiplier,
    )


def fetch_html(url, profile=None, timeout=None):
    return request_url(url, profile=profile or os.getenv("SEDA_ZENROWS_HTML_PROFILE", "auto_html"), timeout=timeout)


def fetch_next_data_html(url, profile=None, timeout=None):
    explicit_profile = profile is not None
    profiles = [profile] if explicit_profile else [os.getenv("SEDA_ZENROWS_PDP_PROFILE", "pdp_next_data")]
    if not explicit_profile and os.getenv("SEDA_ZENROWS_PDP_ESCALATE_JS", "1").lower() not in {"0", "false", "no", "n"}:
        if "pdp_next_data_js" not in profiles:
            profiles.append("pdp_next_data_js")

    last = None
    for current_profile in profiles:
        result = request_url(url, profile=current_profile, timeout=timeout)
        last = result
        html_text = _result_to_next_data_html(result.text)
        if html_text:
            result.text = html_text
            result.success = True
            return result
        if result.error in {"zenrows_disabled", "zenrows_dry_run", "ZENROWS_API_KEY is not set"}:
            return result
    return last or ZenRowsResult(False, url, profile or "pdp_next_data", error="not_attempted")


def _result_to_next_data_html(text):
    if not text:
        return ""
    if "__NEXT_DATA__" in text:
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        return ""
    next_data = parsed.get("next_data") if isinstance(parsed, dict) else ""
    if isinstance(next_data, list):
        next_data = next((item for item in next_data if item), "")
    if not isinstance(next_data, str) or not next_data.strip():
        return ""
    return '<script id="__NEXT_DATA__" type="application/json">' + html.escape(next_data, quote=False) + "</script>"





