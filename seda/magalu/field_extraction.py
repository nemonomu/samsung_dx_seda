import html
import re

from ..common.field_rules import (
    CAPACITY_QUALIFIER_PATTERN,
    canonicalize_ref_refrigerator_type,
    clean_text,
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
    normalize_exact_loading_direction,
    normalize_loading_type,
    ref_refrigerator_type_specificity as common_ref_refrigerator_type_specificity,
    sanitize_labeled_energy_target_value,
    select_ref_capacity_exact_over_qualified,
    select_ref_title_capacity_component,
    select_ldy_capacity_from_levels,
    trim_labeled_energy_suffix,
)


ENERGY_CANONICAL_LABELS = {"consumo aproximado de energia"}
ENERGY_ALIAS_LABELS = {
    "consumo maximo",
    "consumo de energia em funcionamento",
    "consumo de energia",
    "consumo mensal de energia",
    "consumo energetico",
    "consumo medio",
    "consumo medio w",
    "consumo w",
    "consumo de energia w",
    "consumo kwh",
    "consumo",
    "consumo em standby",
    "consumo no modo de espera",
    "consumo de energia agua fria",
    "consumo de energia agua quente",
}

REF_LIQUID_TOTAL_LABELS = {
    "capacidade liquida total",
    "capacidade total liquida",
}
REF_TOTAL_LABELS = {
    "capacidade de armazenagem total l",
    "capacidade de armazenagem total",
    "capacidade total",
    "capacidade total de",
    "capacidade l",
    "capacidades",
}
REF_GENERIC_LABELS = {'capacidade'}
REF_REFRIGERATOR_LABELS = {
    "capacidade do refrigerador l",
    "capacidade do refrigerador",
    "capacidade refrigerador",
    "capacidade liquida do refrigerador",
    "capacidade de armazenagem do refrigerador",
    "capacidade de armazenagem do refrigerador l",
    "capacidade da geladeira",
    "capacidade geladeira",
}
REF_FREEZER_LABELS = {
    "capacidade do freezer l",
    "capacidade do freezer",
    "capacidade freezer",
    "capacidade liquida do freezer",
    "capacidade de armazenagem do freezer",
    "capacidade de armazenagem do freezer l",
    "capacidade do congelador",
    "capacidade congelador",
    "capacidade do congelador l",
    "capacidade de armazenagem do congelador",
    "capacidade de armazenagem do congelador l",
}

LDY_CANONICAL_LABELS = {
    "capacidade de lavagem",
    "capacidade de lavagem kg",
    "capacidade kg de roupas",
}
LDY_ALIAS_LABELS = {
    "capacidade kg",
    "capacidade maxima de lavagem",
    "capacidade para",
    "capacidade de lavar",
    "capacidade da maquina de lavar",
    "capacidade de roupa seca",
    "capacidade total",
    "capacidade",
}

LOADING_CANONICAL_LABELS = {"acesso ao cesto"}
LOADING_ALIAS_LABELS = {
    "abertura da tampa",
    "abertura",
    "tipo de abertura",
    "tipo de abertura eletrodomestico",
    "tipo de abertura do eletrodomestico",
    "tipo de carga",
    "tipo",
}

SCREEN_LABELS = {"polegada", "polegadas", "tamanho da tela"}

_SCREEN_TITLE_UNIT = r'(?:polegadas?|pol\.?|inch(?:es)?|in\b|["\u2033])'
_SCREEN_TITLE_EXPLICIT_RE = re.compile(
    rf'(?<!\d)(?P<number>\d{{2,3}}(?:[.,]\d+)?)\s*{_SCREEN_TITLE_UNIT}',
    re.I,
)
_SCREEN_TITLE_RANGE_RE = re.compile(
    rf'(?<!\d)\d{{1,3}}(?:[.,]\d+)?\s*(?:{_SCREEN_TITLE_UNIT})?\s*'
    rf'(?:a|at[e\u00e9]|[-\u2013\u2014/])\s*\d{{1,3}}(?:[.,]\d+)?\s*{_SCREEN_TITLE_UNIT}',
    re.I,
)
_SCREEN_TITLE_DISPLAY_RE = re.compile(
    r'\b(?:(?:smart\s*)?tv|televisor|qled|oled|crystal\s+uhd)(?=\d|\b)',
    re.I,
)
_SCREEN_TITLE_IMPLICIT_RE = re.compile(
    r'(?<![\w/])(?P<number>\d{2,3})(?![\w/%])',
    re.I,
)
_SCREEN_TITLE_COMPACT_RE = re.compile(
    r'\b(?:smart\s*)?tv(?P<number>\d{2,3})(?![\w/%])',
    re.I,
)
_SCREEN_DISPLAY_PANEL_PREFIX_RE = re.compile(
    r'\b(?:painel|paineis)\s+(?:mini\s+led|oled|qled|led|lcd|va|ips)'
    r'(?P<descriptors>(?:\s+[a-z0-9]+){0,4})\s*$',
    re.I,
)
_SCREEN_TITLE_INVALID_SUFFIX_RE = re.compile(
    r'\s*(?:hz|rpm|nits?|w|watts?|wh|kwh|kw|v|volts?|kg|cm|mm|litros?|lts?|l|'
    r'bits?|fps|gb|mb|tb|anos?|mes(?:es)?|dias?|garantia)\b',
    re.I,
)
_SCREEN_TITLE_INVALID_PREFIX_RE = re.compile(
    r'\b(?:webos|android(?:\s+tv)?|google\s+tv|roku\s+tv|titan\s+os|vidaa|'
    r'versao|geracao|ger|hdmi|usb|modelo|hdr|processador|atualizacao|memoria|'
    r'armazenamento)\s*$',
    re.I,
)

