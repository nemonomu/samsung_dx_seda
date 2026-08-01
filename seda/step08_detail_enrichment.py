import json
import csv
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .casas_bahia.sku_contract import (
    PDP_HTML_MODEL_TOKEN as CASAS_TV_PDP_HTML_MODEL_TOKEN,
    PRODUCT_SOURCE_MODEL_TOKEN as CASAS_TV_PRODUCT_SOURCE_MODEL_TOKEN,
    exact_modelo_candidate as casas_tv_exact_modelo_candidate,
    has_verified_model_token as casas_tv_has_verified_model_token,
    replace_verified_model_token as replace_casas_tv_verified_model_token,
    casas_tv_sku_for_output,
    verified_model_value as casas_tv_verified_model_value,
)
from .casas_bahia.ldy_sku_contract import (
    BRAND_FIELD as CASAS_LDY_BRAND_FIELD,
    EVIDENCE_FIELD as CASAS_LDY_EVIDENCE_FIELD,
    casas_ldy_short_for_output,
    casas_ldy_sku_for_output,
    resolve_ldy_sku as resolve_casas_ldy_sku,
)
from .casas_bahia.field_extraction import is_standalone_dryer_title
from .casas_bahia.recovery_contract import (
    zenrows_fields as casas_bahia_zenrows_fields,
)
from .casas_bahia.ref_sku_contract import (
    BRAND_FIELD as CASAS_REF_BRAND_FIELD,
    EVIDENCE_FIELD as CASAS_REF_EVIDENCE_FIELD,
    casas_ref_short_for_output,
    casas_ref_sku_for_output,
    normalize_ref_sku,
    resolve_casas_ref_sku,
)
from .detail_publish import (
    detail_run_lock,
    file_sha256,
    mark_detail_publish_incomplete,
    publish_detail_files,
    recover_detail_publish_transaction,
)

from .magalu.field_extraction import (
    ENERGY_ALIAS_LABELS as MAGALU_ENERGY_ALIAS_LABELS,
    ENERGY_CANONICAL_LABELS as MAGALU_ENERGY_CANONICAL_LABELS,
    LDY_ALIAS_LABELS as MAGALU_LDY_ALIAS_LABELS,
    LDY_CANONICAL_LABELS as MAGALU_LDY_CANONICAL_LABELS,
    LOADING_ALIAS_LABELS as MAGALU_LOADING_ALIAS_LABELS,
    LOADING_CANONICAL_LABELS as MAGALU_LOADING_CANONICAL_LABELS,
    REF_FREEZER_LABELS as MAGALU_REF_FREEZER_LABELS,
    REF_GENERIC_LABELS as MAGALU_REF_GENERIC_LABELS,
    REF_LIQUID_TOTAL_LABELS as MAGALU_REF_LIQUID_TOTAL_LABELS,
    REF_REFRIGERATOR_LABELS as MAGALU_REF_REFRIGERATOR_LABELS,
    REF_TOTAL_LABELS as MAGALU_REF_TOTAL_LABELS,
    SCREEN_LABELS as MAGALU_SCREEN_LABELS,
    extract_fields as extract_magalu_semantic_fields,
    is_tv_product_title as is_magalu_tv_product_title,
)
from .magalu.recovery_contract import MAGALU_RECOVERY_FIELD_MAP
from .parsers import (
    CASAS_TV_EXACT_MODELO_FIELD,
    _html_target_label_value_pairs,
    _url_product_identity_matches,
    clean_text,
    compact_json,
    extract_next_data,
    high_confidence_tv_model_number_from_text,
    high_confidence_tv_model_number_from_url,
    is_obviously_non_sku_magalu_value,
    is_synthetic_magalu_sku_value,
    parse_detail,
    remove_accents,
    sku_from_url,
)
from .step00_config import (
    OUTPUT_COLUMNS,
    RETAILERS,
    csv_rows_contract_error,
    product_line,
    read_csv,
    run_root,
    write_csv,
)
from .transport import fetch_url, is_blocked_html


SUBCALL_TRACE_COLUMNS = [
    "row_index",
    "run_token",
    "worker_id",
    "retailer",
    "item",
    "sku",
    "product_url",
    "subcall",
    "label",
    "operation",
    "page",
    "attempt",
    "method",
    "success",
    "status_code",
    "length",
    "has_next_data",
    "item_present",
    "error",
    "graphql_errors",
    "response_preview",
    "detail",
]

REVIEW_PAGE_TRACE_COLUMNS = [
    "row_index",
    "run_token",
    "worker_id",
    "retailer",
    "item",
    "sku",
    "product_url",
    "review_url",
    "page",
    "method",
    "status_code",
    "length",
    "has_next_data",
    "parsed_descriptions",
    "new_descriptions",
    "total_reviews_after",
    "target",
    "error",
]


MAGALU_PDP_SEMANTIC_FIELDS = (
    "screen_size",
    "estimated_annual_electricity_use",
    "model_year",
    "ref_refrigerator_type",
    "ref_capacity",
    "ldy_loading_type",
    "ldy_capacity",
    "ldy_color",
)

# Compatibility alias for the paid field-recovery path's exact seven-field
# contract. The read-only DB fallback has its own extended contract.
MAGALU_ZENROWS_FIELD_MAP = MAGALU_RECOVERY_FIELD_MAP

AUTHORITATIVE_AUDITED_FIELDS = (
    "screen_size",
    "estimated_annual_electricity_use",
    "ref_capacity",
    "ldy_capacity",
    "ldy_loading_type",
)

MAGALU_DOM_LABELS = {
    *MAGALU_SCREEN_LABELS,
    *MAGALU_ENERGY_CANONICAL_LABELS,
    *MAGALU_ENERGY_ALIAS_LABELS,
    *MAGALU_REF_LIQUID_TOTAL_LABELS,
    *MAGALU_REF_TOTAL_LABELS,
    *MAGALU_REF_GENERIC_LABELS,
    *MAGALU_REF_REFRIGERATOR_LABELS,
    *MAGALU_REF_FREEZER_LABELS,
    *MAGALU_LDY_CANONICAL_LABELS,
    *MAGALU_LDY_ALIAS_LABELS,
    *MAGALU_LOADING_CANONICAL_LABELS,
    *MAGALU_LOADING_ALIAS_LABELS,
}

def _merge_missing_detail_fields(row, detail, fields):
    updated = False
    for key in fields:
        if detail.get(key) and not row.get(key):
            row[key] = detail[key]
            updated = True
    return updated


def _relevant_audited_fields(row):
    line = str(row.get("product_line") or product_line()).strip().upper()
    return {
        "TV": ("screen_size", "estimated_annual_electricity_use"),
        "REF": ("estimated_annual_electricity_use", "ref_capacity"),
        "LDY": (
            "estimated_annual_electricity_use",
            "ldy_capacity",
            "ldy_loading_type",
        ),
    }.get(line, ())


def _magalu_zenrows_target_fields(row):
    line = str(row.get("product_line") or product_line()).strip().upper()
    return MAGALU_ZENROWS_FIELD_MAP.get(line, ())


def _magalu_zenrows_missing_fields(row):
    return tuple(
        key for key in _magalu_zenrows_target_fields(row) if not row.get(key)
    )


def _base_url(retailer):
    for config in RETAILERS.values():
        if config.name == retailer:
            return config.base_url
    return ""


def _review_count(value):
    if not value:
        return 0
    try:
        parsed = json.loads(value)
    except ValueError:
        return 1
    return len(parsed) if isinstance(parsed, list) else 0


def _review_values(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        text = str(value or "").strip()
        return [text] if text else []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item or "").strip()]


def _metric_int(value):
    text = str(value or "").strip()
    if not text:
        return -1
    text = text.replace(".", "").replace(",", ".")
    try:
        return int(float(text))
    except ValueError:
        return -1


def _trace_enabled():
    return os.getenv("SEDA_DETAIL_TRACE", "1").lower() not in {"0", "false", "no", "n"}


def _trace_row_base(row, row_index, product_url):
    return {
        "row_index": row_index,
        "run_token": os.getenv("SEDA_DETAIL_RUN_TOKEN", ""),
        "worker_id": os.getenv("SEDA_DETAIL_WORKER_ID", ""),
        "retailer": row.get("retailer", ""),
        "item": row.get("item", ""),
        "sku": row.get("sku", ""),
        "product_url": product_url or row.get("product_url", ""),
    }


def _compact_trace_value(value, limit=500):
    if value in ("", None):
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _record_subcall(trace_rows, row, row_index, product_url, subcall, **values):
    if trace_rows is None:
        return
    record = _trace_row_base(row, row_index, product_url)
    record.update(
        {
            "subcall": subcall,
            "label": values.get("label", ""),
            "operation": values.get("operation", ""),
            "page": values.get("page", ""),
            "attempt": values.get("attempt", ""),
            "method": values.get("method", ""),
            "success": int(bool(values.get("success"))),
            "status_code": values.get("status_code", ""),
            "length": values.get("length", ""),
            "has_next_data": values.get("has_next_data", ""),
            "item_present": values.get("item_present", ""),
            "error": _compact_trace_value(values.get("error", "")),
            "graphql_errors": _compact_trace_value(values.get("graphql_errors", "")),
            "response_preview": _compact_trace_value(values.get("response_preview", "")),
            "detail": _compact_trace_value(values.get("detail", "")),
        }
    )
    trace_rows.append(record)


def _result_trace_detail(item, detail, overall_success):
    if "errors" in item:
        base = item.get("errors")
    else:
        base = (
            f"{detail}; overall_success:{int(overall_success)}"
            if detail
            else f"overall_success:{int(overall_success)}"
        )
    parts = []
    content_type = _compact_trace_value(item.get("content_type", ""))
    if content_type:
        parts.append(f"content_type:{content_type}")
    if base not in ("", None):
        parts.append(_compact_trace_value(base))
    for key in (
        "terminal_business_error",
        "recovery",
        "recovery_error",
        "showcase_failed_fetch_circuit_open",
    ):
        value = item.get(key)
        if value in ("", None, False):
            continue
        if value is True:
            value = 1
        parts.append(f"{key}:{_compact_trace_value(value)}")
    return "; ".join(part for part in parts if part)


def _record_result_trace(trace_rows, row, row_index, product_url, subcall, result, success=None, detail=""):
    if trace_rows is None:
        return
    result = result or {}
    overall_success = bool(result.get("success")) if success is None else bool(success)
    trace = result.get("trace") or []
    if not trace:
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            subcall,
            success=overall_success,
            error=result.get("error", ""),
            detail=detail,
        )
        return
    for item in trace:
        try:
            status_code = int(item.get("status_code") or 0)
        except (TypeError, ValueError):
            status_code = 0
        item_success = not item.get("error")
        if status_code:
            item_success = item_success and 200 <= status_code < 300
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            subcall,
            label=item.get("label", ""),
            operation=item.get("operation", ""),
            page=item.get("page", ""),
            attempt=item.get("attempt", ""),
            method=item.get("method", ""),
            success=item_success,
            status_code=item.get("status_code", ""),
            length=item.get("length", ""),
            item_present=item.get("item_present", ""),
            error=item.get("error", ""),
            graphql_errors=item.get("errors", item.get("graphql_errors", "")),
            response_preview=item.get("response_preview", ""),
            detail=_result_trace_detail(item, detail, overall_success),
        )


def _record_review_page_trace(trace_rows, row, row_index, product_url, review_url, item):
    if trace_rows is None:
        return
    record = _trace_row_base(row, row_index, product_url)
    record.update(
        {
            "review_url": review_url,
            "page": item.get("page", ""),
            "method": item.get("method", ""),
            "status_code": item.get("status_code", ""),
            "length": item.get("length", ""),
            "has_next_data": int(bool(item.get("has_next_data"))),
            "parsed_descriptions": item.get("descriptions", 0),
            "new_descriptions": item.get("new_descriptions", 0),
            "total_reviews_after": item.get("total_reviews_after", ""),
            "target": item.get("target", ""),
            "error": _compact_trace_value(item.get("error", "")),
        }
    )
    trace_rows.append(record)


def _write_trace_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in columns})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _detail_trace_path(root, stem, tag=None):
    if tag is None:
        tag = os.getenv("SEDA_DETAIL_TRACE_TAG", "").strip()
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tag or "")).strip("._")
    suffix = f"_{safe_tag}" if safe_tag else ""
    return root / "detail" / "trace" / f"{stem}{suffix}.csv"


def _write_detail_traces(root, subcall_trace_rows, review_page_trace_rows):
    if not _trace_enabled():
        return
    _write_trace_csv(_detail_trace_path(root, "subcall_trace"), subcall_trace_rows, SUBCALL_TRACE_COLUMNS)
    _write_trace_csv(
        _detail_trace_path(root, "magalu_review_page_trace"),
        review_page_trace_rows,
        REVIEW_PAGE_TRACE_COLUMNS,
    )


def _exact_csv_header_error(path, expected_columns):
    path = Path(path)
    if not path.is_file():
        return "missing"
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), None)
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    if header is None:
        return "missing_header"
    duplicates = sorted(
        {column for column in header if header.count(column) > 1}
    )
    if duplicates:
        return f"duplicate_columns:{','.join(duplicates)}"
    missing = [column for column in expected_columns if column not in header]
    if missing:
        return f"missing_columns:{','.join(missing)}"
    unexpected = [column for column in header if column not in expected_columns]
    if unexpected:
        return f"unexpected_columns:{','.join(unexpected)}"
    if header != list(expected_columns):
        return "column_order"
    return ""


def _trace_identity_error(
    trace_rows,
    expected_rows,
    *,
    start_index,
    require_coverage,
    run_token=None,
    worker_id=None,
):
    first_index = start_index + 1
    last_index = start_index + len(expected_rows)
    covered = set()
    for position, trace_row in enumerate(trace_rows, start=1):
        try:
            row_index = int(trace_row.get("row_index") or 0)
        except (TypeError, ValueError):
            return f"row_index_invalid:trace_row={position}"
        if row_index < first_index or row_index > last_index:
            return f"row_index_out_of_slice:{row_index}:{first_index}-{last_index}"
        if run_token is not None and str(trace_row.get("run_token") or "") != str(run_token):
            return f"run_token:row={row_index}"
        if worker_id is not None and str(trace_row.get("worker_id") or "") != str(worker_id):
            return f"worker_id:row={row_index}"
        expected = expected_rows[row_index - first_index]
        if str(trace_row.get("item") or "") != str(expected.get("item") or ""):
            return f"item_identity:row={row_index}"
        if str(trace_row.get("product_url") or "") != str(expected.get("product_url") or ""):
            return f"url_identity:row={row_index}"
        covered.add(row_index)
    if require_coverage:
        missing = [
            str(index)
            for index in range(first_index, last_index + 1)
            if index not in covered
        ]
        if missing:
            return f"coverage_missing:{','.join(missing)}"
    return ""


