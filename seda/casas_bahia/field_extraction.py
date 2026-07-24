import re

from ..common.field_rules import (
    CAPACITY_QUALIFIER_PATTERN,
    clean_text,
    combine_capacity_distinct,
    combine_distinct,
    combine_measurement_distinct,
    extract_direct_labeled_energy_tokens,
    extract_ldy_capacity_from_title,
    extract_ref_capacity_components,
    extract_ref_capacity_from_title,
    extract_ref_capacity_scalar_values,
    extract_ref_title_capacity_components,
    filter_ref_capacity_exact_over_qualified_levels,
    is_energy_value,
    is_auxiliary_water_volume_context,
    is_negated_loading_context,
    is_ref_auxiliary_volume_context,
    is_ref_capacity_category_band,
    is_safe_tv_size_after_os_label,
    is_ldy_capacity_value,
    is_ref_capacity_value,
    is_screen_size_value,
    normalize_key,
    normalize_loading_type,
    sanitize_labeled_energy_target_value,
    select_ref_title_capacity_component,
    select_ldy_capacity_from_levels,
    trim_labeled_energy_suffix,
)


ENERGY_CANONICAL_LABELS = {"consumo de energia"}
ENERGY_ALIAS_LABELS = {
    "consumo aproximado de energia",
    "consumo maximo",
    "consumo medio",
    "consumo medio w",
    "consumo w",
    "consumo de energia w",
    "consumo kwh",
    "consumo energetico",
    "consumo em standby",
    "consumo no modo de espera",
    "consumo com tv ligada",
    "consumo de energia agua fria",
    "consumo de energia agua quente",
}

REF_LIQUID_TOTAL_LABELS = {"capacidade total liquida", "capacidade liquida total"}
REF_CANONICAL_TOTAL_LABELS = {
    "capacidade de armazenagem total l",
    "capacidade de armazenagem total",
}
REF_TOTAL_ALIAS_LABELS = {
    "capacidade total",
    "capacidade total de",
    "capacidade l",
    "capacidades",
}
REF_GENERIC_LABELS = {'capacidade'}
REF_REFRIGERATOR_LABELS = {
    "capacidade do refrigerador l",
    "capacidade do refrigerador",
    "capacidade de armazenagem do refrigerador",
    "capacidade de armazenagem do refrigerador l",
    "capacidade liquida do refrigerador",
    "capacidade da geladeira",
}
REF_FREEZER_LABELS = {
    "capacidade do freezer l",
    "capacidade do freezer",
    "capacidade de armazenagem do freezer",
    "capacidade de armazenagem do freezer l",
    "capacidade liquida do freezer",
    "capacidade do congelador",
    "capacidade do congelador l",
    "capacidade de armazenagem do congelador",
    "capacidade de armazenagem do congelador l",
}

LDY_CANONICAL_LABELS = {"capacidade kg de roupas"}
LDY_ALIAS_LABELS = {
    "capacidade de lavagem",
    "capacidade de lavagem kg",
    "capacidade maxima de lavagem",
    "capacidade total",
    "capacidade de lavar",
    "capacidade de roupa seca",
    "capacidade de roupas",
    "capacidade kg",
    "capacidade",
}

LOADING_CANONICAL_LABELS = {"acesso ao cesto"}
LOADING_ALIAS_LABELS = {
    "abertura da tampa",
    "abertura",
    "tipo de abertura",
    "tipo de carga",
    "tipo",
}