_REF_TITLE_CUBIC_RE = re.compile(
    r'(?<!\w)(?P<number>\d+(?:[.,]\d+)?)\s*'
    r'(?P<unit>p[e\u00e9]s?\s+c[u\u00fa]bicos?)'
    r'(?:\s*\([^)]*(?:litros?|l\b)[^)]*\))?',
    re.I,
)
_REF_TITLE_VOLUME_RE = re.compile(
    r'(?<!\w)(?P<number>\d+(?:[.,]\d+)?)\s*'
    r'(?P<unit>litros?|lts?|ml|l|quartos?|quarts?)\b',
    re.I,
)
_REF_TITLE_CONTAINER_RE = re.compile(
    r'(?<!\w)\d+(?:[.,]\d+)?\s*(?:latas?|garrafas?|unidades?|recipientes?)\b'
    r'(?:'
    r'\s+(?:de|com|x)\s*(?:\(\s*)?\d+(?:[.,]\d+)?\s*'
    r'(?:litros?|lts?|ml|l|quartos?|quarts?)\b\s*\)?'
    r'|\s*[/;:,+&\-\u2013\u2014]\s*(?:\(\s*)?'
    r'(?:\d+(?:[.,]\d+)?\s*ml|'
    r'[0-9](?:[.,]\d+)?\s*(?:litros?|lts?|l))\b\s*\)?'
    r'|\s*(?:\(\s*)?'
    r'(?:\d+(?:[.,]\d+)?\s*ml|'
    r'(?:0[.,]\d+|1(?:[.,]\d+)?|2(?:[.,]0+)?)\s*(?:litros?|lts?|l))\b\s*\)?'
    r')',
    re.I,
)
_REF_TITLE_UNCOUNTED_CONTAINER_RE = re.compile(
    r'(?<!\w)(?:latas?|garrafas?|unidades?|recipientes?)\b'
    r'\s+(?:de|com|x)\s*(?:\(\s*)?'
    r'\d+(?:[.,]\d+)?\s*'
    r'(?:litros?|lts?|ml|l|quartos?|quarts?)\b\s*\)?',
    re.I,
)

_LDY_TITLE_MASS_MENTION_RE = re.compile(
    r'(?<!\w)(?P<number>\d+(?:[.,]\d+)?)\s*'
    r'(?P<unit>kgs?|kg\.?|quilos?|libras?|lbs?)\b',
    re.I,
)
_LDY_TITLE_VOLUME_MENTION_RE = re.compile(
    r'(?<!\w)(?P<number>\d+(?:[.,]\d+)?)\s*'
    r'(?P<unit>litros?|lts?|l)\b',
    re.I,
)
_LOADING_TITLE_RE = re.compile(
    r'(?:top[\s-]+load(?:ing|er)?|front[\s-]+load(?:ing|er)?|'
    r'carga\s+(?:superior|frontal)|'
    r'abertura(?:\s+da\s+tampa)?\s+(?:superior|frontal)|'
    r'(?:m[aá]quina\s+de\s+lavar|lavadora|lava\s+e\s+seca)\s+frontal)',
    re.I,
)

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
_LDY_COMPACT_VOLUME_VALUE_RE = re.compile(
    rf"(?<!\w)(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?"
    r"(?:\s*(?:a|[-\u2013\u2014/])\s*\d+(?:[.,]\d+)?)?"
    r"\s*(?:litros?|lts?|l)\b",
    re.I,
)
_LDY_COMPACT_CONTEXT_RE = re.compile(
    r"\b(?:mini|dobravel)\b",
    re.I,
)
_LDY_DESCRIPTION_COUNT_RE = re.compile(
    r"^(?:x\b|(?:x\s+)?(?:toalhas?|roupas?|pecas?|unidades?|itens?|item|pares?|meias?|"
    r"camisetas?|babadores?|camisas?|cuecas?|calcinhas?|programas?|ciclos?|cargas?)\b)",
    re.I,
)
_ENERGY_TOKEN_RE = re.compile(
    r"(?:abaixo\s+de\s+|aprox(?:imadamente)?\.?\s*|[<>]\s*)?\d+(?:[.,]\d+)?\s*"
    r"(?:kwh|wh|kw|watts?|w)(?:\s*/\s*(?:ano|m[eê]s|ciclo|hora|dia))?",
    re.I,
)


def extract_fields(item, line):
    line = clean_text(line).upper()
    title = clean_text(item.get("title")) if isinstance(item, dict) else ""
    if line == "TV":
        product_title = _is_tv_title(title)
    elif line == "REF":
        product_title = _is_ref_title(title)
    elif line == "LDY":
        product_title = _is_ldy_title(title)
    else:
        product_title = True
    fields = {
        "screen_size": _screen_size(item),
        "estimated_annual_electricity_use": _energy_use(item) if product_title else "",
    }
    if line == "REF":
        fields["ref_capacity"] = _ref_capacity(item) if product_title else ""
    if line == "LDY":
        fields["ldy_capacity"] = _ldy_capacity(item) if product_title else ""
        fields["ldy_loading_type"] = _ldy_loading_type(item) if product_title else ""
    return fields


def _screen_size(item):
    title = clean_text(item.get("title")) if isinstance(item, dict) else ""
    if not _is_tv_title(title):
        return ""
    title_size = _single_title_screen_size(title)
    if title_size:
        for compact_match in _SCREEN_TITLE_COMPACT_RE.finditer(title):
            if (
                _screen_title_measurement_is_accessory(
                    title,
                    compact_match.start("number"),
                    compact_match.end("number"),
                )
                or not _screen_value_matches_number(
                    title_size, compact_match.group("number")
                )
            ):
                continue
            structured_levels = (
                _attribute_candidates(item, SCREEN_LABELS, is_screen_size_value),
                _fact_candidates(item.get('factsheet') or [], SCREEN_LABELS, is_screen_size_value),
            )
            for values in structured_levels:
                structured = combine_measurement_distinct(values)
                if _screen_value_matches_number(structured, compact_match.group('number')):
                    return structured
        return title_size
    attributes = _attribute_candidates(item, SCREEN_LABELS, is_screen_size_value)
    if attributes:
        return combine_measurement_distinct(attributes)

    own = _fact_candidates(item.get("factsheet") or [], SCREEN_LABELS, is_screen_size_value)
    if own:
        return combine_measurement_distinct(own)
    bundled = []
    for bundle in item.get("bundles") or []:
        if isinstance(bundle, dict):
            bundled.extend(_fact_candidates(bundle.get("factsheet") or [], SCREEN_LABELS, is_screen_size_value))
    if bundled:
        return combine_measurement_distinct(bundled)
    return ""