def _merge_parallel_detail_traces(root, parts, rows, run_token):
    """Collect and validate every trace part without publishing canonicals."""
    if not _trace_enabled():
        return [], []
    subcall_rows = []
    review_rows = []
    for worker_id, start, end, _part, tag in parts:
        expected_rows = rows[start:end]
        for stem, columns, target, require_coverage in (
            ("subcall_trace", SUBCALL_TRACE_COLUMNS, subcall_rows, True),
            (
                "magalu_review_page_trace",
                REVIEW_PAGE_TRACE_COLUMNS,
                review_rows,
                False,
            ),
        ):
            path = _detail_trace_path(root, stem, tag=tag)
            header_error = _exact_csv_header_error(path, columns)
            if header_error:
                raise RuntimeError(
                    f"detail_parallel_invalid_trace:worker={worker_id}:"
                    f"{stem}:{header_error}:path={path}"
                )
            part_rows = read_csv(str(path))
            error = _trace_identity_error(
                part_rows,
                expected_rows,
                start_index=start,
                require_coverage=require_coverage,
                run_token=run_token,
                worker_id=worker_id,
            )
            if error:
                raise RuntimeError(
                    f"detail_parallel_invalid_trace:worker={worker_id}:"
                    f"{stem}:{error}"
                )
            target.extend(part_rows)
    return subcall_rows, review_rows


def _resume_detail_trace_prefix(root, expected_rows):
    """Load the canonical trace prefix and validate it before any new request."""
    if not expected_rows or not _trace_enabled():
        return [], []
    loaded = []
    for stem, columns, require_coverage in (
        ("subcall_trace", SUBCALL_TRACE_COLUMNS, True),
        ("magalu_review_page_trace", REVIEW_PAGE_TRACE_COLUMNS, False),
    ):
        path = _detail_trace_path(root, stem, tag="")
        header_error = _exact_csv_header_error(path, columns)
        if header_error:
            raise RuntimeError(
                f"detail_resume_invalid_trace:{stem}:{header_error}:path={path}"
            )
        all_rows = read_csv(str(path))
        prefix = []
        for trace_row in all_rows:
            try:
                row_index = int(trace_row.get("row_index") or 0)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"detail_resume_invalid_trace:{stem}:row_index_invalid"
                ) from exc
            if row_index <= len(expected_rows):
                prefix.append(trace_row)
        error = _trace_identity_error(
            prefix,
            expected_rows,
            start_index=0,
            require_coverage=require_coverage,
        )
        if error:
            raise RuntimeError(f"detail_resume_invalid_trace:{stem}:{error}")
        loaded.append(prefix)
    return tuple(loaded)


def _remove_parallel_detail_trace_parts(root, trace_tags):
    if not _trace_enabled():
        return
    for tag in trace_tags:
        for stem in ("subcall_trace", "magalu_review_page_trace"):
            try:
                _detail_trace_path(root, stem, tag=tag).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(
                    f"[seda] detail trace cleanup warning path="
                    f"{_detail_trace_path(root, stem, tag=tag)} error={exc}",
                    flush=True,
                )


def _magalu_review_target(row):
    try:
        review_limit = int(os.getenv("SEDA_MAGALU_REVIEW_LIMIT", "20"))
    except ValueError:
        review_limit = 20
    review_count = _metric_int(row.get("count_of_reviews"))
    if review_count >= 0:
        return min(review_limit, review_count)
    return review_limit


def _magalu_review_graphql_failure_reason(result):
    """Return why ProductRating did not provide an explicit review count."""
    if not isinstance(result, dict):
        return "count_missing:invalid_result"
    if _magalu_review_count_present(result):
        return ""

    reason = result.get("error") or ""
    trace = result.get("trace")
    if isinstance(trace, list):
        for item in reversed(trace):
            if isinstance(item, dict) and item.get("error"):
                reason = item.get("error")
                break
    if not reason:
        return "count_missing"
    safe_reason = re.sub(
        r"[^a-z0-9_.-]+",
        "_",
        str(reason).casefold(),
    ).strip("_")[:80]
    return f"count_missing:{safe_reason or 'unknown'}"


def _magalu_review_count_value_present(value):
    if value in ("", None) or isinstance(value, bool):
        return False
    text = str(value).strip()
    if not text:
        return False
    normalized = text.replace(".", "").replace(",", ".")
    try:
        number = float(normalized)
        return math.isfinite(number) and number >= 0
    except (TypeError, ValueError):
        return False


def _magalu_review_count_present(result):
    if not isinstance(result, dict):
        return False
    general = result.get("general")
    general = general if isinstance(general, dict) else {}
    if _magalu_review_count_value_present(general.get("commentCount")):
        return True
    page = result.get("page")
    page = page if isinstance(page, dict) else {}
    return _magalu_review_count_value_present(page.get("totalItems"))


def _magalu_html_headers():
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
    }


def _fetch_magalu_next_html(url, label="html"):
    timeout = int(os.getenv("SEDA_MAGALU_HTML_TIMEOUT", os.getenv("SEDA_TIMEOUT", "60")))
    last = {"status_code": 0, "text": "", "error": "not_attempted", "method": "", "label": label}
    if os.getenv("SEDA_MAGALU_HTML_REQUESTS_FETCH", "1").lower() not in {"0", "false", "no", "n"}:
        try:
            response = requests.get(url, headers=_magalu_html_headers(), timeout=timeout)
            last = {
                "status_code": response.status_code,
                "text": response.text or "",
                "error": "",
                "method": "requests",
                "label": label,
            }
            if response.status_code == 200 and "__NEXT_DATA__" in (response.text or ""):
                return last
            last["error"] = f"requests_missing_next_data:{response.status_code}:len={len(response.text or '')}"
        except Exception as exc:
            last = {"status_code": 0, "text": "", "error": f"requests_error:{type(exc).__name__}: {exc}", "method": "requests", "label": label}
    if os.getenv("SEDA_MAGALU_HTML_BROWSER_FALLBACK", "0").lower() in {"0", "false", "no", "n"}:
        return last
    try:
        from .magalu.browser_session import fetch_html

        result = fetch_html(url)
        text = result.get("text") or ""
        return {
            "status_code": result.get("status_code") or 0,
            "text": text,
            "error": result.get("error") or ("" if "__NEXT_DATA__" in text else "browser_missing_next_data"),
            "method": "browser",
            "label": label,
        }
    except Exception as exc:
        if not last.get("error"):
            last["error"] = f"browser_error:{type(exc).__name__}: {exc}"
        else:
            last["error"] = f"{last['error']}|browser_error:{type(exc).__name__}: {exc}"
        return last


def _magalu_review_url(product_url):
    parsed = urlsplit(product_url)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        p_index = parts.index("p")
    except ValueError:
        return ""
    if p_index < 1 or len(parts) <= p_index + 3:
        return ""
    slug = parts[p_index - 1]
    item_id = parts[p_index + 1]
    category = parts[p_index + 2].upper()
    subcategory = parts[p_index + 3].upper()
    path = f"/review/{item_id}/{slug}/{category}/{subcategory}/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _magalu_review_page_url(review_url, page):
    if page <= 1:
        return review_url
    parsed = urlsplit(review_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _merge_magalu_reviews(row, product_url, trace_rows=None, row_index=""):
    if row.get("retailer") != "Magalu":
        return None
    if os.getenv("SEDA_MAGALU_REVIEW_GRAPHQL", "1").lower() in {"0", "false", "no", "n"}:
        _record_subcall(trace_rows, row, row_index, product_url, "review_graphql", success=False, error="disabled")
        return None
    existing_review_count = _review_count(row.get("detailed_review_content"))
    review_limit = int(os.getenv("SEDA_MAGALU_REVIEW_LIMIT", "20"))
    if existing_review_count >= review_limit:
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "review_graphql",
            success=True,
            detail=f"already_has_reviews:{existing_review_count}",
        )
        return None
    if existing_review_count and os.getenv("SEDA_MAGALU_REVIEW_GRAPHQL_AFTER_HTML", "0").lower() not in {"1", "true", "yes", "y"}:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"reviews_html_{existing_review_count}")
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "review_graphql",
            success=False,
            error="skipped_existing_html_reviews",
            detail=f"existing_review_count:{existing_review_count}",
        )
        return None
    if os.getenv("SEDA_MAGALU_SKIP_REVIEW_WITHOUT_RATING", "1").lower() not in {"0", "false", "no", "n"}:
        if not row.get("star_rating") and not row.get("count_of_star_ratings"):
            row["parse_status"] = _append_token(row.get("parse_status", ""), "reviews_skipped_no_rating")
            _record_subcall(trace_rows, row, row_index, product_url, "review_graphql", success=False, error="skipped_no_rating")
            return None

    from .magalu.review_api import fetch_product_rating

    limit = int(os.getenv("SEDA_MAGALU_REVIEW_LIMIT", "20"))
    try:
        review_count = int(float(str(row.get("count_of_reviews") or "").replace(".", "").replace(",", ".")))
    except ValueError:
        review_count = -1
    if review_count >= 0:
        limit = min(limit, review_count)
    if limit <= 0:
        return None
    result = fetch_product_rating(sku_from_url(product_url) or row.get("sku"), limit=limit, context_url=product_url)
    _record_result_trace(trace_rows, row, row_index, product_url, "review_graphql", result, detail=f"limit:{limit}")
    failure_reason = _magalu_review_graphql_failure_reason(result)
    if failure_reason:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            f"reviews_graphql_failed:{failure_reason}",
        )
    reviews = result.get("reviews") or []
    if reviews:
        row["detailed_review_content"] = compact_json(reviews)
        row["fetch_method"] = _append_token(row.get("fetch_method", ""), result.get("method", "graphql_product_rating"))
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"reviews_{len(reviews)}")

    general = result.get("general")
    general = general if isinstance(general, dict) else {}
    if general:
        row["star_rating"] = general.get("rating", "") or row.get("star_rating", "")
        row["count_of_star_ratings"] = general.get("reviewCount", "") or row.get("count_of_star_ratings", "")
    comment_count = general.get("commentCount")
    page = result.get("page")
    page = page if isinstance(page, dict) else {}
    page_count = page.get("totalItems")
    if _magalu_review_count_value_present(comment_count):
        row["count_of_reviews"] = comment_count
    elif _magalu_review_count_value_present(page_count):
        row["count_of_reviews"] = page_count

    # summarized_review_content via reviewSummaryQuery on the browser GraphQL channel
    # (PDP HTML is Akamai-403 on every product; this needs no PDP HTML / ZenRows).
    # Gated off by default until the captured query shape is verified.
    if (
        not row.get("summarized_review_content")
        and os.getenv("SEDA_MAGALU_REVIEW_SUMMARY_GRAPHQL", "1").lower() not in {"0", "false", "no", "n"}
    ):
        from .magalu.review_api import fetch_review_summary

        # reviewSummary(productId:) keys on the /p/<id> item id, not productRating.productId
        summary_result = fetch_review_summary(
            product_id=sku_from_url(product_url) or row.get("item") or row.get("sku"),
            variation_id=result.get("product_id"),
            context_url=product_url,
        )
        _record_result_trace(trace_rows, row, row_index, product_url, "review_summary_graphql", summary_result)
        if summary_result.get("success"):
            row["summarized_review_content"] = summary_result["summary"]
            row["fetch_method"] = _append_token(
                row.get("fetch_method", ""), summary_result.get("method", "graphql_review_summary")
            )
            row["parse_status"] = _append_token(row.get("parse_status", ""), "review_summary_graphql")
        else:
            row["parse_status"] = _append_token(
                row.get("parse_status", ""), f"review_summary_failed:{summary_result.get('error', 'unknown')}"
            )
    return result


def _merge_casas_bahia_apis(row):
    if row.get("retailer") != "Casas Bahia":
        return
    _clear_legacy_casas_recommendation_default(row)
    if os.getenv("SEDA_CASAS_BAHIA_API_ENRICH", "1").lower() in {"0", "false", "no", "n"}:
        return

    product_id = row.get("retailer_product_id", "")
    url_sku_id = sku_from_url(row.get("product_url", ""))
    sku_id = url_sku_id or (
        "" if _is_casas_bahia_ldy_row(row) else row.get("sku", "")
    )
    seller_id = row.get("seller_id", "") or os.getenv("SEDA_CASAS_BAHIA_DEFAULT_SELLER_ID", "10037")

    try:
        from .casas_bahia.detail_api import fetch_freight, fetch_pickup, fetch_product_source, fetch_similar_names

        if (
            os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API", "1").lower()
            not in {"0", "false", "no", "n"}
            and not _skip_casas_sku_api(row, sku_id, "product_source")
        ):
            product_source = fetch_product_source(sku_id)
            if product_source.get("success"):
                product_detail = product_source.get("detail") or {}
                if product_detail.get("retailer_sku_name"):
                    if _is_casas_bahia_ldy_row(row):
                        _merge_casas_bahia_ldy_product_source(
                            row,
                            product_detail,
                        )
                    elif _is_casas_bahia_ref_row(row):
                        _merge_casas_bahia_ref_detail(
                            row,
                            product_detail,
                            identity_verified=True,
                        )
                    else:
                        _merge_casas_bahia_authoritative_detail(
                            row,
                            product_detail,
                            CASAS_TV_PRODUCT_SOURCE_MODEL_TOKEN,
                        )
                    row["fetch_method"] = _append_token(
                        row.get("fetch_method", ""), product_source.get("method", "casas_bahia_product_source_api")
                    )
                    cost = (product_source.get("headers") or {}).get("X-Request-Cost", "")
                    if cost:
                        row["parse_status"] = _append_token(row.get("parse_status", ""), f"product_source_cost:{cost}")
                else:
                    row["parse_status"] = _append_token(row.get("parse_status", ""), "product_source_missing_identity")
            else:
                row["parse_status"] = _append_token(
                    row.get("parse_status", ""), f"product_source_failed:{product_source.get('error','unknown')}"
                )

        product_id = row.get("retailer_product_id", "") or product_id

        if (
            os.getenv("SEDA_CASAS_BAHIA_FREIGHT_API", "1").lower()
            not in {"0", "false", "no", "n"}
            and not _skip_casas_sku_api(row, sku_id, "freight_api")
        ):
            freight = fetch_freight(sku_id, seller_id, referer_url=row.get("product_url", ""))
            if freight.get("success"):
                _merge_non_empty(row, freight.get("detail") or {})
                row["fetch_method"] = _append_token(row.get("fetch_method", ""), freight.get("method", "casas_bahia_freight_api"))
                cost = (freight.get("headers") or {}).get("X-Request-Cost", "")
                if cost:
                    row["parse_status"] = _append_token(row.get("parse_status", ""), f"freight_cost:{cost}")
            else:
                row["parse_status"] = _append_token(row.get("parse_status", ""), f"freight_api_failed:{freight.get('error','unknown')}")

        if (
            os.getenv("SEDA_CASAS_BAHIA_PICKUP_API", "1").lower()
            not in {"0", "false", "no", "n"}
            and not _skip_casas_sku_api(row, sku_id, "pickup_api")
        ):
            pickup = fetch_pickup(sku_id, seller_id)
            if pickup.get("success"):
                _merge_non_empty(row, pickup.get("detail") or {})
                row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_pickup_api")
            else:
                row["parse_status"] = _append_token(row.get("parse_status", ""), f"pickup_api_failed:{pickup.get('error','unknown')}")

        if os.getenv("SEDA_CASAS_BAHIA_RECS_API", "1").lower() not in {"0", "false", "no", "n"}:
            similar = fetch_similar_names(product_id, sku_id=sku_id, current_product=row)
            if similar.get("success") and similar.get("names"):
                row["retailer_sku_name_similar"] = compact_json(similar.get("names"))
                row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_recs_api")
            elif similar.get("success"):
                row["parse_status"] = _append_token(
                    row.get("parse_status", ""),
                    f"recs_empty:{similar.get('source_count', 0)}:{similar.get('filtered_count', 0)}",
                )
            elif not similar.get("success"):
                row["parse_status"] = _append_token(row.get("parse_status", ""), f"recs_api_failed:{similar.get('error','unknown')}")
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"casas_bahia_detail_api_error:{type(exc).__name__}")

    if os.getenv("SEDA_CASAS_BAHIA_REVIEW_API", "1").lower() in {"0", "false", "no", "n"}:
        return
    if _review_count(row.get("detailed_review_content")) >= int(os.getenv("SEDA_CASAS_BAHIA_REVIEW_LIMIT", "20")):
        return
    try:
        from .casas_bahia.review_api import fetch_reviews

        result = fetch_reviews(product_id, referer_url=row.get("product_url", ""))
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"reviews_api_error:{type(exc).__name__}")
        return
    review_method = result.get("method", "casas_bahia_reviews_api")
    zenrows_requested = bool(result.get("zenrows_requested"))
    if zenrows_requested:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            "reviews_zenrows_requested:10x",
        )
    cost = (result.get("headers") or {}).get("X-Request-Cost", "")
    if cost:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            f"reviews_cost:{cost}",
        )
    if result.get("success"):
        general = result.get("general") or {}
        reviews = result.get("reviews") or []
        summary = result.get("summary") or general.get("summary", "")
        if summary and not row.get("summarized_review_content"):
            row["summarized_review_content"] = summary
        if reviews:
            row["detailed_review_content"] = compact_json(reviews)
            row["fetch_method"] = _append_token(row.get("fetch_method", ""), review_method)
            seen = int(result.get("review_items_seen") or len(reviews))
            status = f"reviews_{len(reviews)}/{seen}" if seen != len(reviews) else f"reviews_{len(reviews)}"
            row["parse_status"] = _append_token(row.get("parse_status", ""), status)
        elif zenrows_requested:
            row["fetch_method"] = _append_token(row.get("fetch_method", ""), review_method)
        _merge_zero_preserving(row, "star_rating", general.get("rating", ""))
        _merge_zero_preserving(row, "count_of_star_ratings", general.get("ratingQty", ""))
        _merge_zero_preserving(row, "count_of_reviews", general.get("ratingQty", ""))
        if general.get("recommendationPercentage") not in ("", None):
            row["recommendation_intent"] = f"{general.get('recommendationPercentage')}% dos clientes recomendam esse produto"
    else:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"reviews_api_failed:{result.get('error','unknown')}")