_REF_VALUE = (
    rf"(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s+a\s+\d+(?:[.,]\d+)?\s*(?:litros?|lts?|l)\b"
    rf"|(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*p[eé]s?\s*c[uú]bicos?(?:\s*\([^)]*(?:litros?|l\b)[^)]*\))?"
    rf"|(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*(?:litros?|lts?|ml|l|quartos?|quarts?)\b"
    rf"|(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*latas?(?:\s+de\s+\d+(?:[.,]\d+)?\s*ml)?\b"
    r"|\d+(?:[.,]\d+)?"
)
_LDY_VALUE = (
    rf"(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*(?:kgs?|kg\.?|quilos?|libras?|lbs?)\s*(?:a|[-–—])\s*"
    r"\d+(?:[.,]\d+)?\s*(?:kgs?|kg\.?|quilos?|libras?|lbs?)\b"
    rf"|(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?(?:\s+a\s+\d+(?:[.,]\d+)?)?"
    r"\s*(?:kgs?|kg\.?|quilos?|libras?|lbs?|litros?|lts?|l|ml|rpm|k?wh|kw|watts?|w)?\b"
)
_LDY_EXPLICIT_MASS_UNIT_RE = re.compile(
    r"(?:^|[^a-z])(?:kgs?|kg\.?|quilos?|libras?|lbs?)\b",
    re.I,
)
_LDY_EXACT_TITLE_MASS_RE = re.compile(
    r"(?:de\s+)?\d+(?:[.,]\d+)?\s*(?:kgs?|kg\.?|quilos?|libras?|lbs?)\b",
    re.I,
)
_LDY_TITLE_MASS_MENTION_RE = re.compile(
    r"(?<!\w)(?P<number>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>kgs?|kg\.?|quilos?|libras?|lbs?)\b",
    re.I,
)
_LDY_TITLE_VOLUME_RE = re.compile(
    rf"(?<!\w)(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?"
    r"(?:\s+a\s+\d+(?:[.,]\d+)?)?\s*(?:litros?|lts?|l)\b",
    re.I,
)
_LDY_EXACT_TITLE_VOLUME_RE = re.compile(
    r"(?:de\s+)?\d+(?:[.,]\d+)?\s*(?:litros?|lts?|l)\b",
    re.I,
)
_LDY_TITLE_VOLUME_MENTION_RE = re.compile(
    r"(?<!\w)(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>litros?|lts?|l)\b",
    re.I,
)
_LDY_COMPACT_VOLUME_TITLE_RE = re.compile(
    r"\b(?:mini|portatil|dobravel|tanquinho)\b",
    re.I,
)
_REF_TITLE_VOLUME_MENTION_RE = re.compile(
    r"(?<!\w)(?P<number>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>p[eé]s?\s*c[uú]bicos?|litros?|lts?|ml|l|quartos?|quarts?)\b",
    re.I,
)
_REF_TITLE_CONTAINER_RE = re.compile(
    r"(?<!\w)\d+(?:[.,]\d+)?\s*(?:latas?|garrafas?|unidades?|recipientes?)\b"
    r"(?:"
    r"\s+(?:de|com|x)\s*(?:\(\s*)?\d+(?:[.,]\d+)?\s*"
    r"(?:litros?|lts?|ml|l|quartos?|quarts?)\b\s*\)?"
    r"|\s*\(\s*\d+(?:[.,]\d+)?\s*"
    r"(?:litros?|lts?|ml|l|quartos?|quarts?)\b\s*\)"
    r"|\s*[/;:,+&\-\u2013\u2014]\s*(?:\(\s*)?"
    r"(?:\d+(?:[.,]\d+)?\s*ml|[0-9](?:[.,]\d+)?\s*(?:litros?|lts?|l))\b\s*\)?"
    r"|\s+(?:\d+(?:[.,]\d+)?\s*ml|0[.,]\d+\s*(?:litros?|lts?|l))\b"
    r")",
    re.I,
)
_REF_TITLE_UNCOUNTED_CONTAINER_RE = re.compile(
    r"(?<!\w)(?:latas?|garrafas?|unidades?|recipientes?)\b"
    r"\s+(?:de|com|x)\s*(?:\(\s*)?"
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:litros?|lts?|ml|l|quartos?|quarts?)\b\s*\)?",
    re.I,
)
_TV_EXPLICIT_SCREEN_RE = re.compile(
    r"(?<![\w.,])(?P<number>\d{2,3}(?:[.,]\d+)?)\s*"
    r"(?P<unit>polegadas?\b|pol\.?(?=\s|$)|inches?\b|in\b|[\"”″]|'')",
    re.I,
)
_TV_MULTIPLE_SCREEN_RE = re.compile(
    r"(?<!\d)\d{2,3}(?:[.,]\d+)?\s*"
    r"(?:polegadas?|pol\.?|inches?|in|[\"”″]|'')?\s*"
    r"(?:a|at[eé]|e|ou|[-–—/~])\s*"
    r"\d{2,3}(?:[.,]\d+)?\s*"
    r"(?:polegadas?|pol\.?|inches?|in|[\"”″]|'')",
    re.I,
)
_TV_DISPLAY_CONTEXT_RE = re.compile(
    r"\b(?:smart\s*tv|tv|televisor|qled|oled|crystal\s+uhd)\b",
    re.I,
)
_TV_CONTEXT_NUMBER_RE = re.compile(
    r"(?<![\w/])(?P<number>\d{2,3})(?![\w/%])",
    re.I,
)
_TV_LEADING_DISPLAY_PANEL_PRODUCT_RE = re.compile(
    r"^\s*(?:painel|pain[eé]is)\s+"
    r"(?:(?:mini\s+)?led|oled|qled|lcd|va|ips)"
    r"(?:\s+(?!(?:para|de|racks?|suportes?|parede|sala)\b)[^\W_]+){0,3}\s+"
    r"\d{2,3}(?:[.,]\d+)?\s*"
    r"(?:polegadas?|pol\.?|inches?|in|[\"”″]|'')?\s+"
    r"(?:smart\s*)?tv\b",
    re.I,
)
_LDY_TITLE_LOADING_RE = re.compile(
    r"\b(?:top[\s-]+load(?:ing|er)?|front[\s-]+load(?:ing|er)?|"
    r"carga\s+(?:superior|frontal)|"
    r"abertura(?:\s+da\s+tampa)?\s+(?:superior|frontal)|"
    r"(?:m[aá]quina\s+de\s+lavar|lavadora|lava\s+e\s+seca)\s+frontal)\b",
    re.I,
)
_ENERGY_TOKEN_RE = re.compile(
    r"(?:abaixo\s+de\s+|aprox(?:imadamente)?\.?\s*|[<>]\s*)?\d+(?:[.,]\d+)?\s*"
    r"(?:kwh|wh|kw|watts?|w)(?:\s*/\s*(?:ano|m[eê]s|ciclo|hora|dia))?",
    re.I,
)


def extract_fields(specs, title, description, line, allow_title_fallback=True):
    line = clean_text(line).upper()
    product_title = is_product_title_for_line(title, line)
    fields = {
        "estimated_annual_electricity_use": _energy_use(specs, description)
        if product_title
        else ""
    }
    if line == "REF":
        fields["ref_capacity"] = (
            _ref_capacity(specs, title, description, allow_title_fallback=allow_title_fallback)
            if product_title
            else ""
        )
    if line == "LDY":
        if product_title:
            fields["ldy_capacity"] = _ldy_capacity(
                specs,
                title,
                description,
                allow_title_fallback=allow_title_fallback,
            )
            fields["ldy_loading_type"] = _ldy_loading_type(
                specs,
                title,
                description,
                allow_title_fallback=allow_title_fallback,
            )
        else:
            fields["ldy_capacity"] = ""
            fields["ldy_loading_type"] = ""
    return fields


def extract_fields_by_sources(specs, title, descriptions, line):
    """Extract HTML fallback fields while preserving source priority."""
    descriptions = list(descriptions or [])
    source_fields = [
        extract_fields(specs, title, "", line, allow_title_fallback=False)
    ]
    for description in descriptions or []:
        if clean_text(description):
            source_fields.append(
                extract_fields({}, title, description, line, allow_title_fallback=False)
            )
    source_fields.append(extract_fields({}, title, "", line, allow_title_fallback=True))

    names = ["estimated_annual_electricity_use"]
    normalized_line = clean_text(line).upper()
    if normalized_line == "REF":
        names.append("ref_capacity")
    elif normalized_line == "LDY":
        names.extend(("ldy_capacity", "ldy_loading_type"))

    merged = {}
    combined_description = '; '.join(
        clean_text(description)
        for description in descriptions or []
        if clean_text(description)
    )
    global_ref_capacity = ''
    if normalized_line == 'REF' and _is_ref_title(title):
        global_ref_capacity = _ref_capacity(
            specs,
            title,
            combined_description,
            allow_title_fallback=True,
        )
    global_ldy_capacity = ''
    if normalized_line == 'LDY' and _is_ldy_title(title):
        global_ldy_capacity = _ldy_capacity(
            specs,
            title,
            combined_description,
            allow_title_fallback=True,
        )
    global_ldy_loading_type = ''
    if normalized_line == 'LDY' and _is_ldy_title(title):
        global_ldy_loading_type = _ldy_loading_type(
            specs,
            title,
            combined_description,
            allow_title_fallback=True,
        )
    for name in names:
        candidates = [
            clean_text(fields.get(name))
            for fields in source_fields
            if clean_text(fields.get(name))
        ]
        if name == 'ref_capacity':
            merged[name] = global_ref_capacity
        elif name == 'ldy_capacity':
            merged[name] = global_ldy_capacity
        elif name == 'ldy_loading_type':
            merged[name] = global_ldy_loading_type
        else:
            merged[name] = candidates[0] if candidates else ""
    return merged