def _energy_use(item):
    facts = _effective_facts(item)
    attributes = item.get("attributes") or [] if isinstance(item, dict) else []
    levels = (
        _sanitize_labeled_energy(
            _fact_candidates(facts, ENERGY_CANONICAL_LABELS)
            + _attribute_candidates({"attributes": attributes}, ENERGY_CANONICAL_LABELS)
        ),
        _sanitize_labeled_energy(
            _fact_candidates(facts, ENERGY_ALIAS_LABELS)
            + _attribute_candidates({"attributes": attributes}, ENERGY_ALIAS_LABELS)
        ),
        _energy_values_embedded_in_facts(facts),
        _energy_from_description(_description_text(item)),
    )
    return _first_measurement_level(levels)


def _ref_capacity(item):
    title = clean_text(item.get("title")) if isinstance(item, dict) else ""
    title_capacity = _single_title_ref_capacity(title)
    if title_capacity:
        return title_capacity
    facts = _effective_facts(item)
    text = _description_text(item)
    fact_components = _ref_components_from_values(_all_fact_values(facts))
    description_components = extract_ref_capacity_components(text)
    raw_levels = (
        _ref_scalar_fact_candidates(facts, REF_LIQUID_TOTAL_LABELS),
        _ref_description_values(text, "liquid_total"),
        _ref_scalar_fact_candidates(facts, REF_TOTAL_LABELS) + fact_components["total"],
        _ref_description_values(text, "total") + description_components["total"],
        _ref_scalar_fact_candidates(facts, REF_GENERIC_LABELS),
        _ref_description_values(text, "generic"),
        _ref_scalar_fact_candidates(facts, REF_REFRIGERATOR_LABELS)
        + fact_components["refrigerator"],
        _ref_description_values(text, "refrigerator") + description_components["refrigerator"],
        _ref_scalar_fact_candidates(facts, REF_FREEZER_LABELS) + fact_components["freezer"],
        _ref_description_values(text, "freezer") + description_components["freezer"],
    )
    raw_levels = filter_ref_capacity_exact_over_qualified_levels(raw_levels)
    partitioned_levels = [
        _split_ref_capacity_bands(values) for values in raw_levels
    ]
    exact_levels = [exact for exact, _ in partitioned_levels]
    band_levels = [bands for _, bands in partitioned_levels]
    levels = (*exact_levels, *band_levels)
    selected = _first_capacity_level(levels)
    if selected:
        return selected
    return _safe_ref_capacity_from_title(title) if _is_ref_title(title) else ""


def _ldy_capacity(item):
    title = clean_text(item.get('title')) if isinstance(item, dict) else ''
    if _is_standalone_dryer_title(title):
        return ""
    facts = _effective_facts(item)
    text = _description_text(item)
    exact_title_capacity = _single_title_ldy_capacity(item)
    if exact_title_capacity:
        return exact_title_capacity
    title_capacity = _safe_ldy_capacity_from_title(title) if _is_ldy_title(title) else ''
    allow_compact_volume = bool(
        not title_capacity and _is_compact_ldy_volume_item(item)
    )

    def validator(value):
        return _is_magalu_ldy_capacity_value(
            value,
            allow_compact_volume=allow_compact_volume,
        )

    levels = (
        _fact_candidates(facts, LDY_CANONICAL_LABELS, validator),
        _fact_candidates(facts, LDY_ALIAS_LABELS, validator),
        _ldy_description_values(text, LDY_CANONICAL_LABELS, validator=validator),
        _ldy_description_values(text, LDY_ALIAS_LABELS, validator=validator),
    )
    if not title_capacity and allow_compact_volume:
        title_capacity = _compact_ldy_volume_from_title(title)
    return select_ldy_capacity_from_levels(
        list(levels) + ([[title_capacity]] if title_capacity else [])
    )


def _ldy_loading_type(item):
    title = clean_text(item.get("title")) if isinstance(item, dict) else ""
    title_loading_type = _single_title_loading_type(title)
    if title_loading_type:
        return title_loading_type
    facts = _effective_facts(item)
    levels = (
        _normalized_loading_candidates(_fact_candidates(facts, LOADING_CANONICAL_LABELS)),
        _normalized_loading_candidates(_fact_candidates(facts, LOADING_ALIAS_LABELS)),
        _exact_loading_candidates(
            _all_fact_values(facts) + _all_attribute_values(item)
        ),
        _loading_from_description(_description_text(item)),
    )
    selected = _first_level(levels)
    if selected:
        return selected
    if not _is_ldy_title(title):
        return ""
    return combine_distinct(_title_loading_values(title))


