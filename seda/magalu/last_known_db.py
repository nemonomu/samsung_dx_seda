"""Read-only Magalu fallback for stable semantic fields.

This module never writes to the database. It fills only blank values in the
current in-memory rows after the normal itemQuery/PDP/retry paths have failed.
"""

import os
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from ..common.field_rules import (
    is_ldy_capacity_value,
    is_ref_capacity_category_band,
    is_ref_capacity_value,
    is_screen_size_value,
    normalize_exact_loading_direction,
    normalize_key,
    sanitize_labeled_energy_target_value,
)
from ..common.translations import _translate_ref_refrigerator_type
from ..parsers import (
    is_obviously_non_sku_magalu_value,
    is_synthetic_magalu_sku_value,
)
from ..step00_config import db_connect, output_table
from .field_extraction import (
    _is_compact_ldy_volume_item,
    _is_magalu_ldy_capacity_value,
)
from .recovery_contract import last_known_fields


TRUE_VALUES = {"1", "true", "yes", "y"}
MAGALU_ACCOUNT_KEYS = ("magalu", "magazineluiza")
NULL_LIKE_VALUES = {"none", "null", "nan", "[null]"}
SUCCESS_STATUS_TOKENS = {"detail_item_graphql", "detail_blank_retry"}
REQUIRED_FAILURE_TOKENS = {
    "detail_graphql_failed:item_query_failed",
    "detail_blank_retry_failed",
}
REF_TYPE_VALUES = {
    value.casefold(): value
    for value in (
        "Single Door",
        "Two Door",
        "Three Door",
        "Four Door",
        "French Door",
        "Side by Side",
        "Multidoor",
        "Freezer-on-Bottom",
        "Freezer-on-Top",
    )
}
LDY_COLOR_TOKENS = {
    "amadeirado",
    "amarelo",
    "azul",
    "bege",
    "black",
    "branca",
    "branco",
    "bronze",
    "champagne",
    "chumbo",
    "cinza",
    "cobre",
    "coral",
    "creme",
    "dourada",
    "dourado",
    "grafite",
    "gray",
    "green",
    "grey",
    "inox",
    "laranja",
    "lilas",
    "madeira",
    "marrom",
    "onix",
    "pink",
    "platinum",
    "prata",
    "preta",
    "preto",
    "red",
    "rosa",
    "rose",
    "roxa",
    "roxo",
    "silver",
    "stainless",
    "steel",
    "titanio",
    "titanium",
    "transparente",
    "turquesa",
    "verde",
    "vermelha",
    "vermelho",
    "vinho",
    "white",
}


def backfill_magalu_last_known_fields(
    rows,
    *,
    active_retailer,
    product_line_value,
):
    """Fill eligible blank fields from prior DB rows and return safe counters."""
    line = str(product_line_value or "").strip().upper()
    fields = last_known_fields(line)
    stats = _empty_stats()
    if not _env_flag("SEDA_MAGALU_LAST_KNOWN_DB_FALLBACK"):
        return stats
    if str(active_retailer or "").strip().lower() != "magalu" or not fields:
        return stats
    stats["enabled"] = True

    eligible = [row for row in rows if _eligible_row(row, line, fields)]
    stats["eligible_rows"] = len(eligible)
    if not eligible:
        return stats

    item_keys = sorted(
        {
            _current_identity_key(row)
            for row in eligible
            if _current_identity_key(row)
        }
    )
    stats["queried_items"] = len(item_keys)
    try:
        history = _read_history(item_keys, line, fields)
    except Exception as exc:
        stats["error"] = type(exc).__name__
        print(
            f"[seda] magalu last-known DB fallback skipped "
            f"error={type(exc).__name__}",
            flush=True,
        )
        return stats

    stats["history_rows"] = len(history)
    history_by_item = defaultdict(list)
    for historical in history:
        key = _current_identity_key(historical)
        if key and _historical_identity_valid(historical, key):
            history_by_item[key].append(historical)

    recovered_field_counts = Counter()
    recovered_rows = 0
    for row in eligible:
        key = _current_identity_key(row)
        candidates = history_by_item.get(key, ())
        if not candidates:
            continue
        changed = False
        for field in fields:
            if not _current_field_needs_recovery(row, field):
                continue
            value = _latest_valid_value(candidates, field, current_row=row)
            if not value:
                continue
            row[field] = value
            recovered_field_counts[field] += 1
            changed = True
        if changed:
            recovered_rows += 1

    stats["recovered_rows"] = recovered_rows
    stats["recovered_fields"] = dict(sorted(recovered_field_counts.items()))
    print(
        "[seda] magalu last-known DB fallback "
        f"recovered_rows={recovered_rows}/{len(eligible)} "
        f"recovered_fields={sum(recovered_field_counts.values())}",
        flush=True,
    )
    return stats


