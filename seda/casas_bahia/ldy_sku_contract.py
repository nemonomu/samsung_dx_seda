import re
import unicodedata
from dataclasses import dataclass

from ..parsers import clean_text, is_appliance_spec_token, remove_accents


EVIDENCE_FIELD = "_casas_ldy_sku_evidence"
BRAND_FIELD = "_casas_ldy_brand"
SHORT_DERIVED_TOKEN = "casas_ldy_sku_short_derived"
_RESOLVED_SELECTION_TOKENS = frozenset(
    {
        "casas_ldy_sku_title_selected",
        "casas_ldy_sku_existing_selected",
        "casas_ldy_sku_product_source_selected",
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
_DESCRIPTION_LABEL_RE = re.compile(
    r"\b(?P<label>Modelos?|Refer(?:e|\xEA)ncia|Ref\.?)\s*:\s*"
    r"(?P<value>.{1,240}?)"
    r"(?=(?:[;\n\r]|$|"
    r"\s+[A-Za-z\xC0-\xFF][A-Za-z\xC0-\xFF ()/]{1,35}:))",
    re.IGNORECASE,
)
_VOLTAGE_MODEL_RE = re.compile(
    r"\b(?P<voltage>110|127|220|240)\s*v(?:olts?)?\s*[-:]\s*"
    r"(?P<model>[A-Z0-9]+(?:[-/][A-Z0-9]+)*)",
    re.IGNORECASE,
)
_MARKETING_PREFIX_RE = re.compile(
    r"^(?:ADVANCED|AUTOMATICA|ENERGY|LAVAMAX|PETIT|TRADICIONAL|"
    r"TURBO|TWIN|WAVE|BCO|BRANC[OA]|PRET[OA]|CINZA|INOX|"
    r"TITANIO|TITANIUM)(?:[-/]?\d.*|[-/].*)$",
    re.IGNORECASE,
)
_SAMSUNG_SHORT_PATTERNS = (
    re.compile(r"^(DVG\d{2})(?=[A-Z0-9/-]|$)", re.IGNORECASE),
    re.compile(
        r"^((?:WW|WD|WF|WA)\d{2}[A-Z]{1,2})(?=[A-Z0-9/-]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"^(DV\d{2}[A-Z])(?=[A-Z0-9/-]|$)", re.IGNORECASE),
)


@dataclass(frozen=True)
class LdySkuResolution:
    sku: str
    short: str
    status_tokens: tuple


def normalize_ldy_sku(value):
    return clean_text(value).upper().strip(" \t\r\n.,;:()[]{}")


def is_valid_ldy_manufacturer_sku(value):
    sku = normalize_ldy_sku(value)
    if not sku or len(sku) > 40 or " " in sku:
        return False
    if not _MODEL_TOKEN_RE.fullmatch(sku):
        return False
    compact = re.sub(r"[-/]", "", sku)
    if (
        len(compact) < 4
        or not re.search(r"[A-Z]", compact)
        or not re.search(r"\d", compact)
    ):
        return False
    if is_appliance_spec_token(sku):
        return False
    if re.match(
        r"^\d+(?:[.,]\d+)?(?:KG|KGS|L|LT|LTS|LITROS?|V|VOLTS?)",
        compact,
    ):
        return False
    if re.search(
        r"(?:110|127|220|240)V"
        r"(?:BRANC[OA]|PRET[OA]|CINZA|INOX|TITANIO|TITANIUM)?$",
        compact,
    ):
        return False
    if _MARKETING_PREFIX_RE.fullmatch(sku):
        return False
    return True


def ldy_title_sku_candidates(title):
    return _unique_valid_candidates(title)


def extract_product_source_evidence(description, spec_values, variant_text=""):
    evidence = []
    specs = spec_values if isinstance(spec_values, dict) else {}
    for raw_label, values in specs.items():
        label = _ascii_key(raw_label)
        if label not in {"modelo", "modelos", "referencia", "ref"}:
            continue
        for value in values if isinstance(values, list) else [values]:
            evidence.extend(
                _evidence_for_labeled_value(
                    value,
                    f"product_source_spec:{label}",
                    variant_text,
                )
            )

    for match in _DESCRIPTION_LABEL_RE.finditer(str(description or "")):
        label = _ascii_key(match.group("label")).replace(" ", "")
        evidence.extend(
            _evidence_for_labeled_value(
                match.group("value"),
                f"product_source_description:{label}",
                variant_text,
            )
        )
    return _dedupe_evidence(evidence)


def resolve_ldy_sku(existing_sku, title, evidence, brand=""):
    statuses = []
    existing = normalize_ldy_sku(existing_sku)
    existing_valid = existing if is_valid_ldy_manufacturer_sku(existing) else ""
    if existing and not existing_valid:
        statuses.append("casas_ldy_sku_invalid_existing_rejected")

    title_candidates = ldy_title_sku_candidates(title)
    if len(title_candidates) > 1:
        statuses.append("casas_ldy_sku_title_ambiguous")

    chosen = ""
    origin = ""
    if len(title_candidates) == 1:
        chosen = title_candidates[0]
        origin = "title"
        if existing_valid and existing_valid != chosen:
            statuses.append(_conflict_token("existing", existing_valid, chosen))
    elif existing_valid:
        chosen = existing_valid
        origin = "existing"

    evidence_values = _evidence_values(evidence)
    if chosen:
        if evidence_values and chosen not in evidence_values:
            statuses.append(
                _conflict_token("product_source", chosen, evidence_values[0])
            )
    elif len(evidence_values) == 1:
        chosen = evidence_values[0]
        origin = "product_source"
    elif len(evidence_values) > 1:
        statuses.append("casas_ldy_sku_product_source_ambiguous")

    if origin:
        statuses.append(f"casas_ldy_sku_{origin}_selected")
    else:
        statuses.append("casas_ldy_sku_unresolved")

    short = derive_samsung_short(chosen, brand=brand, title=title)
    if short:
        statuses.append(SHORT_DERIVED_TOKEN)
    return LdySkuResolution(chosen, short, tuple(dict.fromkeys(statuses)))


def casas_ldy_sku_for_output(row, item=""):
    row = row or {}
    status_tokens = set(str(row.get("parse_status") or "").split("+"))
    current = normalize_ldy_sku(row.get("sku", ""))
    if status_tokens & _RESOLVED_SELECTION_TOKENS:
        sku = current if is_valid_ldy_manufacturer_sku(current) else ""
    else:
        sku = resolve_ldy_sku(
            current,
            row.get("retailer_sku_name", ""),
            (),
            brand=row.get(BRAND_FIELD, ""),
        ).sku
    if (
        item
        and sku
        and sku.casefold() == str(item).strip().casefold()
    ):
        return ""
    return sku


def casas_ldy_short_for_output(row, resolved_sku):
    row = row or {}
    expected = samsung_short_family_from_sku(resolved_sku)
    if not expected:
        return ""
    derived = derive_samsung_short(
        resolved_sku,
        brand=row.get(BRAND_FIELD, ""),
        title=row.get("retailer_sku_name", ""),
    )
    if derived:
        return derived
    tokens = str(row.get("parse_status") or "").split("+")
    stored = normalize_ldy_sku(row.get("sku_short_version", ""))
    if SHORT_DERIVED_TOKEN in tokens and stored == expected:
        return expected
    return ""


def derive_samsung_short(sku, brand="", title=""):
    if "samsung" not in _ascii_key(f"{brand} {title}").split():
        return ""
    return samsung_short_family_from_sku(sku)


def samsung_short_family_from_sku(sku):
    normalized = normalize_ldy_sku(sku)
    if not is_valid_ldy_manufacturer_sku(normalized):
        return ""
    for pattern in _SAMSUNG_SHORT_PATTERNS:
        match = pattern.match(normalized)
        if match:
            return match.group(1).upper()
    return ""


def _unique_valid_candidates(text):
    values = []
    normalized = remove_accents(clean_text(text)).upper()
    for match in _MODEL_TOKEN_RE.finditer(normalized):
        candidate = normalize_ldy_sku(match.group(0))
        if (
            is_valid_ldy_manufacturer_sku(candidate)
            and candidate not in values
        ):
            values.append(candidate)
    return values


def _evidence_for_labeled_value(value, source, variant_text):
    raw = clean_text(value)
    if not raw:
        return []
    voltage = _single_voltage(variant_text)
    mapped = {}
    for match in _VOLTAGE_MODEL_RE.finditer(raw):
        candidate = normalize_ldy_sku(match.group("model"))
        if is_valid_ldy_manufacturer_sku(candidate):
            mapped.setdefault(match.group("voltage"), []).append(candidate)
    if voltage and len(set(mapped.get(voltage, []))) == 1:
        candidates = list(dict.fromkeys(mapped[voltage]))
    else:
        candidates = _unique_valid_candidates(raw)
    return [
        {"value": candidate, "source": source, "raw": raw[:240]}
        for candidate in candidates
    ]


def _single_voltage(text):
    values = set(
        re.findall(
            r"\b(110|127|220|240)\s*v(?:olts?)?\b",
            str(text or ""),
            re.IGNORECASE,
        )
    )
    return next(iter(values)) if len(values) == 1 else ""


def _evidence_values(evidence):
    values = []
    for entry in evidence or []:
        raw = entry.get("value", "") if isinstance(entry, dict) else entry
        value = normalize_ldy_sku(raw)
        if is_valid_ldy_manufacturer_sku(value) and value not in values:
            values.append(value)
    return values


def _dedupe_evidence(evidence):
    output = []
    seen = set()
    for entry in evidence:
        key = (entry.get("value", ""), entry.get("source", ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(entry)
    return output


def _conflict_token(source, left, right):
    left = re.sub(
        r"[^A-Z0-9/-]+",
        "_",
        normalize_ldy_sku(left),
    )[:40]
    right = re.sub(
        r"[^A-Z0-9/-]+",
        "_",
        normalize_ldy_sku(right),
    )[:40]
    return f"casas_ldy_sku_conflict:{source}:{left}!={right}"


def _ascii_key(value):
    text = unicodedata.normalize("NFKD", clean_text(value))
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        text.encode("ascii", "ignore").decode("ascii").lower(),
    ).strip()