def _first_level(levels):
    for values in levels:
        selected = combine_distinct(values)
        if selected:
            return selected
    return ""


def _spec_candidates(specs, labels, validator=None, reject_components=False):
    wanted = {normalize_key(label) for label in labels}
    output = []
    for key, values in (specs or {}).items():
        if normalize_key(key) not in wanted:
            continue
        for value in values or []:
            text = clean_text(value)
            components = extract_ref_capacity_components(text) if reject_components else {}
            if reject_components and any(components.values()):
                continue
            if text and (validator is None or validator(text)):
                output.append(text)
    return output


def _energy_use(specs, description):
    levels = (
        _sanitize_labeled_energy(_spec_candidates(specs, ENERGY_CANONICAL_LABELS)),
        _sanitize_labeled_energy(_spec_candidates(specs, ENERGY_ALIAS_LABELS)),
        _embedded_energy_spec_values(specs),
        _energy_from_description(description),
    )
    return _first_energy_level(levels)


def _first_energy_level(levels):
    for values in levels:
        selected = combine_measurement_distinct(values)
        if selected:
            return selected
    return ""


def _ref_capacity(specs, title, description, allow_title_fallback=True):
    exact_title_capacity = (
        _exact_ref_capacity_from_title(title) if allow_title_fallback else ""
    )
    if exact_title_capacity:
        return exact_title_capacity

    raw_levels = (
        _ref_spec_scalar_candidates(specs, REF_LIQUID_TOTAL_LABELS),
        _ref_description_values(description, "liquid_total"),
        _ref_spec_scalar_candidates(specs, REF_CANONICAL_TOTAL_LABELS),
        _ref_spec_scalar_candidates(specs, REF_TOTAL_ALIAS_LABELS),
        _ref_spec_component_values(specs, "total"),
        _ref_description_values(description, "total"),
        _ref_spec_scalar_candidates(specs, REF_GENERIC_LABELS),
        _ref_description_values(description, "generic"),
        _ref_spec_scalar_candidates(specs, REF_REFRIGERATOR_LABELS),
        _ref_spec_component_values(specs, "refrigerator"),
        _ref_description_values(description, "refrigerator"),
        _ref_spec_scalar_candidates(specs, REF_FREEZER_LABELS),
        _ref_spec_component_values(specs, "freezer"),
        _ref_description_values(description, "freezer"),
    )
    filtered_levels = filter_ref_capacity_exact_over_qualified_levels(raw_levels)
    partitioned_levels = [
        _split_ref_capacity_bands(values) for values in filtered_levels
    ]
    exact_levels = [exact_values for exact_values, _ in partitioned_levels]
    deferred_band_levels = [band_values for _, band_values in partitioned_levels]
    levels = (*exact_levels, *deferred_band_levels)
    selected = _first_capacity_level(levels)
    if selected:
        return selected
    if allow_title_fallback and _is_ref_title(title):
        return extract_ref_capacity_from_title(title)
    return ""


def _ref_spec_component_values(specs, kind):
    output = []
    for key, values in (specs or {}).items():
        if "capacidade" not in normalize_key(key):
            continue
        for value in values or []:
            output.extend(extract_ref_capacity_components(value).get(kind, []))
    return output


def _ref_spec_scalar_candidates(specs, labels):
    wanted = {normalize_key(label) for label in labels}
    output = []
    for key, values in (specs or {}).items():
        if normalize_key(key) not in wanted:
            continue
        for value in values or []:
            output.extend(extract_ref_capacity_scalar_values(value))
    return output


def _split_ref_capacity_bands(values):
    exact_values = []
    band_values = []
    for value in values or []:
        target = band_values if is_ref_capacity_category_band(value) else exact_values
        target.append(value)
    return exact_values, band_values


def _ldy_capacity(specs, title, description, allow_title_fallback=True):
    if _is_standalone_dryer_title(title):
        return ""
    exact_title_capacity = (
        _exact_ldy_capacity_from_title(title) if allow_title_fallback else ""
    )
    if exact_title_capacity:
        return exact_title_capacity

    levels = [
        _spec_candidates(specs, LDY_CANONICAL_LABELS, is_ldy_capacity_value),
        _spec_candidates(specs, LDY_ALIAS_LABELS, is_ldy_capacity_value),
        _ldy_description_values(description, canonical=True),
        _ldy_description_values(description, canonical=False),
    ]
    title_capacity = _safe_ldy_capacity_from_title(title) if allow_title_fallback else ""
    if title_capacity:
        levels.append([title_capacity])
    selected = select_ldy_capacity_from_levels(levels)
    if _is_incomplete_ldy_capacity(selected):
        for values in levels:
            for candidate in values:
                if (
                    _has_explicit_ldy_mass_unit(candidate)
                    and _same_ldy_capacity_numbers(selected, candidate)
                ):
                    return candidate
    return selected


def _ldy_loading_type(specs, title, description, allow_title_fallback=True):
    exact_title_loading = (
        _exact_ldy_loading_type_from_title(title) if allow_title_fallback else ""
    )
    if exact_title_loading:
        return exact_title_loading

    levels = (
        _normalized_official_loading(
            _spec_candidates(specs, LOADING_CANONICAL_LABELS)
        ),
        _normalized_official_loading(
            _spec_candidates(specs, LOADING_ALIAS_LABELS)
        ),
        _loading_from_description(description),
    )
    selected = _first_level(levels)
    if selected:
        return selected
    if not allow_title_fallback:
        return ""
    return combine_distinct(_title_loading_values(title))