def _single_title_screen_size(title):
    text = clean_text(title)
    if not _is_tv_title(text):
        return ""

    range_spans = [match.span() for match in _SCREEN_TITLE_RANGE_RE.finditer(text)]
    explicit_spans = []
    candidates = []
    for match in _SCREEN_TITLE_EXPLICIT_RE.finditer(text):
        if _span_overlaps_any(match.span(), range_spans):
            continue
        if _screen_title_measurement_is_accessory(
            text, match.start(), match.end()
        ):
            explicit_spans.append(match.span())
            continue
        if _title_measurement_is_qualified(text, match):
            return ''
        raw = clean_text(match.group(0))
        if not is_screen_size_value(raw):
            continue
        explicit_spans.append(match.span())
        candidates.append((match.start(), _normalized_number(match.group("number")), raw))

    display = _SCREEN_TITLE_DISPLAY_RE.search(text)
    if display:
        tail_start = display.end()
        tail = text[tail_start : tail_start + 100]
        for match in _SCREEN_TITLE_IMPLICIT_RE.finditer(tail):
            absolute_span = (tail_start + match.start(), tail_start + match.end())
            if _span_overlaps_any(absolute_span, range_spans + explicit_spans):
                continue
            if _screen_title_measurement_is_accessory(
                text, absolute_span[0], absolute_span[1]
            ):
                continue
            suffix = tail[match.end() : match.end() + 18]
            number = match.group("number")
            prefix = normalize_key(text[max(0, absolute_span[0] - 30) : absolute_span[0]])
            if _SCREEN_TITLE_INVALID_PREFIX_RE.search(prefix) and not is_safe_tv_size_after_os_label(
                prefix, suffix, number
            ):
                continue
            if _SCREEN_TITLE_INVALID_SUFFIX_RE.match(suffix):
                continue
            if not is_screen_size_value(number):
                continue
            candidates.append((absolute_span[0], _normalized_number(number), f'{number}"'))

    keys = {key for _, key, _ in candidates}
    if len(keys) != 1:
        return ""
    return min(candidates, key=lambda item: item[0])[2]


def _screen_value_matches_number(value, number):
    values = {
        _normalized_number(token)
        for token in re.findall(r'\d{2,3}(?:[.,]\d+)?', clean_text(value))
    }
    return values == {_normalized_number(number)}


