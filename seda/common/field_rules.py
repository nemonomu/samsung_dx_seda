import html
import re
import unicodedata


def clean_text(value):
    """Return a compact display value without changing its units or notation."""
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_key(value):
    """Normalize labels/values for comparison while preserving source values."""
    text = unicodedata.normalize("NFKD", clean_text(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonicalize_ref_refrigerator_type(value):
    """Return one canonical refrigerator architecture from a raw value.

    Compound source values are resolved by the approved semantic order:
    named multi-door layouts, bottom-freezer layouts, top-freezer layouts,
    explicit 1/3/4 door counts, then Duplex/2-door.  The returned value is
    canonical so downstream translation cannot reinterpret the same raw text
    with a different priority.
    """
    key = normalize_key(value)
    if not key:
        return ""

    for pattern, canonical in (
        (r"\bside\s+by\s+side\b", "Side by Side"),
        (r"\bfrench(?:\s+door)?\b|\bporta\s+francesa\b", "French Door"),
        (r"\bmultidoor\b|\bmulti\s+door\b", "Multidoor"),
    ):
        if re.search(pattern, key):
            return canonical

    if re.search(
        r"\b(?:inverse|inverso)\b|\bbottom\s+freezer\b|"
        r"\bfreezer\s+(?:embaixo|inferior)\b",
        key,
    ):
        return "Inverse"
    if re.search(
        r"\btop\s+freezer\b|\bfreezer\s+(?:em\s+cima|superior)\b",
        key,
    ):
        return "Top Freezer"

    count_match = re.search(r"\b([134])\s*portas?\b", key)
    if count_match:
        count = count_match.group(1)
        return f"{count} porta" if count == "1" else f"{count} portas"
    for pattern, canonical in (
        (r"\buma\s+porta\b", "1 porta"),
        (r"\btres\s+portas\b", "3 portas"),
        (r"\bquatro\s+portas\b", "4 portas"),
    ):
        if re.search(pattern, key):
            return canonical

    if re.search(r"\bduplex\b", key):
        return "Duplex"
    if re.search(r"\b2\s*portas?\b|\bduas\s+portas\b", key):
        return "2 portas"
    return ""


def ref_refrigerator_type_specificity(value):
    canonical = canonicalize_ref_refrigerator_type(value)
    if canonical in {"Side by Side", "French Door", "Multidoor"}:
        return 5
    if canonical == "Inverse":
        return 4
    if canonical == "Top Freezer":
        return 3
    if canonical in {"1 porta", "3 portas", "4 portas"}:
        return 2
    if canonical in {"Duplex", "2 portas"}:
        return 1
    return 0


def _comparison_key(value):
    return normalize_key(value)


def collapse_duplicate_range(value):
    """Collapse malformed repeated values such as ``14-14`` to ``14``."""
    text = clean_text(value)
    parts = [clean_text(part) for part in re.split(r"\s*[-–—]\s*", text)]
    if len(parts) >= 2 and all(parts):
        keys = {_comparison_key(part) for part in parts}
        if len(keys) == 1:
            return parts[0]
    return text


def combine_distinct(values, separator=","):
    """Keep the first spelling of each value and preserve source order.

    Values are candidates, not a comma-delimited string.  This intentionally
    does not split strings because decimal commas are common in Portuguese.
    """
    output = []
    seen = set()
    for value in values or []:
        text = collapse_duplicate_range(value)
        key = _comparison_key(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return separator.join(output)


def combine_capacity_distinct(values, separator=","):
    """Deduplicate equivalent exact capacities while preserving the first raw form."""
    return combine_measurement_distinct(values, separator=separator)


def combine_measurement_distinct(values, separator=","):
    """Deduplicate exact measurements, treating a missing unit as a wildcard.

    A target can expose the same value both as ``14`` and ``14 kg`` (or ``55``
    and ``55 polegadas``). Those are equivalent within one field and the first
    raw spelling is retained. Explicitly different units are not conflated;
    for example, ``53 Quartos`` and ``53L`` remain distinct source values.
    """
    output = []
    seen_text = set()
    seen_unitless = set()
    seen_units = {}
    for value in values or []:
        text = collapse_duplicate_range(value)
        key = _measurement_comparison_key(text)
        if not text or not key:
            continue

        exact = _measurement_parts(text)
        if exact:
            number, unit = exact
            units = seen_units.setdefault(number, set())
            if number in seen_unitless:
                if not unit:
                    continue
                if not units:
                    # The first explicit unit corroborates the earlier bare
                    # value. Remember it so a genuinely different explicit
                    # unit with the same number is still preserved.
                    units.add(unit)
                    continue
                if unit in units:
                    continue
            elif (unit and unit in units) or (not unit and units):
                continue
            if unit:
                units.add(unit)
            else:
                seen_unitless.add(number)
        elif key in seen_text:
            continue

        seen_text.add(key)
        output.append(text)
    return separator.join(output)


def _measurement_comparison_key(value):
    '''Keep approximation/limit qualifiers distinct from an exact value.'''
    text = clean_text(value)
    qualifier = ''
    if re.match(r'^[<>~]', text):
        qualifier = text[0]
    else:
        match = re.match(
            r'^(abaixo\s+de|acima\s+de|ate|aprox(?:imadamente)?\.?)\b',
            normalize_key(text),
            re.I,
        )
        if match:
            qualifier = normalize_key(match.group(1))
    key = normalize_key(text)
    return f'{qualifier}:{key}' if qualifier else key


def _measurement_parts(value):
    exact = re.fullmatch(
        r'(?:de\s+)?(\d+(?:[.,]\d+)?)\s*'
        r'(kg\.?|kgs|quilos?|libras?|lbs?|ml|l|lts?|litros?|quartos?|quarts?|pol\.?|polegadas?|inches?|["”″]|kwh|wh|kw|watts?|w)?',
        clean_text(value),
        re.I,
    )
    if not exact:
        return None
    number = exact.group(1).replace(",", ".")
    number = number.rstrip("0").rstrip(".") if "." in number else number.lstrip("0") or "0"
    unit = normalize_key(exact.group(2))
    if unit in {"kg", "kgs", "quilo", "quilos"}:
        unit = "kg"
    elif unit in {"libra", "libras", "lb", "lbs"}:
        unit = "lb"
    elif unit in {"l", "lt", "lts", "litro", "litros"}:
        unit = "l"
    elif unit == "ml":
        unit = "ml"
    elif unit in {"quarto", "quartos", "quart", "quarts"}:
        unit = "quart"
    elif unit in {"pol", "polegada", "polegadas", "inch", "inches"} or exact.group(2) in {'"', "”", "″"}:
        unit = "inch"
    elif unit in {"w", "watt", "watts"}:
        unit = "w"
    return number, unit


CAPACITY_QUALIFIER_PATTERN = (
    r"(?:(?:acima|abaixo|menos|mais|cerca)\s+de\s+|at[eé]\s+|"
    r"aprox(?:imadamente)?\.?\s*|[<>~]\s*)"
)
_REF_CONTAINER_COUNT_PATTERN = r"(?:latas?|garrafas?|unidades?|recipientes?)"
_REF_CONTAINER_CONNECTOR_PATTERN = r"(?:\s+(?:de|com|x)|\s*[/;:,+&\-\u2013\u2014])"

_REF_COMPONENT_VALUE = (
    r"(?<!\w)(?:"
    rf"(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s+a\s+\d+(?:[.,]\d+)?\s*(?:litros?|lts?|l)\b"
    rf"|(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*p[eé]s?\s*c[uú]bicos?(?:\s*\([^)]*(?:litros?|l\b)[^)]*\))?"
    rf"|(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*(?:litros?|lts?|ml|l|quartos?|quarts?)\b"
    rf"|(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*latas?(?:\s+de\s+\d+(?:[.,]\d+)?\s*ml)?\b"
    r"|\d+(?:[.,]\d+)?(?!\d|[.,]\d)"
    r"(?!\s*(?:v|volts?|w|watts?|wh|kwh|kw|hz|rpm|kg|cm|mm|portas?)\b)"
    r")"
)


def extract_ref_capacity_components(value):
    """Extract labeled total/refrigerator/freezer capacities from mixed text.

    Raw source spelling and source order are retained. This is intended for
    values such as ``Freezer: 84 L; Refrigerador: 305 L`` where returning the
    entire mixed string would violate the compartment priority policy.
    """
    text = clean_text(value)
    output = {"total": [], "refrigerator": [], "freezer": []}
    if not text:
        return output

    labels = {
        "total": (
            r"(?:capacidade\s+(?:de\s+armazenagem\s+)?(?:total(?:\s+l[ií]quida)?|l[ií]quida\s+total)"
            r"|total\s+l[ií]quida|total)"
        ),
        "refrigerator": (
            r"(?:(?:capacidade(?:\s+de\s+armazenagem)?(?:\s+l[ií]quida)?\s+(?:do\s+)?)?"
            r"(?:refrigerador|refrigeradora|geladeira))"
        ),
        "freezer": (
            r"(?:(?:capacidade(?:\s+de\s+armazenagem)?(?:\s+l[ií]quida)?\s+(?:do\s+)?)?"
            r"(?:freezer|congelador))"
        ),
    }

    candidates = []
    for kind, label in labels.items():
        patterns = (
            (
                2,
                rf'\b{label}\b\s*(?:\(\s*l\s*\))?\s*(?::|=|-|de)?\s*'
                rf'(?P<value>{_REF_COMPONENT_VALUE})',
            ),
            (
                3,
                rf'(?P<value>{_REF_COMPONENT_VALUE})\s*(?:de|do|da)\s+\b{label}\b',
            ),
            (
                1,
                rf'(?P<value>{_REF_COMPONENT_VALUE})\s+\b{label}\b',
            ),
        )
        for priority, pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                if kind == 'total' and re.search(
                    r'\bpeso\s*$',
                    normalize_key(text[max(0, match.start() - 20) : match.start()]),
                ):
                    continue
                if is_ref_auxiliary_volume_context(
                    text,
                    match.start('value'),
                    match.end('value'),
                ):
                    continue
                raw = clean_text(match.group('value'))
                if is_ref_capacity_value(raw):
                    candidates.append(
                        (
                            match.start('value'),
                            match.end('value'),
                            priority,
                            match.start(),
                            kind,
                            raw,
                        )
                    )

    # A value can sit between two labels. Resolve ownership by syntax strength:
    # explicit reverse (value de/do/da label), then forward (label value), then
    # connectorless reverse (value label).
    winners = {}
    for candidate in candidates:
        span = candidate[:2]
        current = winners.get(span)
        if current is None or candidate[2] > current[2]:
            winners[span] = candidate

    seen = {kind: set() for kind in output}
    for _, _, _, _, kind, raw in sorted(winners.values(), key=lambda item: (item[0], item[3])):
        key = normalize_key(raw)
        if key and key not in seen[kind]:
            seen[kind].add(key)
            output[kind].append(raw)
    return output


_REF_TITLE_EXACT_COMPONENT_VALUE = (
    r"\d+(?:[.,]\d+)?\s*(?:litros?|lts?|ml|l|quartos?|quarts?)?\b"
)
_REF_TITLE_COMPARTMENT_LABEL = r"(?:freezer|congelador|refrigerador|refrigeradora)"


def extract_ref_title_capacity_components(value):
    """Extract title compartments without cross-pairing adjacent value/labels."""
    output, _ = _extract_ref_title_capacity_component_matches(value)
    return output


def _extract_ref_title_capacity_component_matches(value):
    """Return locally-owned title components and their selected value spans."""
    text = clean_text(value)
    output = {"total": [], "refrigerator": [], "freezer": []}
    if not text:
        return output, []

    for total in extract_ref_capacity_components(text)["total"]:
        if re.fullmatch(_REF_TITLE_EXACT_COMPONENT_VALUE, total, re.I):
            output["total"].append(total)

    def kind_for(label):
        return "freezer" if normalize_key(label) in {"freezer", "congelador"} else "refrigerator"

    def usable_component_match(match):
        raw = clean_text(match.group("value"))
        if not is_ref_capacity_value(raw):
            return False
        if is_ref_auxiliary_volume_context(
            text,
            match.start("value"),
            match.end("value"),
        ):
            return False
        if not re.fullmatch(r"\d+(?:[.,]\d+)?", raw):
            return True
        suffix = normalize_key(text[match.end("value") : match.end("value") + 24])
        if re.match(r"^portas?\b", suffix):
            return False
        gap = clean_text(match.group("gap"))
        prefix = normalize_key(
            text[max(0, match.start("label") - 48) : match.start("label")]
        )
        return bool(
            gap
            or re.search(
                r"\bcapacidade(?:\s+(?:liquida|de\s+armazenagem))?\s+(?:do|da)\s*$",
                prefix,
            )
        )

    candidates = {}

    def add_candidate(match, direction):
        if not usable_component_match(match):
            return
        raw = clean_text(match.group("value"))
        label_span = match.span("label")
        value_span = match.span("value")
        gap = clean_text(match.group("gap"))
        prefix = normalize_key(
            text[max(0, label_span[0] - 48) : label_span[0]]
        )
        explicit = bool(gap) or bool(
            re.search(
                r"\bcapacidade(?:\s+(?:liquida|de\s+armazenagem))?\s+(?:do|da)\s*$",
                prefix,
            )
        )
        score = (30 if explicit else 10) + (1 if direction == "forward" else 0)
        key = (label_span, value_span, kind_for(match.group("label")))
        candidate = (score, label_span, value_span, key[2], raw)
        current = candidates.get(key)
        if current is None or candidate[0] > current[0]:
            candidates[key] = candidate

    forward_pattern = (
        rf"\b(?P<label>{_REF_TITLE_COMPARTMENT_LABEL})\b"
        r"(?P<gap>(?:\s+(?:com\s+)?capacidade(?:\s+de)?|"
        r"\s+com(?:\s+capacidade)?(?:\s+de)?|\s*[:=\-]|\s+de)?)\s*"
        rf"(?P<value>{_REF_TITLE_EXACT_COMPONENT_VALUE})"
    )
    for match in re.finditer(forward_pattern, text, re.I):
        add_candidate(match, "forward")

    reverse_pattern = (
        rf"(?<!\w)(?P<value>{_REF_TITLE_EXACT_COMPONENT_VALUE})\s*"
        r"(?P<gap>(?:(?:de\s+capacidade\s+)?(?:de|do|da|no|na)\s+)?)"
        rf"\b(?P<label>{_REF_TITLE_COMPARTMENT_LABEL})\b"
    )
    for match in re.finditer(reverse_pattern, text, re.I):
        add_candidate(match, "reverse")

    ordered = sorted(
        candidates.values(),
        key=lambda item: (min(item[1][0], item[2][0]), -item[0]),
    )
    selected = _maximum_ref_title_component_matching(ordered)

    seen = {"refrigerator": set(), "freezer": set()}
    owned_spans = []
    for _, _, value_span, kind, raw in sorted(selected, key=lambda item: item[2][0]):
        key = _measurement_comparison_key(raw)
        if key and key not in seen[kind]:
            seen[kind].add(key)
            output[kind].append(raw)
            owned_spans.append(value_span)
    return output, owned_spans


def _maximum_ref_title_component_matching(candidates):
    """Return the deterministic maximum-cardinality, maximum-score matching."""
    if not candidates:
        return []

    label_spans = sorted({candidate[1] for candidate in candidates})
    value_spans = sorted({candidate[2] for candidate in candidates})
    label_nodes = {span: index + 1 for index, span in enumerate(label_spans)}
    value_offset = 1 + len(label_spans)
    value_nodes = {
        span: value_offset + index for index, span in enumerate(value_spans)
    }
    source = 0
    sink = value_offset + len(value_spans)
    graph = [[] for _ in range(sink + 1)]

    def add_edge(start, end, capacity, cost):
        forward = [end, len(graph[end]), capacity, cost]
        reverse = [start, len(graph[start]), 0, -cost]
        graph[start].append(forward)
        graph[end].append(reverse)
        return forward

    for span in label_spans:
        add_edge(source, label_nodes[span], 1, 0)
    for span in value_spans:
        add_edge(value_nodes[span], sink, 1, 0)

    # One score point must outweigh every possible source-order preference.
    # Powers of two then make the earliest differing candidate deterministic.
    preference_base = 1 << len(candidates)
    candidate_edges = []
    for index, candidate in enumerate(candidates):
        score, label_span, value_span = candidate[:3]
        source_preference = 1 << (len(candidates) - index - 1)
        utility = score * preference_base + source_preference
        edge = add_edge(
            label_nodes[label_span],
            value_nodes[value_span],
            1,
            -utility,
        )
        candidate_edges.append((candidate, edge))

    node_count = len(graph)
    while True:
        distances = [None] * node_count
        previous = [None] * node_count
        distances[source] = 0
        for _ in range(node_count - 1):
            updated = False
            for node, edges in enumerate(graph):
                if distances[node] is None:
                    continue
                for edge_index, edge in enumerate(edges):
                    if edge[2] <= 0:
                        continue
                    distance = distances[node] + edge[3]
                    if distances[edge[0]] is None or distance < distances[edge[0]]:
                        distances[edge[0]] = distance
                        previous[edge[0]] = (node, edge_index)
                        updated = True
            if not updated:
                break
        if distances[sink] is None:
            break

        node = sink
        while node != source:
            previous_node, edge_index = previous[node]
            edge = graph[previous_node][edge_index]
            edge[2] -= 1
            graph[node][edge[1]][2] += 1
            node = previous_node

    return [candidate for candidate, edge in candidate_edges if edge[2] == 0]


def select_ref_title_capacity_component(value):
    """Return the highest-priority unambiguous exact title compartment."""
    components = extract_ref_title_capacity_components(value)
    for kind in ("total", "refrigerator", "freezer"):
        unique = {}
        for raw in components[kind]:
            key = _measurement_comparison_key(raw)
            if key:
                unique.setdefault(key, raw)
        if len(unique) == 1:
            return kind, next(iter(unique.values()))
        if len(unique) > 1:
            return kind, ""
    return "", ""


def is_ref_capacity_value(value):
    """Accept any numeric refrigerator-capacity notation with the right meaning."""
    text = clean_text(value)
    key = normalize_key(text)
    if not text or not re.search(r"\d", text):
        return False
    # A container count describes how many items fit in the appliance, not the
    # appliance's physical storage volume.  Even a phrase such as
    # ``12 garrafas de 750 ml`` must not be treated as a 750 ml refrigerator.
    if re.search(
        rf"\d+(?:[.,]\d+)?\s*{_REF_CONTAINER_COUNT_PATTERN}\b",
        text,
        re.I,
    ) or re.search(rf"\b{_REF_CONTAINER_COUNT_PATTERN}\b", key):
        return False
    if re.search(r"(?:^|[^a-z])(?:rpm|w|watts?|wh|kwh|kw|v|volts?|voltagem|hz)\b", key):
        return False
    if re.search(r"(?:^|[^a-z])(?:kg|quilos?|libras?|lbs?)\b", key):
        return False
    if re.search(r"(?:^|[^a-z])(?:cm|mm)\b|\b(?:4k|8k|hdr\d*|hdmi\d*|usb\d*)\b", key):
        return False
    if re.fullmatch(
        r"(?:sim|nao|superior|frontal|front(?:\s+loading?)?|top(?:\s+loading?)?|eletrica|bivolt)",
        key,
    ):
        return False
    return True


def is_ref_capacity_category_band(value):
    """Identify broad catalog bands without treating practical limits as bands.

    ``ate 260 litros`` can be the seller's only statement of an appliance's
    usable capacity, so it deliberately remains an ordinary capacity. Explicit
    lower/upper bands are marked so callers can demote them only when a more
    exact capacity is also available.
    """
    text = clean_text(value).casefold()
    unit = r"(?:litros?|lts?|l)"
    number = r"\d+(?:[.,]\d+)?"
    return bool(
        re.fullmatch(rf"de\s+{number}\s+a\s+{number}\s*{unit}", text)
        or re.fullmatch(
            rf"{number}\s*(?:{unit}\s*)?"
            rf"(?:\s+a\s+|\s*(?:-|\u2013|\u2014|/)\s*)"
            rf"{number}\s*{unit}",
            text,
        )
        or re.fullmatch(rf"(?:acima|abaixo)\s+de\s+{number}\s*{unit}", text)
    )


def _ref_single_capacity_signature(value):
    """Return (number, unit, qualified) only for a single scalar capacity."""
    match = re.fullmatch(
        r"(?:(?P<qualifier>(?:(?:acima|abaixo|menos|mais|cerca)\s+de|"
        r"at[eé]|aprox(?:imadamente)?\.?|[<>~]))\s*)?"
        r"(?:de\s+)?(?P<number>\d+(?:[.,]\d+)?)\s*"
        r"(?P<unit>ml|l|lts?|litros?|quartos?|quarts?)?",
        clean_text(value),
        re.I,
    )
    if not match:
        return None
    parts = _measurement_parts(match.group("number") + (match.group("unit") or ""))
    if not parts:
        return None
    return parts[0], parts[1], bool(match.group("qualifier"))


def select_ref_capacity_exact_over_qualified(values):
    """Prefer an exact REF scalar over the same qualified scalar only."""
    filtered_levels = filter_ref_capacity_exact_over_qualified_levels([values])
    return combine_capacity_distinct(filtered_levels[0] if filtered_levels else [])


def _ref_capacity_signatures_equivalent(left, right):
    """Treat a missing unit as inherited, but never merge explicit units."""
    return left[0] == right[0] and (
        left[1] == right[1] or not left[1] or not right[1]
    )


def filter_ref_capacity_exact_over_qualified_levels(levels):
    """Remove same-scalar qualifiers across levels while preserving level shape."""
    candidates_by_level = [
        [clean_text(value) for value in values or [] if clean_text(value)]
        for values in levels or []
    ]
    parsed_by_level = [
        [_ref_single_capacity_signature(value) for value in values]
        for values in candidates_by_level
    ]
    exact_signatures = [
        signature[:2]
        for signatures in parsed_by_level
        for signature in signatures
        if signature is not None and not signature[2]
    ]
    return [
        [
            value
            for value, signature in zip(values, signatures)
            if not (
                signature is not None
                and signature[2]
                and any(
                    _ref_capacity_signatures_equivalent(
                        signature[:2],
                        exact_signature,
                    )
                    for exact_signature in exact_signatures
                )
            )
        ]
        for values, signatures in zip(candidates_by_level, parsed_by_level)
    ]


def is_auxiliary_water_volume_context(value, start, end):
    """Return whether one volume belongs to water storage/use, not capacity."""
    text = clean_text(value)
    prefix = normalize_key(text[max(0, start - 90) : start])
    raw_suffix = text[end : end + 60]
    suffix = (
        ""
        if re.match(r"^\s*[+;,|:]", raw_suffix)
        else normalize_key(raw_suffix)
    )
    return bool(
        re.search(
            r"\b(?:(?:dispenser|reservatorio)(?:\s+(?:de|para)\s+agua)?|"
            r"tanque\s+(?:de|para)\s+agua)"
            r"(?:\s+(?:com\s+)?capacidade)?(?:\s+de)?\s*$",
            prefix,
        )
        or re.search(r"\bconsumo(?:\s+de)?\s+agua(?:\s+de)?\s*$", prefix)
        or re.match(
            r"^(?:de|para)?\s*agua\b",
            suffix,
        )
        or re.match(
            r"^(?:de\s+)?(?:reservatorio|dispenser|tanque)(?:\s+de)?\s+agua\b",
            suffix,
        )
        or re.match(r"^(?:de\s+)?consumo(?:\s+de)?\s+agua\b", suffix)
    )


def is_ref_auxiliary_volume_context(value, start, end):
    """Reject water-storage and tank volumes only on refrigerator paths."""
    if is_auxiliary_water_volume_context(value, start, end):
        return True
    text = clean_text(value)
    prefix = normalize_key(text[max(0, start - 120) : start])
    raw_suffix = text[end : end + 80]
    suffix = (
        ""
        if re.match(r"^\s*[+;,|:]", raw_suffix)
        else normalize_key(raw_suffix)
    )
    appliance = r"(?:refrigerador|refrigeradora|geladeira|freezer|congelador)"
    auxiliary = (
        r"(?:(?:reservatorio|dispenser)(?:\s+(?:de|para)\s+agua)?|"
        r"tanque(?:\s+(?:de|para)\s+agua)?)"
    )
    return bool(
        re.search(
            rf"\b{auxiliary}"
            rf"(?:\s+(?:do|da|de)\s+{appliance})?"
            r"(?:\s+(?:com\s+)?capacidade)?(?:\s+de)?\s*[:=\-]?\s*$",
            prefix,
        )
        or re.match(
            rf"^(?:de|do|da|para)?\s*{auxiliary}\b",
            suffix,
        )
    )


def is_negated_loading_context(value, start):
    """Detect a nearby loading negation, resetting scope at contrast clauses."""
    prefix = clean_text(value)[:start]
    scope = re.split(
        r"[,;|]|\b(?:loadingreset|mas|por[eé]m|contudo)\b",
        prefix,
        flags=re.I,
    )[-1]
    scope = re.split(
        r"\be\s+(?:possui|tem|com|sim)\b",
        scope,
        flags=re.I,
    )[-1]
    key = normalize_key(scope)
    return bool(
        re.search(r"\bsem(?:\s+[a-z0-9]+){0,7}\s*$", key)
        or re.search(
            r"\bnao(?:\s+(?:e|possui|tem))?(?:\s+[a-z0-9]+){0,8}\s*$",
            key,
        )
    )


def is_safe_tv_size_after_os_label(prefix, suffix, number):
    """Allow Google/Roku TV sizes only when a resolution confirms the number."""
    if not re.search(r"\b(?:google|roku)\s+tv\s*$", normalize_key(prefix)):
        return False
    try:
        numeric = float(str(number).replace(",", "."))
    except (TypeError, ValueError):
        return False
    return numeric >= 19 and bool(
        re.match(r"^(?:4k|8k|uhd|full\s+hd|fhd|hd)\b", normalize_key(suffix))
    )


def _is_probable_container_unit_volume(
    value,
    max_liters=2,
    upper_inclusive=True,
):
    match = re.fullmatch(
        r"(?P<number>\d+(?:[.,]\d+)?)\s*"
        r"(?P<unit>ml|litros?|lts?|l)\b",
        clean_text(value),
        re.I,
    )
    if not match:
        return False
    unit = normalize_key(match.group("unit"))
    if unit == "ml":
        return True
    number = float(match.group("number").replace(",", "."))
    if upper_inclusive:
        return 0 < number <= max_liters
    return 0 < number < max_liters


def is_ldy_capacity_value(value):
    """Accept clothes-mass capacity, including ranges and nonstandard units."""
    text = clean_text(value)
    key = normalize_key(text)
    if not text or not re.search(r"\d", text):
        return False
    if re.search(r"(?:^|[^a-z])(?:rpm|w|watts?|wh|kwh|kw|v|volts?|voltagem|hz)\b", key):
        return False
    if re.search(r"(?:^|[^a-z])(?:l|litros?|liters?|lts?|ml)\b", key):
        return False
    if re.search(r"\b(?:superior|frontal|front\s+load(?:ing)?|top\s+load(?:ing)?|sim|nao)\b", key):
        return False
    if re.search(r"(?:^|[^a-z])(?:cm|mm)\b|\b(?:4k|8k|hdr\d*|hdmi\d*|usb\d*)\b", key):
        return False
    if re.search(r"(?:^|[^a-z])(?:kg|kgs|quilos?|libras?|lbs?)\b", key):
        return True

    numbers = re.findall(r"\d+(?:[.,]\d+)?", text)
    if not numbers:
        return False
    parsed = [float(number.replace(",", ".")) for number in numbers]
    return max(parsed) <= 100


def select_ldy_capacity_level(values):
    '''Resolve repeated values from one LDY target without losing raw values.'''
    return _resolve_ldy_capacity_level(values)[0]


def select_ldy_capacity_from_levels(levels):
    '''Apply LDY conflict policy while retaining same-level corroboration.'''
    tentative = ''
    range_tentative = ''
    for values in levels or []:
        selected, corroborated = _resolve_ldy_capacity_level(values)
        if not selected:
            continue
        suspicious = _is_suspicious_unitless_ldy_capacity(selected)
        if not suspicious:
            if _is_ldy_capacity_range(selected):
                if not range_tentative:
                    range_tentative = selected
                continue
            if tentative and _capacity_numbers_are_distinct(tentative, selected):
                return selected
            return tentative or selected
        if corroborated:
            return selected
        if not tentative:
            tentative = selected
    return range_tentative or tentative


def _resolve_ldy_capacity_level(values):
    candidates = [clean_text(value) for value in values or [] if clean_text(value)]
    filtered = []
    for candidate in candidates:
        if _is_suspicious_unitless_ldy_capacity(candidate):
            credible = [
                value
                for value in candidates
                if not _is_suspicious_unitless_ldy_capacity(value)
            ]
            if any(_capacity_numbers_are_distinct(candidate, value) for value in credible):
                continue
        filtered.append(candidate)
    exact_candidates = [
        candidate for candidate in filtered if not _is_ldy_capacity_range(candidate)
    ]
    if exact_candidates and len(exact_candidates) != len(filtered):
        filtered = exact_candidates
    selected = combine_capacity_distinct(filtered)
    corroborated = False
    if _is_suspicious_unitless_ldy_capacity(selected):
        corroborated = any(
            not _is_suspicious_unitless_ldy_capacity(value)
            and not _capacity_numbers_are_distinct(selected, value)
            for value in candidates
        )
    return selected, corroborated


def select_priority_ldy_capacity(candidates):
    '''Select among ordered target/source levels with the same LDY policy.'''
    candidates = [clean_text(value) for value in candidates or [] if clean_text(value)]
    for index, candidate in enumerate(candidates):
        if not _is_suspicious_unitless_ldy_capacity(candidate):
            return candidate
        for alternative in candidates[index + 1 :]:
            if _is_suspicious_unitless_ldy_capacity(alternative):
                continue
            if _capacity_numbers_are_distinct(candidate, alternative):
                return alternative
            return candidate
        return candidate
    return ''


def _is_suspicious_unitless_ldy_capacity(value):
    return bool(re.fullmatch(r'0[.,]\d+', clean_text(value)))


def _is_ldy_capacity_range(value):
    text = clean_text(value)
    unit = r"(?:kg|kgs|quilos?|libras?|lbs?)"
    number = r"\d+(?:[.,]\d+)?"
    return bool(
        re.fullmatch(
            rf"(?:de\s+)?{number}\s*(?:{unit}\s*)?"
            rf"(?:a|-|\u2013|\u2014|/)\s*{number}\s*{unit}",
            text,
            re.I,
        )
        or re.fullmatch(
            rf"(?:(?:acima|abaixo|menos|mais)\s+de|at[eé]|[<>])\s*"
            rf"{number}\s*{unit}",
            text,
            re.I,
        )
    )


def _capacity_numbers_are_distinct(left, right):
    def numbers(value):
        output = set()
        for token in re.findall(r'\d+(?:[.,]\d+)?', clean_text(value)):
            normalized = token.replace(',', '.')
            normalized = normalized.rstrip('0').rstrip('.') if '.' in normalized else normalized
            output.add(normalized.lstrip('0') or '0')
        return output

    left_numbers = numbers(left)
    right_numbers = numbers(right)
    return bool(left_numbers and right_numbers and left_numbers.isdisjoint(right_numbers))


def is_energy_value(value):
    """Accept consumption values/text while excluding efficiency and voltage."""
    text = clean_text(value)
    key = normalize_key(text)
    if not text:
        return False
    if key in {"bivolt", "eletrica", "classe a", "classe energetica a"}:
        return False
    if re.search(r"\b(?:eficiencia energetica|classe de eficiencia|selo procel)\b", key):
        return False
    has_energy_unit = bool(
        re.search(r"\d+(?:[.,]\d+)?\s*(?:kwh|wh|kw|watts?|w)(?:\b|/)", text, re.I)
    )
    if "agua" in key and "energia" not in key and not has_energy_unit:
        return False
    if re.search(r"\b(?:bivolt|voltagem|tensao)\b", key) and not has_energy_unit:
        return False
    if re.fullmatch(
        r"(?:(?:de\s+)?\d+(?:[.,]\d+)?\s*(?:a|ate|[-–—/])?\s*)+\s*(?:v|volts?)",
        key,
    ):
        return False
    if re.search(r"\b(?:baixo consumo|baixo consumo de energia|stand\s*by|standby)\b", key):
        return True
    return has_energy_unit


_ENERGY_CONCATENATED_LABEL_RE = (
    r"(?-i:(?:Consumo|Standby|Pot(?:ência|encia)|Tens(?:ão|ao)|Voltagem|"
    r"Alimenta(?:ção|cao)|Padr(?:ão|ao)|Entradas?|Mem(?:ória|oria)|C(?:ódigo|odigo)|"
    r"Motor|Sistema|Capacidade|Dimens(?:ões|oes)|Peso|Frequ(?:ência|encia)|Cor|"
    r"Modelo|Marca|Garantia|Conectividade|Classifica(?:ção|cao)|Sensor|"
    r"Desligamento|Economia|Tipo))"
)
_ENERGY_TOKEN_TERMINATOR_RE = (
    r"(?=$|[\s.,;:|/()\[\]{}<>+=-]|" + _ENERGY_CONCATENATED_LABEL_RE + r")"
)
_ENERGY_VALUE_TOKEN_RE = re.compile(
    r"(?:abaixo\s+de\s+|aprox(?:imadamente)?\.?\s*|menos\s+de\s+|de\s+|[<>~]\s*)?"
    r"\d+(?:[.,]\d+)?\s*(?:kwh|wh|kw|watts?|w)(?:\s*/\s*(?:ano|m[eê]s|ciclo|hora|dia))?"
    + _ENERGY_TOKEN_TERMINATOR_RE,
    re.I,
)
_ENERGY_NEXT_SPEC_RE = re.compile(
    r"(?:[,;]\s*|\s+)(?:[-–—]\s*)?(?:e\s+)?(?:entradas?|mem[oó]ria|c[oó]digo|motor|sistema|pot[eê]ncia|"
    r"voltagem|tens[aã]o|alimenta[cç][aã]o|padr[aã]o|capacidade|dimens[oõ]es|peso|frequ[eê]ncia|cor|modelo|marca|"
    r"garantia|conectividade|classifica[cç][aã]o|outros\s+recursos|sensor(?:\s+ecol[oó]gico)?|"
    r"desligamento(?:\s+autom[aá]tico)?|economia\s+de\s+energia(?:\s+autom[aá]tica)?)"
    r"\b\s*(?::|=|-)?",
    re.I,
)


def trim_labeled_energy_suffix(value, allow_numeric=False):
    """Cut a following spec label without discarding compound energy values.

    Product descriptions frequently flatten adjacent specification rows into a
    single string. A boundary is honored only after an energy token (or an
    explicitly allowed bare numeric target), so a later motor/power value is
    never promoted when the consumption value itself is absent.
    """
    text = clean_text(value)
    if not text:
        return ""
    for match in _ENERGY_NEXT_SPEC_RE.finditer(text):
        prefix = clean_text(text[: match.start()].rstrip(" ,;|:"))
        has_energy = bool(_ENERGY_VALUE_TOKEN_RE.search(prefix))
        bare_numeric = allow_numeric and bool(re.fullmatch(r"[<>~]?\s*\d+(?:[.,]\d+)?", prefix))
        if has_energy or bare_numeric:
            return prefix
    return text


_DIRECT_ENERGY_BOUNDARY_RE = (
    r"(?:consumo|stand\s*by|standby|pot[eê]ncia|tens[aã]o|voltagem|alimenta[cç][aã]o|"
    r"padr[aã]o|entradas?|mem[oó]ria|c[oó]digo|motor|sistema|capacidade|dimens[oõ]es|"
    r"peso|frequ[eê]ncia|cor|modelo|marca|garantia|conectividade|classifica[cç][aã]o)"
)
_DIRECT_LABELED_ENERGY_RE = re.compile(
    rf"(?:consumo|stand\s*by|standby)\b"
    rf"(?:(?!\b{_DIRECT_ENERGY_BOUNDARY_RE}\b).){{0,80}}?"
    rf"(?P<value>{_ENERGY_VALUE_TOKEN_RE.pattern})",
    re.I | re.S,
)


def _direct_energy_label_start_allowed(text, start):
    """Allow flattened spec boundaries without matching inside ordinary words."""
    if start <= 0 or not text[start - 1].isalnum():
        return True
    joined = re.search(r"([^\W_]+)$", text[:start])
    token = joined.group(1) if joined else ""
    if not token or len(token) > 12:
        return False
    letters_are_upper = all(not char.isalpha() or char.isupper() for char in token)
    known_spec_tokens = {"CH", "RMS", "W", "V", "HZ", "KWH", "WH", "KW"}
    return letters_are_upper and (
        any(char.isdigit() for char in token) or token in known_spec_tokens
    )


def extract_direct_labeled_energy_tokens(value):
    """Extract energy tokens immediately governed by consumption/standby text.

    The bounded scan accepts flattened ``label value`` prose and concatenated
    rows, but never crosses another consumption or general specification label.
    """
    text = clean_text(value)
    output = []
    for match in _DIRECT_LABELED_ENERGY_RE.finditer(text):
        if not _direct_energy_label_start_allowed(text, match.start()):
            continue
        prefix = normalize_key(text[match.start() : match.start("value")])
        if "agua" in prefix:
            continue
        output.append((match.start(), clean_text(match.group("value"))))
    return output


def sanitize_labeled_energy_target_value(value):
    """Keep raw target values unless they clearly describe another meaning."""
    text = trim_labeled_energy_suffix(value, allow_numeric=True)
    if not text:
        return ""
    text = collapse_duplicate_range(text)
    key = normalize_key(text)
    if not re.search(r"\d", text):
        return text if is_energy_value(text) else ""
    if re.search(r"\b(?:eficiencia|classe|procel|economia)\b", key) or "%" in text:
        return ""
    if re.search(
        r"\d\s*(?:ml|litros?|lts?|l|rpm|kg|kgs|quilos?|libras?|lbs?|vazao|hz)\b",
        key,
    ):
        return ""
    first_energy = _ENERGY_VALUE_TOKEN_RE.search(text)
    if first_energy and first_energy.start():
        prefix = normalize_key(text[: first_energy.start()])
        if re.search(
            r"\b(?:entradas?|memoria|codigo|motor|sistema|potencia|capacidade|dimensoes|"
            r"peso|frequencia|cor|modelo|marca|garantia|conectividade|classificacao)\b",
            prefix,
        ):
            return ""
    voltage_tokens = re.findall(r"[a-z]+|\d+", key)
    voltage_only_tokens = {
        "110", "127", "220", "240", "a", "ate", "bivolt", "ou", "v", "volt", "volts",
    }
    if voltage_tokens and set(voltage_tokens).issubset(voltage_only_tokens):
        return ""
    energy_tokens = [clean_text(match.group(0)) for match in _ENERGY_VALUE_TOKEN_RE.finditer(text)]
    has_voltage_context = bool(
        re.search(r"\b(?:bivolt|voltagem|tensao)\b|\b(?:110|127|220|240)\s*(?:v|volts?)\b", key)
    )
    if has_voltage_context and len(energy_tokens) == 1:
        return energy_tokens[0]
    if is_energy_value(text):
        return text
    words = set(re.findall(r"[a-z]+", key))
    allowed_words = {
        "abaixo", "ano", "aprox", "aproximadamente", "ciclo", "de", "dia",
        "hora", "kwh", "kw", "menos", "mes", "por", "v", "volt", "volts",
        "w", "watt", "watts", "wh",
    }
    if words.issubset(allowed_words):
        return text
    return ""


def normalize_loading_type(value):
    """Return normalized loading directions in their first source order."""
    text = clean_text(value)
    key = normalize_key(re.sub(r"[,;|]", " loadingreset ", text))
    matches = []
    patterns = (
        (r"\b(?:top\s+load(?:ing|er)?|superior)\b", "Top load"),
        (r"\b(?:front\s+load(?:ing|er)?|frontal)\b", "Front load"),
    )
    for pattern, normalized in patterns:
        for match in re.finditer(pattern, key):
            if is_negated_loading_context(key, match.start()):
                continue
            if normalized == "Front load" and re.search(
                    r"\b(?:painel|porta)\s+$",
                    key[max(0, match.start() - 45) : match.start()],
                ):
                continue
            matches.append((match.start(), normalized))
    matches.sort(key=lambda item: item[0])
    return combine_distinct([normalized for _, normalized in matches])


def normalize_exact_loading_direction(value):
    """Normalize only a complete loading-direction value, never prose fragments."""
    text = clean_text(value)
    if not re.fullmatch(
        r"(?:superior|frontal|top[\s-]+load(?:ing|er)?|front[\s-]+load(?:ing|er)?|"
        r"(?:abertura(?:\s+da\s+tampa)?|carga)\s+(?:superior|frontal))",
        text,
        re.I,
    ):
        return ""
    return normalize_loading_type(text)


_REF_TITLE_PATTERNS = (
    rf"(?<!\w)(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*p[eé]s?\s*c[uú]bicos?(?:\s*\([^)]*(?:litros?|l\b)[^)]*\))?",
    rf"(?<!\w)(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s+a\s+\d+(?:[.,]\d+)?\s*(?:litros?|lts?|l)\b",
    rf"(?<!\w)(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*(?:litros?|lts?|l)?(?:\s+a\s+|\s*(?:-|\u2013|\u2014|/)\s*)\d+(?:[.,]\d+)?\s*(?:litros?|lts?|l)\b",
    rf"(?<!\w)(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*(?:litros?|lts?|ml|l)\b",
    rf"(?<!\w)(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*(?:quartos?|quarts?)\b",
    rf"(?<!\w)(?:{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*latas?(?:{_REF_CONTAINER_CONNECTOR_PATTERN}\s*\d+(?:[.,]\d+)?\s*ml)?\b",
)


def extract_ref_capacity_from_title(title):
    text = clean_text(title)
    components, component_spans = _extract_ref_title_capacity_component_matches(text)
    key = normalize_key(text)
    compartment_count = len(
        re.findall(r"\b(?:geladeira|refrigerador|refrigeradora|freezer|congelador)\b", key)
    )

    def selected_component(kind):
        values = []
        for value in components[kind]:
            if (
                kind != "total"
                and re.fullmatch(r"\d+(?:[.,]\d+)?", clean_text(value))
                and "capacidade" not in key
                and compartment_count < 2
            ):
                continue
            values.append(value)
        return combine_capacity_distinct(values)

    total = selected_component("total")
    if total:
        return total
    matches = []
    for pattern in _REF_TITLE_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            raw = clean_text(match.group(0))
            # The volume following a container count is the size of each can,
            # bottle, or container, not the refrigerator capacity.  The full
            # count is rejected by is_ref_capacity_value(); this guard also
            # rejects a nested ``750 ml`` standalone-volume title match.
            prefix = text[max(0, match.start() - 60) : match.start()]
            container_prefix_match = re.search(
                rf"\b\d+(?:[.,]\d+)?\s*"
                rf"(?P<container>{_REF_CONTAINER_COUNT_PATTERN})"
                rf"(?P<connector>{_REF_CONTAINER_CONNECTOR_PATTERN})?"
                r"\s*(?:\(\s*)?$",
                prefix,
                re.I,
            )
            if not container_prefix_match:
                container_prefix_match = re.search(
                    rf"\b(?P<container>{_REF_CONTAINER_COUNT_PATTERN})"
                    r"(?P<connector>\s+(?:de|com|x))"
                    r"\s*(?:\(\s*)?$",
                    prefix,
                    re.I,
                )
            if container_prefix_match:
                connector = clean_text(container_prefix_match.group("connector"))
                word_connector = normalize_key(connector) in {"de", "com", "x"}
                punctuation_connector = bool(
                    re.search(r"[/;:,+&\-\u2013\u2014]", connector)
                )
                container = normalize_key(container_prefix_match.group("container"))
                if word_connector:
                    continue
                if punctuation_connector and _is_probable_container_unit_volume(
                    raw,
                    max_liters=10,
                    upper_inclusive=False,
                ):
                    continue
                no_connector_limit = 2 if container.startswith("lata") else 5
                if not connector and _is_probable_container_unit_volume(
                    raw,
                    max_liters=no_connector_limit,
                ):
                    continue
            if is_ref_auxiliary_volume_context(text, match.start(), match.end()):
                continue
            if any(
                start < match.end() and match.start() < end
                for start, end in component_spans
            ):
                continue
            if is_ref_capacity_value(raw):
                matches.append((match.start(), -(match.end() - match.start()), match, raw))

    freezer_candidates = []
    seen_spans = set()
    for _, _, match, raw in sorted(matches, key=lambda item: (item[0], item[1])):
        span = (match.start(), match.end())
        if span in seen_spans:
            continue
        seen_spans.add(span)
        prefix = normalize_key(text[max(0, match.start() - 80) : match.start()])
        if re.search(
            r"\b(?:freezer|congelador)\b"
            r"(?:\s+(?:com\s+)?capacidade)?(?:\s+de)?\s*$",
            prefix,
        ):
            freezer_candidates.append(raw)
            continue
        return raw

    refrigerator = selected_component("refrigerator")
    if refrigerator:
        return refrigerator
    freezer = selected_component("freezer")
    if freezer:
        return freezer
    if freezer_candidates:
        return freezer_candidates[0]
    return ""


def extract_ref_capacity_scalar_values(value):
    """Return physical-capacity scalars from one structured target value.

    Some retailer targets mix the appliance volume with a container count,
    for example ``18 litros / 22 latas``. The full string is not a valid
    refrigerator capacity, but discarding it would also lose the meaningful
    volume. Compartment composites remain owned by
    :func:`extract_ref_capacity_components` and are intentionally excluded.
    """
    text = clean_text(value)
    if not text or any(extract_ref_capacity_components(text).values()):
        return []
    extracted = extract_ref_capacity_from_title(text)
    if extracted and is_ref_capacity_value(extracted):
        return [extracted]
    if re.fullmatch(r"[<>~]?\s*\d+(?:[.,]\d+)?", text) and is_ref_capacity_value(text):
        return [text]
    return []


def extract_ldy_capacity_from_title(title):
    text = clean_text(title)
    match = re.search(
        rf"(?<!\w)(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*(?:kg|kgs|quilos?|libras?|lbs?)?\s*(?:a|-|\u2013|\u2014|/)\s*"
        r"\d+(?:[.,]\d+)?\s*(?:kg|kgs|quilos?|libras?|lbs?)\b"
        rf"|(?<!\w)(?:de\s+|{CAPACITY_QUALIFIER_PATTERN})?\d+(?:[.,]\d+)?\s*"
        r"(?:kg|kgs|quilos?|libras?|lbs?)\b",
        text,
        re.I,
    )
    if match and is_ldy_capacity_value(match.group(0)):
        return clean_text(match.group(0))
    return ""


def extract_screen_size_from_title(title):
    text = clean_text(title)
    range_spans = [
        (match.start(), match.end())
        for match in re.finditer(
            r'(?<!\d)\d{1,3}(?:[.,]\d+)?\s*(?:polegadas?|[\"”″])?\s*'
            r'(?:a|at[eé]|[-–—])\s*\d{1,3}(?:[.,]\d+)?\s*(?:polegadas?|[\"”″])?',
            text,
            re.I,
        )
    ]
    range_spans.extend(
        (match.start(), match.end())
        for match in re.finditer(
            r'(?<!\d)\d{1,3}(?:[.,]\d+)?\s*/\s*\d{1,3}(?:[.,]\d+)?\s*'
            r'(?:polegadas?|pol\.?|inches?)?',
            text,
            re.I,
        )
    )
    for match in re.finditer(
        r'(?<!\d)(\d{2,3}(?:[.,]\d+)?)\s*(?:polegadas?|[\"”″])', text, re.I
    ):
        if any(start < match.end() and match.start() < end for start, end in range_spans):
            continue
        return clean_text(match.group(0))
    display = re.search(
        r'\b(?:(?:smart\s+)?tv|televisor|qled|oled|crystal\s+uhd)\b',
        text,
        re.I,
    )
    if display:
        tail = text[display.end() : display.end() + 90]
        for match in re.finditer(r'(?<![\w/])(\d{2,3})(?![\w/%])', tail):
            value = match.group(1)
            suffix = tail[match.end() : match.end() + 16]
            if re.match(
                r'\s*(?:hz|rpm|nits?|w|watts?|wh|kwh|kw|v|volts?|kg|cm|mm|litros?|lts?|l)\b',
                suffix,
                re.I,
            ):
                continue
            absolute_start = display.end() + match.start()
            absolute_end = display.end() + match.end()
            if any(start < absolute_end and absolute_start < end for start, end in range_spans):
                continue
            if is_screen_size_value(value):
                return f'{value}{chr(34)}'
    return ""


def is_screen_size_value(value):
    text = clean_text(value)
    if not text or not re.search(r'\d', text):
        return False
    if re.search(r'%|/', text):
        return False
    numbers = re.findall(r'(?<!\d)\d+(?:[.,]\d+)?(?!\d)', text)
    if len(numbers) != 1:
        return False
    try:
        number = float(numbers[0].replace(',', '.'))
    except ValueError:
        return False
    if not 10 <= number <= 200:
        return False
    invalid_unit_pattern = (
        r'\d\s*(?:v|volts?|w|watts?|kwh|wh|kw|hz|kg|kgs|quilos?|libras?|lbs?|'
        r'cm|mm|m|litros?|lts?|l)\b'
    )
    if re.search(invalid_unit_pattern, text, re.I):
        return False
    if not text or not re.search(r"\d", text):
        return False
    if re.search(r"\b\d{3,4}\s*[x×]\s*\d{3,4}\b", text, re.I):
        return False
    # Compatibility ranges generally describe a mount, not a television panel.
    if re.search(
        r'\d\s*(?:[\x22”]|polegadas?)?\s*(?:a|at[eé]|[-–—])\s*\d',
        text,
        re.I,
    ):
        return False
    if re.search(r"\d\s*(?:[\"”]|polegadas?)?\s*(?:a|ate|-)\s*\d", normalize_key(text)):
        return False
    return bool(re.search(r"\d{2,3}(?:[.,]\d+)?", text))
