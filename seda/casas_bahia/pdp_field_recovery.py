"""Paid Casas PDP recovery for missing safe fields and validated SKU values."""

import os

from ..parsers import CASAS_TV_EXACT_MODELO_FIELD, parse_detail
from ..step00_config import product_line
from .ldy_sku_contract import (
    BRAND_FIELD as LDY_BRAND_FIELD,
    EVIDENCE_FIELD as LDY_EVIDENCE_FIELD,
    resolve_ldy_sku,
)
from .ref_sku_contract import (
    BRAND_FIELD as REF_BRAND_FIELD,
    EVIDENCE_FIELD as REF_EVIDENCE_FIELD,
    resolve_casas_ref_sku,
)
from .recovery_contract import CASAS_ZENROWS_FIELD_MAP


_CONFIG_ERRORS = {
    "zenrows_disabled",
    "zenrows_dry_run",
    "key_missing",
}


def fetch_pdp_fields_via_zenrows(
    product_url,
    requested_fields,
    *,
    product_line_value=None,
    timeout=None,
    max_requests=2,
):
    line = str(product_line_value or product_line()).strip().upper()
    safe_fields = set(_all_safe_fields())
    requested = tuple(
        field
        for field in dict.fromkeys(requested_fields or ())
        if field in safe_fields
    )
    if not product_url or not requested or max_requests <= 0:
        return {
            "success": False,
            "detail": {},
            "error": "not_attempted",
            "request_count": 0,
            "attempts": [],
        }

    from ..magalu.zenrows_client import request_url

    try:
        timeout = int(
            timeout
            or os.getenv("SEDA_CASAS_BAHIA_ZENROWS_FIELD_TIMEOUT", "45")
        )
    except (TypeError, ValueError):
        timeout = 45
    timeout = max(5, min(timeout, 180))
    profiles = tuple(
        profile
        for profile in dict.fromkeys(
            (
                os.getenv(
                    "SEDA_CASAS_BAHIA_ZENROWS_FIELD_PROFILE_10X",
                    "premium_html",
                ).strip(),
                os.getenv(
                    "SEDA_CASAS_BAHIA_ZENROWS_FIELD_PROFILE_25X",
                    "pdp_js_full",
                ).strip(),
            )
        )
        if profile
    )
    if not profiles:
        return {
            "success": False,
            "detail": {},
            "error": "unknown_profile:blank",
            "request_count": 0,
            "attempts": [],
        }
    available = {}
    attempts = []
    last_error = "no_target_values"
    last_headers = {}
    identity_verified = False

    for profile in profiles[:max_requests]:
        result = request_url(
            product_url,
            profile=profile,
            timeout=timeout,
            extra={"proxy_country": "br"},
        )
        last_headers = result.headers or {}
        attempt = {
            "profile": profile,
            "success": bool(result.success),
            "status_code": result.status_code,
            "error": result.error,
            "estimated_multiplier": result.estimated_multiplier,
            "headers": result.headers or {},
        }
        attempts.append(attempt)
        if not result.success:
            last_error = result.error or f"status_{result.status_code}"
            if last_error in _CONFIG_ERRORS or str(last_error).startswith(
                "unknown_profile:"
            ):
                break
            continue

        try:
            detail = parse_detail(
                result.text or "",
                "Casas Bahia",
                "https://www.casasbahia.com.br",
                product_url,
            )
        except Exception as exc:
            # The paid HTTP request already completed and was appended above.
            # Preserve that request count even when local parsing fails.
            last_error = f"parse_exception:{type(exc).__name__}"
            break
        if detail.get("_detail_identity_conflict") is True:
            last_error = "identity_conflict"
            break
        if detail.get("_detail_identity_verified") is not True:
            last_error = "identity_unverified"
            continue
        identity_verified = True
        safe_internal = detail.get("_casas_pdp_safe_recovery")
        safe_internal = safe_internal if isinstance(safe_internal, dict) else {}
        for field in requested:
            if field == "sku":
                value = _validated_sku(detail, line)
            else:
                value = detail.get(field) or safe_internal.get(field)
            if value not in ("", None, [], {}) and field not in available:
                available[field] = value
                if field == "sku" and line == "TV":
                    available[CASAS_TV_EXACT_MODELO_FIELD] = True
        if all(available.get(field) for field in requested):
            last_error = ""
            break
        last_error = "partial_target_values" if available else "no_target_values"

    terminal_error = (
        last_error
        if last_error in _CONFIG_ERRORS
        or str(last_error).startswith("unknown_profile:")
        else ""
    )
    return {
        "success": bool(available),
        "detail": available,
        # Keep configuration failures visible even when an earlier profile
        # recovered only part of the requested fields.  The caller can retain
        # those safe values while stopping further paid requests.
        "error": terminal_error if available else last_error,
        "identity_verified": identity_verified,
        "method": "casas_bahia_pdp_zenrows",
        "headers": last_headers,
        "request_count": len(attempts),
        "attempts": attempts,
    }


def _all_safe_fields():
    return tuple(
        dict.fromkeys(
            field
            for fields in CASAS_ZENROWS_FIELD_MAP.values()
            for field in fields
        )
    )


def _validated_sku(detail, line):
    """Return only a product-line contract-approved PDP SKU."""
    if line == "TV":
        return (
            detail.get("sku", "")
            if detail.get(CASAS_TV_EXACT_MODELO_FIELD) is True
            else ""
        )

    title = detail.get("retailer_sku_name", "")
    if line == "REF":
        evidence = _detail_evidence(
            detail,
            REF_EVIDENCE_FIELD,
            preserve_dict_entries=False,
        )
        return resolve_casas_ref_sku(
            "",
            title,
            evidence,
            brand=detail.get(REF_BRAND_FIELD, ""),
        ).sku

    if line == "LDY":
        evidence = _detail_evidence(
            detail,
            LDY_EVIDENCE_FIELD,
            preserve_dict_entries=True,
        )
        return resolve_ldy_sku(
            "",
            title,
            evidence,
            brand=detail.get(LDY_BRAND_FIELD, ""),
        ).sku
    return ""


def _detail_evidence(detail, field, *, preserve_dict_entries):
    raw = detail.get(field)
    if raw in ("", None, [], {}, ()):
        raw = detail.get("sku", "")
    values = raw if isinstance(raw, (list, tuple)) else (raw,)
    if preserve_dict_entries:
        return tuple(values)
    return tuple(
        value.get("value", "") if isinstance(value, dict) else value
        for value in values
    )