def _embedded_energy_spec_values(specs):
    output = []
    for values in (specs or {}).values():
        for value in values or []:
            output.extend(_energy_from_description(value))
    return output


def _sanitize_labeled_energy(values):
    output = []
    for value in values:
        compact = sanitize_labeled_energy_target_value(value)
        if compact:
            output.append(compact)
    return output


def _compact_energy_value(value, allow_numeric=False):
    text = _trim_next_label(clean_text(value), allow_numeric=allow_numeric).rstrip(" ,;|")
    if allow_numeric and re.fullmatch(
        r'(?:[<>]\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*\(\s*(?:kwh|wh|kw|watts?|w)'
        r'(?:\s*/\s*(?:ano|m[eê]s|ciclo|hora|dia))?\s*\))',
        text,
        re.I,
    ):
        return text
    if allow_numeric:
        numeric = text[:-1] if re.fullmatch(r"\d+(?:[.,]\d+)?\.", text) else text
        if re.fullmatch(r"\d+(?:[.,]\d+)?", numeric):
            return numeric
    if not is_energy_value(text):
        return ""
    first_energy = _ENERGY_TOKEN_RE.search(text)
    if first_energy and first_energy.start():
        prefix = normalize_key(text[: first_energy.start()])
        if re.search(
            r"\b(?:entradas?|memoria|codigo|motor|sistema|potencia|voltagem|tensao|capacidade|"
            r"dimensoes|peso|frequencia|cor|modelo|marca|garantia|conectividade|classificacao)\b",
            prefix,
        ):
            return ""
    key = normalize_key(text)
    if len(text) > 100 or re.search(
        r'\b(?:controle de temperatura|frequencia|peso|voltagem|tensao|bivolt|volts?|cor|dimensoes|capacidade|agua)\b'
        r'|(?:^|\s)\d+(?:[.,]\d+)?\s*v(?:\b|/)',
        key,
    ):
        match = _ENERGY_TOKEN_RE.search(text)
        return clean_text(match.group(0)) if match else ""
    return text


def _first_capacity_level(levels):
    for values in levels:
        selected = combine_capacity_distinct(values)
        if selected:
            return selected
    return ""


def _energy_from_description(text):
    matches = []
    for match in re.finditer(
        r'\b(?P<label>consumo[^:;]{0,60}|stand\s*by[^:;]{0,30}|standby[^:;]{0,30})\s*[:=-]\s*'
        r'(?P<value>[^;|]{1,100}?)(?=\s+(?:-\s*)?(?:consumo|stand\s*by|standby)[^:;]{0,60}\s*[:=]|[;|]|$)',
        clean_text(text),
        re.I,
    ):
        label = normalize_key(match.group("label"))
        if re.search(r"\bconsumo(?:\s+aproximado)?\s+de\s+agua\b", label):
            continue
        if re.search(
            r"\b(?:potencia|tensao|voltagem|alimentacao|padrao|modelo|marca|cor|"
            r"capacidade|dimensoes|peso|frequencia)\b",
            label,
        ):
            continue
        value = _compact_energy_value(match.group("value"), allow_numeric=True)
        if value:
            matches.append((match.start(), value))
    existing_starts = {position for position, _ in matches}
    for position, raw_value in extract_direct_labeled_energy_tokens(text):
        if position in existing_starts:
            continue
        value = _compact_energy_value(raw_value, allow_numeric=False)
        if value:
            matches.append((position, value))
    for match in re.finditer(
        r'\bconsumo(?:\s+[^:;()]{0,40})?\s*\(\s*(?:kwh|wh|kw|watts?|w)'
        r'(?:\s*/\s*(?:ano|m[eê]s|ciclo|hora|dia))?\s*\)\s*'
        r'(?P<value>[<>]?\s*\d+(?:[.,]\d+)?)(?!\d|[.,]\d)(?!(?:\s*)(?:v|volts?)\b)',
        clean_text(text),
        re.I,
    ):
        value = _compact_energy_value(match.group('value'), allow_numeric=True)
        if value:
            matches.append((match.start(), value))
    if not matches:
        for match in re.finditer(r"\b(?:consumo|stand\s*by|standby)[^;()]{0,50}\(([^)]{1,80})\)", clean_text(text), re.I):
            value = _compact_energy_value(match.group(1), allow_numeric=False)
            if value:
                matches.append((match.start(), value))
    if not matches:
        match = re.search(
            r"\bbaixo\s+consumo(?:\s+de\s+energia)?\b(?!\s+de\s+[aá]gua\b)",
            clean_text(text),
            re.I,
        )
        if match:
            matches.append((match.start(), clean_text(match.group(0))))
    return [value for _, value in sorted(matches, key=lambda item: item[0])]


def _trim_next_label(value, allow_numeric=False):
    return trim_labeled_energy_suffix(value, allow_numeric=allow_numeric)