def _clear_legacy_casas_recommendation_default(row):
    tokens = str(row.get("parse_status") or "").split("+")
    value = str(row.get("recommendation_intent") or "").strip().casefold()
    if (
        "recommendation_default_0" in tokens
        and value == "0% dos clientes recomendam esse produto"
    ):
        row["recommendation_intent"] = ""
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            "recommendation_default_0_cleared",
        )


def _clear_magalu_listing_metrics(row):
    if row.get("retailer") != "Magalu":
        return
    for key in ("star_rating", "count_of_star_ratings", "count_of_reviews"):
        row[key] = ""


def _merge_non_empty(row, detail):
    row.update(
        {
            key: value
            for key, value in detail.items()
            if not str(key).startswith("_") and value not in ("", None, [], {})
        }
    )


def _is_magalu_tv_row(row):
    line = str(row.get("product_line") or product_line()).strip().upper()
    return row.get("retailer") == "Magalu" and line == "TV"


def _is_casas_bahia_tv_row(row):
    line = str(row.get("product_line") or product_line()).strip().upper()
    return row.get("retailer") == "Casas Bahia" and line == "TV"


def _is_casas_bahia_ldy_row(row):
    line = str(row.get("product_line") or product_line()).strip().upper()
    return row.get("retailer") == "Casas Bahia" and line == "LDY"


def _is_casas_bahia_ref_row(row):
    line = str(row.get("product_line") or product_line()).strip().upper()
    return row.get("retailer") == "Casas Bahia" and line == "REF"


def _skip_casas_sku_api(row, sku_id, api_name):
    if sku_id or not _is_casas_bahia_ldy_row(row):
        return False
    row["parse_status"] = _append_token(
        row.get("parse_status", ""),
        f"{api_name}_skipped:missing_url_item",
    )
    return True


def _merge_casas_bahia_ldy_detail(
    row,
    detail,
    *,
    identity_verified=False,
):
    if not _is_casas_bahia_ldy_row(row) or not identity_verified:
        return False
    brand = detail.get(CASAS_LDY_BRAND_FIELD, "")
    evidence = detail.get(CASAS_LDY_EVIDENCE_FIELD) or ()
    if not evidence and detail.get("sku"):
        evidence = (detail.get("sku"),)
    title = (
        detail.get("retailer_sku_name")
        or row.get("retailer_sku_name", "")
    )
    resolution = resolve_casas_ldy_sku(
        row.get("sku", ""),
        title,
        evidence,
        brand=brand,
    )
    detail_status = detail.get("parse_status", "")
    safe_detail = {
        key: value
        for key, value in detail.items()
        if not str(key).startswith("_")
        and key not in {"sku", "sku_short_version", "parse_status"}
    }
    _merge_authoritative_detail(
        row,
        safe_detail,
        identity_verified=True,
    )
    if brand:
        row[CASAS_LDY_BRAND_FIELD] = brand
    row["sku"] = resolution.sku
    if detail_status:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            detail_status,
        )
    for token in resolution.status_tokens:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            token,
        )
    row["sku_short_version"] = casas_ldy_short_for_output(
        row,
        resolution.sku,
    )
    return bool(resolution.sku)


def _merge_casas_bahia_ldy_product_source(row, detail):
    return _merge_casas_bahia_ldy_detail(
        row,
        detail,
        identity_verified=True,
    )


def _merge_casas_bahia_ref_detail(
    row,
    detail,
    *,
    identity_verified=False,
):
    if not _is_casas_bahia_ref_row(row) or not identity_verified:
        return False
    detail_status = detail.get("parse_status", "")
    brand = detail.get(CASAS_REF_BRAND_FIELD, "")
    evidence = detail.get(CASAS_REF_EVIDENCE_FIELD) or ()
    if not evidence and detail.get("sku"):
        evidence = (detail.get("sku"),)
    title = (
        detail.get("retailer_sku_name")
        or row.get("retailer_sku_name", "")
    )
    safe_detail = {
        key: value
        for key, value in detail.items()
        if not str(key).startswith("_")
        and key not in {"sku", "sku_short_version", "parse_status"}
    }
    _merge_authoritative_detail(
        row,
        safe_detail,
        identity_verified=True,
    )
    if brand:
        row[CASAS_REF_BRAND_FIELD] = brand
    previous_sku = normalize_ref_sku(row.get("sku", ""))
    resolution = resolve_casas_ref_sku(
        row.get("sku") or row.get("sku_short_version", ""),
        title,
        evidence,
        brand=brand,
    )
    row["sku"] = resolution.sku
    if previous_sku != normalize_ref_sku(resolution.sku):
        row["sku_short_version"] = ""
    row["sku_short_version"] = casas_ref_short_for_output(
        row,
        resolution.sku,
    )
    if detail_status:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            detail_status,
        )
    for token in resolution.status_tokens:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            token,
        )
    return bool(resolution.sku)


def _mark_casas_tv_verified_sku(row, detail, token):
    if not _is_casas_bahia_tv_row(row):
        return False
    candidate = casas_tv_exact_modelo_candidate(row, detail)
    if not candidate or clean_text(row.get("sku")) != candidate:
        return False
    row["parse_status"] = replace_casas_tv_verified_model_token(
        row.get("parse_status", ""),
        token,
    )
    return True


def _merge_casas_bahia_authoritative_detail(
    row,
    detail,
    model_token,
    *,
    identity_verified=False,
):
    if not _is_casas_bahia_tv_row(row):
        _merge_authoritative_detail(
            row,
            detail,
            identity_verified=identity_verified,
        )
        return False

    candidate = casas_tv_exact_modelo_candidate(row, detail)
    preserve_product_source = (
        model_token == CASAS_TV_PDP_HTML_MODEL_TOKEN
        and casas_tv_has_verified_model_token(
            row,
            CASAS_TV_PRODUCT_SOURCE_MODEL_TOKEN,
        )
        and casas_tv_verified_model_value(
            row,
            sku_from_url(row.get("product_url", "")),
        )
    )
    excluded = {"parse_status"}
    if not candidate or preserve_product_source:
        excluded.add("sku")
    safe_detail = {
        key: value
        for key, value in detail.items()
        if key not in excluded
    }
    detail_status = detail.get("parse_status", "")
    _merge_authoritative_detail(
        row,
        safe_detail,
        identity_verified=identity_verified,
    )
    if detail_status:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            detail_status,
        )
    if not candidate or preserve_product_source:
        return False
    return _mark_casas_tv_verified_sku(row, detail, model_token)


def _magalu_tv_sku_is_recovery_target(row):
    current = str(row.get("sku") or "").strip().casefold()
    if not current:
        return True
    if is_obviously_non_sku_magalu_value(row.get("sku")):
        return True
    status_tokens = set(str(row.get("parse_status") or "").split("+"))
    if "sku_factsheet_reference_recovered" in status_tokens:
        return False
    sentinels = {
        str(row.get("item") or "").strip().casefold(),
        str(sku_from_url(row.get("product_url", "")) or "").strip().casefold(),
    }
    return current in {
        value for value in sentinels if value
    } or is_synthetic_magalu_sku_value(row.get("sku"))


def _merge_magalu_tv_reference_sku(row, detail, *, identity_verified):
    """Apply title-first recovery without replacing an existing valid SKU.

    One high-confidence product-title model is the first recovery candidate.
    Referencia is used only when the title has no safe model candidate.
    """
    if not _is_magalu_tv_row(row):
        return False
    reference = str(detail.get("_magalu_factsheet_reference") or "").strip()
    title = detail.get("retailer_sku_name") or row.get("retailer_sku_name")
    screen_size_hint = detail.get("screen_size") or row.get("screen_size")
    title_model = ""
    url_model = ""
    if is_magalu_tv_product_title(title):
        title_model = high_confidence_tv_model_number_from_text(
            title,
            screen_size_hint=screen_size_hint,
        )
        if not title_model:
            url_model = high_confidence_tv_model_number_from_url(
                row.get("product_url"),
                screen_size_hint=screen_size_hint,
            )
    safe_reference = (
        reference if not is_obviously_non_sku_magalu_value(reference) else ""
    )
    candidate = title_model or url_model or safe_reference
    actual_item = str(detail.get("_detail_item_id") or "").strip().casefold()
    expected_items = {
        str(row.get("item") or "").strip().casefold(),
        str(sku_from_url(row.get("product_url", "")) or "").strip().casefold(),
    }
    expected_items.discard("")
    identity_matches = bool(actual_item) and bool(expected_items) and all(
        actual_item == expected for expected in expected_items
    )
    if not identity_verified or not identity_matches or not candidate:
        return False

    recovery_target = _magalu_tv_sku_is_recovery_target(row)
    current_sku = str(row.get("sku") or "").strip()
    if not recovery_target:
        if current_sku != candidate:
            row["parse_status"] = _append_token(
                row.get("parse_status", ""),
                "sku_reference_conflict_preserved",
            )
        return False

    if title_model or url_model:
        row["sku"] = title_model or url_model
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            (
                "sku_title_high_confidence_recovered"
                if title_model
                else "sku_url_high_confidence_recovered"
            ),
        )
        return True

    row["sku"] = safe_reference
    row["parse_status"] = _append_token(
        row.get("parse_status", ""),
        "sku_factsheet_reference_recovered",
    )
    return True


def _normalized_detail_name(value):
    text = remove_accents(clean_text(value)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _detail_identity_mode(row, detail):
    if detail.get("_detail_identity_conflict") is True:
        return "conflict"
    if detail.get("_detail_identity_verified") is True:
        return "verified"
    existing_name = _normalized_detail_name(row.get("retailer_sku_name"))
    detail_name = _normalized_detail_name(detail.get("retailer_sku_name"))
    if existing_name and detail_name and existing_name == detail_name:
        return "same_name"
    return ""


def _merge_authoritative_detail(row, detail, *, identity_verified=False):
    """Merge a verified product-detail payload, including explicit audited blanks.

    Listing values are only hints.  Once an authoritative detail producer has
    evaluated an audited field, an explicit blank means that the listing value
    was not valid for the product and must be cleared rather than retained.
    Fields omitted from the payload remain untouched.
    """
    if _is_magalu_tv_row(row):
        _merge_non_empty(row, {key: value for key, value in detail.items() if key != "sku"})
        _merge_magalu_tv_reference_sku(
            row,
            detail,
            identity_verified=identity_verified,
        )
    else:
        _merge_non_empty(row, detail)
    for key in AUTHORITATIVE_AUDITED_FIELDS:
        if key not in detail:
            continue
        value = detail.get(key)
        row[key] = "" if value in ("", None, [], {}) else value


def _merge_generic_product_detail(row, detail):
    """Apply product detail according to its identity proof strength."""
    mode = _detail_identity_mode(row, detail)
    if mode == "verified":
        if _is_casas_bahia_tv_row(row):
            _merge_casas_bahia_authoritative_detail(
                row,
                detail,
                CASAS_TV_PDP_HTML_MODEL_TOKEN,
                identity_verified=True,
            )
        elif _is_casas_bahia_ldy_row(row):
            _merge_casas_bahia_ldy_detail(
                row,
                detail,
                identity_verified=True,
            )
        elif _is_casas_bahia_ref_row(row):
            _merge_casas_bahia_ref_detail(
                row,
                detail,
                identity_verified=True,
            )
        else:
            _merge_authoritative_detail(row, detail, identity_verified=True)
        return True
    if mode == "same_name":
        fields = tuple(
            key
            for key in detail
            if not str(key).startswith("_")
            and not (
                (_is_magalu_tv_row(row) or _is_casas_bahia_tv_row(row))
                and key == "sku"
            )
            and not (
                _is_casas_bahia_ldy_row(row)
                and key in {"sku", "sku_short_version"}
            )
            and not (
                _is_casas_bahia_ref_row(row)
                and key in {"sku", "sku_short_version"}
            )
        )
        return _merge_missing_detail_fields(row, detail, fields)
    # Explicit conflict or absent identity: merge no product-bound fields.
    return False


def _merge_zero_preserving(row, key, value):
    if value in ("", None, [], {}):
        return
    row[key] = _metric_text(value)


def _metric_text(value):
    if value in ("", None):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text
    if number == 0:
        return "0"
    return f"{number:g}"


def _magalu_graphql_detail(
    row,
    product_url,
    trace_rows=None,
    row_index="",
    trace_subcall="detail_graphql",
):
    if row.get("retailer") != "Magalu":
        return None
    if os.getenv("SEDA_MAGALU_DETAIL_GRAPHQL", "1").lower() in {"0", "false", "no", "n"}:
        _record_subcall(trace_rows, row, row_index, product_url, trace_subcall, success=False, error="disabled")
        return None

    from .magalu.detail_api import fetch_detail

    item_id = sku_from_url(product_url) or row.get("item") or row.get("sku")
    result = fetch_detail(item_id, seller_id=_magalu_seller_id(row, product_url), context_url=product_url)
    _record_result_trace(trace_rows, row, row_index, product_url, trace_subcall, result, detail=f"item_id:{item_id}")
    if not result.get("success"):
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"detail_graphql_failed:{result.get('error','unknown')}")
        return result
    _merge_authoritative_detail(
        row,
        result.get("detail") or {},
        identity_verified=True,
    )
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), "graphql_item")
    row["parse_status"] = _append_token(row.get("parse_status", ""), "detail_item_graphql")
    return result