def _screen_title_measurement_is_accessory(title, start, end=None):
    source = clean_text(title)
    prefix = normalize_key(source[max(0, start - 90) : start])
    suffix = normalize_key(source[end : end + 60]) if end is not None else ""
    panel_in_prefix = re.search(r'\b(?:painel|paineis)\b', prefix)
    if panel_in_prefix and re.search(
        r'\b(?:sala|parede|racks?|suportes?)\b',
        prefix,
    ):
        return True
    if panel_in_prefix and re.match(
        r'^(?:para\s+sala|(?:de|para)\s+parede|'
        r'(?:(?:com|para)\s+)?racks?|'
        r'(?:(?:com|para)\s+)?suportes?)\b',
        suffix,
    ):
        return True
    display_panel = _SCREEN_DISPLAY_PANEL_PREFIX_RE.search(prefix)
    if display_panel and not re.search(
        r'\b(?:sala|racks?|suportes?|parede)\b',
        display_panel.group("descriptors"),
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


def _single_title_ref_capacity(title):
    text = clean_text(title)
    if not _is_ref_title(text):
        return ""

    title_components = extract_ref_title_capacity_components(text)
    component_kind, component_capacity = select_ref_title_capacity_component(text)
    if component_kind == "total":
        return component_capacity
    component_keys = {
        re.sub(r"[^a-z0-9]+", "", normalize_key(value))
        for values in title_components.values()
        for value in values
    }

    candidates = []
    occupied_spans = [match.span() for match in _REF_TITLE_CONTAINER_RE.finditer(text)]
    occupied_spans.extend(
        match.span() for match in _REF_TITLE_UNCOUNTED_CONTAINER_RE.finditer(text)
    )
    for match in _REF_TITLE_CUBIC_RE.finditer(text):
        occupied_spans.append(match.span())
        raw = clean_text(match.group(0))
        if re.sub(r"[^a-z0-9]+", "", normalize_key(raw)) in component_keys:
            continue
        if (
            _title_measurement_is_qualified(text, match)
            or re.search(r"\baprox", normalize_key(raw))
            or is_ref_auxiliary_volume_context(
                text, match.start(), match.end()
            )
        ):
            continue
        if is_ref_capacity_value(raw):
            candidates.append((match.start(), _measurement_key(match), raw))
    for match in _REF_TITLE_VOLUME_RE.finditer(text):
        if _span_overlaps_any(match.span(), occupied_spans):
            continue
        if _ref_title_volume_is_freezer_component(text, match):
            continue
        if is_ref_auxiliary_volume_context(text, match.start(), match.end()):
            continue
        if _title_measurement_is_qualified(text, match):
            continue
        raw = clean_text(match.group(0))
        if re.sub(r"[^a-z0-9]+", "", normalize_key(raw)) in component_keys:
            continue
        if is_ref_capacity_value(raw):
            candidates.append((match.start(), _measurement_key(match), raw))

    keys = {key for _, key, _ in candidates}
    if len(keys) == 1:
        return min(candidates, key=lambda item: item[0])[2]
    if len(keys) > 1:
        return ""
    if component_kind in {"refrigerator", "freezer"}:
        return component_capacity
    return ""


def _safe_ref_capacity_from_title(title):
    text = clean_text(title)
    value = extract_ref_capacity_from_title(text)
    if not value:
        return ""
    value_match = _REF_TITLE_VOLUME_RE.fullmatch(clean_text(value))
    if not value_match:
        return value
    occupied_spans = [match.span() for match in _REF_TITLE_CONTAINER_RE.finditer(text)]
    occupied_spans.extend(
        match.span() for match in _REF_TITLE_UNCOUNTED_CONTAINER_RE.finditer(text)
    )
    for match in _REF_TITLE_VOLUME_RE.finditer(text):
        if (
            _measurement_key(match) == _measurement_key(value_match)
            and _span_overlaps_any(match.span(), occupied_spans)
        ):
            return ""
    return value


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


def _single_title_ldy_capacity(item):
    title = clean_text(item.get("title")) if isinstance(item, dict) else ""
    if not _is_ldy_title(title):
        return ""

    paired_capacity = _paired_title_ldy_washing_capacity(title)
    if paired_capacity is not None:
        return paired_capacity

    mass_matches = [
        match
        for match in _LDY_TITLE_MASS_MENTION_RE.finditer(title)
        if not _ldy_title_mass_is_product_weight(title, match)
        and not _ldy_title_mass_is_drying_capacity(title, match)
    ]
    mass = _single_exact_title_measurement(title, mass_matches)
    if mass:
        return mass
    if mass_matches or not _is_compact_ldy_volume_item(item):
        return ""

    volume_matches = list(_LDY_TITLE_VOLUME_MENTION_RE.finditer(title))
    volume = _single_exact_title_measurement(title, volume_matches)
    if not volume:
        return ""
    match = next(
        (candidate for candidate in volume_matches if clean_text(candidate.group(0)) == volume),
        None,
    )
    if not match:
        return ""
    prefix = normalize_key(title[max(0, match.start() - 80) : match.start()])
    suffix = normalize_key(title[match.end() : match.end() + 40])
    if (
        is_auxiliary_water_volume_context(title, match.start(), match.end())
        or re.search(r"\b(?:agua|consumo|economia|reservatorio)\b", prefix)
    ):
        return ""
    if re.match(r"^(?:de\s+)?agua\b", suffix):
        return ""
    return volume


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
    raw_prefix = clean_text(title)[max(0, match.start() - 60) : match.start()]
    prefix = normalize_key(raw_prefix)
    suffix = normalize_key(title[match.end() : match.end() + 35])
    if (
        re.search(r"\blavagem(?:\s+de)?\s*$", prefix)
        and not re.search(r"[/;:,+&\-\u2013\u2014]\s*$", raw_prefix)
    ):
        return False
    if re.search(
        r"\b(?:capacidade(?:(?:\s+(?:maxima\s+)?de)?\s+secagem|"
        r"(?:\s+maxima)?\s+para\s+secar)|secagem)"
        r"(?:\s+de)?\s*$",
        prefix,
    ):
        return True
    if (
        re.search(r"\blava\s+e\s+seca\b.*\bseca\s*$", prefix)
        and not re.search(r"\broupas?\s+secas?\s*$", prefix)
    ):
        return True
    suffix_label = re.match(
        r"^(?:(?:de|para)\s+)?(?:secagem|secar)\b",
        suffix,
    )
    if not suffix_label:
        return False
    following = suffix[suffix_label.end() :]
    return not re.match(
        r"^\s*\d+(?:[.,]\d+)?\s*(?:kgs?|kg|quilos?|libras?|lbs?)\b",
        following,
    )


def _paired_title_ldy_washing_capacity(title):
    """Return wash capacity from a conventional Lava e Seca Xkg/Ykg pair.

    ``None`` means the title has no direct two-value pair. An empty string
    means that a slash pair exists but cannot safely represent wash capacity.
    """
    text = clean_text(title)
    matches = list(_LDY_TITLE_MASS_MENTION_RE.finditer(text))
    if len(matches) != 2:
        return None
    first, second = matches
    separator = text[first.end() : second.start()]
    slash_pair = bool(re.fullmatch(r"\s*/\s*", separator))
    adjacent_pair = bool(re.fullmatch(r"\s+", separator))
    if not slash_pair and not adjacent_pair:
        return None
    if not re.search(r"\blava\s+e\s+seca\b", normalize_key(text)):
        return "" if slash_pair else None
    if any(_measurement_key(match)[1] != "kg" for match in matches):
        return ""
    if any(_title_measurement_is_qualified(text, match) for match in matches):
        return ""
    if any(_ldy_title_mass_is_product_weight(text, match) for match in matches):
        return ""
    if _ldy_title_mass_is_drying_capacity(text, first):
        return ""
    return clean_text(first.group(0))


def _safe_ldy_capacity_from_title(title):
    paired_capacity = _paired_title_ldy_washing_capacity(title)
    if paired_capacity is not None:
        return paired_capacity
    value = extract_ldy_capacity_from_title(title)
    if not value:
        return ""
    for match in _LDY_TITLE_MASS_MENTION_RE.finditer(clean_text(title)):
        if (
            normalize_key(match.group(0)) == normalize_key(value)
            and (
                _ldy_title_mass_is_product_weight(title, match)
                or _ldy_title_mass_is_drying_capacity(title, match)
            )
        ):
            return ""
    return value


def _single_exact_title_measurement(text, matches):
    if not matches or any(_title_measurement_is_qualified(text, match) for match in matches):
        return ""
    candidates = [
        (match.start(), _measurement_key(match), clean_text(match.group(0)))
        for match in matches
    ]
    keys = {key for _, key, _ in candidates}
    if len(keys) != 1:
        return ""
    return min(candidates, key=lambda item: item[0])[2]


def _single_title_loading_type(title):
    text = clean_text(title)
    if not _is_ldy_title(text):
        return ""
    values = list(dict.fromkeys(_title_loading_values(text)))
    return values[0] if len(values) == 1 else ""


def _title_loading_values(title):
    text = clean_text(title)
    values = []
    for match in _LOADING_TITLE_RE.finditer(text):
        if is_negated_loading_context(text, match.start()):
            continue
        normalized = normalize_loading_type(match.group(0))
        if normalized:
            values.append(normalized)
    return values


def _measurement_key(match):
    number = _normalized_number(match.group("number"))
    unit = normalize_key(match.group("unit"))
    if unit in {"kg", "kgs", "quilo", "quilos"}:
        unit = "kg"
    elif unit in {"libra", "libras", "lb", "lbs"}:
        unit = "lb"
    elif unit in {"l", "lt", "lts", "litro", "litros"}:
        unit = "l"
    elif unit in {"quarto", "quartos", "quart", "quarts"}:
        unit = "quart"
    elif "cubico" in unit:
        unit = "cubic"
    return number, unit


def _normalized_number(value):
    number = clean_text(value).replace(",", ".")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number.lstrip("0") or "0"


def _title_measurement_is_qualified(text, match):
    source = clean_text(text)
    raw_prefix = source[max(0, match.start() - 40) : match.start()]
    prefix = normalize_key(raw_prefix)
    suffix = normalize_key(source[match.end() : match.end() + 40])
    return bool(
        re.search(
            r"\b(?:acima|abaixo|ate|cerca|mais|menos|aprox|aproximadamente|"
            r"aproximado|aproximada|estimado|estimada)(?:\s+de)?$",
            prefix,
        )
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


def _span_overlaps_any(span, spans):
    return any(start < span[1] and span[0] < end for start, end in spans)


def _first_level(levels):
    for values in levels:
        selected = combine_distinct(values)
        if selected:
            return selected
    return ""


def _first_measurement_level(levels):
    for values in levels:
        selected = combine_measurement_distinct(values)
        if selected:
            return selected
    return ""


def _first_capacity_level(levels):
    for values in levels:
        selected = select_ref_capacity_exact_over_qualified(values)
        if selected:
            return selected
    return ""


def _split_ref_capacity_bands(values):
    exact = []
    bands = []
    for value in values or []:
        target = bands if is_ref_capacity_category_band(value) else exact
        target.append(value)
    return exact, bands


def _effective_facts(item):
    if not isinstance(item, dict):
        return []
    own = item.get("factsheet")
    if isinstance(own, list) and own:
        return own
    merged = []
    for bundle in item.get("bundles") or []:
        if isinstance(bundle, dict) and isinstance(bundle.get("factsheet"), list):
            merged.extend(bundle["factsheet"])
    return merged


def _iter_facts(facts):
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        if fact.get("keyName") or fact.get("slug"):
            yield fact
        yield from _iter_facts(fact.get("elements") or [])


def _fact_values(fact):
    values = []
    if clean_text(fact.get("value")):
        values.append(clean_text(fact.get("value")))
    for element in fact.get("elements") or []:
        if not isinstance(element, dict):
            continue
        # A labeled child is its own semantic fact. Promoting its value to the
        # parent makes a group such as Capacidades look like one total value.
        if element.get("keyName") or element.get("slug"):
            continue
        if clean_text(element.get("value")):
            values.append(clean_text(element.get("value")))
    return values


def _fact_candidates(facts, labels, validator=None):
    wanted = {normalize_key(label) for label in labels}
    output = []
    for fact in _iter_facts(facts):
        if normalize_key(fact.get("keyName") or fact.get("slug")) not in wanted:
            continue
        for value in _fact_values(fact):
            if validator is None or validator(value):
                output.append(value)
    return output


def _all_fact_values(facts):
    return [value for fact in _iter_facts(facts) for value in _fact_values(fact)]


def _all_attribute_values(item):
    output = []
    for attribute in item.get("attributes") or [] if isinstance(item, dict) else []:
        if not isinstance(attribute, dict):
            continue
        for value in (attribute.get("current"), attribute.get("value")):
            text = clean_text(value)
            if text:
                output.append(text)
    return output


def _ref_components_from_values(values):
    output = {"total": [], "refrigerator": [], "freezer": []}
    for value in values:
        components = extract_ref_capacity_components(value)
        for kind in output:
            output[kind].extend(components[kind])
    return output


def _ref_scalar_fact_candidates(facts, labels):
    output = []
    for value in _fact_candidates(facts, labels):
        components = extract_ref_capacity_components(value)
        if any(components.values()):
            continue
        output.extend(extract_ref_capacity_scalar_values(value))
    return output


def _attribute_candidates(item, labels, validator=None):
    wanted = {normalize_key(label) for label in labels}
    output = []
    for attribute in item.get("attributes") or [] if isinstance(item, dict) else []:
        if not isinstance(attribute, dict):
            continue
        if normalize_key(attribute.get("label") or attribute.get("type")) not in wanted:
            continue
        values = [attribute.get("current"), attribute.get("value")]
        for value in values:
            text = clean_text(value)
            if text and (validator is None or validator(text)):
                output.append(text)
    return output


def _description_text(item):
    raw = str(item.get("description") or "") if isinstance(item, dict) else ""
    raw = re.sub(
        r'</\s*(?:div|td|th)\s*>\s*<\s*(?:div|td|th)\b[^>]*>',
        ': ',
        raw,
        flags=re.I,
    )
    raw = re.sub(r"<(?=\s*\d)", " __SEDA_LT__", raw)
    raw = re.sub(r"<\s*br\s*/?\s*>", " ", raw, flags=re.I)
    raw = re.sub(
        r"<\s*/\s*(?:p|div|li|tr|td|th|h[1-6]|section|article|ul|ol|table)\s*>",
        "; ",
        raw,
        flags=re.I,
    )
    raw = re.sub(r"<[^>]+>", " ", raw)
    text = clean_text(html.unescape(raw)).replace('__SEDA_LT__', '<')
    return re.sub(r':\s*:\s*', ': ', text)


def _energy_values_embedded_in_facts(facts):
    output = []
    for fact in _iter_facts(facts):
        for value in _fact_values(fact):
            output.extend(_energy_from_description(value))
    return output


def _sanitize_labeled_energy(values):
    output = []
    for value in values:
        compact = sanitize_labeled_energy_value(value)
        if compact:
            output.append(compact)
    return output


def sanitize_labeled_energy_value(value):
    '''Apply the labeled-target energy contract to one raw DOM/API value.'''
    return sanitize_labeled_energy_target_value(value)


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
        r"\b(?:controle de temperatura|frequencia|peso|voltagem|tensao|bivolt|volts?|cor|dimensoes|capacidade|agua)\b"
        r"|(?:^|\s)\d+(?:[.,]\d+)?\s*v(?:\b|/)",
        key,
    ):
        match = _ENERGY_TOKEN_RE.search(text)
        return clean_text(match.group(0)) if match else ""
    return text


def _energy_from_description(text):
    matches = []
    for match in re.finditer(
        r'\b(?P<label>consumo[^:;]{0,60}|stand\s*by[^:;]{0,30}|standby[^:;]{0,30})\s*[:=-]\s*'
        r'(?P<value>[^;|]{1,100}?)(?=\s+(?:-\s*)?(?:consumo|stand\s*by|standby)[^:;]{0,60}\s*[:=]|[;|]|$)',
        text,
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
        r'(?P<value>[<>]?\s*\d+(?:[.,]\d+)?)(?!\d|[.,]\d|\s*(?:v|volts?)\b)',
        text,
        re.I,
    ):
        value = _compact_energy_value(match.group('value'), allow_numeric=True)
        if value:
            matches.append((match.start(), value))
    if not matches:
        for match in re.finditer(r"\b(?:consumo|stand\s*by|standby)[^;()]{0,50}\(([^)]{1,80})\)", text, re.I):
            value = _compact_energy_value(match.group(1), allow_numeric=False)
            if value:
                matches.append((match.start(), value))
    if not matches:
        for match in re.finditer(r"\bbaixo\s+consumo(?:\s+de\s+(?:energia|[aá]gua))?\b", text, re.I):
            value = clean_text(match.group(0))
            if is_energy_value(value):
                matches.append((match.start(), value))
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
        for match in re.finditer(pattern, text, re.I):
            value = clean_text(match.group(1))
            if kind == "generic":
                segment_start = text.rfind(";", 0, match.start()) + 1
                segment_end = text.find(";", match.end())
                if segment_end < 0:
                    segment_end = len(text)
                components = extract_ref_capacity_components(text[segment_start:segment_end])
                if components["refrigerator"] or components["freezer"]:
                    continue
            if is_ref_capacity_value(value):
                output.append((match.start(), value))
    return [value for _, value in sorted(output, key=lambda item: item[0])]


def _ldy_description_values(text, labels, validator=is_ldy_capacity_value):
    output = []
    for label in sorted(labels, key=len, reverse=True):
        words = [re.escape(word) for word in label.split()]
        label_pattern = r"\s+".join(words)
        for match in re.finditer(rf"\b{label_pattern}\b\s*(?:[:=-]\s*)?({_LDY_VALUE})", text, re.I):
            value = clean_text(match.group(1))
            if _is_ldy_description_count(text, match, value):
                continue
            if validator(value):
                output.append((match.start(), value))
    return [value for _, value in sorted(output, key=lambda item: item[0])]


def _is_magalu_ldy_capacity_value(value, allow_compact_volume=False):
    if is_ldy_capacity_value(value):
        return True
    return bool(
        allow_compact_volume
        and _LDY_COMPACT_VOLUME_VALUE_RE.fullmatch(clean_text(value))
    )


def _is_compact_ldy_volume_item(item):
    if not isinstance(item, dict):
        return False
    title = clean_text(item.get("title"))
    context_values = [clean_text(item.get("path"))]
    for field_name in ("category", "subcategory"):
        value = item.get(field_name)
        if isinstance(value, dict):
            context_values.extend(
                clean_text(value.get(key))
                for key in ("id", "name", "url")
                if clean_text(value.get(key))
            )
        elif clean_text(value):
            context_values.append(clean_text(value))
    context = normalize_key(" ".join(context_values))
    if re.search(r"\bmmlp\b|\bmini\s+(?:maquina\s+de\s+lavar|lavadora)\b", context):
        return True
    return bool(
        _is_ldy_title(title)
        and _LDY_COMPACT_CONTEXT_RE.search(normalize_key(title))
    )


def _compact_ldy_volume_from_title(title):
    source = clean_text(title)
    candidates = {}
    for match in _LDY_COMPACT_VOLUME_VALUE_RE.finditer(source):
        prefix = normalize_key(source[max(0, match.start() - 40) : match.start()])
        suffix = normalize_key(source[match.end() : match.end() + 24])
        if (
            is_auxiliary_water_volume_context(source, match.start(), match.end())
            or re.search(
                r"\b(?:agua|consumo|economia)(?:\s+de)?$",
                prefix,
            )
            or re.match(r"^(?:de\s+)?agua\b", suffix)
        ):
            continue
        raw = clean_text(match.group(0))
        key = re.sub(r"\s+", "", raw).casefold()
        candidates.setdefault(key, raw)
    return next(iter(candidates.values())) if len(candidates) == 1 else ""


def _is_ldy_description_count(text, match, value):
    if not re.fullmatch(r"\d+", clean_text(value)):
        return False
    suffix = normalize_key(text[match.end(1) : match.end(1) + 60])
    return bool(_LDY_DESCRIPTION_COUNT_RE.match(suffix))


def _normalized_loading_candidates(values):
    return [
        normalized
        for value in values
        for normalized in [_normalize_labeled_loading_value(value)]
        if normalized
    ]


def _normalize_labeled_loading_value(value):
    key = normalize_key(value)
    if key in {"porta frontal", "abertura pela porta frontal"}:
        return "Front load"
    return normalize_loading_type(value)


def _exact_loading_candidates(values):
    return [
        normalized
        for value in values
        for normalized in [normalize_exact_loading_direction(value)]
        if normalized
    ]


def _loading_from_description(text):
    output = []
    patterns = (
        r"\b(?:acesso\s+ao\s+cesto|abertura(?:\s+da\s+tampa)?|tipo(?:\s+de\s+abertura)?|tipo\s+de\s+carga)\s*[:=-]?\s*"
        r"(superior|frontal|top\s+load(?:ing)?|front\s+load(?:ing)?)\b",
        r"\b(carga\s+superior|abertura\s+(?:superior|frontal))\b",
        r"\b(front\s+load(?:ing)?|top\s+load(?:ing)?)\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            if is_negated_loading_context(text, match.start(1)):
                continue
            normalized = normalize_loading_type(match.group(1))
            if normalized:
                output.append((match.start(), normalized))
    return [value for _, value in sorted(output, key=lambda item: item[0])]


def _title_has_main_display_panel_measurement(title):
    source = clean_text(title)
    key = normalize_key(source)
    if not re.search(
        r"\b(?:smart\s*)?tv(?=\d|\b)|\btelevisor\b",
        key,
    ):
        return False
    for match in _SCREEN_TITLE_EXPLICIT_RE.finditer(source):
        prefix = normalize_key(source[max(0, match.start() - 90) : match.start()])
        if not _SCREEN_DISPLAY_PANEL_PREFIX_RE.search(prefix):
            continue
        if _screen_title_measurement_is_accessory(
            source, match.start(), match.end()
        ):
            continue
        return True
    return False


def _is_tv_title(title):
    key = normalize_key(title)
    if not key:
        return False
    if re.search(
        r"\btv\s+box\b|\b(?:smart\s+)?tv\s+stick\b|\bstick\s+(?:smart\s+)?tv\b"
        r"|\bcontrole\s+r\s*tv\b",
        key,
    ):
        return False
    if _title_has_main_display_panel_measurement(title):
        return True
    return _product_precedes_accessory(
        key,
        r"(?:\bsmart\s*tv(?=\d|\b)|\btv(?=\d|\b)|\b(?:televisor|qled|oled|crystal uhd)\b)",
        r"\b(?:suportes?|capas?|(?:controles?|controlos?)(?:\s+remotos?)?|antenas?|(?:painel|paineis)|racks?|bases?|"
        r"(?:pedestal|pedestais)|placas?|pecas?|cabos?|conversor(?:es)?|adaptador(?:es)?|fontes?|"
        r"barras?\s+de\s+led|displays?|"
        r"telas?\s+de\s+reposicao)\b"
        r"|\bcr\b(?=\s+(?:para\s+)?(?:smart\s+)?tv\b)",
    )


def is_tv_product_title(title):
    """Expose the shared Magalu TV-vs-accessory title classification."""
    return _is_tv_title(title)


def ref_refrigerator_type_from_title(title):
    """Return one explicit refrigerator architecture stated in the title.

    This is deliberately narrower than a general title inference. It runs only
    for titles that pass the existing REF product/accessory guard and returns an
    empty value when no architecture is stated, allowing the producer's
    existing factsheet and door-count fallbacks to remain authoritative.
    """
    text = clean_text(title)
    if not text or not _is_ref_title(text):
        return ""
    return canonicalize_ref_refrigerator_type(text)


def ref_refrigerator_type_specificity(value):
    """Rank one explicit architecture without inferring from unrelated text."""
    return common_ref_refrigerator_type_specificity(value)


def select_ref_refrigerator_type(title_type, spec_types):
    """Select by semantic specificity, preferring title only on equal rank."""
    candidates = [(clean_text(title_type), True)]
    candidates.extend((clean_text(value), False) for value in spec_types or ())
    selected = ""
    selected_key = (0, 0)
    for value, is_title in candidates:
        canonical = canonicalize_ref_refrigerator_type(value)
        rank = ref_refrigerator_type_specificity(canonical)
        candidate_key = (rank, int(is_title))
        if canonical and candidate_key > selected_key:
            selected = canonical
            selected_key = candidate_key
    return selected


def _is_ref_title(title):
    key = normalize_key(title)
    return _product_precedes_accessory(
        key,
        r"\b(?:geladeira|refrigerador|refrigeradora|freezer|frigobar|cervejeira|adega|cooler|minibar)\b",
        r"\b(?:sensor(?:es)?|suportes?|prateleiras?|gavetas?|portas?|placas?|pecas?|"
        r"organizador(?:es)?|marmitas?|motor(?:es)?|bases?|pes?|carrinhos?|recipientes?|dobradicas?|"
        r"almofadas?|restaurador(?:es)?|termostatos?|compressor(?:es)?|filtros?|puxador(?:es)?|"
        r"antivibracao|imas?|cestos?|resistencias?|cabos?|ventoinhas?|valvulas?|termistor(?:es)?|"
        r"painel\s+de\s+controle|formas?(?:\s+bandeja)?(?:\s+de)?\s+gelo|buchas?|lampadas?|emblemas?|"
        r"carro\s+para|reles?|gaxetas?|tampas?|moto\s+ventilador|restaura\s+desempenho|"
        r"unidade\s+refrigeradora)\b",
    )


def _is_ldy_title(title):
    key = normalize_key(title)
    if re.search(
        r"\b(?:(?:lava(?:r|dora)?)(?:\s+de)?|maquina\s+de\s+lavar)\s+loucas?\b"
        r"|lavadora(?:\s+de)?\s+(?:alta\s+)?pressao"
        r"|\b(?:secadora|lavadora)\s+de\s+cabelo\b|\bescova\s+secadora\b",
        key,
    ):
        return False
    return _product_precedes_accessory(
        key,
        r"\b(?:lavadora|maquina de lavar|lava e seca|tanquinho|secadora)\b",
        r"\b(?:(?:painel|paineis)|placas?|tampas?|vidros?|(?:anel|aneis)|atuador(?:es)?|"
        r"interruptor(?:es)?|vedac(?:ao|oes)|tirantes?|travas?|capas?|suportes?|coletor(?:es)?|"
        r"desembaracador(?:es)?|rodinhas?|bases?|aparador(?:es)?|agitador(?:es)?|batedor(?:es)?|"
        r"tubos?|caixas?\s+mecanicas?|mecanismos?|motor(?:es)?|correias?|mangueiras?|filtros?|"
        r"dispensers?|puxador(?:es)?|almofadas?|limpador(?:es)?|refil|mop|pes?|"
        r"guarnic(?:ao|oes)|tanque|sensor\s+(?:termistor|temperatura)|ajustador(?:es)?|"
        r"prateleiras?|polias?|valvulas?|portas?)\b",
    )


def _is_standalone_dryer_title(title):
    key = normalize_key(title)
    return bool(
        re.search(r"\bsecadora\b", key)
        and not re.search(
            r"\b(?:lava\s+e\s+seca|lavadora|maquina\s+de\s+lavar)\b",
            key,
        )
    )


def _product_precedes_accessory(key, product_pattern, accessory_pattern):
    product = re.search(product_pattern, key)
    if not product:
        return False
    accessory = re.search(accessory_pattern, key)
    return not accessory or product.start() < accessory.start()