def _ref_description_values(text, kind):
    if not text:
        return []
    patterns = []
    if kind == "liquid_total":
        patterns = [
            rf"capacidade\s+(?:total\s+)?l[ií]quida(?:\s+total)?\s*(?:\(\s*l\s*\))?\s*(?:[:=]|de\s+)?\s*({_REF_VALUE})",
            rf"capacidade\s+l[ií]quida(?:(?!capacidade\s+bruta).){{0,220}}?\btotal\s*[:=]\s*({_REF_VALUE})",
        ]
    elif kind == "total":
        patterns = [
            rf"capacidade(?:\s+de\s+armazenagem)?\s+total(?:\s+bruta)?\s*(?:\(\s*l\s*\))?\s*(?:[:=]|de\s+)?\s*({_REF_VALUE})",
            rf"capacidade\s*\(\s*l\s*\)\s*[:=]\s*({_REF_VALUE})",
            rf"\b({_REF_VALUE})\s+de\s+capacidade\s+total\b",
            rf"\btotal\s+de\s+({_REF_VALUE})",
        ]
    elif kind == "generic":
        patterns = [
            rf"capacidade(?!\s+(?:do|da|total|l[ií]quida|bruta|freezer|refrigerador|de\s+(?:lavagem|armazenagem)))"
            rf"\s*(?:\(\s*l\s*\))?\s*(?:[:=]|\s+de)?\s*({_REF_VALUE})",
        ]
    elif kind == "refrigerator":
        patterns = [
            rf"capacidade(?:\s+de\s+armazenagem)?(?:\s+l[ií]quida)?\s+(?:do\s+)?(?:refrigerador|refrigeradora|geladeira)\s*(?:\(\s*l\s*\))?\s*[:=-]?\s*({_REF_VALUE})",
            rf"\b({_REF_VALUE})\s+(?:de|do)\s+(?:refrigerador|refrigeradora|geladeira)\b",
        ]
    elif kind == "freezer":
        patterns = [
            rf"capacidade(?:\s+de\s+armazenagem)?(?:\s+l[ií]quida)?\s+(?:do\s+)?(?:freezer|congelador)\s*(?:\(\s*l\s*\))?\s*[:=-]?\s*({_REF_VALUE})",
            rf"\b({_REF_VALUE})\s+(?:de|do)\s+(?:freezer|congelador)\b",
        ]
    output = []
    for pattern in patterns:
        for match in re.finditer(pattern, clean_text(text), re.I):
            value = clean_text(match.group(1))
            if is_ref_capacity_value(value):
                output.append((match.start(), value))
    return [value for _, value in sorted(output, key=lambda item: item[0])]


def _ldy_description_values(text, canonical):
    if canonical:
        label_patterns = (r"capacidade\s*\(\s*kg\s+de\s+roupas\s*\)", r"capacidade\s+kg\s+de\s+roupas")
    else:
        label_patterns = (
            r"capacidade\s+de\s+lavagem(?:\s*-\s*kg\s*-|\s*\(\s*kg\s*\))?",
            r"capacidade\s+m[aá]xima\s+de\s+lavagem",
            r"capacidade\s+total",
            r"capacidade\s+de\s+lavar",
            r"capacidade\s+de\s+roupa\s+seca",
            r"capacidade\s*-\s*kg\s*-",
            r"capacidade(?!\s+(?:de|total|m[aá]xima|kg))",
        )
    source = clean_text(text)
    output = []
    for label_pattern in label_patterns:
        for match in re.finditer(rf"\b{label_pattern}\s*(?:[:=-]\s*)?({_LDY_VALUE})", source, re.I):
            value = clean_text(match.group(1))
            if not _has_explicit_ldy_mass_unit(value):
                partial_unit = re.match(r"\s*k\b", source[match.end(1) :], re.I)
                if partial_unit:
                    value = clean_text(f"{value} {partial_unit.group(0).strip()}")
            if is_ldy_capacity_value(value):
                output.append((match.start(), value))
    ordered = sorted(output, key=lambda item: item[0])
    values = []
    for index, (_, value) in enumerate(ordered):
        if _is_incomplete_ldy_capacity(value) and any(
            _has_explicit_ldy_mass_unit(later_value)
            and _same_ldy_capacity_numbers(value, later_value)
            for _, later_value in ordered[index + 1 :]
        ):
            continue
        values.append(value)
    return values


def _has_explicit_ldy_mass_unit(value):
    return bool(_LDY_EXPLICIT_MASS_UNIT_RE.search(clean_text(value)))


def _is_incomplete_ldy_capacity(value):
    text = clean_text(value)
    key = normalize_key(text)
    return bool(text) and not _has_explicit_ldy_mass_unit(text) and (
        key.startswith("de ") or bool(re.search(r"\s+k\.?$", key))
    )


def _same_ldy_capacity_numbers(left, right):
    def numbers(value):
        output = []
        for token in re.findall(r"\d+(?:[.,]\d+)?", clean_text(value)):
            normalized = token.replace(",", ".")
            if "." in normalized:
                normalized = normalized.rstrip("0").rstrip(".")
            output.append(normalized.lstrip("0") or "0")
        return tuple(output)

    left_numbers = numbers(left)
    return bool(left_numbers) and left_numbers == numbers(right)


def _normalized_official_loading(values):
    output = []
    for value in values:
        normalized = normalize_loading_type(value)
        if not normalized and re.fullmatch(
            r"(?:porta\s+frontal|abertura\s+(?:pela\s+)?porta\s+frontal)",
            normalize_key(value),
        ):
            normalized = "Front load"
        if normalized:
            output.append(normalized)
    return output


