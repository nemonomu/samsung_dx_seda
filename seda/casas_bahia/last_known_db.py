"""Read-only Casas Bahia fallback for fields that would save as NULL.

The lookup runs once per batch, never writes to the database, and accepts
history only when account, product line, item and the URL /p/{item} identity
all agree.  Existing non-blank current values are never overwritten.
"""

import os
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from ..step00_config import db_connect, output_table
from .field_extraction import is_standalone_dryer_title
from .recovery_contract import last_known_fields
from .ref_sku_contract import (
    LAST_KNOWN_SELECTED_TOKEN,
    casas_ref_short_for_output,
    casas_ref_sku_for_output,
    casas_ref_title_sku,
    is_valid_ref_manufacturer_sku,
    normalize_ref_sku,
)


TRUE_VALUES = {"1", "true", "yes", "y"}
CASAS_ACCOUNT_KEYS = ("casasbahia", "casasbahiacombr")
RECOVERED_FIELDS_KEY = "_casas_bahia_last_known_db_recovered_fields"


def backfill_casas_bahia_last_known_fields(
    rows,
    *,
    active_retailer,
    product_line_value,
):
    """Fill only final-output blanks and return non-sensitive counters."""
    line = str(product_line_value or "").strip().upper()
    fields = last_known_fields(line)
    stats = _empty_stats()
    if not _env_flag("SEDA_CASAS_BAHIA_LAST_KNOWN_DB_FALLBACK"):
        return stats
    if _canonical_retailer(active_retailer) != "casas_bahia" or not fields:
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
            "[seda] casas bahia last-known DB fallback skipped "
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
            if field == "sku":
                value = _latest_ref_sku(candidates)
            else:
                value = _latest_stored_value(candidates, field)
            if not value:
                continue
            row[field] = value
            if field == "sku":
                row["sku_short_version"] = casas_ref_short_for_output(
                    row,
                    value,
                )
                row["parse_status"] = _append_token(
                    row.get("parse_status", ""),
                    LAST_KNOWN_SELECTED_TOKEN,
                )
            _mark_recovered_field(row, field)
            recovered_field_counts[field] += 1
            changed = True
        if changed:
            recovered_rows += 1

    stats["recovered_rows"] = recovered_rows
    stats["recovered_fields"] = dict(sorted(recovered_field_counts.items()))
    print(
        "[seda] casas bahia last-known DB fallback "
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
    ) != "casas_bahia":
        return False
    row_line = str(
        row.get("product_line") or row.get("product") or ""
    ).strip().upper()
    if row_line != line:
        return False
    status_key = str(row.get("parse_status") or "").casefold()
    if "identity_mismatch" in status_key or "identity_conflict" in status_key:
        return False
    if not _current_identity_key(row):
        return False
    return any(
        _current_field_needs_recovery(row, field)
        for field in fields
    )


def _read_history(item_keys, line, fields):
    if not item_keys:
        return []
    table = _quoted_table_name(output_table())
    selected_columns = [
        "item",
        "product_url",
        "retailer_sku_name",
        *fields,
    ]
    if line == "REF":
        selected_columns.append("sku_short_version")
    selected_columns.extend(("crawl_strdatetime", "batch_id"))
    selected_columns = tuple(dict.fromkeys(selected_columns))
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
            (
                *CASAS_ACCOUNT_KEYS,
                line,
                list(item_keys),
                _history_limit(),
            ),
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


def _latest_stored_value(candidates, field):
    for candidate in candidates:
        value = candidate.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _latest_ref_sku(candidates):
    for candidate in candidates:
        title = str(candidate.get("retailer_sku_name") or "")
        current = normalize_ref_sku(candidate.get("sku"))
        if is_valid_ref_manufacturer_sku(
            current,
            title=title,
        ):
            return current

        # Legacy Casas REF rows intentionally stored the full model in the
        # short column. Promote it only when that historical title independently
        # resolves to the exact same full model; family-only shorts never pass.
        legacy = normalize_ref_sku(candidate.get("sku_short_version"))
        title_sku = casas_ref_title_sku(title)
        if (
            legacy
            and title_sku
            and legacy == title_sku
            and is_valid_ref_manufacturer_sku(legacy, title=title)
        ):
            return legacy
    return ""


def _mark_recovered_field(row, field):
    recovered = row.get(RECOVERED_FIELDS_KEY)
    if not isinstance(recovered, set):
        recovered = (
            set(recovered)
            if isinstance(recovered, (tuple, list, frozenset))
            else set()
        )
        row[RECOVERED_FIELDS_KEY] = recovered
    recovered.add(field)


def recovered_from_last_known_db(row, field):
    recovered = row.get(RECOVERED_FIELDS_KEY)
    return (
        isinstance(recovered, (set, tuple, list, frozenset))
        and field in recovered
    )


def _current_field_needs_recovery(row, field):
    line = str(
        row.get("product_line") or row.get("product") or ""
    ).strip().upper()
    if (
        line == "LDY"
        and field in {"ldy_capacity", "ldy_loading_type"}
        and is_standalone_dryer_title(row.get("retailer_sku_name", ""))
    ):
        return False
    if field == "sku":
        item = _current_identity_key(row)
        return not bool(casas_ref_sku_for_output(row, item))
    return not _nonblank(row.get(field))


def _historical_identity_valid(row, expected_item):
    item = _current_identity_key(row)
    return bool(item and item == expected_item)


def _url_item_key(url):
    parsed = urlsplit(str(url or "").strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host != "casasbahia.com.br" and not host.endswith(
        ".casasbahia.com.br"
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
    return value is not None and bool(str(value).strip())


def _canonical_retailer(value):
    normalized = re.sub(r"[^a-z]", "", str(value or "").casefold())
    return "casas_bahia" if normalized in CASAS_ACCOUNT_KEYS else ""


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
    return ".".join(f'"{part.lower()}"' for part in parts)


def _history_limit():
    try:
        value = int(
            os.getenv(
                "SEDA_CASAS_BAHIA_LAST_KNOWN_HISTORY_LIMIT",
                "30",
            )
        )
    except ValueError:
        value = 30
    return max(1, min(value, 100))


def _statement_timeout_ms():
    try:
        value = int(
            os.getenv(
                "SEDA_CASAS_BAHIA_LAST_KNOWN_DB_TIMEOUT_MS",
                "15000",
            )
        )
    except ValueError:
        value = 15000
    return max(1000, min(value, 60000))


def _env_flag(name):
    return str(os.getenv(name, "0")).strip().casefold() in TRUE_VALUES


def _append_token(existing, token):
    existing = str(existing or "").strip()
    token = str(token or "").strip()
    if not token:
        return existing
    tokens = [part for part in existing.split("+") if part]
    if token in tokens:
        return existing
    return "+".join([*tokens, token])