def _merge_magalu_zenrows_detail(row, product_url, trace_rows=None, row_index=""):
    if row.get("retailer") != "Magalu":
        return False
    if os.getenv("SEDA_MAGALU_ZENROWS_DETAIL_FALLBACK", "1").lower() in {"0", "false", "no", "n"}:
        _record_subcall(trace_rows, row, row_index, product_url, "zenrows_detail", success=False, error="disabled")
        return False
    try:
        from .magalu.zenrows_client import fetch_pdp_rendered_html

        result = fetch_pdp_rendered_html(product_url)
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"zenrows_detail_error:{type(exc).__name__}")
        _record_subcall(trace_rows, row, row_index, product_url, "zenrows_detail", success=False, error=f"{type(exc).__name__}: {exc}")
        return False
    token = f"zenrows_detail:{result.profile}:{result.estimated_multiplier}"
    if result.error:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:{result.error}")
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "zenrows_detail",
            method=token,
            success=False,
            status_code=getattr(result, "status_code", ""),
            length=len(result.text or ""),
            error=result.error,
        )
        return False
    detail = parse_detail(result.text or "", row.get("retailer", ""), _base_url(row.get("retailer", "")), product_url)
    identity_mode = _detail_identity_mode(row, detail)
    if identity_mode not in {"verified", "same_name"}:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:missing_product_identity")
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "zenrows_detail",
            method=token,
            success=False,
            status_code=getattr(result, "status_code", ""),
            length=len(result.text or ""),
            error="missing_product_identity",
        )
        return False
    meaningful_keys = (
        "retailer_sku_name",
        "final_sku_price",
        "screen_size",
        "model_year",
        "ref_refrigerator_type",
        "ref_capacity",
        "ldy_loading_type",
        "ldy_capacity",
        "delivery_availability",
        "pick_up_availability",
        "summarized_review_content",
        "count_of_star_ratings",
        "count_of_reviews",
    )
    if not any(detail.get(key) for key in meaningful_keys):
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:empty_detail")
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "zenrows_detail",
            method=token,
            success=False,
            status_code=getattr(result, "status_code", ""),
            length=len(result.text or ""),
            error="empty_detail",
        )
        return False
    if identity_mode == "verified":
        _merge_authoritative_detail(row, detail, identity_verified=True)
        merged = True
    else:
        merged = _merge_generic_product_detail(row, detail)
        if not merged:
            row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:same_name_no_missing_fields")
            return False
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), token)
    cost = (result.headers or {}).get("X-Request-Cost", "")
    status = "zenrows_detail_html" if not cost else f"zenrows_detail_html_cost:{cost}"
    row["parse_status"] = _append_token(row.get("parse_status", ""), status)
    _record_subcall(
        trace_rows,
        row,
        row_index,
        product_url,
        "zenrows_detail",
        method=token,
        success=True,
        status_code=getattr(result, "status_code", ""),
        length=len(result.text or ""),
        detail=status,
    )
    return merged

def _retry_magalu_shipping_blanks(row, product_url):
    if row.get("retailer") != "Magalu":
        return False
    if os.getenv("SEDA_MAGALU_SHIPPING_BLANK_RETRY", "1").lower() in {"0", "false", "no", "n"}:
        return False
    if not _needs_magalu_shipping_retry(row):
        return False

    from .magalu.detail_api import fetch_shipping_for_item_id

    item_id = sku_from_url(product_url) or row.get("item") or row.get("sku")
    attempts = int(os.getenv("SEDA_MAGALU_SHIPPING_BLANK_RETRY_ATTEMPTS", "1"))
    for attempt in range(1, attempts + 1):
        result = fetch_shipping_for_item_id(item_id, seller_id=_magalu_seller_id(row, product_url), context_url=product_url)
        if result.get("delivery") and not row.get("delivery_availability"):
            row["delivery_availability"] = result["delivery"]
        if result.get("pickup") and not row.get("pick_up_availability"):
            row["pick_up_availability"] = result["pickup"]
        if result.get("delivery") or result.get("pickup"):
            row["fetch_method"] = _append_token(row.get("fetch_method", ""), "shipping_blank_retry")
            row["parse_status"] = _append_token(row.get("parse_status", ""), "shipping_blank_retry")
            return True
        if attempt == attempts:
            row["parse_status"] = _append_token(
                row.get("parse_status", ""),
                f"shipping_blank_retry_failed:{result.get('error', 'empty_shipping')}",
            )
    return False


def _needs_magalu_shipping_retry(row):
    return row.get("retailer") == "Magalu" and (not row.get("delivery_availability") or not row.get("pick_up_availability"))


def _magalu_seller_id(row, product_url=""):
    seller_id = (row.get("seller_id") or "").strip()
    if seller_id:
        return seller_id
    parsed = urlsplit(product_url or row.get("product_url", ""))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return (query.get("seller_id") or "").strip()


def _needs_magalu_detail_retry(row):
    if row.get("retailer") != "Magalu":
        return False
    for token in reversed(str(row.get("parse_status") or "").split("+")):
        if token == "detail_item_graphql":
            return False
        if token == "detail_graphql_failed:item_query_failed":
            return True
        if token.startswith("detail_graphql_failed:"):
            return False
    return not (row.get("sku") or "").strip()


def _has_real_magalu_sku(row):
    sku = str(row.get("sku") or "").strip()
    if not sku:
        return False
    item_values = {
        str(row.get("item") or "").strip(),
        sku_from_url(row.get("product_url", "")),
    }
    return sku not in {value for value in item_values if value}


def _backfill_magalu_tv_sku_from_title(row, *, final_pass=False):
    """Recover one unresolved TV SKU from its verified row title or product slug."""
    if not _is_magalu_tv_row(row) or not _magalu_tv_sku_is_recovery_target(row):
        return False
    row_item = str(row.get("item") or "").strip().casefold()
    url_item = str(sku_from_url(row.get("product_url", "")) or "").strip().casefold()
    if not row_item or not url_item or row_item != url_item:
        return False
    parse_status = str(row.get("parse_status") or "").casefold()
    if "identity_mismatch" in parse_status or "identity_conflict" in parse_status:
        return False
    title = row.get("retailer_sku_name")
    if not is_magalu_tv_product_title(title):
        return False
    screen_size_hint = row.get("screen_size")
    title_candidate = high_confidence_tv_model_number_from_text(
        title,
        screen_size_hint=screen_size_hint,
    )
    url_candidate = "" if title_candidate else high_confidence_tv_model_number_from_url(
        row.get("product_url"),
        screen_size_hint=screen_size_hint,
    )
    candidate = title_candidate or url_candidate
    if not candidate:
        return False
    row["sku"] = candidate
    source = "title" if title_candidate else "url"
    status_token = (
        f"sku_{source}_final_recovered"
        if final_pass
        else f"sku_{source}_fallback_after_detail_retry"
    )
    row["parse_status"] = _append_token(
        row.get("parse_status", ""),
        status_token,
    )
    return True


def _backfill_magalu_tv_skus_from_title_url(rows, checkpoint_writer=None):
    """Run one free parent-side pass before any paid ZenRows field recovery."""
    candidates = 0
    updated = 0
    for row in rows:
        if _is_magalu_tv_row(row) and _magalu_tv_sku_is_recovery_target(row):
            candidates += 1
        if _backfill_magalu_tv_sku_from_title(row, final_pass=True):
            updated += 1
    if candidates:
        print(
            f"[seda] magalu tv final sku recovery updated={updated}/{candidates}",
            flush=True,
        )
    if updated and checkpoint_writer:
        checkpoint_writer(rows)
    return rows


def _retry_magalu_detail_blanks(row, product_url, trace_rows=None, row_index=""):
    if not _needs_magalu_detail_retry(row):
        return False
    attempts = int(os.getenv("SEDA_MAGALU_DETAIL_BLANK_RETRY_ATTEMPTS", "1"))
    for attempt in range(1, attempts + 1):
        result = _magalu_graphql_detail(
            row,
            product_url,
            trace_rows=trace_rows,
            row_index=row_index,
            trace_subcall="detail_graphql_retry",
        )
        if result and result.get("success"):
            row["parse_status"] = _append_token(row.get("parse_status", ""), "detail_blank_retry")
            return True
    row["parse_status"] = _append_token(row.get("parse_status", ""), "detail_blank_retry_failed")
    return False


def _backfill_magalu_detail_blanks(
    rows,
    output,
    checkpoint_every=25,
    trace_rows=None,
    row_index_offset=0,
    checkpoint_writer=None,
):
    """Second pass: re-fetch items whose item query failed the first time."""
    if os.getenv("SEDA_MAGALU_DETAIL_BLANK_RETRY", "1").lower() in {"0", "false", "no", "n"}:
        return rows
    candidates = [
        (row_index, row)
        for row_index, row in enumerate(rows, start=row_index_offset + 1)
        if _needs_magalu_detail_retry(row)
    ]
    limit = int(os.getenv("SEDA_MAGALU_DETAIL_BLANK_RETRY_LIMIT", "0"))
    if limit:
        candidates = candidates[:limit]
    total = len(candidates)
    if not total:
        return rows
    print(f"[seda] detail blank retry candidates={total}", flush=True)
    updated = 0
    for index, (row_index, row) in enumerate(candidates, start=1):
        before_sku = str(row.get("sku") or "").strip()
        changed = _retry_magalu_detail_blanks(
            row,
            row.get("product_url", ""),
            trace_rows=trace_rows,
            row_index=row_index,
        )
        if (
            not _has_real_magalu_sku(row)
            or _magalu_tv_sku_is_recovery_target(row)
        ):
            _backfill_magalu_tv_sku_from_title(row)
        recovered = _has_real_magalu_sku(row) and str(row.get("sku") or "").strip() != before_sku
        if recovered:
            updated += 1
        print(
            f"[seda] detail retry {index}/{total} item={_safe_log_value(row.get('item'))} "
            f"sku={_safe_log_value(row.get('sku'))} query_success={int(bool(changed))} "
            f"updated={int(recovered)}",
            flush=True,
        )
        if checkpoint_every and index % checkpoint_every == 0:
            if checkpoint_writer:
                checkpoint_writer(rows)
            else:
                write_csv(output, rows, columns=OUTPUT_COLUMNS)
            print(f"[seda] detail retry checkpoint {output} updated={updated}", flush=True)
    print(f"[seda] detail blank retry updated={updated}/{total}", flush=True)
    return rows


def _magalu_zenrows_field_recovery_enabled():
    scoped = os.getenv("SEDA_MAGALU_ZENROWS_FIELD_FALLBACK", "0").lower()
    if scoped not in {"1", "true", "yes", "y"}:
        return False

    # Use the same centralized contract as the actual request. This preserves
    # explicit global kill switches while allowing retailer-scoped defaults in
    # the interleaved Magalu/Casas batches.
    from .magalu.zenrows_client import dry_run, enabled

    return enabled() and not dry_run()


def _magalu_zenrows_sku_recovery_pending(row):
    if not _is_magalu_tv_row(row):
        return False
    return _magalu_tv_sku_is_recovery_target(row)


def _needs_magalu_zenrows_field_recovery(row):
    return (
        row.get("retailer") == "Magalu"
        and bool(
            _magalu_zenrows_missing_fields(row)
            or _magalu_zenrows_sku_recovery_pending(row)
        )
        and _needs_magalu_detail_retry(row)
    )


def _magalu_zenrows_item_key(row):
    url_item = clean_text(sku_from_url(row.get("product_url", "")))
    row_item = clean_text(row.get("item"))
    if url_item and row_item and url_item.casefold() != row_item.casefold():
        return (), "input_item_identity_mismatch"
    item_id = url_item or row_item
    if not item_id:
        return (), "missing_item_id"
    line = str(row.get("product_line") or product_line()).strip().upper()
    return (line, item_id.casefold()), ""


def _merge_magalu_zenrows_field_result(
    row,
    result,
    product_url,
    *,
    trace_rows=None,
    row_index="",
    cache_hit=False,
):
    missing_before = _magalu_zenrows_missing_fields(row)
    metadata = result.get("zenrows") or {}
    if cache_hit:
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "zenrows_field_graphql",
            method="zenrows_item_cache",
            success=bool(result.get("success")),
            status_code=metadata.get("status_code", ""),
            item_present=int(bool(result.get("success"))),
            error=result.get("error", ""),
            detail="cached_result",
        )
    else:
        cost = metadata.get("request_cost", "")
        detail = f"missing:{','.join(missing_before)}"
        if cost:
            detail += f"; cost:{cost}"
        _record_result_trace(
            trace_rows,
            row,
            row_index,
            product_url,
            "zenrows_field_graphql",
            result,
            detail=detail,
        )
    if not result.get("success"):
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            f"zenrows_field_failed:{result.get('error', 'unknown')}",
        )
        return False
    detail = result.get("detail") or {}
    sku_recovered = _merge_magalu_tv_reference_sku(
        row,
        detail,
        identity_verified=detail.get("_detail_identity_verified") is True,
    )
    _merge_missing_detail_fields(row, detail, missing_before)
    filled = tuple(key for key in missing_before if row.get(key))
    if not filled and not sku_recovered:
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            "zenrows_field_no_target_values",
        )
        return False
    row["fetch_method"] = _append_token(
        row.get("fetch_method", ""),
        "zenrows_graphql_item",
    )
    recovered_targets = list(filled)
    if sku_recovered:
        recovered_targets.append("sku")
    row["parse_status"] = _append_token(
        row.get("parse_status", ""),
        f"zenrows_field_recovered:{','.join(recovered_targets)}",
    )
    return bool(filled or sku_recovered)