def select_tv_title_screen_size(title):
    """Return a safe title-first TV size.

    A non-empty string is one unambiguous title size, None means the title
    contains multiple/qualified size candidates and must not be used as a
    fallback, and an empty string means no safe title-first candidate.
    """
    source = clean_text(title)
    if not is_tv_product_title(source):
        return ""
    range_spans = []
    for match in _TV_MULTIPLE_SCREEN_RE.finditer(source):
        if not _tv_screen_measurement_is_accessory(
            source,
            match.start(),
            match.end(),
        ):
            return None
        range_spans.append(match.span())

    candidates = []
    explicit_spans = []

    def add_candidate(number, raw):
        normalized = number.replace(",", ".")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        normalized = normalized.lstrip("0") or "0"
        candidates.append((normalized, clean_text(raw)))

    for match in _TV_EXPLICIT_SCREEN_RE.finditer(source):
        if any(
            start < match.end() and match.start() < end
            for start, end in range_spans
        ):
            continue
        if _tv_screen_measurement_is_accessory(
            source,
            match.start(),
            match.end(),
        ):
            explicit_spans.append((match.start(), match.end()))
            continue
        raw = clean_text(match.group(0))
        if not is_screen_size_value(raw):
            continue
        if _title_measurement_is_qualified(source, match):
            return None
        explicit_spans.append((match.start(), match.end()))
        add_candidate(match.group("number"), raw)

    display_matches = list(_TV_DISPLAY_CONTEXT_RE.finditer(source))
    display_matches.extend(re.finditer(r'\b(?:smart\s*)?tv(?=\d)', source, re.I))
    for display in display_matches:
        display_prefix = normalize_key(
            source[max(0, display.start() - 18) : display.start()]
        )
        if re.search(r"\bandroid\s*$", display_prefix):
            continue
        os_display = bool(re.search(r"\b(?:google|roku)\s*$", display_prefix))
        tail_start = display.end()
        tail = source[tail_start : tail_start + 90]
        for match in _TV_CONTEXT_NUMBER_RE.finditer(tail):
            absolute_span = (
                tail_start + match.start(),
                tail_start + match.end(),
            )
            if any(
                start < absolute_span[1] and absolute_span[0] < end
                for start, end in range_spans + explicit_spans
            ):
                continue
            if _tv_screen_measurement_is_accessory(
                source,
                absolute_span[0],
                absolute_span[1],
            ):
                continue
            suffix = normalize_key(tail[match.end() : match.end() + 16])
            if re.match(r'^(?:bits?|fps)\b', suffix):
                continue
            if re.match(
                r"^(?:hz|rpm|nits?|w|watts?|wh|kwh|kw|v|volts?|kg|cm|mm|"
                r"litros?|lts?|l|gb|mb|tb|k|anos?|mes(?:es)?|dias?|garantia)\b",
                suffix,
            ):
                continue
            prefix = normalize_key(tail[max(0, match.start() - 22) : match.start()])
            if re.search(
                r'\b(?:processador|atualizacao|memoria|armazenamento)\s*$',
                prefix,
            ):
                continue
            number = match.group("number")
            invalid_prefix = re.search(
                r"\b(?:webos|android(?:\s+tv)?|google\s+tv|roku\s+tv|"
                r"titan\s+os|vidaa|versao|geracao|ger|hdmi|usb|modelo|hdr)\s*$",
                prefix,
            )
            os_prefix = f"{display_prefix} tv" if os_display else prefix
            if (invalid_prefix or os_display) and not is_safe_tv_size_after_os_label(
                os_prefix, suffix, number
            ):
                continue
            if is_screen_size_value(number):
                add_candidate(number, f'{number}"')

    unique = {}
    for key, raw in candidates:
        unique.setdefault(key, raw)
    if len(unique) > 1:
        return None
    if not unique:
        return ""
    return next(iter(unique.values()))


def _tv_screen_measurement_is_accessory(title, start, end=None):
    source = clean_text(title)
    prefix = normalize_key(source[max(0, start - 90) : start])
    suffix = normalize_key(source[end : end + 60]) if end is not None else ""
    if re.search(
        r"\b(?:painel|paineis)\b.*"
        r"\b(?:para\s+sala|(?:de|para)\s+parede|racks?|suportes?)\b"
        r"(?:\s+[a-z0-9]+){0,4}\s*$",
        prefix,
    ):
        return True
    if re.search(r"\b(?:painel|paineis)\b.*$", prefix) and re.match(
        r"^(?:para\s+sala|(?:de|para)\s+parede|"
        r"(?:para\s+)?racks?|(?:(?:com|para)\s+)?suportes?)\b",
        suffix,
    ):
        return True
    if re.search(
        r"\b(?:painel|paineis)\s+"
        r"(?:(?:mini\s+)?led|oled|qled|lcd|va|ips)"
        r"(?:\s+[a-z0-9]+){0,4}\s*$",
        prefix,
    ):
        return False
    return bool(
        re.search(
            r'\b(?:suportes?|painel|paineis|racks?|bases?|pedestal|pedestais)\b'
            r'(?:\s+[a-z0-9]+){0,4}\s*$',
            prefix,
        )
        or re.search(
            r'\b(?:suportes?|painel|paineis|racks?|bases?|pedestal|pedestais)\b.*'
            r'\b(?:smart\s+)?tv(?:\s+de)?\s*$',
            prefix,
        )
    )


def _exact_ref_capacity_from_title(title):
    """Return one exact title volume; ranges and mixed volumes stay target-led."""
    source = clean_text(title)
    if not _is_ref_title(source):
        return ""
    title_components = extract_ref_title_capacity_components(source)
    component_kind, component_capacity = select_ref_title_capacity_component(source)
    if component_kind == "total":
        return component_capacity
    component_keys = {
        re.sub(r"[^a-z0-9]+", "", normalize_key(value))
        for values in title_components.values()
        for value in values
    }
    occupied_spans = [match.span() for match in _REF_TITLE_CONTAINER_RE.finditer(source)]
    occupied_spans.extend(
        match.span() for match in _REF_TITLE_UNCOUNTED_CONTAINER_RE.finditer(source)
    )
    matches = []
    for match in _REF_TITLE_VOLUME_MENTION_RE.finditer(source):
        if any(
            start < match.end() and match.start() < end
            for start, end in occupied_spans
        ):
            continue
        if _ref_title_volume_is_freezer_component(source, match):
            # Compartment volumes are ranked after a distinct headline/main
            # volume and are resolved by the component parser below.
            continue
        if is_ref_auxiliary_volume_context(source, match.start(), match.end()):
            continue
        raw = clean_text(match.group(0))
        if re.sub(r"[^a-z0-9]+", "", normalize_key(raw)) in component_keys:
            continue
        if is_ref_capacity_value(raw):
            matches.append(match)
    measurements = set()
    for match in matches:
        number = match.group("number").replace(",", ".")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        number = number.lstrip("0") or "0"
        unit = normalize_key(match.group("unit"))
        if unit in {"l", "lt", "lts", "litro", "litros"}:
            unit = "l"
        elif unit in {"quarto", "quartos", "quart", "quarts"}:
            unit = "quart"
        measurements.add((number, unit))
    if len(measurements) == 1 and not any(
        _title_measurement_is_qualified(source, match) for match in matches
    ):
        return clean_text(matches[0].group(0))
    if len(measurements) > 1:
        return ""
    if component_kind in {"refrigerator", "freezer"}:
        return component_capacity
    return ""


def _ref_title_volume_is_freezer_component(title, match):
    key = normalize_key(title)
    if not re.search(r"\b(?:geladeira|refrigerador|refrigeradora)\b", key):
        return False
    prefix = normalize_key(title[max(0, match.start() - 70) : match.start()])
    return bool(
        re.search(
            r"\b(?:freezer|congelador)\b"
            r"(?:\s+(?:com\s+)?capacidade)?(?:\s+de)?\s*$",
            prefix,
        )
    )