def _empty_stats():
    return {
        "enabled": False,
        "eligible_rows": 0,
        "queried_items": 0,
        "history_rows": 0,
        "recovered_rows": 0,
        "recovered_fields": {},
        "error": "",
    }


def _eligible_row(row, line, fields):
    if _canonical_retailer(
        row.get("retailer") or row.get("account_name")
    ) != "magalu":
        return False
    row_line = str(
        row.get("product_line") or row.get("product") or ""
    ).strip().upper()
    if row_line != line:
        return False
    if not any(_current_field_needs_recovery(row, field) for field in fields):
        return False
    tokens = {
        token.strip()
        for token in str(row.get("parse_status") or "").split("+")
        if token.strip()
    }
    if not REQUIRED_FAILURE_TOKENS.issubset(tokens):
        return False
    if SUCCESS_STATUS_TOKENS.intersection(tokens):
        return False
    status_key = str(row.get("parse_status") or "").casefold()
    if "identity_mismatch" in status_key or "identity_conflict" in status_key:
        return False
    return bool(_current_identity_key(row))


def _read_history(item_keys, line, fields):
    if not item_keys:
        return []
    table = _quoted_table_name(output_table())
    history_limit = _history_limit()
    selected_columns = (
        "item",
        "product_url",
        "retailer_sku_name",
        *fields,
        "crawl_strdatetime",
        "batch_id",
    )
    quoted_columns = ", ".join(
        f'"{column}"'
        for column in selected_columns
    )
    sql = (
        "WITH ranked_history AS ("
        f" SELECT {quoted_columns},"
        " ROW_NUMBER() OVER ("
        " PARTITION BY lower(trim(coalesce(\"item\", '')))"
        " ORDER BY "
        "NULLIF(trim(coalesce(\"crawl_strdatetime\", '')), '') DESC NULLS LAST,"
        " NULLIF(trim(coalesce(\"batch_id\", '')), '') DESC NULLS LAST,"
        " NULLIF(trim(coalesce(\"product_url\", '')), '') DESC NULLS LAST"
        " ) AS history_rank"
        f" FROM {table}"
        " WHERE regexp_replace("
        "lower(coalesce(\"account_name\", '')), '[^a-z]', '', 'g'"
        ") IN (%s, %s)"
        " AND upper(trim(coalesce(\"product\", ''))) = %s"
        " AND lower(trim(coalesce(\"item\", ''))) = ANY(%s)"
        ")"
        f" SELECT {quoted_columns} FROM ranked_history"
        " WHERE history_rank <= %s"
        " ORDER BY lower(trim(coalesce(\"item\", ''))), history_rank"
    )
    connection = None
    cursor = None
    try:
        connection = db_connect()
        cursor = connection.cursor()
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(
            f"SET LOCAL statement_timeout = {_statement_timeout_ms()}"
        )
        cursor.execute(
            sql,
            (*MAGALU_ACCOUNT_KEYS, line, list(item_keys), history_limit),
        )
        records = cursor.fetchall()
        return [
            dict(zip(selected_columns, record))
            for record in records
        ]
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass


def _latest_valid_value(candidates, field, *, current_row):
    for candidate in candidates:
        value = _validated_history_value(
            field,
            candidate.get(field),
            current_row=current_row,
            history_row=candidate,
        )
        if value:
            return value
    return ""