def _backfill_magalu_zenrows_fields(
    rows,
    output,
    checkpoint_every=5,
    trace_rows=None,
    checkpoint_writer=None,
):
    """Final item-only recovery after all free GraphQL/PDP attempts finish."""
    if not _magalu_zenrows_field_recovery_enabled():
        return rows
    candidates = [
        (row_index, row)
        for row_index, row in enumerate(rows, start=1)
        if _needs_magalu_zenrows_field_recovery(row)
    ]
    candidates.sort(
        key=lambda entry: (
            0 if _magalu_zenrows_missing_fields(entry[1]) else 1,
            entry[0],
        )
    )
    if not candidates:
        return rows

    from .magalu.detail_api import fetch_item_fields_via_zenrows

    max_items = max(0, int(os.getenv("SEDA_MAGALU_ZENROWS_FIELD_MAX_ITEMS", "25")))
    failure_limit = max(
        1,
        int(os.getenv("SEDA_MAGALU_ZENROWS_FIELD_FAILURE_STREAK", "3")),
    )
    cache = {}
    attempted = 0
    updated = 0
    failure_streak = 0
    stopped_reason = ""
    print(
        f"[seda] zenrows field recovery candidates={len(candidates)} max_items={max_items}",
        flush=True,
    )
    for position, (row_index, row) in enumerate(candidates, start=1):
        product_url = row.get("product_url", "")
        cache_key, key_error = _magalu_zenrows_item_key(row)
        if key_error:
            row["parse_status"] = _append_token(
                row.get("parse_status", ""),
                f"zenrows_field_skipped:{key_error}",
            )
            _record_subcall(
                trace_rows,
                row,
                row_index,
                product_url,
                "zenrows_field_graphql",
                success=False,
                error=key_error,
            )
            continue
        cache_hit = cache_key in cache
        if cache_hit:
            result = cache[cache_key]
        else:
            if max_items and attempted >= max_items:
                stopped_reason = "max_items"
                break
            result = fetch_item_fields_via_zenrows(
                cache_key[1],
                seller_id=_magalu_seller_id(row, product_url),
                context_url=product_url,
            )
            cache[cache_key] = result
            attempted += 1
        if _merge_magalu_zenrows_field_result(
            row,
            result,
            product_url,
            trace_rows=trace_rows,
            row_index=row_index,
            cache_hit=cache_hit,
        ):
            updated += 1
        if not cache_hit:
            if result.get("success"):
                failure_streak = 0
            else:
                failure_streak += 1
                if result.get("error") in {
                    "zenrows_disabled",
                    "zenrows_dry_run",
                    "key_missing",
                } or str(result.get("error") or "").startswith("unknown_profile:"):
                    stopped_reason = result.get("error") or "configuration_error"
                    break
                if failure_streak >= failure_limit:
                    stopped_reason = f"failure_streak:{failure_streak}"
                    break
        print(
            f"[seda] zenrows field {position}/{len(candidates)} "
            f"item={_safe_log_value(row.get('item'))} cache={int(cache_hit)} "
            f"success={int(bool(result.get('success')))} "
            f"remaining={','.join(_magalu_zenrows_missing_fields(row)) or 'none'}",
            flush=True,
        )
        if (
            checkpoint_every
            and not cache_hit
            and attempted
            and attempted % checkpoint_every == 0
        ):
            if checkpoint_writer:
                checkpoint_writer(rows)
            else:
                write_csv(output, rows, columns=OUTPUT_COLUMNS)
            print(
                f"[seda] zenrows field checkpoint {output} attempted={attempted} updated={updated}",
                flush=True,
            )
    print(
        f"[seda] zenrows field recovery updated={updated}/{len(candidates)} "
        f"attempted={attempted} unique={len(cache)} stopped={stopped_reason or 'no'}",
        flush=True,
    )
    return rows


_CASAS_ZENROWS_COMPLETE_TOKEN = "casas_zenrows_field_complete"
_CASAS_ZENROWS_CONFIG_ERRORS = {
    "zenrows_disabled",
    "zenrows_dry_run",
    "key_missing",
}


def _casas_zenrows_field_recovery_enabled():
    scoped = os.getenv(
        "SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK",
        "0",
    ).lower()
    if scoped not in {"1", "true", "yes", "y"}:
        return False
    from .magalu.zenrows_client import dry_run, enabled

    return enabled() and not dry_run()


def _casas_zenrows_field_satisfied(row, field):
    line = str(row.get("product_line") or product_line()).strip().upper()
    if field == "sku":
        item = clean_text(sku_from_url(row.get("product_url", "")))
        if line == "TV":
            return bool(casas_tv_sku_for_output(row, item))
        if line == "REF":
            return bool(casas_ref_sku_for_output(row, item))
        if line == "LDY":
            return bool(casas_ldy_sku_for_output(row, item))
        return False
    return row.get(field) not in ("", None, [], {})


def _casas_zenrows_missing_fields(row):
    if row.get("retailer") != "Casas Bahia":
        return ()
    line = str(row.get("product_line") or product_line()).strip().upper()
    fields = list(casas_bahia_zenrows_fields(line))
    if line == "LDY" and is_standalone_dryer_title(
        row.get("retailer_sku_name", "")
    ):
        fields = [
            field
            for field in fields
            if field not in {"ldy_capacity", "ldy_loading_type"}
        ]
    return tuple(
        field
        for field in fields
        if not _casas_zenrows_field_satisfied(row, field)
    )


def _casas_zenrows_item_key(row):
    parsed = urlsplit(str(row.get("product_url") or "").strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host != "casasbahia.com.br" and not host.endswith(
        ".casasbahia.com.br"
    ):
        return (), "invalid_product_host"
    url_item = clean_text(sku_from_url(row.get("product_url", "")))
    row_item = clean_text(row.get("item"))
    if url_item and row_item and url_item.casefold() != row_item.casefold():
        return (), "input_item_identity_mismatch"
    item_id = url_item or row_item
    if not item_id:
        return (), "missing_item_id"
    line = str(row.get("product_line") or product_line()).strip().upper()
    return (line, item_id.casefold()), ""


def _casas_zenrows_completed(row):
    return _CASAS_ZENROWS_COMPLETE_TOKEN in {
        token.strip()
        for token in str(row.get("parse_status") or "").split("+")
        if token.strip()
    }


def _casas_zenrows_int_env(name, default, *, minimum=0, maximum=1000):
    try:
        value = int(str(os.getenv(name, default)).strip())
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _merge_casas_zenrows_field_result(
    row,
    result,
    *,
    trace_rows=None,
    row_index="",
    cache_hit=False,
):
    product_url = row.get("product_url", "")
    missing_before = _casas_zenrows_missing_fields(row)
    if cache_hit:
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "casas_zenrows_field_pdp",
            method="casas_zenrows_item_cache",
            success=bool(result.get("success")),
            error=result.get("error", ""),
            detail="cached_result",
        )
    else:
        for attempt_number, attempt in enumerate(
            result.get("attempts") or (),
            start=1,
        ):
            request_cost = (attempt.get("headers") or {}).get(
                "X-Request-Cost",
                "",
            )
            _record_subcall(
                trace_rows,
                row,
                row_index,
                product_url,
                "casas_zenrows_field_pdp",
                attempt=attempt_number,
                method=(
                    f"casas_zenrows_pdp:{attempt.get('profile', '')}:"
                    f"{attempt.get('estimated_multiplier', '')}"
                ),
                success=bool(attempt.get("success")),
                status_code=attempt.get("status_code", ""),
                error=attempt.get("error", ""),
                detail=(
                    f"request_cost:{request_cost}"
                    if request_cost
                    else ""
                ),
            )
    detail = result.get("detail") or {}
    _merge_missing_detail_fields(
        row,
        detail,
        tuple(field for field in missing_before if field != "sku"),
    )
    if "sku" in missing_before and result.get("identity_verified") is True:
        line = str(
            row.get("product_line") or product_line()
        ).strip().upper()
        if line == "TV" and detail.get(CASAS_TV_EXACT_MODELO_FIELD) is True:
            _merge_casas_bahia_authoritative_detail(
                row,
                {
                    "sku": detail.get("sku", ""),
                    CASAS_TV_EXACT_MODELO_FIELD: True,
                },
                CASAS_TV_PDP_HTML_MODEL_TOKEN,
                identity_verified=True,
            )
        elif line == "REF":
            _merge_casas_bahia_ref_detail(
                row,
                {"sku": detail.get("sku", "")},
                identity_verified=True,
            )
        elif line == "LDY":
            _merge_casas_bahia_ldy_detail(
                row,
                {"sku": detail.get("sku", "")},
                identity_verified=True,
            )
    filled = tuple(
        field
        for field in missing_before
        if _casas_zenrows_field_satisfied(row, field)
    )
    row["parse_status"] = _append_token(
        row.get("parse_status", ""),
        _CASAS_ZENROWS_COMPLETE_TOKEN,
    )
    if filled:
        row["fetch_method"] = _append_token(
            row.get("fetch_method", ""),
            "casas_zenrows_pdp",
        )
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            f"casas_zenrows_field_recovered:{','.join(filled)}",
        )
        return True
    row["parse_status"] = _append_token(
        row.get("parse_status", ""),
        f"casas_zenrows_field_failed:{result.get('error', 'no_target_values')}",
    )
    return False


def _backfill_casas_zenrows_fields(
    rows,
    output,
    checkpoint_every=5,
    trace_rows=None,
    checkpoint_writer=None,
):
    """Recover missing allowlisted fields or a validated line-specific SKU."""
    if not _casas_zenrows_field_recovery_enabled():
        return rows
    candidates = []
    requested_by_key = {}
    for row_index, row in enumerate(rows, start=1):
        missing = _casas_zenrows_missing_fields(row)
        if not missing or _casas_zenrows_completed(row):
            continue
        cache_key, key_error = _casas_zenrows_item_key(row)
        if key_error:
            row["parse_status"] = _append_token(
                row.get("parse_status", ""),
                f"casas_zenrows_field_skipped:{key_error}",
            )
            continue
        candidates.append((row_index, row, cache_key))
        requested_by_key.setdefault(cache_key, set()).update(missing)
    if not candidates:
        return rows

    from .casas_bahia.pdp_field_recovery import (
        fetch_pdp_fields_via_zenrows,
    )

    failure_limit = _casas_zenrows_int_env(
        "SEDA_CASAS_BAHIA_ZENROWS_FIELD_FAILURE_STREAK",
        3,
        minimum=1,
        maximum=20,
    )
    cache = {}
    attempted_items = 0
    request_count = 0
    updated = 0
    failure_streak = 0
    stopped_reason = ""
    print(
        "[seda] casas zenrows field recovery "
        f"candidates={len(candidates)}",
        flush=True,
    )
    for position, (row_index, row, cache_key) in enumerate(
        candidates,
        start=1,
    ):
        cache_hit = cache_key in cache
        if cache_hit:
            result = cache[cache_key]
        else:
            requested = tuple(
                field
                for field in casas_bahia_zenrows_fields(cache_key[0])
                if field in requested_by_key[cache_key]
            )
            try:
                result = fetch_pdp_fields_via_zenrows(
                    row.get("product_url", ""),
                    requested,
                    product_line_value=cache_key[0],
                    max_requests=2,
                )
            except Exception as exc:
                result = {
                    "success": False,
                    "detail": {},
                    "error": f"exception:{type(exc).__name__}",
                    "request_count": 0,
                    "attempts": [],
                }
            cache[cache_key] = result
            attempted_items += 1
            request_count += int(result.get("request_count") or 0)
        changed = _merge_casas_zenrows_field_result(
            row,
            result,
            trace_rows=trace_rows,
            row_index=row_index,
            cache_hit=cache_hit,
        )
        if changed:
            updated += 1
        if not cache_hit:
            requested_values = requested_by_key[cache_key]
            target_success = any(
                (result.get("detail") or {}).get(field)
                for field in requested_values
            )
            if target_success:
                failure_streak = 0
            else:
                failure_streak += 1
            error = str(result.get("error") or "")
            if (
                error in _CASAS_ZENROWS_CONFIG_ERRORS
                or error.startswith("unknown_profile:")
            ):
                stopped_reason = error or "configuration_error"
                break
            if failure_streak >= failure_limit:
                stopped_reason = f"failure_streak:{failure_streak}"
                break
        print(
            f"[seda] casas zenrows field {position}/{len(candidates)} "
            f"item={_safe_log_value(row.get('item'))} "
            f"cache={int(cache_hit)} updated={int(changed)} "
            f"remaining={','.join(_casas_zenrows_missing_fields(row)) or 'none'}",
            flush=True,
        )
        if (
            checkpoint_every
            and not cache_hit
            and attempted_items % checkpoint_every == 0
        ):
            if checkpoint_writer:
                checkpoint_writer(rows)
            else:
                write_csv(output, rows, columns=OUTPUT_COLUMNS)
    print(
        "[seda] casas zenrows field recovery "
        f"updated={updated}/{len(candidates)} "
        f"items={attempted_items} requests={request_count} "
        f"stopped={stopped_reason or 'no'}",
        flush=True,
    )
    return rows


def _backfill_magalu_shipping_blanks(
    rows,
    output,
    checkpoint_every=25,
    checkpoint_writer=None,
):
    if os.getenv("SEDA_MAGALU_SHIPPING_BLANK_RETRY", "1").lower() in {"0", "false", "no", "n"}:
        return rows
    candidates = [row for row in rows if _needs_magalu_shipping_retry(row)]
    limit = int(os.getenv("SEDA_MAGALU_SHIPPING_BLANK_RETRY_LIMIT", "0"))
    if limit:
        candidates = candidates[:limit]
    total = len(candidates)
    if not total:
        return rows
    print(f"[seda] shipping blank retry candidates={total}", flush=True)
    updated = 0
    for index, row in enumerate(candidates, start=1):
        before = (row.get("delivery_availability", ""), row.get("pick_up_availability", ""))
        changed = _retry_magalu_shipping_blanks(row, row.get("product_url", ""))
        after = (row.get("delivery_availability", ""), row.get("pick_up_availability", ""))
        if changed and after != before:
            updated += 1
        print(
            f"[seda] shipping retry {index}/{total} item={_safe_log_value(row.get('item'))} "
            f"updated={int(changed and after != before)}",
            flush=True,
        )
        if checkpoint_every and index % checkpoint_every == 0:
            if checkpoint_writer:
                checkpoint_writer(rows)
            else:
                write_csv(output, rows, columns=OUTPUT_COLUMNS)
            print(f"[seda] shipping retry checkpoint {output} updated={updated}", flush=True)
    print(f"[seda] shipping blank retry updated={updated}/{total}", flush=True)
    return rows


def _merge_magalu_pdp_html(
    row,
    product_url,
    trace_rows=None,
    row_index="",
    allow_zenrows_pdp=False,
):
    if row.get("retailer") != "Magalu":
        return
    try:
        review_limit = int(os.getenv("SEDA_MAGALU_REVIEW_LIMIT", "20"))
    except ValueError:
        review_limit = 20
    try:
        known_review_count = int(float(str(row.get("count_of_reviews") or "").replace(".", "").replace(",", ".")))
    except ValueError:
        known_review_count = -1
    target_reviews = review_limit if known_review_count < 0 else min(review_limit, known_review_count)
    needs_reviews = target_reviews > 0 and _review_count(row.get("detailed_review_content")) < target_reviews
    needs_rating = not row.get("star_rating") or not row.get("count_of_star_ratings") or not row.get("count_of_reviews")
    needs_summary = not row.get("summarized_review_content") and any(
        row.get(key) for key in ("star_rating", "count_of_star_ratings", "count_of_reviews", "detailed_review_content")
    )
    needs_similar = not row.get("retailer_sku_name_similar")
    needs_specs = any(not row.get(key) for key in _relevant_audited_fields(row))
    if not needs_summary and not needs_similar and not needs_reviews and not needs_rating and not needs_specs:
        _record_subcall(trace_rows, row, row_index, product_url, "pdp_html", success=True, error="", detail="not_needed")
        return
    if os.getenv("SEDA_MAGALU_PDP_HTML_FETCH", "1").lower() in {"0", "false", "no", "n"}:
        _record_subcall(trace_rows, row, row_index, product_url, "pdp_html", success=False, error="disabled")
        return
    result = _fetch_magalu_next_html(product_url, label="pdp")
    text = result.get("text") or ""
    _record_subcall(
        trace_rows,
        row,
        row_index,
        product_url,
        "pdp_html",
        label=result.get("label", ""),
        method=result.get("method", ""),
        success=result.get("status_code") == 200 and "__NEXT_DATA__" in text,
        status_code=result.get("status_code", 0),
        length=len(text),
        has_next_data=int("__NEXT_DATA__" in text),
        error=result.get("error", ""),
    )
    if result.get("status_code") != 200 or "__NEXT_DATA__" not in (result.get("text") or ""):
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            f"pdp_html_failed:{result.get('status_code', 0)}:len={len(text)}:next={int('__NEXT_DATA__' in text)}",
        )
        if allow_zenrows_pdp and _merge_magalu_zenrows_pdp_html(
            row, product_url, trace_rows=trace_rows, row_index=row_index
        ):
            return
        return
    detail = parse_detail(result.get("text") or "", row.get("retailer", ""), _base_url(row.get("retailer", "")), product_url)
    identity_mode = _detail_identity_mode(row, detail)
    if identity_mode not in {"verified", "same_name"}:
        reason = "identity_conflict" if identity_mode == "conflict" else "missing_product_identity"
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"pdp_html_{reason}")
        _record_subcall(trace_rows, row, row_index, product_url, "pdp_html_identity", success=False, error=reason)
        _merge_magalu_zenrows_pdp_html(row, product_url, trace_rows=trace_rows, row_index=row_index)
        return
    if _is_magalu_tv_row(row):
        _merge_magalu_tv_reference_sku(
            row,
            detail,
            identity_verified=identity_mode == "verified",
        )
    elif detail.get("sku"):
        if identity_mode == "verified" or not row.get("sku"):
            row["sku"] = detail["sku"]
    _merge_missing_detail_fields(row, detail, (
        "summarized_review_content",
        "retailer_sku_name_similar",
        *MAGALU_PDP_SEMANTIC_FIELDS,
        "star_rating",
        "count_of_star_ratings",
        "count_of_reviews",
        "detailed_review_content",
    ))
    if _merge_magalu_exact_html_specs(row, result.get("text") or "", detail):
        row["parse_status"] = _append_token(row.get("parse_status", ""), "pdp_html_specs")
    if _merge_magalu_shipping_from_next_data(row, result.get("text") or "", product_url, trace_rows=trace_rows, row_index=row_index):
        row["parse_status"] = _append_token(row.get("parse_status", ""), "pdp_html_shipping")
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), f"{result.get('method') or 'unknown'}_pdp_html")
    row["parse_status"] = _append_token(row.get("parse_status", ""), "pdp_html")


