import html
import json
import re
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests


def main():
    html_text = Path("references/magalu_ai_summary_response.html").read_text(encoding="utf-8", errors="ignore")
    next_data = _next_data(html_text)
    query = next_data.get("query") or {}
    build_id = next_data.get("buildId", "")
    path0 = query.get("path0", "")
    path2 = query.get("path2", "")
    path3 = query.get("path3", "")
    path4 = query.get("path4", "")
    seller_id = query.get("seller_id", "magazineluiza")
    candidates = [
        f"https://www.magazineluiza.com.br/_next/data/{build_id}/{path0}/p/{path2}/{path3}/{path4}.json",
        f"https://www.magazineluiza.com.br/mixer/_next/data/{build_id}/{path0}/p/{path2}/{path3}/{path4}.json",
        f"https://m.magazineluiza.com.br/mixer-web/static/v1.194.0/_next/data/{build_id}/{path0}/p/{path2}/{path3}/{path4}.json",
        f"https://www.magazineluiza.com.br/mixer-web/static/v1.194.0/_next/data/{build_id}/{path0}/p/{path2}/{path3}/{path4}.json",
    ]
    params = {
        "seller_id": seller_id,
        "path0": path0,
        "path2": path2,
        "path3": path3,
        "path4": path4,
    }
    cookie = _cookie_from_curl_text()
    for url in candidates:
        full_url = f"{url}?{urlencode(params)}"
        try:
            response = requests.get(full_url, headers=_headers(cookie), timeout=60)
        except Exception as exc:
            print(f"\nURL {full_url}\nstatus=0 error={type(exc).__name__}: {exc}")
            continue
        summary_len = 0
        if "application/json" in response.headers.get("content-type", ""):
            try:
                payload = response.json()
                summary = (
                    payload.get("pageProps", {})
                    .get("data", {})
                    .get("reviewSummaryQuery", {})
                    .get("summary", "")
                )
                summary_len = len(summary or "")
            except ValueError:
                pass
        print(
            f"\nURL {full_url}\nstatus={response.status_code} "
            f"content_type={response.headers.get('content-type','')} len={len(response.text or '')} "
            f"summary_len={summary_len} sample={_ascii((response.text or '')[:240])}"
        )


def _next_data(text):
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S | re.I)
    if not match:
        return {}
    return json.loads(html.unescape(match.group(1)))


def _headers(cookie=""):
    headers = {
        "accept": "application/json,text/plain,*/*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "referer": "https://www.magazineluiza.com.br/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
    }
    if cookie:
        headers["cookie"] = cookie
    return headers


def _cookie_from_curl_text():
    for path in [Path("references/magalu_ai_summary.txt"), Path("references/magalu_ai_reviews_all.txt")]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r'-b \^"([^"]*)\^"', text, re.S)
        if match:
            return _decode_windows_curl(match.group(1))
    return ""


def _decode_windows_curl(value):
    return (
        value.replace("^&", "&")
        .replace("^%", "%")
        .replace("^#", "#")
        .replace('^"', '"')
        .replace("^^", "^")
    )


def _ascii(value):
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


if __name__ == "__main__":
    main()