def _exact_ldy_loading_type_from_title(title):
    """Return one explicitly named title loading direction."""
    source = clean_text(title)
    if not _is_ldy_title(source):
        return ""
    values = list(dict.fromkeys(_title_loading_values(source)))
    return values[0] if len(values) == 1 else ""


def _title_loading_values(title):
    source = clean_text(title)
    values = []
    for match in _LDY_TITLE_LOADING_RE.finditer(source):
        if is_negated_loading_context(source, match.start()):
            continue
        normalized = normalize_loading_type(match.group(0))
        if normalized:
            values.append(normalized)
    return values


def _exact_ldy_capacity_from_title(title):
    """Return a single exact title capacity suitable for Casas-first priority."""
    source = clean_text(title)
    mass_mentions = [
        match
        for match in _LDY_TITLE_MASS_MENTION_RE.finditer(source)
        if not _ldy_title_mass_is_product_weight(source, match)
        and not _ldy_title_mass_is_drying_capacity(source, match)
    ]
    mass_capacity = clean_text(mass_mentions[0].group(0)) if mass_mentions else ""
    mass_measurements = _title_measurement_keys(
        " ".join(match.group(0) for match in mass_mentions),
        _LDY_TITLE_MASS_MENTION_RE,
    )
    if (
        mass_capacity
        and _LDY_EXACT_TITLE_MASS_RE.fullmatch(mass_capacity)
        and len(mass_measurements) == 1
        and not any(_title_measurement_is_qualified(source, match) for match in mass_mentions)
    ):
        return mass_capacity

    # Compact/portable washers are often sold by tub volume instead of clothes
    # mass. Keep this narrow so a full-size washer's water-volume wording is not
    # mistaken for its load capacity.
    if mass_measurements or not _LDY_COMPACT_VOLUME_TITLE_RE.search(normalize_key(source)):
        return ""
    match = _LDY_TITLE_VOLUME_RE.search(source)
    if not match:
        return ""
    volume_capacity = clean_text(match.group(0))
    volume_measurements = _title_measurement_keys(source, _LDY_TITLE_VOLUME_MENTION_RE)
    if (
        _LDY_EXACT_TITLE_VOLUME_RE.fullmatch(volume_capacity)
        and len(volume_measurements) == 1
    ):
        prefix = normalize_key(source[max(0, match.start() - 40) : match.start()])
        suffix = normalize_key(source[match.end() : match.end() + 20])
        if re.search(
            r"\b(?:agua|consumo|economia)(?:\s+de)?$|\breservatorio\b",
            prefix,
        ) or is_auxiliary_water_volume_context(
            source,
            match.start(),
            match.end(),
        ) or re.match(r"^(?:de\s+)?agua\b", suffix) or _title_measurement_is_qualified(
            source, match
        ):
            return ""
        volume = _LDY_TITLE_VOLUME_MENTION_RE.search(volume_capacity)
        return f'{volume.group("number")}L' if volume else ""
    return ""


def _ldy_title_mass_is_product_weight(title, match):
    prefix = normalize_key(title[max(0, match.start() - 50) : match.start()])
    suffix = normalize_key(title[match.end() : match.end() + 30])
    return bool(
        re.search(
            r"\b(?:peso(?:\s+(?:liquido|bruto|total))?"
            r"(?:\s+(?:do\s+produto|da\s+maquina))?|pesa)"
            r"(?:\s+de)?\s*$",
            prefix,
        )
        or re.match(r"^de\s+peso\b", suffix)
    )


def _ldy_title_mass_is_drying_capacity(title, match):
    prefix = normalize_key(title[max(0, match.start() - 60) : match.start()])
    suffix = normalize_key(title[match.end() : match.end() + 35])
    suffix_drying = re.match(
        r"^(?:(?:de|para)\s+)?(?:secagem|secar)\b",
        suffix,
    )
    suffix_binds_next_mass = re.match(
        r"^(?:secagem|secar)(?:\s+maxima)?(?:\s+de)?\s+"
        r"\d+(?:[.,]\d+)?\s*(?:kgs?|kg|quilos?|libras?|lbs?)\b",
        suffix,
    )
    return bool(
        re.search(
            r"\b(?:capacidade(?:(?:\s+(?:maxima\s+)?de)?\s+secagem|"
            r"(?:\s+maxima)?\s+para\s+secar)|secagem)"
            r"(?:\s+de)?\s*$",
            prefix,
        )
        or (
            re.search(r"\blava\s+e\s+seca\b.*\bseca\s*$", prefix)
            and not re.search(r"\broupas?\s+secas?\s*$", prefix)
        )
        or (suffix_drying and not suffix_binds_next_mass)
    )


def _safe_ldy_capacity_from_title(title):
    value = extract_ldy_capacity_from_title(title)
    if not value:
        return ""
    source = clean_text(title)
    for match in _LDY_TITLE_MASS_MENTION_RE.finditer(source):
        if (
            _same_ldy_capacity_numbers(value, match.group(0))
            and (
                _ldy_title_mass_is_product_weight(source, match)
                or _ldy_title_mass_is_drying_capacity(source, match)
            )
        ):
            return ""
    return value


def _title_measurement_keys(text, pattern):
    measurements = set()
    for match in pattern.finditer(clean_text(text)):
        number = match.group("number").replace(",", ".")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        number = number.lstrip("0") or "0"
        unit = normalize_key(match.group("unit"))
        if unit in {"kg", "kgs", "quilo", "quilos"}:
            unit = "kg"
        elif unit in {"libra", "libras", "lb", "lbs"}:
            unit = "lb"
        elif unit in {"l", "lt", "lts", "litro", "litros"}:
            unit = "l"
        measurements.add((number, unit))
    return measurements


