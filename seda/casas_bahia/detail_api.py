import json
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path

import requests

from ._net import is_retryable_exc, request_with_retry, retry_after_seconds, sleep_backoff, throttle
from ..parsers import (
    appliance_model_number_from_text,
    clean_text,
    ldy_sku_from_text,
    ldy_sku_short_version_from_text,
    ref_sku_short_version_from_text,
    screen_size_from_text,
)
from ..step00_config import product_line, run_root


PDP_API = "https://pdp-api.casasbahia.com.br"
RECS_API = "https://recs.casasbahia.com.br/v1/recommendations"
PICKUP_API = "https://vv-retira-ponto-retirada-api-retira.viavarejo.com.br/api/v2/PontosRetirada/melhorLoja/cep"
PRODUCT_SOURCE_URL = f"{PDP_API}/api/v2/sku/source/CB"

def fetch_product_source(sku_id, timeout=None):
    if not sku_id:
        return {"success": False, "error": "missing_sku"}
    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    url = f"{PRODUCT_SOURCE_URL}?skuId={sku_id}"
    cached = _read_product_source_cache(sku_id)
    if cached is not None:
        return {
            "success": True,
            "detail": _product_source_detail(cached),
            "method": "casas_bahia_product_source_cache",
            "headers": {},
        }
    attempts = _product_source_attempts()
    last_error = "not_attempted"
    for attempt in attempts:
        retries = int(os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_RETRIES", "1"))
        for retry in range(retries + 1):
            if retry:
                time.sleep(float(os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_RETRY_SLEEP_SECONDS", "1.5")) * retry)
            if attempt == "zenrows":
                result = _fetch_product_source_zenrows(url, timeout=timeout)
            else:
                result = _fetch_product_source_direct(url, timeout=timeout)
            if result.get("success"):
                _write_product_source_cache(sku_id, result.get("data") or {})
                detail = _product_source_detail(result.get("data") or {})
                return {
                    "success": True,
                    "detail": detail,
                    "method": result.get("method", "casas_bahia_product_source_api"),
                    "headers": result.get("headers") or {},
                }
            last_error = result.get("error", "unknown")
    return {"success": False, "error": last_error}

def _product_source_cache_path(sku_id):
    if os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_CACHE", "1").lower() in {"0", "false", "no", "n"}:
        return None
    override = os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_CACHE_DIR", "").strip()
    directory = Path(override) if override else run_root() / "detail" / "product_source"
    safe_sku = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sku_id or "").strip())[:80] or "sku"
    return directory / f"{safe_sku}.json"

def _read_product_source_cache(sku_id):
    path = _product_source_cache_path(sku_id)
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

def _write_product_source_cache(sku_id, data):
    path = _product_source_cache_path(sku_id)
    if not path or not isinstance(data, dict):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return

def _product_source_attempts():
    mode = os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_MODE", "direct_first").lower().strip()
    if mode in {"0", "false", "no", "n", "off"}:
        return []
    if mode == "direct":
        return ["direct"]
    if mode == "direct_first":
        return ["direct", "zenrows"]
    if mode == "zenrows":
        return ["zenrows"]
    return ["zenrows", "direct"]

def _fetch_product_source_direct(url, timeout=None):
    try:
        response = requests.get(url, headers=_headers(), timeout=timeout)
    except Exception as exc:
        return {"success": False, "error": f"direct_{type(exc).__name__}: {exc}", "method": "casas_bahia_product_source_direct"}
    if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
        prefix = "blocked" if _blocked_response(response.text, response.status_code) else "status"
        return {
            "success": False,
            "error": f"direct_{prefix}_{response.status_code}",
            "method": "casas_bahia_product_source_direct",
        }
    try:
        data = response.json()
    except ValueError:
        return {"success": False, "error": "direct_invalid_json", "method": "casas_bahia_product_source_direct"}
    return {"success": True, "data": data, "method": "casas_bahia_product_source_direct"}

def _fetch_product_source_zenrows(url, timeout=None):
    if os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_ZENROWS", "1").lower() in {"0", "false", "no", "n"}:
        return {"success": False, "error": "zenrows_disabled", "method": "casas_bahia_product_source_zenrows"}
    try:
        from ..magalu.zenrows_client import request_url
    except Exception as exc:
        return {"success": False, "error": f"zenrows_import_{type(exc).__name__}: {exc}", "method": "casas_bahia_product_source_zenrows"}

    restore = {
        "SEDA_ALLOW_ZENROWS": os.environ.get("SEDA_ALLOW_ZENROWS"),
        "SEDA_ZENROWS_DRY_RUN": os.environ.get("SEDA_ZENROWS_DRY_RUN"),
    }
    os.environ["SEDA_ALLOW_ZENROWS"] = "1"
    os.environ["SEDA_ZENROWS_DRY_RUN"] = "0"
    try:
        result = request_url(url, profile=os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_ZENROWS_PROFILE", "premium_html"), timeout=timeout)
    finally:
        for key, value in restore.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if not result.success:
        return {
            "success": False,
            "error": f"zenrows_{result.error or result.status_code}",
            "method": f"casas_bahia_product_source_zenrows:{result.estimated_multiplier}",
            "headers": result.headers,
        }
    try:
        data = json.loads(result.text or "")
    except ValueError:
        return {
            "success": False,
            "error": "zenrows_invalid_json",
            "method": f"casas_bahia_product_source_zenrows:{result.estimated_multiplier}",
            "headers": result.headers,
        }
    return {
        "success": True,
        "data": data,
        "method": f"casas_bahia_product_source_zenrows:{result.estimated_multiplier}",
        "headers": result.headers,
    }

def _product_source_detail(data):
    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    sku = data.get("sku") if isinstance(data.get("sku"), dict) else {}
    spec_groups = product.get("specGroups")
    spec_values = _spec_values(spec_groups)
    grouped_specs = _grouped_spec_values(spec_groups)
    name = _known_text(product.get("name") or product.get("rawName"))
    sku_name = _known_text(sku.get("name"))
    line = product_line()
    model = _first_spec(spec_values, ["modelo"])
    screen_size = _screen_size_from_specs(spec_values, name)
    energy_use = _known_text(_first_spec(spec_values, ["consumo de energia"]))
    model_year = _known_text(_first_spec(spec_values, ["ano de lancamento"]))
    detail = {
        "retailer_sku_name": name,
        "screen_size": screen_size,
        "estimated_annual_electricity_use": energy_use,
        "model_year": model_year,
        "retailer_product_id": _known_text(product.get("id")),
    }
    if line == "TV":
        detail["sku"] = model
    if line == "REF":
        detail.update(
            {
                "ref_refrigerator_type": _known_text(
                    _first_group_spec(grouped_specs, ["caracteristicas"], ["modelo"])
                    or _first_spec(spec_values, ["modelo"])
                ),
                "ref_capacity": _known_text(
                    _first_group_spec(
                        grouped_specs,
                        ["especificacoes tecnicas"],
                        ["capacidade de armazenagem total (l)", "capacidade de armazenagem total", "capacidade total"],
                    )
                    or _first_spec(spec_values, ["capacidade de armazenagem total (l)", "capacidade de armazenagem total", "capacidade total"])
                ),
                "sku_short_version": ref_sku_short_version_from_text(name) or appliance_model_number_from_text(name),
            }
        )
    if line == "LDY":
        detail.update(
            {
                "ldy_loading_type": _known_text(
                    _first_group_spec(grouped_specs, ["caracteristicas"], ["acesso ao cesto"])
                    or _first_spec(spec_values, ["acesso ao cesto"])
                ),
                "ldy_color": _known_text(
                    _first_group_spec(grouped_specs, ["especificacoes tecnicas"], ["cor"])
                    or _first_spec(spec_values, ["cor"])
                ),
                "ldy_capacity": _known_text(
                    _first_group_spec(
                        grouped_specs,
                        ["caracteristicas"],
                        ["capacidade kg de roupas", "capacidade"],
                    )
                    or _first_spec(spec_values, ["capacidade kg de roupas", "capacidade"])
                ),
                "sku_short_version": ldy_sku_short_version_from_text(name),
                "sku": ldy_sku_from_text(name),
            }
        )
    return detail

def _spec_values(groups):
    specs = {}
    if not isinstance(groups, list):
        return specs
    for group in groups:
        if not isinstance(group, dict):
            continue
        for item in group.get("specs") or []:
            if not isinstance(item, dict):
                continue
            key = _normalize_key(item.get("name"))
            value = _known_text(item.get("value"))
            if key and value:
                specs.setdefault(key, []).append(value)
    return specs

def _grouped_spec_values(groups):
    grouped = {}
    if not isinstance(groups, list):
        return grouped
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = _normalize_key(group.get("name") or group.get("title") or group.get("description"))
        if not group_name:
            continue
        group_specs = grouped.setdefault(group_name, {})
        for item in group.get("specs") or []:
            if not isinstance(item, dict):
                continue
            key = _normalize_key(item.get("name"))
            value = _known_text(item.get("value"))
            if key and value:
                group_specs.setdefault(key, []).append(value)
    return grouped

def _first_spec(specs, labels):
    for label in labels:
        wanted = _normalize_key(label)
        for key, values in specs.items():
            if wanted == key:
                for value in values:
                    if _known_text(value):
                        return _known_text(value)
    return ""

def _first_group_spec(grouped_specs, group_labels, spec_labels):
    wanted_groups = [_normalize_key(label) for label in group_labels]
    wanted_specs = [_normalize_key(label) for label in spec_labels]
    for group_key, specs in grouped_specs.items():
        if group_key not in wanted_groups:
            continue
        for spec_key, values in specs.items():
            if spec_key not in wanted_specs:
                continue
            for value in values:
                text = _known_text(value)
                if text:
                    return text
    return ""

def _screen_size_from_specs(specs, title):
    values = []
    wanted = _normalize_key("tamanho da tela")
    for key, items in specs.items():
        if wanted == key:
            values.extend(items)
    for value in values:
        extracted = _screen_size_from_tamanho_tela(value)
        if extracted:
            return extracted
    return ""

def _screen_size_from_tamanho_tela(value):
    text = _known_text(value)
    if not text:
        return ""
    explicit = re.search(r"^\s*-?\s*(\d{2,3})\s*(?:\"|''|polegadas|pol\b|in\b)", text, re.I)
    if explicit:
        return f'{explicit.group(1)}"'
    quoted = re.search(r"(\d{2,3})\s*(?:\"|'')", text, re.I)
    if quoted:
        return f'{quoted.group(1)}"'
    numeric = re.fullmatch(r"\d{2,3}", text)
    if numeric:
        return f'{numeric.group(0)}"'
    if re.fullmatch(r"\d{2,3}\s*a\s*\d{2,3}\s*polegadas", text, re.I):
        return ""
    return screen_size_from_text(text)

def _known_text(value):
    text = clean_text(value)
    normalized = _normalize_key(text)
    if normalized in {"", "nao informado", "nao se aplica", "sem informacao"}:
        return ""
    if text in {".", "-", "--"}:
        return ""
    return text

def fetch_freight(sku_id, seller_id, zipcode=None, timeout=None, referer_url=None):
    if not sku_id or not seller_id:
        return {"success": False, "error": "missing_sku_or_seller"}
    zipcode = zipcode or os.getenv("SEDA_POSTAL_CODE", "01010-010")
    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    url = f"{PDP_API}/api/v2/sku/{sku_id}/freight/seller/{seller_id}/zipcode/{zipcode}/source/CB"
    params = _freight_params()
    headers = _headers(zipcode=zipcode, include_cvip=True)
    if referer_url:
        headers["referer"] = str(referer_url)
    retries = _int_env("SEDA_CASAS_BAHIA_FREIGHT_RETRIES", _int_env("SEDA_CASAS_BAHIA_API_RETRIES", 3))
    last_error = "not_attempted"
    # pdp-api rate-limits (429) when hit back-to-back and occasionally drops the
    # TLS handshake; pace per host and back off on rate-limit / transient errors.
    for attempt in range(retries + 1):
        retry_after = None
        retryable = False
        for transport in _freight_transports():
            throttle(host="cb_pdp")
            try:
                response = _freight_get(transport, url, params, headers, timeout)
            except Exception as exc:
                last_error = f"{transport}_{type(exc).__name__}: {exc}"
                retryable = retryable or is_retryable_exc(exc)
                continue
            if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
                content_type = response.headers.get("content-type", "")[:60]
                body_len = len(response.text or "")
                prefix = "blocked" if _blocked_response(response.text, response.status_code) else "status"
                last_error = f"{transport}_{prefix}_{response.status_code}:ct={content_type}:len={body_len}"
                if response.status_code in (429, 500, 502, 503, 504):
                    retryable = True
                    retry_after = retry_after or retry_after_seconds(response)
                continue
            try:
                data = response.json()
            except ValueError:
                last_error = "invalid_json"
                continue
            return {"success": True, "detail": _freight_detail(data), "method": f"{transport}_freight_api"}
        if attempt < retries and retryable:
            sleep_backoff(attempt, retry_after=retry_after)
            continue
        break
    if _freight_zenrows_enabled() and referer_url:
        fallback = _fetch_freight_zenrows_pdp(referer_url, sku_id, seller_id, zipcode=zipcode, timeout=timeout)
        if fallback.get("success"):
            return fallback
        last_error = f"{last_error}|{fallback.get('error', 'zenrows_pdp_failed')}"
    return {"success": False, "error": last_error}

def _freight_zenrows_enabled():
    return os.getenv("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_FALLBACK", "0").lower() in {"1", "true", "yes", "y"}

def _fetch_freight_zenrows_pdp(product_url, sku_id, seller_id, zipcode=None, timeout=None):
    try:
        from ..magalu.zenrows_client import request_url
    except Exception as exc:
        return {"success": False, "error": f"zenrows_import_{type(exc).__name__}: {exc}"}

    zipcode = zipcode or os.getenv("SEDA_POSTAL_CODE", "01010-010")
    instructions = [
        {"wait_for": "#frete"},
        {"fill": ["#frete", zipcode]},
        {"click": "button[data-testid=calcular-frete]"},
        {"wait": int(os.getenv("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_AFTER_CLICK_WAIT_MS", "8000"))},
    ]
    extra = {
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": os.getenv("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_PROXY_COUNTRY", "br"),
        "json_response": "true",
        "wait": os.getenv("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_WAIT", "15000"),
        "js_instructions": json.dumps(instructions, separators=(",", ":")),
    }
    restore = {
        "SEDA_ALLOW_ZENROWS": os.environ.get("SEDA_ALLOW_ZENROWS"),
        "SEDA_ZENROWS_DRY_RUN": os.environ.get("SEDA_ZENROWS_DRY_RUN"),
        "SEDA_ZENROWS_SESSION_ID": os.environ.get("SEDA_ZENROWS_SESSION_ID"),
    }
    profile = os.getenv("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_PROFILE", "basic_html")
    attempts = _int_env("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_ATTEMPTS", 3)
    base_session = _int_env("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_SESSION_ID", 12345)
    last_error = "zenrows_pdp_not_attempted"
    last_headers = {}
    last_method = "zenrows_pdp_shipping"
    try:
        os.environ["SEDA_ALLOW_ZENROWS"] = "1"
        os.environ["SEDA_ZENROWS_DRY_RUN"] = "0"
        for attempt in range(max(1, attempts)):
            os.environ["SEDA_ZENROWS_SESSION_ID"] = str(base_session + attempt)
            result = request_url(product_url, profile=profile, timeout=timeout, extra=extra)
            _dump_zenrows_freight_response(result.text, attempt=attempt + 1)
            last_headers = result.headers
            last_method = f"zenrows_pdp_shipping:{result.estimated_multiplier}"
            if not result.success:
                last_error = f"zenrows_pdp_{result.error or result.status_code}"
                continue
            payload = _parse_json_text(result.text)
            if not isinstance(payload, dict):
                last_error = "zenrows_pdp_invalid_json"
                continue
            data = _freight_json_from_zenrows_payload(payload, sku_id, seller_id)
            if not isinstance(data, dict):
                last_error = "zenrows_pdp_missing_freight_xhr"
                continue
            detail = _freight_detail(data)
            if not detail.get("delivery_availability") and not detail.get("pick_up_availability"):
                last_error = "zenrows_pdp_empty_freight"
                continue
            return {
                "success": True,
                "detail": detail,
                "method": last_method,
                "headers": last_headers,
            }
    finally:
        for key, value in restore.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return {
        "success": False,
        "error": last_error,
        "headers": last_headers,
        "method": last_method,
    }

def _int_env(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except ValueError:
        return default

def _dump_zenrows_freight_response(text, attempt=None):
    path = os.getenv("SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_DUMP", "").strip()
    if not path:
        return
    try:
        output = Path(path)
        if attempt is not None and output.suffix:
            output = output.with_name(f"{output.stem}_{attempt}{output.suffix}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text or "", encoding="utf-8")
    except OSError:
        return

def _parse_json_text(text):
    try:
        return json.loads(text or "")
    except ValueError:
        return None

def _freight_json_from_zenrows_payload(payload, sku_id, seller_id):
    expected = f"/sku/{sku_id}/freight/seller/{seller_id}/"
    candidates = []
    for entry in payload.get("xhr") or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        if expected not in url:
            continue
        candidates.append(entry)
    for entry in candidates:
        data = _parse_json_text(entry.get("body") or "")
        if isinstance(data, dict) and data.get("options"):
            return data
    for entry in candidates:
        data = _parse_json_text(entry.get("body") or "")
        if isinstance(data, dict):
            return data
    return None

def _freight_params():
    params = {
        "channel": os.getenv("SEDA_CASAS_BAHIA_FREIGHT_CHANNEL", "DESKTOP"),
        "orderby": "price",
    }
    profile = os.getenv("SEDA_CASAS_BAHIA_FREIGHT_QUERY_PROFILE", "har").strip().lower()
    if profile in {"utm", "legacy", "mobile"}:
        params.update(
            {
                "utm_medium": os.getenv("SEDA_CASAS_BAHIA_UTM_MEDIUM", "cpc"),
                "utm_source": os.getenv("SEDA_CASAS_BAHIA_UTM_SOURCE", "gp_branding"),
                "utm_campaign": os.getenv("SEDA_CASAS_BAHIA_UTM_CAMPAIGN", "cb_all_gg_brand_exata"),
            }
        )
    return params

def _freight_transports():
    raw = os.getenv("SEDA_CASAS_BAHIA_FREIGHT_TRANSPORTS", "requests,curl_cffi")
    transports = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return transports or ["requests"]

def _freight_get(transport, url, params, headers, timeout):
    if transport == "requests":
        return requests.get(url, params=params, headers=headers, timeout=timeout)
    if transport == "curl_cffi":
        from curl_cffi import requests as curl_requests

        return curl_requests.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            impersonate=os.getenv("SEDA_CASAS_BAHIA_CURL_IMPERSONATE", "chrome"),
        )
    raise ValueError(f"unknown_freight_transport:{transport}")

def fetch_similar_names(product_id, sku_id=None, current_product=None, timeout=None):
    if not product_id:
        return {"success": False, "error": "missing_product_id"}
    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    body = {
        "pageType": "PRODUCT",
        "device": "DESKTOP",
        "model": os.getenv("SEDA_CASAS_BAHIA_RECS_MODEL", "similar_itens"),
        "productIds": [str(product_id)],
        "utmCampaign": os.getenv("SEDA_CASAS_BAHIA_UTM_CAMPAIGN", "cb_all_gg_brand_exata"),
        "utmMedium": os.getenv("SEDA_CASAS_BAHIA_UTM_MEDIUM", "cpc"),
        "utmSource": os.getenv("SEDA_CASAS_BAHIA_UTM_SOURCE", "gp_branding"),
        "zipCode": os.getenv("SEDA_POSTAL_CODE", "01010-010").replace("-", ""),
        "sessionId": os.getenv("SEDA_CASAS_BAHIA_SESSION_ID", "89ec6f5d-3c85-40ba-af9c-9bf91e8af2b0"),
        "regionId": os.getenv("SEDA_CASAS_BAHIA_REGION_ID", "126000"),
    }
    if sku_id:
        body["skuId"] = str(sku_id)
    session = requests.Session()
    session.trust_env = os.getenv("SEDA_CASAS_BAHIA_TRUST_ENV_PROXY", "0").lower() in {"1", "true", "yes", "y"}
    try:
        response = request_with_retry(
            lambda: session.post(RECS_API, json=body, headers=_headers(), timeout=timeout),
            throttle_host="cb_recs",
        )
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
        return {"success": False, "error": f"status_{response.status_code}"}
    try:
        data = response.json()
    except ValueError:
        return {"success": False, "error": "invalid_json"}
    products = (data.get("data") or {}).get("products") or []
    names = [
        title
        for product in products
        for title in [str(product.get("title") or "").strip()]
        if title and _is_relevant_title(title)
    ]
    return {"success": True, "names": names[:20], "source_count": len(products), "filtered_count": len(names)}

def fetch_pickup(sku_id, seller_id, zipcode=None, timeout=None):
    if not sku_id or not seller_id:
        return {"success": False, "error": "missing_sku_or_seller"}
    zipcode = (zipcode or os.getenv("SEDA_POSTAL_CODE", "01010-010")).replace("-", "")
    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    params = {
        "cep": zipcode,
        f"sku[{sku_id}]": "1",
        "idLojista": seller_id,
    }
    try:
        response = request_with_retry(
            lambda: requests.get(PICKUP_API, params=params, headers=_pickup_headers(), timeout=timeout),
            throttle_host="cb_pickup",
        )
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
        return {"success": False, "error": f"status_{response.status_code}"}
    try:
        data = response.json()
    except ValueError:
        return {"success": False, "error": "invalid_json"}
    text = _pickup_text(data)
    return {"success": bool(text), "detail": {"pick_up_availability": text}, "error": "" if text else "empty_pickup"}

def _is_relevant_title(title):
    line = product_line()
    text = str(title or "").lower()
    if line == "REF":
        return bool(re.search(r"\b(?:geladeira|refrigerador|refrigeradora|freezer|frigobar)\b", text, re.I))
    if line == "LDY":
        return bool(re.search(r"\b(?:lavadora|lava\s+e\s+seca|secadora|m[aá]quina\s+de\s+lavar)\b", text, re.I))
    return "smart tv" in text or " tv " in f" {text} " or "televisor" in text

def _pickup_text(data):
    if not isinstance(data, dict):
        return ""
    prazo = data.get("prazo") if isinstance(data.get("prazo"), dict) else {}
    formatted = str(prazo.get("textoFormatado") or "").strip()
    if not formatted:
        hours = prazo.get("prazoHorasEntrega") or data.get("prazoEmHoras")
        days = prazo.get("prazoDiasEntrega") or data.get("prazoEmDias")
        if hours not in ("", None):
            formatted = f"{hours}h"
        elif days not in ("", None):
            formatted = f"{days} dias"
    if not formatted:
        return ""
    return f"Retira Rapido Retirar em {formatted}"

def _freight_detail(data):
    delivery = []
    pickup = []
    skipped_delivery = []
    for option in _iter_freight_options(data):
        if not isinstance(option, dict):
            continue
        name = str(option.get("name") or "").strip()
        deadline = str(
            option.get("formattedDeadlineDelivery")
            or option.get("deliveryDateDetailed")
            or option.get("deadline")
            or option.get("description")
            or ""
        ).strip()
        text = " ".join(part for part in [name, deadline] if part)
        normalized_name = _normalize_ascii(name)
        if "retira" in normalized_name or "withdraw" in normalized_name or "pickup" in normalized_name:
            pickup.append(text)
        elif text and _is_normal_delivery_option(normalized_name):
            delivery.append(text)
        elif text:
            skipped_delivery.append(text)
    if not delivery and isinstance(data, dict):
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        message = _known_text(error.get("message"))
        if message and not _is_freight_calculation_error(message):
            delivery.append(message)
        elif pickup and not skipped_delivery:
            delivery.append(os.getenv("SEDA_CASAS_BAHIA_NO_DELIVERY_TEXT", "Entrega indisponivel para este CEP"))
    return {
        "delivery_availability": "; ".join(dict.fromkeys(delivery)),
        "pick_up_availability": "; ".join(dict.fromkeys(pickup)),
    }

def _is_freight_calculation_error(message):
    normalized = _normalize_ascii(message)
    return "calculo de frete apresentou problemas" in normalized

def _is_normal_delivery_option(normalized_name):
    text = str(normalized_name or "").strip().lower()
    return bool(re.search(r"\bnormal\b", text))

def _iter_freight_options(value):
    if isinstance(value, list):
        for item in value:
            yield from _iter_freight_options(item)
        return
    if not isinstance(value, dict):
        return
    if value.get("name") and (
        value.get("formattedDeadlineDelivery")
        or value.get("deliveryDateDetailed")
        or value.get("deadline")
        or value.get("description")
    ):
        yield value
    for key in ("options", "freights", "items", "deliveries", "shippingOptions"):
        child = value.get(key)
        if child:
            yield from _iter_freight_options(child)

def _normalize_ascii(value):
    text = str(value or "").lower()
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    replacements = {
        "á": "a",
        "à": "a",
        "â": "a",
        "ã": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ú": "u",
        "ç": "c",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text

def _normalize_key(value):
    text = unicodedata.normalize("NFKD", clean_text(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

def _blocked_response(text, status_code=0):
    haystack = str(text or "").lower()
    if status_code in {401, 403, 429}:
        return True
    return any(marker in haystack for marker in ("akamai", "access denied", "ops! algo deu errado", "captcha", "customdeny"))

def _headers(zipcode=None, include_cvip=False):
    headers = {
        "accept": "*/*",
        "accept-language": os.getenv("SEDA_CASAS_BAHIA_ACCEPT_LANGUAGE", "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"),
        "cache-control": os.getenv("SEDA_CASAS_BAHIA_CACHE_CONTROL", "no-cache"),
        "content-type": "application/json",
        "origin": "https://www.casasbahia.com.br",
        "priority": "u=1, i",
        "referer": "https://www.casasbahia.com.br/",
        "sec-ch-ua": os.getenv(
            "SEDA_CASAS_BAHIA_SEC_CH_UA",
            '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            os.getenv(
                "SEDA_CASAS_BAHIA_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            )
        ),
    }
    cookie = os.getenv("SEDA_CASAS_BAHIA_API_COOKIE", "").strip()
    if cookie:
        headers["cookie"] = cookie
    cvip = _cvip_header(zipcode) if include_cvip else os.getenv("SEDA_CASAS_BAHIA_X_CVIP", "").strip()
    if cvip:
        headers["x-cvip"] = cvip
    return headers

def _cvip_header(zipcode=None):
    raw = os.getenv("SEDA_CASAS_BAHIA_X_CVIP", "").strip()
    zip_digits = re.sub(r"\D+", "", str(zipcode or os.getenv("SEDA_POSTAL_CODE", "01010-010")))
    if raw:
        if zip_digits and "cepClienteProvavel=" not in raw:
            return f"{raw.rstrip('&')}&cepClienteProvavel={zip_digits}"
        return raw
    user_guid = (
        os.getenv("SEDA_CASAS_BAHIA_USER_GUID", "").strip()
        or os.getenv("SEDA_CASAS_BAHIA_SESSION_ID", "").strip()
        or str(uuid.uuid5(uuid.NAMESPACE_URL, "casas-bahia-seda"))
    )
    if not zip_digits:
        return f"IPI-CasasBahia=UsuarioGUID={user_guid}"
    return f"IPI-CasasBahia=UsuarioGUID={user_guid}&cepClienteProvavel={zip_digits}"

def _pickup_headers():
    headers = _headers()
    headers["sec-fetch-site"] = "cross-site"
    return headers
