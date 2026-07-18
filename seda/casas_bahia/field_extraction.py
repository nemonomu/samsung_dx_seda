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
    is_energy_value,
    is_ldy_capacity_value,
    is_ref_capacity_value,
    normalize_key,
    normalize_exact_loading_direction,
    normalize_loading_type,
    sanitize_labeled_energy_target_value,
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
_ENERGY_TOKEN_RE = re.compile(
    r"(?:abaixo\s+de\s+|aprox(?:imadamente)?\.?\s*|[<>]\s*)?\d+(?:[.,]\d+)?\s*"
    r"(?:kwh|wh|kw|watts?|w)(?:\s*/\s*(?:ano|m[eê]s|ciclo|hora|dia))?",
    re.I,
)


def extract_fields(specs, title, description, line, allow_title_fallback=True):
    line = clean_text(line).upper()
    if line == "TV":
        product_title = is_tv_product_title(title)
    elif line == "REF":
        product_title = _is_ref_title(title)
    elif line == "LDY":
        product_title = _is_ldy_title(title)
    else:
        product_title = True
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
    levels = (
        _spec_candidates(
            specs,
            REF_LIQUID_TOTAL_LABELS,
            is_ref_capacity_value,
            reject_components=True,
        ),
        _ref_description_values(description, "liquid_total"),
        _spec_candidates(
            specs,
            REF_CANONICAL_TOTAL_LABELS,
            is_ref_capacity_value,
            reject_components=True,
        ),
        _spec_candidates(specs, REF_TOTAL_ALIAS_LABELS, is_ref_capacity_value, reject_components=True),
        _ref_spec_component_values(specs, "total"),
        _ref_description_values(description, "total"),
        _spec_candidates(specs, REF_GENERIC_LABELS, is_ref_capacity_value, reject_components=True),
        _ref_description_values(description, "generic"),
        _spec_candidates(
            specs,
            REF_REFRIGERATOR_LABELS,
            is_ref_capacity_value,
            reject_components=True,
        ),
        _ref_spec_component_values(specs, "refrigerator"),
        _ref_description_values(description, "refrigerator"),
        _spec_candidates(
            specs,
            REF_FREEZER_LABELS,
            is_ref_capacity_value,
            reject_components=True,
        ),
        _ref_spec_component_values(specs, "freezer"),
        _ref_description_values(description, "freezer"),
    )
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


def _ldy_capacity(specs, title, description, allow_title_fallback=True):
    levels = [
        _spec_candidates(specs, LDY_CANONICAL_LABELS, is_ldy_capacity_value),
        _spec_candidates(specs, LDY_ALIAS_LABELS, is_ldy_capacity_value),
        _ldy_description_values(description, canonical=True),
        _ldy_description_values(description, canonical=False),
    ]
    if allow_title_fallback:
        title_capacity = extract_ldy_capacity_from_title(title)
        if title_capacity:
            levels.append([title_capacity])
    return select_ldy_capacity_from_levels(levels)


def _ldy_loading_type(specs, title, description, allow_title_fallback=True):
    levels = (
        _normalized_loading(_spec_candidates(specs, LOADING_CANONICAL_LABELS)),
        _normalized_loading(_spec_candidates(specs, LOADING_ALIAS_LABELS)),
        _exact_loading_values(specs),
        _loading_from_description(description),
    )
    selected = _first_level(levels)
    if selected:
        return selected
    if not allow_title_fallback:
        return ""
    explicit = re.findall(
        r"(?:top\s+load(?:ing)?|front\s+load(?:ing)?|carga\s+superior|abertura\s+(?:superior|frontal))",
        clean_text(title),
        re.I,
    )
    return combine_distinct([normalize_loading_type(value) for value in explicit])


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
    output = []
    for label_pattern in label_patterns:
        for match in re.finditer(rf"\b{label_pattern}\s*(?:[:=-]\s*)?({_LDY_VALUE})", clean_text(text), re.I):
            value = clean_text(match.group(1))
            if is_ldy_capacity_value(value):
                output.append((match.start(), value))
    return [value for _, value in sorted(output, key=lambda item: item[0])]


def _normalized_loading(values):
    return [normalized for value in values for normalized in [normalize_loading_type(value)] if normalized]


def _exact_loading_values(specs):
    return [
        normalized
        for values in (specs or {}).values()
        for value in values or []
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
        for match in re.finditer(pattern, clean_text(text), re.I):
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


def is_tv_product_title(title):
    key = normalize_key(title)
    if not key or re.search(
        r'\btv\s+box\b|\b(?:smart\s+)?tv\s+stick\b|\bstick\s+(?:smart\s+)?tv\b'
        r'|\bcontrole\s+r\s*tv\b',
        key,
    ):
        return False
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
