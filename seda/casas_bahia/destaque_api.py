import os
import re
import time

import requests


DESTAQUE_URL = "https://api-destaque-descoberta.casasbahia.com.br/Destaque/Sku/{sku_id}/Lojista/{seller_id}"
DISCOUNT_RE = re.compile(r"(\d+(?:[,.]\d+)?)\s*%\s*de\s+desconto\b", re.I)


def fetch_discount_type(sku_id, seller_id, timeout=None):
    sku_id = str(sku_id or "").strip()
    seller_id = str(seller_id or "").strip()
    if not sku_id or not seller_id:
        return {"success": False, "discount_type": "", "error": "missing_sku_or_seller"}

    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    retries = int(os.getenv("SEDA_CASAS_BAHIA_DESTAQUE_RETRIES", "2"))
    sleep_seconds = float(os.getenv("SEDA_CASAS_BAHIA_DESTAQUE_RETRY_SLEEP_SECONDS", "1.0"))
    url = DESTAQUE_URL.format(sku_id=sku_id, seller_id=seller_id)
    trace = []
    session = requests.Session()
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(sleep_seconds * attempt)
        try:
            response = session.get(url, headers=_headers(), timeout=timeout)
        except Exception as exc:
            trace.append({"attempt": attempt + 1, "status_code": 0, "error": f"{type(exc).__name__}: {exc}"})
            continue
        trace.append({"attempt": attempt + 1, "status_code": response.status_code, "length": len(response.text or "")})
        if response.status_code != 200:
            continue
        try:
            data = response.json()
        except ValueError:
            trace[-1]["error"] = "invalid_json"
            continue
        values = discount_types_from_payload(data)
        return {
            "success": True,
            "discount_type": "; ".join(values),
            "count": len(values),
            "trace": trace,
        }
    return {"success": False, "discount_type": "", "error": "destaque_http_failed", "trace": trace}


def discount_types_from_payload(data):
    value = data.get("value") if isinstance(data, dict) else {}
    destaques = value.get("destaques") if isinstance(value, dict) else []
    results = []
    for destaque in destaques or []:
        if not isinstance(destaque, dict):
            continue
        for key in ("dscFlag", "titulo", "descricao"):
            text = str(destaque.get(key) or "").strip()
            for match in DISCOUNT_RE.finditer(text):
                value = _normalize_percent(match.group(1))
                if value and value not in results:
                    results.append(value)
    return results


def _normalize_percent(value):
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return ""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}% de desconto"


def _headers():
    return {
        "accept": "*/*",
        "accept-language": os.getenv("SEDA_CASAS_BAHIA_ACCEPT_LANGUAGE", "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"),
        "cache-control": "no-cache",
        "origin": "https://www.casasbahia.com.br",
        "pragma": "no-cache",
        "referer": "https://www.casasbahia.com.br/",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "x-origem": "vv-categoria-frontend",
        "xaplication": "vv-categoria-frontend",
    }
