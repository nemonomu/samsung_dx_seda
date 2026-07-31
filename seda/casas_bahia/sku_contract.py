import re
from urllib.parse import urlsplit

from ..parsers import (
    CASAS_TV_EXACT_MODELO_FIELD,
    clean_text,
    high_confidence_tv_model_number_from_text,
    remove_accents,
    screen_size_from_text,
    sku_from_url,
)


PRODUCT_SOURCE_MODEL_TOKEN = "casas_tv_sku_model_product_source"
PDP_HTML_MODEL_TOKEN = "casas_tv_sku_model_pdp_html"
VERIFIED_MODEL_TOKENS = frozenset(
    {
        PRODUCT_SOURCE_MODEL_TOKEN,
        PDP_HTML_MODEL_TOKEN,
    }
)


_MIXED_MODEL_TOKEN_RE = re.compile(
    r"\b(?=[A-Z0-9/-]*[A-Z])(?=[A-Z0-9/-]*\d)"
    r"[A-Z0-9]+(?:[-/][A-Z0-9]+)*\b"
)
_TECHNICAL_TOKEN_RE = re.compile(
    r"(?:"
    r"\d+(?:K\d*|HZ|GHZ|MHZ|FPS|BITS?|V|VOLTS?|HDMI\d*|USB\d*)|"
    r"(?:HDMI|USB|RJ|HDR|ANDROID|WEBOS|TIZEN|WIFI|WI-FI|BLUETOOTH|"
    r"DOLBY|FREESYNC|SMART|AI)\d+[A-Z0-9]*|"
    r"(?:HD|FHD|UHD|LED|DLED|QLED|OLED|MINILED|NANOCELL)\d+|"
    r"(?:A|ALPHA)\d+(?:GEN\d+)?|(?:GEN|GER)\d+|"
    r"TV\d+(?:SMART\d*)?"
    r")",
    re.I,
)


def casas_tv_title_model(text, screen_size_hint=""):
    """Return one conservative model candidate from a Casas Bahia TV title."""
    normalized = remove_accents(clean_text(text)).upper()
    if not normalized:
        return ""
    if (
        re.search(r"\s+\+\s+", normalized)
        and len(re.findall(r"\b(?:SMART\s+)?TV\b", normalized)) >= 2
    ):
        return ""

    candidate = high_confidence_tv_model_number_from_text(
        text,
        screen_size_hint=screen_size_hint,
    )
    if candidate:
        return candidate

    screen_digits = re.sub(r"\D", "", screen_size_from_text(normalized))
    if not screen_digits:
        screen_digits = re.sub(r"\D", "", clean_text(screen_size_hint))

    candidates = []
    seen = set()
    for match in _MIXED_MODEL_TOKEN_RE.finditer(normalized):
        value = match.group(0)
        compact = re.sub(r"[-/]", "", value)
        if compact in seen or len(compact) < 5:
            continue
        seen.add(compact)
        if _TECHNICAL_TOKEN_RE.fullmatch(value):
            continue
        if value[0].isdigit() and screen_digits and not compact.startswith(screen_digits):
            continue
        prefix = normalized[max(0, match.start() - 32) : match.start()]
        if re.search(
            r"(?:PROCESSADOR|PROCESSOR|CHIPSET|CPU)\s+(?:AI\s+)?$",
            prefix,
        ):
            continue
        candidates.append(value)
    return candidates[0] if len(candidates) == 1 else ""


def casas_tv_sku_for_output(row, url_item=""):
    """Publish a title model first, then an identity-proven PDP Modelo.

    The title path is limited to the conservative model extractor and a valid
    current item/URL identity. It never publishes the whole product title.
    Read-only DB recovery is applied separately when both paths stay blank.
    """
    if not _current_item_matches_url(row, url_item):
        return ""
    title_model = casas_tv_title_model(
        row.get("retailer_sku_name", ""),
        screen_size_hint=row.get("screen_size", ""),
    )
    if _is_distinct_from_identity(title_model, row, url_item):
        return title_model
    return verified_model_value(row, url_item)


def verified_model_value(row, url_item=""):
    candidate = clean_text(row.get("sku"))
    if has_verified_model_token(row) and _is_distinct_from_identity(
        candidate,
        row,
        url_item,
    ):
        return candidate
    return ""


def exact_modelo_candidate(row, detail):
    """Return an exact Modelo only when the PDP URL proves product identity."""
    if detail.get(CASAS_TV_EXACT_MODELO_FIELD) is not True:
        return ""
    url_item = clean_text(sku_from_url(row.get("product_url", "")))
    if not url_item:
        return ""
    candidate = clean_text(detail.get("sku"))
    if not _is_distinct_from_identity(candidate, row, url_item):
        return ""
    return candidate


def has_verified_model_token(row, token=""):
    tokens = set(str(row.get("parse_status") or "").split("+"))
    if token:
        return token in tokens
    return bool(tokens & VERIFIED_MODEL_TOKENS)


def replace_verified_model_token(status, token):
    """Replace stale model provenance while preserving unrelated status tokens."""
    parts = []
    for part in str(status or "").split("+"):
        part = part.strip()
        if not part or part in VERIFIED_MODEL_TOKENS or part in parts:
            continue
        parts.append(part)
    if token:
        if token not in VERIFIED_MODEL_TOKENS:
            raise ValueError(f"unsupported Casas TV model token: {token}")
        parts.append(token)
    return "+".join(parts)


def _is_distinct_from_identity(candidate, row, url_item):
    if not candidate:
        return False
    candidate_key = candidate.casefold()
    identities = {
        clean_text(url_item).casefold(),
        clean_text(row.get("item")).casefold(),
        clean_text(row.get("retailer_product_id")).casefold(),
    }
    identities.discard("")
    return candidate_key not in identities


def _current_item_matches_url(row, url_item):
    try:
        parsed = urlsplit(clean_text(row.get("product_url")))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if host != "casasbahia.com.br" and not host.endswith(
        ".casasbahia.com.br"
    ):
        return False
    parsed_item = clean_text(sku_from_url(row.get("product_url"))).casefold()
    url_key = clean_text(url_item).casefold()
    if not parsed_item or not url_key or parsed_item != url_key:
        return False
    row_key = clean_text(row.get("item")).casefold()
    return not row_key or row_key == url_key
