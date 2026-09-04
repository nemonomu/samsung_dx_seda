"""Casas Bahia seller별 merchandising badge에서 할인 유형을 읽는다."""

import os
import re
import time

import requests


DESTAQUE_URL = "https://api-destaque-descoberta.casasbahia.com.br/Destaque/Sku/{sku_id}/Lojista/{seller_id}"
DISCOUNT_RE = re.compile(r"(\d+(?:[,.]\d+)?)\s*%\s*de\s+desconto\b", re.I)
PERCENT_RE = re.compile(r"(?<!\d)(\d+(?:[,.]\d+)?)\s*%")
COUPON_RE = re.compile(r"\bcupom\b", re.I)


def fetch_discount_type(sku_id, seller_id, timeout=None):
    """정확한 SKU·seller 쌍의 destaque endpoint에서 할인 문구를 조회한다.

    Args:
        sku_id: 선택된 판매 제안의 SKU 식별자.
        seller_id: 같은 제안의 판매자 식별자.
        timeout: HTTP 요청 제한 시간(초).

    Returns:
        중복 제거된 할인 문구, 개수, 재시도 trace를 담은 dict. 두 identity 중
        하나라도 없거나 요청이 모두 실패하면 ``success=False``다.

    다른 seller의 badge는 가격 조건이 다를 수 있으므로 임의 seller fallback을
    하지 않는다.
    """
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
    """destaque payload에서 화면 우선순위에 맞는 할인 유형을 추출한다."""
    value = data.get("value") if isinstance(data, dict) else {}
    destaques = value.get("destaques") if isinstance(value, dict) else []
    results = []
    for destaque in destaques or []:
        if not isinstance(destaque, dict):
            continue
        texts = [
            str(destaque.get(key) or "").strip()
            for key in ("dscFlag", "titulo", "descricao")
        ]
        texts = [text for text in texts if text]
        is_coupon = any(COUPON_RE.search(text) for text in texts)
        if is_coupon:
            for text in texts:
                match = PERCENT_RE.search(text)
                if not match:
                    continue
                percent = _normalize_percent_value(match.group(1))
                if percent:
                    # The storefront renders one coupon badge. In the verified
                    # storefront samples, API order matched that display priority,
                    # so keep the first coupon
                    # with an explicit percentage instead of joining all
                    # promotion candidates into one database value.
                    return [f"USE O CUPOM DESCONTO {percent}%"]
            # A coupon without an explicit percentage is not assigned a guessed
            # number, and percentages from another destaque are never borrowed.
            continue
        for text in texts:
            for match in DISCOUNT_RE.finditer(text):
                value = _normalize_percent(match.group(1))
                if value and value not in results:
                    results.append(value)
    return results


def _normalize_percent(value):
    text = _normalize_percent_value(value)
    return f"{text}% de desconto" if text else ""


def _normalize_percent_value(value):
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return ""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


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