def _merge_magalu_similar(
    row,
    product_url,
    trace_rows=None,
    row_index="",
    prior_error="",
):
    if row.get("retailer") != "Magalu":
        return False
    if row.get("retailer_sku_name_similar"):
        _record_subcall(trace_rows, row, row_index, product_url, "similar_graphql", success=True, detail="already_has_similar")
        return False
    if prior_error == "showcase_failed_fetch_circuit_open":
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "similar_graphql",
            success=False,
            error=prior_error,
            detail="skipped_after_detail_graphql_circuit",
        )
        return False
    if os.getenv("SEDA_MAGALU_SIMILAR_GRAPHQL", "1").lower() in {"0", "false", "no", "n"}:
        _record_subcall(trace_rows, row, row_index, product_url, "similar_graphql", success=False, error="disabled")
        return False
    item_id = sku_from_url(product_url) or row.get("item")
    if not item_id:
        _record_subcall(trace_rows, row, row_index, product_url, "similar_graphql", success=False, error="missing_item_id")
        return False
    try:
        from .magalu.detail_api import fetch_similar_names

        result = fetch_similar_names(item_id, context_url=product_url)
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"similar_graphql_error:{type(exc).__name__}")
        _record_subcall(trace_rows, row, row_index, product_url, "similar_graphql", success=False, error=f"{type(exc).__name__}: {exc}")
        return False
    _record_result_trace(trace_rows, row, row_index, product_url, "similar_graphql", result, detail=f"item_id:{item_id}")
    names = result.get("names") or []
    if not names:
        row["parse_status"] = _append_token(row.get("parse_status", ""), "similar_graphql_empty")
        return False
    row["retailer_sku_name_similar"] = compact_json(names)
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), "graphql_similar")
    row["parse_status"] = _append_token(row.get("parse_status", ""), f"similar_graphql_{len(names)}")
    return True


def _merge_magalu_exact_html_specs(row, html_text, detail=None):
    """Validate repeated DOM specs through the normal Magalu field extractor."""
    detail = detail if isinstance(detail, dict) else {}
    if (
        row.get("retailer") != "Magalu"
        or _detail_identity_mode(row, detail) != "verified"
    ):
        return False
    pairs = _html_target_label_value_pairs(html_text, MAGALU_DOM_LABELS)
    if not pairs:
        return False
    synthetic_item = {
        "title": detail.get("retailer_sku_name") or row.get("retailer_sku_name") or "",
        "path": row.get("product_url") or "",
        "factsheet": [
            {"keyName": label, "value": value}
            for label, value in pairs
        ],
        "attributes": [],
        "bundles": [],
    }
    fields = extract_magalu_semantic_fields(
        synthetic_item,
        str(row.get("product_line") or product_line()).strip().upper(),
    )
    return _merge_missing_detail_fields(
        row,
        fields,
        _relevant_audited_fields(row),
    )


def _merge_magalu_shipping_from_next_data(row, html_text, product_url, trace_rows=None, row_index=""):
    if row.get("retailer") != "Magalu":
        return False
    if os.getenv("SEDA_MAGALU_SHIPPING_FROM_SSR_ITEM", "1").lower() in {"0", "false", "no", "n"}:
        _record_subcall(trace_rows, row, row_index, product_url, "shipping_ssr_item", success=False, error="disabled")
        return False
    if row.get("delivery_availability") and row.get("pick_up_availability"):
        _record_subcall(trace_rows, row, row_index, product_url, "shipping_ssr_item", success=True, detail="already_has_shipping")
        return False
    item = _magalu_next_item_from_html(html_text)
    if not item:
        _record_subcall(trace_rows, row, row_index, product_url, "shipping_ssr_item", success=False, error="missing_ssr_item")
        return False
    if not _url_product_identity_matches(product_url, item.get("id")):
        expected = sku_from_url(product_url)
        actual = clean_text(item.get("id"))
        reason = "item_identity_missing" if expected and not actual else "item_identity_mismatch"
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"shipping_ssr_{reason}")
        _record_subcall(trace_rows, row, row_index, product_url, "shipping_ssr_item", success=False, error=reason)
        return False
    try:
        from .magalu.detail_api import fetch_shipping

        result = fetch_shipping(item, seller_id=_magalu_seller_id(row, product_url), context_url=product_url)
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"shipping_ssr_item_error:{type(exc).__name__}")
        _record_subcall(trace_rows, row, row_index, product_url, "shipping_ssr_item", success=False, error=f"{type(exc).__name__}: {exc}")
        return False
    _record_result_trace(trace_rows, row, row_index, product_url, "shipping_ssr_item", result, detail=f"item_id:{item.get('id', '')}")
    updated = False
    if result.get("delivery") and not row.get("delivery_availability"):
        row["delivery_availability"] = result["delivery"]
        updated = True
    if result.get("pickup") and not row.get("pick_up_availability"):
        row["pick_up_availability"] = result["pickup"]
        updated = True
    if updated:
        row["fetch_method"] = _append_token(row.get("fetch_method", ""), "graphql_shipping_ssr_item")
        return True
    row["parse_status"] = _append_token(row.get("parse_status", ""), f"shipping_ssr_item_failed:{result.get('error', 'empty_shipping')}")
    return False


def _magalu_next_item_from_html(html_text):
    data = extract_next_data(html_text)
    if not isinstance(data, dict):
        return {}
    props = data.get("props") if isinstance(data.get("props"), dict) else {}
    page_props = props.get("pageProps") if isinstance(props.get("pageProps"), dict) else {}
    page_data = page_props.get("data") if isinstance(page_props.get("data"), dict) else {}
    if not page_data:
        page_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    return page_data.get("item") if isinstance(page_data.get("item"), dict) else {}


def _merge_magalu_review_pages(row, product_url, trace_rows=None, review_page_trace_rows=None, row_index=""):
    if row.get("retailer") != "Magalu":
        return None
    if os.getenv("SEDA_MAGALU_REVIEW_HTML_PAGES", "1").lower() in {"0", "false", "no", "n"}:
        _record_subcall(trace_rows, row, row_index, product_url, "review_html_pages", success=False, error="disabled")
        return None
    target = _magalu_review_target(row)
    if target <= 0:
        _record_subcall(trace_rows, row, row_index, product_url, "review_html_pages", success=True, detail=f"target:{target}")
        return {"success": True, "reviews": [], "trace": [], "method": "review_html_pages", "target": target}
    reviews = _review_values(row.get("detailed_review_content"))
    if len(reviews) >= target:
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "review_html_pages",
            success=True,
            detail=f"already_has_reviews:{len(reviews)}/{target}",
        )
        return {"success": True, "reviews": reviews[:target], "trace": [], "method": "review_html_pages", "target": target}
    review_url = _magalu_review_url(product_url)
    if not review_url:
        row["parse_status"] = _append_token(row.get("parse_status", ""), "review_html_missing_url")
        _record_subcall(trace_rows, row, row_index, product_url, "review_html_pages", success=False, error="missing_review_url")
        return {"success": False, "reviews": reviews, "trace": [], "method": "review_html_pages", "target": target, "error": "missing_review_url"}

    max_pages = int(os.getenv("SEDA_MAGALU_REVIEW_HTML_MAX_PAGES", "10"))
    # stop after this many consecutive pages that add no new review text; beyond the
    # real page count Magalu still returns a 200 page with empty userReviews.items,
    # so without this guard the loop downloads up to max_pages of empty ~0.5MB HTML.
    empty_streak_limit = int(os.getenv("SEDA_MAGALU_REVIEW_HTML_EMPTY_STREAK", "2"))
    start_page = 1 if not reviews else 2
    seen = {review.casefold() for review in reviews}
    trace = []
    methods = []
    empty_streak = 0
    total_pages = 0  # real page count from userReviews.page.totalPages, once known
    for page in range(start_page, max_pages + 1):
        if total_pages and page > total_pages:
            # never fetch past the last real review page
            break
        page_url = _magalu_review_page_url(review_url, page)
        result = _fetch_magalu_next_html(page_url, label=f"review_page_{page}")
        methods.append(result.get("method") or "unknown")
        trace_item = {
            "page": page,
            "method": result.get("method", ""),
            "status_code": result.get("status_code", 0),
            "length": len(result.get("text") or ""),
            "has_next_data": "__NEXT_DATA__" in (result.get("text") or ""),
            "error": result.get("error", ""),
            "target": target,
        }
        if result.get("status_code") == 200 and "__NEXT_DATA__" in (result.get("text") or ""):
            detail = parse_detail(result.get("text") or "", row.get("retailer", ""), _base_url(row.get("retailer", "")), product_url)
            identity_mode = _detail_identity_mode(row, detail)
            if identity_mode not in {'verified', 'same_name'}:
                reason = 'identity_conflict' if identity_mode == 'conflict' else 'missing_product_identity'
                trace_item['error'] = reason
                trace_item['descriptions'] = 0
                trace_item['new_descriptions'] = 0
                trace_item['total_reviews_after'] = len(reviews)
                trace.append(trace_item)
                row['parse_status'] = _append_token(
                    row.get('parse_status', ''),
                    f'review_html_pages_{reason}',
                )
                _record_review_page_trace(
                    review_page_trace_rows,
                    row,
                    row_index,
                    product_url,
                    review_url,
                    trace_item,
                )
                _record_subcall(
                    trace_rows,
                    row,
                    row_index,
                    product_url,
                    'review_html_pages',
                    success=False,
                    error=reason,
                    detail=f'page:{page}',
                )
                break
            for key in ("star_rating", "count_of_star_ratings", "count_of_reviews"):
                if detail.get(key) and not row.get(key):
                    row[key] = detail[key]
            parsed_total_pages = _metric_int(detail.get("total_review_pages"))
            if parsed_total_pages > 0:
                total_pages = parsed_total_pages
            if _metric_int(row.get("count_of_reviews")) == 0:
                # page confirms zero comments -> stop; do not collect anything
                trace_item["descriptions"] = 0
                trace_item["total_reviews_after"] = len(reviews)
                trace.append(trace_item)
                _record_review_page_trace(review_page_trace_rows, row, row_index, product_url, review_url, trace_item)
                _record_subcall(trace_rows, row, row_index, product_url, "review_html_pages", success=True, detail="count_zero")
                return {"success": True, "reviews": reviews[:target], "trace": trace, "method": "review_html_pages", "target": target}
            page_reviews = _review_values(detail.get("detailed_review_content"))
            trace_item["descriptions"] = len(page_reviews)
            added = 0
            for description in page_reviews:
                key = description.casefold()
                if key in seen:
                    continue
                seen.add(key)
                reviews.append(description)
                added += 1
                if len(reviews) >= target:
                    break
            trace_item["new_descriptions"] = added
            empty_streak = 0 if added else empty_streak + 1
        else:
            # non-200 / missing __NEXT_DATA__ yields nothing either
            empty_streak += 1
        trace_item["total_reviews_after"] = len(reviews)
        trace.append(trace_item)
        _record_review_page_trace(review_page_trace_rows, row, row_index, product_url, review_url, trace_item)
        if len(reviews) >= target:
            break
        if empty_streak >= empty_streak_limit:
            # review text exhausted (or pages blocked) -> further pages won't help
            break

    if reviews:
        row["detailed_review_content"] = compact_json(reviews[:target])
        row["fetch_method"] = _append_token(row.get("fetch_method", ""), f"{'+'.join(dict.fromkeys(methods))}_review_pages")
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"reviews_html_pages_{len(reviews[:target])}/{target}")
    elif trace:
        last = trace[-1]
        reason = last.get("error") or "missing_product_rating"
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"review_html_pages_failed:{reason}")
    result = {"success": len(reviews) >= target, "reviews": reviews[:target], "trace": trace, "method": "review_html_pages", "target": target}
    _record_result_trace(
        trace_rows,
        row,
        row_index,
        product_url,
        "review_html_pages",
        result,
        detail=f"reviews:{len(reviews[:target])}/{target}",
    )
    return result


