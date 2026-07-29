"""Read-only Magalu fallback for fields that would otherwise be blank.

This module never writes to the database. It fills only values that would be
blank in the current output, using the newest non-blank stored value for the
same verified Magalu item. Stored values are trusted as-is; their wording and
format are not reinterpreted by the fallback.
"""

import os
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from ..parsers import (
    is_appliance_spec_token,
    is_obviously_non_sku_magalu_value,
    is_synthetic_magalu_sku_value,
)
from ..step00_config import db_connect, output_table
from .recovery_contract import last_known_fields


TRUE_VALUES = {"1", "true", "yes", "y"}
MAGALU_ACCOUNT_KEYS = ("magalu", "magazineluiza")
RECOVERED_FIELDS_KEY = "_magalu_last_known_db_recovered_fields"


def backfill_magalu_last_known_fields(
    rows,
    *,
    active_retailer,
    product_line_value,
):
    """Fill fields that would otherwise save blank and return safe counters."""
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
            value = _latest_stored_value(candidates, field)
            if not value:
                continue
            row[field] = value
            _mark_recovered_field(row, field)
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


def _latest_stored_value(candidates, field):
    for candidate in candidates:
        value = candidate.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
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
    """Return whether this in-memory field came from read-only DB history."""
    recovered = row.get(RECOVERED_FIELDS_KEY)
    return (
        isinstance(recovered, (set, tuple, list, frozenset))
        and field in recovered
    )


def _current_field_needs_recovery(row, field):
    if field == "sku":
        return _current_sku_needs_recovery(row)
    return not _nonblank(row.get(field))


def _current_sku_needs_recovery(row):
    text = str(row.get("sku") or "").strip()
    if not text:
        return True
    line = str(
        row.get("product_line") or row.get("product") or ""
    ).strip().upper()
    if line in {"REF", "LDY"} and is_appliance_spec_token(text):
        return True
    trusted_tv_reference = (
        line == "TV"
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
    return value is not None and bool(str(value).strip())


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
