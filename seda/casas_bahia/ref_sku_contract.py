"""Conservative Casas Bahia refrigerator full/short SKU contract."""

import re
import unicodedata
from dataclasses import dataclass

from ..common.field_rules import clean_text


EVIDENCE_FIELD = "_casas_ref_sku_evidence"
BRAND_FIELD = "_casas_ref_brand"
TITLE_SELECTED_TOKEN = "casas_ref_sku_title_selected"
EVIDENCE_SELECTED_TOKEN = "casas_ref_sku_product_source_selected"
LAST_KNOWN_SELECTED_TOKEN = "casas_ref_sku_last_known_selected"
_TRUSTED_SELECTION_TOKENS = frozenset(
    {
        TITLE_SELECTED_TOKEN,
        EVIDENCE_SELECTED_TOKEN,
        LAST_KNOWN_SELECTED_TOKEN,
    }
)

_MODEL_TOKEN_RE = re.compile(
    r"(?<![A-Z0-9])"
    r"(?=[A-Z0-9/-]*[A-Z])"
    r"(?=[A-Z0-9/-]*\d)"
    r"[A-Z0-9]+(?:[-/][A-Z0-9]+)*"
    r"(?![A-Z0-9])",
    re.IGNORECASE,
)
_SAMSUNG_SHORT_RE = re.compile(
    r"^((?:RS|RF|RT|RB|RL|RR)\d{2}[A-Z]?)",
    re.IGNORECASE,
)
_PANASONIC_FULL_RE = re.compile(
    r"^NR-((?:BB|BT)\d{2})[A-Z0-9/-]*$",
    re.IGNORECASE,
)
_MEASUREMENT_RE = re.compile(
    r"^\d+(?:[.,]\d+)?(?:KG|KGS|L|LT|LTS|LITROS?|ML|CM|MM|M|"
    r"V|VOLTS?|W|WATTS?|WH|KWH|KW|HZ|KHZ|MHZ|GHZ|RPM|HDMI|USB)$",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"^(?:\d+PORTAS?|PORTAS?\d+|FROSTFREE|INVERTER|BIVOLT|"
    r"DUPLEX|MULTIDOOR|SIDEBYSIDE)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RefSkuResolution:
    sku: str
    status_tokens: tuple


def normalize_ref_sku(value):
    return clean_text(value).upper().strip(" \t\r\n.,;:()[]{}")


def is_valid_ref_manufacturer_sku(value, *, brand="", title=""):
    sku = normalize_ref_sku(value)
    if not sku or len(sku) > 40 or " " in sku:
        return False
    if not _MODEL_TOKEN_RE.fullmatch(sku):
        return False
    compact = re.sub(r"[-/]", "", sku)
    if not re.search(r"[A-Z]", compact) or not re.search(r"\d", compact):
        return False
    if len(compact) < 4:
        electrolux_context = "electrolux" in _ascii_key(f"{brand} {title}")
        if not (
            electrolux_context
            and re.fullmatch(r"I[BM]\d", compact, re.IGNORECASE)
        ):
            return False
    if re.fullmatch(r"\d{8,14}", compact):
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", compact):
        return False
    if _MEASUREMENT_RE.fullmatch(compact) or _NOISE_RE.fullmatch(compact):
        return False
    if re.fullmatch(r"(?:110|127|220|240)V(?:OLTS?)?", compact):
        return False
    return True


def casas_ref_title_candidates(title, *, brand=""):
    text = _ascii_text(clean_text(title)).upper()
    values = []
    for match in _MODEL_TOKEN_RE.finditer(text):
        candidate = normalize_ref_sku(match.group(0))
        if (
            is_valid_ref_manufacturer_sku(
                candidate,
                brand=brand,
                title=title,
            )
            and candidate not in values
        ):
            values.append(candidate)
    return values


def select_ref_sku_candidates(values, *, brand="", title=""):
    candidates = []
    for raw in values or ():
        for candidate in casas_ref_title_candidates(raw, brand=brand):
            if candidate not in candidates:
                candidates.append(candidate)
    if not candidates:
        return ""

    panasonic = [
        value for value in candidates if _PANASONIC_FULL_RE.fullmatch(value)
    ]
    if "panasonic" in _ascii_key(f"{brand} {title}") and len(panasonic) == 1:
        return panasonic[0]

    maximal = []
    compact_values = {
        value: re.sub(r"[-/]", "", value)
        for value in candidates
    }
    for value in candidates:
        compact = compact_values[value]
        if any(
            value != other
            and len(compact_values[other]) > len(compact)
            and compact in compact_values[other]
            for other in candidates
        ):
            continue
        maximal.append(value)
    return maximal[0] if len(maximal) == 1 else ""


def casas_ref_title_sku(title, *, brand=""):
    return select_ref_sku_candidates(
        (title,),
        brand=brand,
        title=title,
    )


def resolve_casas_ref_sku(existing_sku, title, evidence=(), *, brand=""):
    title_candidates = casas_ref_title_candidates(title, brand=brand)
    title_sku = select_ref_sku_candidates(
        (title,),
        brand=brand,
        title=title,
    )
    evidence_sku = select_ref_sku_candidates(
        evidence,
        brand=brand,
        title=title,
    )
    existing = normalize_ref_sku(existing_sku)
    existing_valid = existing if is_valid_ref_manufacturer_sku(
        existing,
        brand=brand,
        title=title,
    ) else ""

    statuses = []
    if title_sku:
        statuses.append(TITLE_SELECTED_TOKEN)
        return RefSkuResolution(title_sku, tuple(statuses))
    if len(title_candidates) > 1:
        statuses.append("casas_ref_sku_title_ambiguous")
        if evidence_sku and evidence_sku in title_candidates:
            statuses.append(EVIDENCE_SELECTED_TOKEN)
            return RefSkuResolution(evidence_sku, tuple(statuses))
        statuses.append("casas_ref_sku_unresolved")
        return RefSkuResolution("", tuple(statuses))
    if evidence_sku:
        statuses.append(EVIDENCE_SELECTED_TOKEN)
        return RefSkuResolution(evidence_sku, tuple(statuses))
    if existing_valid:
        statuses.append("casas_ref_sku_existing_selected")
        return RefSkuResolution(existing_valid, tuple(statuses))
    statuses.append("casas_ref_sku_unresolved")
    return RefSkuResolution("", tuple(statuses))


def casas_ref_sku_for_output(row, item=""):
    row = row or {}
    title = row.get("retailer_sku_name", "")
    brand = row.get(BRAND_FIELD, "")
    current = normalize_ref_sku(row.get("sku", ""))
    tokens = set(str(row.get("parse_status") or "").split("+"))
    if tokens & _TRUSTED_SELECTION_TOKENS:
        # Product Source brand/evidence fields are intentionally private and
        # do not cross the OUTPUT_COLUMNS CSV boundary.  The trusted token is
        # the persisted proof that this atomic SKU pair was already selected
        # and validated before that boundary; do not invalidate it again with
        # the now-missing private brand context.
        sku = current
    else:
        sku = resolve_casas_ref_sku(
            "",
            title,
            (),
            brand=brand,
        ).sku
    if item and sku and sku.casefold() == str(item).strip().casefold():
        return ""
    return sku


def casas_ref_short_for_output(row, resolved_sku):
    sku = normalize_ref_sku(resolved_sku)
    if not sku:
        return ""
    row = row or {}
    tokens = set(str(row.get("parse_status") or "").split("+"))
    stored_sku = normalize_ref_sku(row.get("sku", ""))
    stored_short = normalize_ref_sku(row.get("sku_short_version", ""))
    if (
        tokens & _TRUSTED_SELECTION_TOKENS
        and stored_sku == sku
        and stored_short
    ):
        return stored_short
    context = _ascii_key(
        f"{row.get(BRAND_FIELD, '')} "
        f"{row.get('retailer_sku_name', '')}"
    )
    if "samsung" in context.split():
        match = _SAMSUNG_SHORT_RE.match(sku)
        if match:
            return match.group(1).upper()
    match = _PANASONIC_FULL_RE.match(sku)
    if match:
        alias = match.group(1).upper()
        # Treat a Panasonic family token as a real short version only when
        # the title exposes it separately; never invent it by slicing NR-*.
        title_candidates = casas_ref_title_candidates(
            row.get("retailer_sku_name", ""),
            brand=row.get(BRAND_FIELD, ""),
        )
        if alias in title_candidates:
            return alias
    return sku


def extract_product_source_evidence(spec_values, *, model=""):
    """Return explicit Product Source model/reference values only."""
    evidence = []
    for raw in (model,):
        value = clean_text(raw)
        if value and value not in evidence:
            evidence.append(value)
    wanted = {
        _ascii_key(label)
        for label in (
            "modelo",
            "referencia",
            "referência",
            "ref",
            "codigo do modelo",
            "código do modelo",
        )
    }
    for raw_label, values in (spec_values or {}).items():
        if _ascii_key(raw_label) not in wanted:
            continue
        for raw in values if isinstance(values, (list, tuple)) else (values,):
            value = clean_text(raw)
            if value and value not in evidence:
                evidence.append(value)
    return tuple(evidence)


def _ascii_text(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode("ascii")


def _ascii_key(value):
    text = _ascii_text(clean_text(value)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()