def _merge_magalu_zenrows_pdp_html(row, product_url, trace_rows=None, row_index=""):
    if row.get("retailer") != "Magalu":
        return False
    if os.getenv("SEDA_MAGALU_ZENROWS_PDP_FALLBACK", "0").lower() not in {"1", "true", "yes", "y"}:
        _record_subcall(trace_rows, row, row_index, product_url, "zenrows_pdp_html", success=False, error="disabled")
        return False
    try:
        from .magalu.zenrows_client import fetch_next_data_html

        try:
            timeout = int(os.getenv("SEDA_MAGALU_ZENROWS_PDP_TIMEOUT", "120"))
        except ValueError:
            timeout = 120
        attempts = [
            fetch_next_data_html(
                product_url,
                profile="pdp_next_data",
                timeout=timeout,
            )
        ]
        first = attempts[0]
        first_has_next_data = "__NEXT_DATA__" in (first.text or "")
        if (
            not first_has_next_data
            and first.status_code in {200, 422}
            and first.error not in {"key_missing", "zenrows_disabled", "zenrows_dry_run"}
        ):
            attempts.append(
                fetch_next_data_html(
                    product_url,
                    profile="pdp_js_full",
                    timeout=timeout,
                )
            )
        result = next(
            (
                attempt
                for attempt in attempts
                if attempt.success
                and not attempt.error
                and "__NEXT_DATA__" in (attempt.text or "")
            ),
            attempts[-1],
        )
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"zenrows_pdp_error:{type(exc).__name__}")
        _record_subcall(
            trace_rows, row, row_index, product_url,
            "zenrows_pdp_html",
            success=False,
            error=type(exc).__name__,
        )
        return False
    for attempt_number, attempt in enumerate(attempts, start=1):
        has_next_data = "__NEXT_DATA__" in (attempt.text or "")
        request_cost = (attempt.headers or {}).get("X-Request-Cost", "")
        _record_subcall(
            trace_rows, row, row_index, product_url, "zenrows_pdp_html_attempt",
            attempt=attempt_number,
            method=f"zenrows_pdp:{attempt.profile}:{attempt.estimated_multiplier}",
            success=bool(attempt.success and not attempt.error and has_next_data),
            status_code=attempt.status_code,
            length=len(attempt.text or ""),
            has_next_data=int(has_next_data),
            error=attempt.error or ("" if has_next_data else "missing_next_data"),
            detail=f"request_cost:{request_cost}" if request_cost else "",
        )
    token = f"zenrows_pdp:{result.profile}:{result.estimated_multiplier}"
    if result.error:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:{result.error}")
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "zenrows_pdp_html",
            method=token,
            success=False,
            status_code=result.status_code,
            length=len(result.text or ""),
            error=result.error,
        )
        return False
    if not result.success or "__NEXT_DATA__" not in (result.text or ""):
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:missing_next_data")
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "zenrows_pdp_html",
            method=token,
            success=False,
            status_code=result.status_code,
            length=len(result.text or ""),
            has_next_data=int("__NEXT_DATA__" in (result.text or "")),
            error="missing_next_data",
        )
        return False
    detail = parse_detail(result.text or "", row.get("retailer", ""), _base_url(row.get("retailer", "")), product_url)
    identity_mode = _detail_identity_mode(row, detail)
    if identity_mode not in {"verified", "same_name"}:
        reason = "identity_conflict" if identity_mode == "conflict" else "missing_product_identity"
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:{reason}")
        _record_subcall(
            trace_rows,
            row,
            row_index,
            product_url,
            "zenrows_pdp_html",
            method=token,
            success=False,
            status_code=result.status_code,
            length=len(result.text or ""),
            has_next_data=1,
            error=reason,
        )
        return False
    sku_merged = False
    if _is_magalu_tv_row(row):
        sku_merged = _merge_magalu_tv_reference_sku(
            row,
            detail,
            identity_verified=identity_mode == "verified",
        )
    elif detail.get("sku") and (
        identity_mode == "verified" or not row.get("sku")
    ):
        before_sku = row.get("sku")
        row["sku"] = detail["sku"]
        sku_merged = row.get("sku") != before_sku
    merged = _merge_missing_detail_fields(
        row,
        detail,
        ("summarized_review_content", "retailer_sku_name_similar", *MAGALU_PDP_SEMANTIC_FIELDS),
    )
    merged = merged or sku_merged
    if _merge_magalu_exact_html_specs(row, result.text or "", detail):
        row["parse_status"] = _append_token(row.get("parse_status", ""), "zenrows_pdp_html_specs")
        merged = True
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), token)
    row["parse_status"] = _append_token(row.get("parse_status", ""), "zenrows_pdp_html")
    _record_subcall(
        trace_rows,
        row,
        row_index,
        product_url,
        "zenrows_pdp_html",
        method=token,
        success=True,
        status_code=result.status_code,
        length=len(result.text or ""),
        has_next_data=1,
    )
    return merged