def _validated_history_value(
    field,
    value,
    *,
    current_row,
    history_row=None,
):
    text = str(value or "").strip()
    if not text or text.casefold() in NULL_LIKE_VALUES:
        return ""
    if field == "sku":
        return _validated_sku(text, current_row, history_row)
    if field == "screen_size":
        return text if is_screen_size_value(text) else ""
    if field == "estimated_annual_electricity_use":
        # The shared sanitizer preserves agreed bare values such as "1" while
        # rejecting voltage/efficiency noise and trimming a following spec.
        return sanitize_labeled_energy_target_value(text)
    if field == "model_year":
        return text if re.fullmatch(r"20[1-3]\d", text) else ""
    if field == "ref_refrigerator_type":
        translated = _translate_ref_refrigerator_type(text)
        return REF_TYPE_VALUES.get(str(translated).casefold(), "")
    if field == "ref_capacity":
        if (
            _is_auxiliary_ref_capacity(text)
            or _is_compound_ref_capacity(text)
            or not is_ref_capacity_value(text)
        ):
            return ""
        return "" if is_ref_capacity_category_band(text) else text
    if field == "ldy_loading_type":
        return _validated_loading_type(text)
    if field == "ldy_color":
        return _validated_ldy_color(text)
    if field == "ldy_capacity":
        if (
            not _is_compound_ldy_capacity(text)
            and not _is_suspicious_unitless_ldy_capacity(text)
            and is_ldy_capacity_value(text)
        ):
            return text
        contexts = [
            {
                "title": current_row.get("retailer_sku_name", ""),
                "path": current_row.get("product_url", ""),
            }
        ]
        if history_row:
            contexts.append(
                {
                    "title": history_row.get("retailer_sku_name", ""),
                    "path": history_row.get("product_url", ""),
                }
            )
        if (
            any(_is_compact_ldy_volume_item(context) for context in contexts)
            and _is_magalu_ldy_capacity_value(
                text,
                allow_compact_volume=True,
            )
        ):
            return text
        return ""
    return ""


def _current_field_needs_recovery(row, field):
    if field == "sku":
        return _current_sku_needs_recovery(row)
    return not _nonblank(row.get(field))


def _current_sku_needs_recovery(row):
    text = str(row.get("sku") or "").strip()
    if not text:
        return True
    trusted_tv_reference = (
        str(row.get("product_line") or row.get("product") or "")
        .strip()
        .upper()
        == "TV"
        and "sku_factsheet_reference_recovered"
        in {
            token.strip()
            for token in str(row.get("parse_status") or "").split("+")
            if token.strip()
        }
    )
    if is_obviously_non_sku_magalu_value(text):
        return True
    identity = _current_identity_key(row)
    if identity and text.casefold() == identity and not trusted_tv_reference:
        return True
    return bool(
        is_synthetic_magalu_sku_value(text)
        and not trusted_tv_reference
    )


def _validated_sku(value, current_row, history_row=None):
    text = str(value or "").strip()
    key = normalize_key(text)
    if (
        not text
        or text.casefold() in NULL_LIKE_VALUES
        or key in {
            "n a",
            "na",
            "nao informado",
            "nao se aplica",
            "sem modelo",
            "sem referencia",
            "undefined",
            "unknown",
        }
        or not re.search(r"[A-Za-z0-9]", text)
        or re.match(r"https?://", text, re.I)
        or is_obviously_non_sku_magalu_value(text)
        or is_synthetic_magalu_sku_value(text)
    ):
        return ""
    identity_keys = {_current_identity_key(current_row)}
    if history_row:
        identity_keys.add(_current_identity_key(history_row))
    identity_keys.discard("")
    return "" if text.casefold() in identity_keys else text


def _validated_ldy_color(value):
    text = str(value or "").strip()
    key = normalize_key(text)
    if (
        not text
        or len(text) > 80
        or text.casefold() in NULL_LIKE_VALUES
        or not re.search(r"[A-Za-z]", key)
        or re.search(r"https?://|[<>]", text, re.I)
        or re.search(
            r"\b(?:110|127|220|240)\s*v(?:olts?)?\b|\bbivolt\b",
            key,
            re.I,
        )
        or re.search(
            r"\d+(?:[.,]\d+)?\s*(?:kg|kgs|quilos?|l|litros?|ml)\b",
            key,
            re.I,
        )
        or normalize_exact_loading_direction(text)
    ):
        return ""
    if key in {
        "automatica",
        "automatico",
        "roupa",
        "sim",
        "nao",
        "n a",
        "na",
        "nao informado",
        "sem cor",
    }:
        return ""
    if re.search(
        r"\b(?:conforme|disponibilidade|estoque|friday|promocao|oferta)\b|"
        r"\bnao\s+se\s+aplica\b",
        key,
        re.I,
    ):
        return ""
    return text if LDY_COLOR_TOKENS.intersection(key.split()) else ""