def _title_measurement_is_qualified(text, match):
    raw_prefix = clean_text(text)[max(0, match.start() - 40) : match.start()]
    prefix = normalize_key(raw_prefix)
    suffix = normalize_key(clean_text(text)[match.end() : match.end() + 40])
    return bool(
        re.search(
            r"\b(?:acima|abaixo|ate|cerca|mais|menos|aprox|aproximadamente|"
            r"aproximado|aproximada|estimado|estimada)(?:\s+de)?$",
            prefix,
        )
        # In compact title ranges only the trailing number may carry the unit
        # (for example 11-15kg or 11/15kg). Treating that trailing token as
        # exact would incorrectly override structured targets.
        or re.search(r"\d+(?:[.,]\d+)?\s*[-\u2013\u2014/~]\s*$", raw_prefix)
        or re.search(
            r"\b(?:entre\s+|de\s+)?\d+(?:[.,]\d+)?\s+(?:a|e|ou|ate)\s*$",
            prefix,
        )
        or re.match(
            r"^(?:aprox|aproximadamente|aproximad[oa]s?|estimad[oa]s?|"
            r"cerca|mais\s+ou\s+menos|no\s+maximo|maxim[oa]s?)\b",
            suffix,
        )
    )


def _loading_from_description(text):
    output = []
    source = clean_text(text)
    patterns = (
        r"\b(?:acesso\s+ao\s+cesto|abertura(?:\s+da\s+tampa)?|tipo(?:\s+de\s+abertura)?|tipo\s+de\s+carga)\s*[:=-]?\s*"
        r"(superior|frontal|top\s+load(?:ing)?|front\s+load(?:ing)?)\b",
        r"\b(carga\s+superior|abertura\s+(?:superior|frontal))\b",
        r"\b(front\s+load(?:ing)?|top\s+load(?:ing)?)\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, source, re.I):
            if is_negated_loading_context(source, match.start(1)):
                continue
            normalized = normalize_loading_type(match.group(1))
            if normalized:
                output.append((match.start(), normalized))
    return [value for _, value in sorted(output, key=lambda item: item[0])]


def _is_ref_title(title):
    key = normalize_key(title)
    return _product_precedes_accessory(
        key,
        r'\b(?:geladeira|refrigerador|refrigeradora|freezer|frigobar|cervejeira|adega|cooler|minibar)\b',
        r'\b(?:sensor(?:es)?|suportes?|prateleiras?|gavetas?|portas?|placas?|pecas?|'
        r'organizador(?:es)?|marmitas?|motor(?:es)?|bases?|pes?|carrinhos?|recipientes?|dobradicas?|'
        r'almofadas?|restaurador(?:es)?|termostatos?|compressor(?:es)?|filtros?|puxador(?:es)?|'
        r'antivibracao|imas?|cestos?|resistencias?|cabos?|ventoinhas?|valvulas?|termistor(?:es)?|'
        r'painel\s+de\s+controle|formas?(?:\s+bandeja)?(?:\s+de)?\s+gelo|buchas?|lampadas?|emblemas?|'
        r'carro\s+para|reles?|gaxetas?|tampas?|moto\s+ventilador|restaura\s+desempenho|'
        r'unidade\s+refrigeradora)\b',
    )


def is_product_title_for_line(title, line):
    line = clean_text(line).upper()
    if line == "TV":
        return is_tv_product_title(title)
    if line == "REF":
        return _is_ref_title(title)
    if line == "LDY":
        return _is_ldy_title(title)
    return True


def is_tv_product_title(title):
    source = clean_text(title)
    key = normalize_key(source)
    if not key or re.search(
        r'\btv\s+box\b|\b(?:smart\s+)?tv\s+stick\b|\bstick\s+(?:smart\s+)?tv\b'
        r'|\bcontrole\s+r\s*tv\b',
        key,
    ):
        return False
    if _TV_LEADING_DISPLAY_PANEL_PRODUCT_RE.search(source):
        return True
    return _product_precedes_accessory(
        key,
        r'(?:\bsmart\s*tv(?=\d|\b)|\btv(?=\d|\b)|\b(?:televisor|qled|oled|crystal uhd)\b)',
        r'\b(?:suportes?|capas?|(?:controles?|controlos?)(?:\s+remotos?)?|antenas?|(?:painel|paineis)|racks?|bases?|'
        r'(?:pedestal|pedestais)|placas?|pecas?|cabos?|conversor(?:es)?|adaptador(?:es)?|fontes?|'
        r'barras?\s+de\s+led|displays?|'
        r'telas?\s+de\s+reposicao)\b|\bcr\b(?=\s+(?:para\s+)?(?:smart\s+)?tv\b)',
    )


# Backward-compatible private name for existing probes/tests.
_is_tv_title = is_tv_product_title


def _is_standalone_dryer_title(title):
    key = normalize_key(title)
    return bool(re.search(r"\bsecadora(?:\s+de\s+roupas?)?\b", key)) and not bool(
        re.search(r"\b(?:lava\s+e\s+seca|lavadora|maquina\s+de\s+lavar|tanquinho)\b", key)
    )


def _is_ldy_title(title):
    key = normalize_key(title)
    if re.search(
        r'\b(?:(?:lava(?:r|dora)?)(?:\s+de)?|maquina\s+de\s+lavar)\s+loucas?\b'
        r'|lavadora(?:\s+de)?\s+(?:alta\s+)?pressao'
        r'|\b(?:secadora|lavadora)\s+de\s+cabelo\b|\bescova\s+secadora\b',
        key,
    ):
        return False
    return _product_precedes_accessory(
        key,
        r'\b(?:lavadora|maquina de lavar|lava e seca|tanquinho|secadora)\b',
        r'\b(?:(?:painel|paineis)|placas?|tampas?|vidros?|(?:anel|aneis)|atuador(?:es)?|'
        r'interruptor(?:es)?|vedac(?:ao|oes)|tirantes?|travas?|capas?|suportes?|coletor(?:es)?|'
        r'desembaracador(?:es)?|rodinhas?|bases?|aparador(?:es)?|agitador(?:es)?|batedor(?:es)?|'
        r'tubos?|caixas?\s+mecanicas?|mecanismos?|motor(?:es)?|correias?|mangueiras?|filtros?|'
        r'dispensers?|puxador(?:es)?|almofadas?|limpador(?:es)?|refil|mop|pes?|'
        r'guarnic(?:ao|oes)|tanque|sensor\s+(?:termistor|temperatura)|ajustador(?:es)?|'
        r'prateleiras?|polias?|valvulas?|portas?)\b',
    )


def _product_precedes_accessory(key, product_pattern, accessory_pattern):
    product = re.search(product_pattern, key)
    if not product:
        return False
    accessory = re.search(accessory_pattern, key)
    return not accessory or product.start() < accessory.start()