def _detail_fetch_url(row, product_url):
    if row.get("retailer") != "Casas Bahia":
        return product_url
    parsed = urlsplit(product_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("frete", os.getenv("SEDA_POSTAL_CODE", "01010-010"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _fallback_fetch_enabled_for_row(row, fallback_fetch):
    if not fallback_fetch:
        return False
    if row.get("retailer") == "Magalu":
        return os.getenv("SEDA_MAGALU_DETAIL_HTML_FALLBACK", "0").lower() in {"1", "true", "yes", "y"}
    if row.get("retailer") != "Casas Bahia":
        return True
    return os.getenv("SEDA_CASAS_BAHIA_PDP_HTML_FETCH", "0").lower() in {"1", "true", "yes", "y"}


def _has_blocked_graphql_trace(result):
    if not result:
        return False
    for trace_item in result.get("trace") or []:
        try:
            status_code = int(trace_item.get("status_code") or 0)
        except (TypeError, ValueError):
            status_code = 0
        error = str(trace_item.get("error") or "").lower()
        if status_code in {401, 403, 429}:
            return True
        if "blocked" in error or "invalid_json" in error or "non_json_or_blocked" in error:
            return True
    return False


def _is_expected_magalu_item_query_block(result):
    if not result or result.get("error") != "item_query_failed":
        return False
    trace = result.get("trace") or []
    if not trace:
        return False
    for trace_item in trace:
        if str(trace_item.get("method") or "") != "browser_graphql":
            continue
        try:
            status_code = int(trace_item.get("status_code") or 0)
        except (TypeError, ValueError):
            status_code = 0
        error = str(trace_item.get("error") or "").lower()
        if status_code in {401, 403, 429} or any(
            marker in error
            for marker in ("blocked", "invalid_json", "non_json_or_blocked")
        ):
            return False
    for trace_item in trace:
        if (
            str(trace_item.get("label") or "") == "item"
            and str(trace_item.get("method") or "") == "browser_graphql"
            and str(trace_item.get("error") or "") == "graphql_item_missing"
        ):
            try:
                if int(trace_item.get("status_code") or 0) == 200:
                    return True
            except (TypeError, ValueError):
                continue
    for trace_item in trace:
        label = str(trace_item.get("label") or "")
        method = str(trace_item.get("method") or "")
        if label != "item" or method != "requests":
            return False
    return _has_blocked_graphql_trace(result)


def _abort_on_magalu_blocked_streak(
    kind,
    streak,
    threshold,
    output,
    rows,
    checkpoint_writer=None,
):
    if threshold <= 0 or streak < threshold:
        return
    if checkpoint_writer:
        checkpoint_writer(rows)
    else:
        write_csv(output, rows, columns=OUTPUT_COLUMNS)
    print(f"[seda] aborting Magalu {kind}: blocked_graphql_streak={streak} checkpoint={output}", flush=True)
    raise RuntimeError(f"magalu_{kind}_blocked_graphql_streak:{streak}")


def _append_token(value, token):
    value = str(value or "").strip()
    token = str(token or "").strip()
    if not token:
        return value
    if not value:
        return token
    parts = value.split("+")
    return value if token in parts else f"{value}+{token}"


def _safe_log_value(value):
    return str(value or "").encode("ascii", "backslashreplace").decode("ascii")


def _safe_filename(value):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return text[:120] or "sku"


def _detail_raw_filename(row, index):
    run_token = os.getenv("SEDA_DETAIL_RUN_TOKEN", "").strip()
    worker_id = os.getenv("SEDA_DETAIL_WORKER_ID", "").strip()
    run_prefix = f"r{_safe_filename(run_token)}_" if run_token else ""
    worker_prefix = f"w{worker_id}_" if worker_id else ""
    return f"{run_prefix}{worker_prefix}{index:04d}_{_safe_filename(row.get('sku') or 'sku')}.html"


def _parallel_part_error(expected_rows, actual_rows):
    if len(actual_rows) != len(expected_rows):
        return f"row_count:{len(actual_rows)}!={len(expected_rows)}"
    expected_ids = [
        (
            str(row.get("item") or ""),
            str(row.get("product_url") or ""),
        )
        for row in expected_rows
    ]
    actual_ids = [
        (
            str(row.get("item") or ""),
            str(row.get("product_url") or ""),
        )
        for row in actual_rows
    ]
    if actual_ids != expected_ids:
        mismatch = next(
            (
                index
                for index, (expected, actual) in enumerate(zip(expected_ids, actual_ids))
                if expected != actual
            ),
            0,
        )
        return f"identity_at:{mismatch}:actual={actual_ids[mismatch]!r}:expected={expected_ids[mismatch]!r}"
    return csv_rows_contract_error(actual_rows, OUTPUT_COLUMNS)


def _publish_stage_path(canonical, run_token):
    canonical = Path(canonical)
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(run_token or "")).strip("._")
    if not token:
        raise RuntimeError("detail_publish_stage_token_blank")
    return canonical.with_name(
        f".{canonical.name}.{token}.detail_publish.stage"
    )


def _fsync_file(path):
    with Path(path).open("r+b") as handle:
        os.fsync(handle.fileno())


def _publish_detail_snapshot(
    root,
    output,
    product_rows,
    subcall_trace_rows,
    review_page_trace_rows,
    *,
    run_token=None,
    final_complete=False,
    expected_total=None,
    target_sha256="",
    target_path="",
    include_traces=None,
):
    """Stage, validate and transactionally publish one complete checkpoint."""
    root = Path(root)
    token = run_token or f"{os.getpid()}_{time.time_ns()}"
    product_stage = _publish_stage_path(output, token)
    write_csv(product_stage, product_rows, columns=OUTPUT_COLUMNS)
    _fsync_file(product_stage)
    header_error = _exact_csv_header_error(product_stage, OUTPUT_COLUMNS)
    if header_error:
        raise RuntimeError(f"detail_publish_invalid_product_stage:{header_error}")
    staged_product_rows = read_csv(str(product_stage))
    product_error = _parallel_part_error(product_rows, staged_product_rows)
    if product_error:
        raise RuntimeError(f"detail_publish_invalid_product_stage:{product_error}")

    publish_files = []
    trace_included = _trace_enabled() if include_traces is None else bool(include_traces)
    if trace_included:
        trace_specs = (
            (
                "subcall_trace",
                SUBCALL_TRACE_COLUMNS,
                subcall_trace_rows,
                True,
            ),
            (
                "magalu_review_page_trace",
                REVIEW_PAGE_TRACE_COLUMNS,
                review_page_trace_rows,
                False,
            ),
        )
        for stem, columns, trace_rows, require_coverage in trace_specs:
            canonical = _detail_trace_path(root, stem, tag="")
            staged = _publish_stage_path(canonical, token)
            _write_trace_csv(staged, trace_rows, columns)
            header_error = _exact_csv_header_error(staged, columns)
            if header_error:
                raise RuntimeError(
                    f"detail_publish_invalid_trace_stage:{stem}:{header_error}"
                )
            staged_trace_rows = read_csv(str(staged))
            trace_error = _trace_identity_error(
                staged_trace_rows,
                product_rows,
                start_index=0,
                require_coverage=require_coverage,
            )
            if trace_error:
                raise RuntimeError(
                    f"detail_publish_invalid_trace_stage:{stem}:{trace_error}"
                )
            publish_files.append(
                {"name": stem, "canonical": canonical, "staged": staged}
            )
    publish_files.append(
        {"name": "product", "canonical": Path(output), "staged": product_stage}
    )
    return publish_detail_files(
        root,
        publish_files,
        run_token=token,
        metadata={
            "complete": bool(final_complete),
            "product_row_count": len(product_rows),
            "expected_row_count": (
                len(product_rows) if expected_total is None else int(expected_total)
            ),
            "target_sha256": str(target_sha256 or ""),
            "target_path": str(target_path or ""),
            "trace_included": trace_included,
        },
    )


def _checkpoint_detail_state(
    root,
    output,
    product_rows,
    subcall_trace_rows,
    review_page_trace_rows,
    *,
    transactional,
    final_complete=False,
    expected_total=None,
    target_sha256="",
    target_path="",
):
    if transactional:
        return _publish_detail_snapshot(
            root,
            output,
            product_rows,
            subcall_trace_rows,
            review_page_trace_rows,
            final_complete=final_complete,
            expected_total=expected_total,
            target_sha256=target_sha256,
            target_path=target_path,
        )
    # Worker parts are private staging inputs for their parent transaction.
    # Keep trace-first ordering so an interrupted worker never exposes product
    # progress without its available diagnostic receipt.
    _write_detail_traces(root, subcall_trace_rows, review_page_trace_rows)
    write_csv(output, product_rows, columns=OUTPUT_COLUMNS)
    return None


def _resume_prefix(output, skip, is_worker, expected_rows, target_path=None):
    if not skip or is_worker:
        return []
    if len(expected_rows) != skip:
        raise RuntimeError(
            f"detail_resume_invalid_input:expected_count:{len(expected_rows)}!={skip}"
        )
    if not os.path.exists(output):
        raise RuntimeError(f"detail_resume_invalid_output:missing_output:{output}")
    if target_path and os.path.exists(target_path):
        if os.stat(output).st_mtime_ns < os.stat(target_path).st_mtime_ns:
            raise RuntimeError(
                "detail_resume_invalid_output:"
                f"older_than_target:path={output}:target={target_path}"
            )
    header_error = _exact_csv_header_error(output, OUTPUT_COLUMNS)
    if header_error:
        raise RuntimeError(
            f"detail_resume_invalid_output:{header_error}:path={output}"
        )
    prefix = read_csv(output)[:skip]
    error = _parallel_part_error(expected_rows, prefix)
    if error:
        raise RuntimeError(f"detail_resume_invalid_output:{error}:path={output}")
    return prefix


def _run_parallel(
    workers,
    rows,
    output,
    root=None,
    target_sha256="",
    target_path="",
    expected_total=None,
):
    """Fan step08 out across N child processes, each with its own browser, then merge.

    Detail is dominated by serial browser GraphQL round-trips; a single browser page
    processes them one at a time. Splitting the targets across N separate processes
    (distinct browser port + profile each) gives ~N x throughput. Merge preserves
    target order because the slices are contiguous.
    """
    if root is None:
        output_parent = Path(output).resolve().parent
        root = output_parent.parent if output_parent.name.lower() == "output" else output_parent
    else:
        root = Path(root)
    total = len(rows)
    expected_total = total if expected_total is None else int(expected_total)
    if total == 0:
        _publish_detail_snapshot(
            root,
            output,
            [],
            [],
            [],
            final_complete=expected_total == 0,
            expected_total=expected_total,
            target_sha256=target_sha256,
            target_path=target_path,
        )
        print(f"[seda] wrote {output} rows=0")
        return
    workers = max(1, min(workers, total))
    slice_size = math.ceil(total / workers)
    base_port = int(os.getenv("SEDA_MAGALU_BROWSER_BASE_PORT", "9350"))
    profile_base = os.getenv("SEDA_MAGALU_BROWSER_PROFILE", "C:/tmp/seda_magalu_drission_profile")
    stagger = float(os.getenv("SEDA_MAGALU_DETAIL_WORKER_STAGGER_SECONDS", "4"))
    out_dir = os.path.dirname(output) or "."
    run_token = f"{os.getpid()}_{time.time_ns()}"
    trace_enabled = _trace_enabled()
    parts, procs = [], []
    for i in range(workers):
        start = i * slice_size
        end = min(total, start + slice_size)
        if start >= end:
            continue
        # A per-parent token prevents an interrupted previous run (or a second
        # concurrent parent) from being mistaken for this worker's output.
        part = os.path.join(out_dir, f"_detail_part_{run_token}_{i}.csv")
        if os.path.exists(part):
            os.remove(part)
        env = dict(os.environ)
        env["SEDA_DETAIL_RUN_TOKEN"] = run_token
        env["SEDA_DETAIL_WORKER_ID"] = str(i)
        trace_tag = f"{run_token}_w{i}"
        env["SEDA_DETAIL_TRACE_TAG"] = trace_tag
        env["SEDA_MAGALU_DETAIL_WORKERS"] = "1"  # child stays serial (also guarded by WORKER_ID)
        env["SEDA_DETAIL_SKIP"] = str(start)
        env["SEDA_DETAIL_LIMIT"] = str(end)
        env["SEDA_DETAIL_TOTAL_ROWS"] = str(total)
        env["SEDA_DETAIL_OUTPUT_CSV"] = part
        env["SEDA_MAGALU_BROWSER_LOCAL_PORT"] = str(base_port + i)
        env["SEDA_MAGALU_BROWSER_PROFILE"] = f"{profile_base}_w{i}"
        env["SEDA_DETAIL_TRACE"] = "1" if trace_enabled else "0"
        env.pop("SEDA_MAGALU_BROWSER_ADDRESS", None)  # each worker launches its own browser
        parts.append((i, start, end, part, trace_tag))
        print(f"[seda] detail worker {i}: rows [{start}:{end}) port={base_port + i} -> {part}", flush=True)
        procs.append(subprocess.Popen([sys.executable, "-m", "seda.magalu.step08_detail_enrichment"], env=env))
        if stagger > 0 and i < workers - 1:
            time.sleep(stagger)  # stagger browser launches to avoid startup contention
    failed = []
    for (i, _s, _e, _p, _tag), proc in zip(parts, procs):
        return_code = proc.wait()
        if return_code != 0:
            failed.append((i, return_code))
    if failed:
        detail = ",".join(f"worker={i}:exit={return_code}" for i, return_code in failed)
        raise RuntimeError(f"detail_parallel_worker_failed:{detail}")

    merged = []
    invalid = []
    for (i, start, end, part, _tag) in parts:
        if not os.path.exists(part):
            invalid.append(f"worker={i}:missing_output")
            continue
        header_error = _exact_csv_header_error(part, OUTPUT_COLUMNS)
        if header_error:
            invalid.append(f"worker={i}:{header_error}")
            continue
        part_rows = read_csv(part)
        error = _parallel_part_error(rows[start:end], part_rows)
        if error:
            invalid.append(f"worker={i}:{error}")
            continue
        merged.extend(part_rows)
    if invalid:
        raise RuntimeError(f"detail_parallel_invalid_output:{';'.join(invalid)}")
    if len(merged) != total:
        raise RuntimeError(f"detail_parallel_merged_count:{len(merged)}!={total}")

    subcall_rows, review_rows = _merge_parallel_detail_traces(
        root,
        parts,
        rows,
        run_token,
    )

    def recovery_checkpoint(snapshot_rows):
        return _publish_detail_snapshot(
            root,
            output,
            snapshot_rows,
            subcall_rows,
            review_rows,
            run_token=run_token,
            final_complete=False,
            expected_total=expected_total,
            target_sha256=target_sha256,
            target_path=target_path,
        )

    merged = _backfill_magalu_tv_skus_from_title_url(
        merged,
        checkpoint_writer=recovery_checkpoint,
    )
    merged = _backfill_magalu_zenrows_fields(
        merged,
        output,
        checkpoint_every=int(
            os.getenv("SEDA_MAGALU_ZENROWS_FIELD_CHECKPOINT_EVERY", "5")
        ),
        trace_rows=subcall_rows,
        checkpoint_writer=recovery_checkpoint,
    )
    _publish_detail_snapshot(
        root,
        output,
        merged,
        subcall_rows,
        review_rows,
        run_token=run_token,
        final_complete=len(merged) == expected_total,
        expected_total=expected_total,
        target_sha256=target_sha256,
        target_path=target_path,
    )
    for _i, _start, _end, part, _tag in parts:
        try:
            os.remove(part)
        except OSError:
            pass
    _remove_parallel_detail_trace_parts(root, [tag for _i, _s, _e, _p, tag in parts])
    print(f"[seda] wrote {output} rows={len(merged)}/{total} (parallel workers={workers})", flush=True)


def main():
    root = run_root()
    is_worker = bool(os.getenv("SEDA_DETAIL_WORKER_ID"))
    if is_worker:
        return _run_detail_main(root, is_worker=True)
    with detail_run_lock(root):
        return _run_detail_main(root, is_worker=False)


def _run_detail_main(root, *, is_worker):
    if not is_worker:
        # Resolve a crash-interrupted publish before reading checkpoint state or
        # issuing any network request.
        recover_detail_publish_transaction(root)
    input_csv = os.getenv("SEDA_DETAIL_TARGET_CSV", str(root / "output" / "seda_final_targets.csv"))
    input_path = Path(input_csv)
    if not input_path.is_file():
        raise FileNotFoundError(f"detail_target_missing:{input_path}")
    target_header_error = _exact_csv_header_error(input_path, OUTPUT_COLUMNS)
    if target_header_error:
        raise RuntimeError(
            f"detail_target_invalid_header:{target_header_error}:path={input_path}"
        )
    target_sha256 = file_sha256(input_path) if not is_worker else ""
    rows = read_csv(input_csv)
    target_rows_error = csv_rows_contract_error(rows, OUTPUT_COLUMNS)
    if target_rows_error:
        raise RuntimeError(
            f"detail_target_invalid_rows:{target_rows_error}:path={input_path}"
        )
    if not is_worker and file_sha256(input_path) != target_sha256:
        raise RuntimeError(f"detail_target_changed_during_read:{input_path}")
    source_row_count = len(rows)
    limit = int(os.getenv("SEDA_DETAIL_LIMIT", "0"))
    if limit:
        rows = rows[:limit]
    total_rows = (
        int(os.getenv("SEDA_DETAIL_TOTAL_ROWS", str(source_row_count)))
        if is_worker
        else source_row_count
    )
    target_path = str(input_path.resolve()) if not is_worker else ""
    output = os.getenv("SEDA_DETAIL_OUTPUT_CSV", str(root / "output" / "final_output_enriched.csv"))
    if not is_worker:
        mark_detail_publish_incomplete(
            root,
            expected_row_count=total_rows,
            target_sha256=target_sha256,
            target_path=target_path,
        )
    if os.getenv("SEDA_MAGALU_SHIPPING_BACKFILL_ONLY", "0").lower() in {"1", "true", "yes", "y"}:
        checkpoint_every = int(os.getenv("SEDA_DETAIL_CHECKPOINT_EVERY", "25"))

        def shipping_checkpoint(snapshot_rows):
            return _publish_detail_snapshot(
                root,
                output,
                snapshot_rows,
                [],
                [],
                expected_total=total_rows,
                target_sha256=target_sha256,
                target_path=target_path,
                include_traces=False,
            )

        rows = _backfill_magalu_shipping_blanks(
            rows,
            output,
            checkpoint_every=checkpoint_every,
            checkpoint_writer=shipping_checkpoint,
        )
        _publish_detail_snapshot(
            root,
            output,
            rows,
            [],
            [],
            final_complete=len(rows) == total_rows,
            expected_total=total_rows,
            target_sha256=target_sha256,
            target_path=target_path,
            include_traces=False,
        )
        print(f"[seda] wrote {output} rows={len(rows)}")
        return
    workers = int(os.getenv("SEDA_MAGALU_DETAIL_WORKERS", "1") or "1")
    is_magalu = os.getenv("SEDA_ACTIVE_RETAILER", "").strip().lower() == "magalu"
    is_resume = bool(os.getenv("SEDA_DETAIL_SKIP", "").strip())
    if is_magalu and workers > 1 and not is_worker and not is_resume:
        # fan out across N child processes (each its own browser), then merge.
        # Magalu-only; skipped when resuming (SEDA_DETAIL_SKIP) so resume stays serial.
        _run_parallel(
            workers,
            rows,
            output,
            root=root,
            target_sha256=target_sha256,
            target_path=target_path,
            expected_total=total_rows,
        )
        return
    skip = int(os.getenv("SEDA_DETAIL_SKIP", "0"))
    enriched = _resume_prefix(
        output,
        skip,
        is_worker,
        rows[:skip],
        target_path=input_csv,
    )
    if skip and not is_worker:
        subcall_trace_rows, review_page_trace_rows = _resume_detail_trace_prefix(
            root,
            rows[:skip],
        )
    else:
        subcall_trace_rows = []
        review_page_trace_rows = []
    if skip:
        rows = rows[skip:]
    row_index_offset = skip if is_worker else len(enriched)
    checkpoint_every = int(os.getenv("SEDA_DETAIL_CHECKPOINT_EVERY", "25"))
    fallback_fetch = os.getenv("SEDA_DETAIL_FALLBACK_FETCH", "1").lower() not in {"0", "false", "no", "n"}
    magalu_detail_blocked_streak = 0
    magalu_review_blocked_streak = 0
    magalu_detail_abort_threshold = int(os.getenv("SEDA_MAGALU_DETAIL_403_ABORT_THRESHOLD", "5"))
    magalu_review_abort_threshold = int(os.getenv("SEDA_MAGALU_REVIEW_403_ABORT_THRESHOLD", "5"))

    def checkpoint_writer(snapshot_rows):
        return _checkpoint_detail_state(
            root,
            output,
            snapshot_rows,
            subcall_trace_rows,
            review_page_trace_rows,
            transactional=not is_worker,
            expected_total=total_rows,
            target_sha256=target_sha256,
            target_path=target_path,
        )

    for index, row in enumerate(rows, start=row_index_offset + 1):
        url = row.get("product_url", "")
        if row.get("retailer") == "Magalu" and not row.get("seller_id"):
            row["seller_id"] = _magalu_seller_id(row, url)
        if not url:
            row["parse_status"] = "missing_product_url"
            _record_subcall(subcall_trace_rows, row, index, url, "detail_row", success=False, error="missing_product_url")
            enriched.append(row)
            continue
        _clear_magalu_listing_metrics(row)
        graph_result = _magalu_graphql_detail(row, url, trace_rows=subcall_trace_rows, row_index=index)
        detail_done = bool(graph_result and graph_result.get("success"))
        if row.get("retailer") == "Magalu" and graph_result is not None:
            if detail_done:
                magalu_detail_blocked_streak = 0
            elif _has_blocked_graphql_trace(graph_result) and not _is_expected_magalu_item_query_block(graph_result):
                magalu_detail_blocked_streak += 1
                _abort_on_magalu_blocked_streak(
                    "detail",
                    magalu_detail_blocked_streak,
                    magalu_detail_abort_threshold,
                    output,
                    enriched + [row],
                    checkpoint_writer=checkpoint_writer,
                )
            else:
                magalu_detail_blocked_streak = 0
        result = None
        if not detail_done and row.get("retailer") == "Magalu":
            detail_done = _merge_magalu_zenrows_detail(row, url, trace_rows=subcall_trace_rows, row_index=index)
        if not detail_done and _fallback_fetch_enabled_for_row(row, fallback_fetch):
            result = fetch_url(_detail_fetch_url(row, url))
            _record_subcall(
                subcall_trace_rows,
                row,
                index,
                url,
                "detail_fallback_fetch",
                method=result.method,
                success=bool(result.text and not result.error and not is_blocked_html(result.text, result.status_code)),
                status_code=result.status_code,
                length=len(result.text or ""),
                has_next_data=int("__NEXT_DATA__" in (result.text or "")),
                error=result.error,
            )
            raw_dir = root / "detail" / "raw" / row.get("retailer", "unknown").lower().replace(" ", "_")
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / _detail_raw_filename(row, index)
            raw_path.write_text(result.text or result.error, encoding="utf-8", errors="ignore")
            blocked = is_blocked_html(result.text, result.status_code)
            if result.text and not result.error and not blocked:
                detail = parse_detail(result.text, row.get("retailer", ""), _base_url(row.get("retailer", "")), url)
                _merge_generic_product_detail(row, detail)
                row["fetch_method"] = _append_token(row.get("fetch_method", ""), result.method)
            else:
                detail_error = result.error or ("blocked_html" if blocked else "empty_detail")
                row["parse_status"] = _append_token(row.get("parse_status", ""), f"detail_fetch_failed:{detail_error}")
        elif not detail_done:
            row["parse_status"] = _append_token(row.get("parse_status", ""), "detail_fetch_skipped")
            _record_subcall(subcall_trace_rows, row, index, url, "detail_fallback_fetch", success=False, error="skipped")
        review_result = _merge_magalu_reviews(row, url, trace_rows=subcall_trace_rows, row_index=index)
        if row.get("retailer") == "Magalu" and review_result is not None:
            if review_result.get("success"):
                magalu_review_blocked_streak = 0
            elif _has_blocked_graphql_trace(review_result):
                magalu_review_blocked_streak += 1
                _abort_on_magalu_blocked_streak(
                    "review",
                    magalu_review_blocked_streak,
                    magalu_review_abort_threshold,
                    output,
                    enriched + [row],
                    checkpoint_writer=checkpoint_writer,
                )
            else:
                magalu_review_blocked_streak = 0
        _merge_magalu_pdp_html(
            row,
            url,
            trace_rows=subcall_trace_rows,
            row_index=index,
            allow_zenrows_pdp=_is_expected_magalu_item_query_block(graph_result),
        )
        _merge_magalu_similar(
            row,
            url,
            trace_rows=subcall_trace_rows,
            row_index=index,
            prior_error=(graph_result or {}).get("similar_error", ""),
        )
        _merge_magalu_review_pages(
            row,
            url,
            trace_rows=subcall_trace_rows,
            review_page_trace_rows=review_page_trace_rows,
            row_index=index,
        )
        _merge_casas_bahia_apis(row)
        enriched.append(row)
        method = row.get("fetch_method") or (result.method if result else "")
        print(
            f"[seda] detail {index}/{total_rows} {_safe_log_value(row.get('retailer'))} "
            f"sku={_safe_log_value(row.get('sku'))} method={_safe_log_value(method)} "
            f"status={_safe_log_value(str(row.get('parse_status', '')).split('+')[-1])}",
            flush=True,
        )
        if checkpoint_every and index % checkpoint_every == 0:
            checkpoint_writer(enriched)
            print(f"[seda] checkpoint {output} rows={len(enriched)}", flush=True)
    enriched = _backfill_magalu_detail_blanks(
        enriched,
        output,
        checkpoint_every=checkpoint_every,
        trace_rows=subcall_trace_rows,
        row_index_offset=skip if is_worker else 0,
        checkpoint_writer=checkpoint_writer,
    )
    if not is_worker:
        enriched = _backfill_magalu_tv_skus_from_title_url(
            enriched,
            checkpoint_writer=checkpoint_writer,
        )
        enriched = _backfill_magalu_zenrows_fields(
            enriched,
            output,
            checkpoint_every=int(
                os.getenv("SEDA_MAGALU_ZENROWS_FIELD_CHECKPOINT_EVERY", "5")
            ),
            trace_rows=subcall_trace_rows,
            checkpoint_writer=checkpoint_writer,
        )
        enriched = _backfill_casas_zenrows_fields(
            enriched,
            output,
            checkpoint_every=_casas_zenrows_int_env(
                "SEDA_CASAS_BAHIA_ZENROWS_FIELD_CHECKPOINT_EVERY",
                5,
                maximum=100,
            ),
            trace_rows=subcall_trace_rows,
            checkpoint_writer=checkpoint_writer,
        )
    enriched = _backfill_magalu_shipping_blanks(
        enriched,
        output,
        checkpoint_every=checkpoint_every,
        checkpoint_writer=checkpoint_writer,
    )
    _checkpoint_detail_state(
        root,
        output,
        enriched,
        subcall_trace_rows,
        review_page_trace_rows,
        transactional=not is_worker,
        final_complete=len(enriched) == total_rows,
        expected_total=total_rows,
        target_sha256=target_sha256,
        target_path=target_path,
    )
    print(f"[seda] wrote {output} rows={len(enriched)}")


if __name__ == "__main__":
    main()