def _is_compound_ldy_capacity(value):
    text = str(value or "").strip()
    unit = r"(?:kg|kgs|quilos?|libras?|lbs?)"
    qualifier = r"(?:de|acima|abaixo|mais|menos|at(?:e|\u00e9)|[<>])"
    separator = r"[,;|]"
    return bool(
        re.search(
            rf"{unit}\s*{separator}\s*(?:{qualifier}\s*)?\d",
            text,
            re.I,
        )
        or re.search(
            rf"\d\s*{separator}\s*{qualifier}\b",
            text,
            re.I,
        )
    )


def _is_auxiliary_ref_capacity(value):
    text = str(value or "").strip()
    key = normalize_key(text)
    return bool(
        re.search(
            r"\b(?:reservatorio|tanque)\b.*\b(?:agua|water)\b"
            r"|\b(?:agua|water)\b.*\b(?:reservatorio|tanque)\b",
            key,
            re.I,
        )
        or re.search(r"\d+(?:[.,]\d+)?\s*ml\b", text, re.I)
    )


def _is_compound_ref_capacity(value):
    text = str(value or "").strip()
    unit = (
        r"(?:ml|litros?|lts?|l|quartos?|quarts?|"
        r"p(?:e|\u00e9)s?\s*c(?:u|\u00fa)bicos?|cu\.?\s*ft\.?)"
    )
    qualifier = (
        r"(?:de|acima|abaixo|mais|menos|at(?:e|\u00e9)|"
        r"aprox(?:imadamente)?|[<>~])"
    )
    return bool(
        re.search(
            rf"{unit}\s*[,;|]\s*(?:{qualifier}\s*)?\d",
            text,
            re.I,
        )
    )


def _is_suspicious_unitless_ldy_capacity(value):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", text):
        return False
    try:
        return float(text.replace(",", ".")) <= 2
    except ValueError:
        return True


def _validated_loading_type(value):
    parts = [
        part.strip()
        for part in re.split(r"\s*[,;|]\s*", str(value or ""))
        if part.strip()
    ]
    normalized = [
        normalize_exact_loading_direction(part)
        for part in parts
    ]
    if not parts or any(not value for value in normalized):
        return ""
    unique = list(dict.fromkeys(normalized))
    return unique[0] if len(unique) == 1 else ""


def _historical_identity_valid(row, expected_item):
    item = _current_identity_key(row)
    return bool(
        item
        and item == expected_item
    )


def _url_item_key(url):
    parsed = urlsplit(str(url or "").strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    if (
        host != "magazineluiza.com.br"
        and not host.endswith(".magazineluiza.com.br")
    ):
        return ""
    match = re.search(r"(?:^|/)p/([^/?#]+)(?:/|$)", parsed.path, re.I)
    return _item_key(match.group(1)) if match else ""


def _item_key(value):
    return str(value or "").strip().casefold()


def _current_identity_key(row):
    row_item = _item_key(row.get("item"))
    url_item = _url_item_key(row.get("product_url"))
    if not url_item or (row_item and row_item != url_item):
        return ""
    return row_item or url_item


def _nonblank(value):
    return bool(str(value or "").strip())


def _canonical_retailer(value):
    normalized = re.sub(r"[^a-z]", "", str(value or "").casefold())
    return "magalu" if normalized in MAGALU_ACCOUNT_KEYS else ""


def _quoted_table_name(value):
    raw = str(value or "").strip()
    parts = raw.split(".")
    if (
        not parts
        or len(parts) > 2
        or any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part)
            for part in parts
        )
    ):
        raise RuntimeError("invalid_output_table")
    # step13/14 interpolate unquoted identifiers, so PostgreSQL folds custom
    # mixed-case settings to lowercase. Quote that same effective identifier.
    return ".".join(f'"{part.lower()}"' for part in parts)


def _history_limit():
    try:
        value = int(os.getenv("SEDA_MAGALU_LAST_KNOWN_HISTORY_LIMIT", "30"))
    except ValueError:
        value = 30
    return max(1, min(value, 100))


def _statement_timeout_ms():
    try:
        value = int(
            os.getenv(
                "SEDA_MAGALU_LAST_KNOWN_DB_TIMEOUT_MS",
                "15000",
            )
        )
    except ValueError:
        value = 15000
    return max(1000, min(value, 60000))


def _env_flag(name):
    return str(os.getenv(name, "0")).strip().casefold() in TRUE_VALUES
