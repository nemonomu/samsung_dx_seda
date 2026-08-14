"""Validate one isolated Magalu TV main-listing ZenRows smoke run."""

import argparse
import csv
import json
from pathlib import Path
from urllib.parse import urlparse

from seda.magalu.search_api import _strict_search_payload_error
from seda.parsers import extract_next_data, sku_from_url
from seda.zenrows_usage import summarize_usage


EXPECTED_PAGE = 1
EXPECTED_PAGE_SIZE = 60
EXPECTED_SORT_TYPE = "score"
EXPECTED_SORT_ORIENTATION = "desc"
EXPECTED_PRODUCT_LINE = "tv"
EXPECTED_RETAILER = "magalu"
EXPECTED_LISTING_URL = "https://www.magazineluiza.com.br/busca/tv/"
MAX_HTTP_CALLS = 4


def validate_smoke_run(run_root, transport="zenrows"):
    root = Path(run_root)
    transport = str(transport or "zenrows").strip().lower()
    if transport not in {"zenrows", "production"}:
        raise ValueError(f"unknown_transport_contract:{transport}")
    errors = []
    evidence = {
        "run_root": str(root),
        "scope": "main",
        "transport_contract": transport,
        "expected_page": EXPECTED_PAGE,
        "expected_page_size": EXPECTED_PAGE_SIZE,
    }

    manifest_path = root / "main" / "manifest.json"
    raw_path = root / "main" / "raw" / EXPECTED_RETAILER / "page_001.html"
    failed_path = (
        root
        / "main"
        / "raw_failed"
        / EXPECTED_RETAILER
        / "page_001.json"
    )
    csv_path = (
        root
        / "main"
        / "parsed"
        / "main_occurrences.csv"
    )
    evidence["artifacts"] = {
        "manifest": str(manifest_path),
        "raw": str(raw_path),
        "raw_failed": str(failed_path),
        "parsed_csv": str(csv_path),
    }

    manifest = _read_json(manifest_path, errors, "manifest")
    if isinstance(manifest, dict):
        _validate_manifest(manifest, evidence, errors, transport)

    raw_text = _read_text(raw_path, errors, "raw")
    if raw_text and isinstance(manifest, dict):
        _validate_raw(raw_text, manifest, evidence, errors)

    if failed_path.exists():
        errors.append("raw_failed_present")

    csv_records = _read_csv(csv_path, errors)
    csv_rows = len(csv_records)
    evidence["csv_rows"] = csv_rows
    manifest_rows = _as_int(manifest.get("rows"), 0) if isinstance(manifest, dict) else 0
    if csv_rows != manifest_rows:
        errors.append(f"csv_manifest_row_mismatch:{csv_rows}!={manifest_rows}")
    if isinstance(manifest, dict):
        _validate_csv(csv_records, manifest, errors)

    method = evidence.get("transport_method", "")
    if transport == "production" and method in {
        "direct_graphql_search",
        "browser_graphql_search",
    }:
        usage = _validate_no_zenrows_usage(root, errors)
    else:
        usage = _validate_usage(root, errors)
    evidence["zenrows_usage"] = usage

    report_path = root / "status" / "listing_smoke_check.json"
    report = {
        "passed": not errors,
        "errors": errors,
        "evidence": evidence,
        "report_path": str(report_path),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _validate_manifest(manifest, evidence, errors, transport):
    if manifest.get("run_id") != "main":
        errors.append(f"manifest_run_id:{manifest.get('run_id') or 'missing'}")
    rows = _as_int(manifest.get("rows"), 0)
    evidence["manifest_rows"] = rows
    if rows <= 0:
        errors.append("manifest_rows_empty")
    if manifest.get("failures") != []:
        errors.append("manifest_failures_present")
    if manifest.get("retailers") != [EXPECTED_RETAILER]:
        errors.append("manifest_retailers_mismatch")
    if manifest.get("pages") != [EXPECTED_PAGE]:
        errors.append("manifest_pages_mismatch")
    expected_fetch_mode = (
        "zenrows"
        if transport == "zenrows"
        else "magalu_listing_graphql_zenrows"
    )
    if manifest.get("fetch_mode") != expected_fetch_mode:
        errors.append(
            f"manifest_fetch_mode:{manifest.get('fetch_mode') or 'missing'}"
            f"!={expected_fetch_mode}"
        )

    stats = manifest.get("listing_stats")
    if not isinstance(stats, list) or len(stats) != 1 or not isinstance(stats[0], dict):
        errors.append("listing_stats_invalid")
        return
    item = stats[0]
    evidence["listing_stats"] = item
    method = str(item.get("method") or "")
    evidence["transport_method"] = method
    if item.get("retailer") != "Magalu":
        errors.append(f"listing_stats_retailer:{item.get('retailer') or 'missing'}")
    expected = {
        "page": EXPECTED_PAGE,
        "pagination_page": EXPECTED_PAGE,
        "pagination_size": EXPECTED_PAGE_SIZE,
        "selected_sort_type": EXPECTED_SORT_TYPE,
        "selected_sort_orientation": EXPECTED_SORT_ORIENTATION,
    }
    for key, value in expected.items():
        if item.get(key) != value:
            errors.append(f"listing_stats_{key}:{item.get(key)}!={value}")
    if _as_int(item.get("raw_products"), 0) <= 0:
        errors.append("listing_stats_raw_products_empty")
    if _as_int(item.get("parsed_rows"), 0) <= 0:
        errors.append("listing_stats_parsed_rows_empty")
    unique = _as_int(item.get("unique"), 0)
    if unique <= 0:
        errors.append("listing_stats_unique_empty")
    allowed_methods = {"zenrows_graphql_search", "zenrows"}
    if transport == "production":
        allowed_methods.update(
            {"direct_graphql_search", "browser_graphql_search"}
        )
    if method not in allowed_methods:
        errors.append(f"listing_stats_method:{method or 'missing'}")
    raw_products = _as_int(item.get("raw_products"), 0)
    kept_products = _as_int(item.get("kept_products"), 0)
    dropped_products = _as_int(item.get("dropped_products"), 0)
    parsed_rows = _as_int(item.get("parsed_rows"), 0)
    if unique > parsed_rows:
        errors.append("listing_stats_unique_exceeds_parsed")
    if raw_products != kept_products + dropped_products:
        errors.append("listing_stats_product_arithmetic_mismatch")
    if parsed_rows != kept_products:
        errors.append("listing_stats_parsed_kept_mismatch")
    if parsed_rows != rows:
        errors.append("listing_stats_manifest_rows_mismatch")


def _validate_raw(raw_text, manifest, evidence, errors):
    stats = manifest.get("listing_stats")
    item = stats[0] if isinstance(stats, list) and stats and isinstance(stats[0], dict) else {}
    url = str(item.get("url") or "")
    if not url:
        errors.append("listing_stats_url_missing")
        return
    if url != EXPECTED_LISTING_URL:
        errors.append(f"listing_stats_url:{url}!=expected_tv_url")
        return
    data = extract_next_data(raw_text)
    props = data.get("props") if isinstance(data, dict) else {}
    page_props = props.get("pageProps") if isinstance(props, dict) else {}
    page_data = page_props.get("data") if isinstance(page_props, dict) else {}
    search = page_data.get("search") if isinstance(page_data, dict) else None
    payload_error = _strict_search_payload_error(
        url,
        search,
        EXPECTED_PAGE_SIZE,
    )
    evidence["raw_payload_error"] = payload_error
    if payload_error:
        errors.append(f"raw_payload:{payload_error}")
        return
    products = search.get("products") if isinstance(search, dict) else []
    raw_products = _as_int(item.get("raw_products"), 0)
    if len(products) != raw_products:
        errors.append(f"raw_manifest_product_mismatch:{len(products)}!={raw_products}")


def _validate_csv(records, manifest, errors):
    stats = manifest.get("listing_stats")
    item = stats[0] if isinstance(stats, list) and stats and isinstance(stats[0], dict) else {}
    source_url = str(item.get("url") or "")
    for index, record in enumerate(records, start=1):
        retailer = str(record.get("retailer") or "").strip()
        product_line = str(record.get("product_line") or "").strip()
        item_id = str(record.get("item") or "").strip()
        name = str(record.get("retailer_sku_name") or "").strip()
        product_url = str(record.get("product_url") or "").strip()
        row_source_url = str(record.get("source_url") or "").strip()
        if retailer != "Magalu":
            errors.append(f"csv_row_{index}_retailer")
        if product_line != "TV":
            errors.append(f"csv_row_{index}_product_line")
        if not item_id:
            errors.append(f"csv_row_{index}_item_missing")
        if not name:
            errors.append(f"csv_row_{index}_name_missing")
        parsed_url = urlparse(product_url)
        if parsed_url.netloc != "www.magazineluiza.com.br" or not sku_from_url(product_url):
            errors.append(f"csv_row_{index}_product_url_invalid")
        elif sku_from_url(product_url) != item_id:
            errors.append(f"csv_row_{index}_url_item_mismatch")
        if row_source_url != source_url:
            errors.append(f"csv_row_{index}_source_url_mismatch")


def _validate_usage(root, errors):
    usage_root = root / "status" / "zenrows_usage"
    try:
        executions = sorted(path for path in usage_root.iterdir() if path.is_dir())
    except FileNotFoundError:
        executions = []
    except OSError as exc:
        errors.append(f"usage_directory_error:{type(exc).__name__}")
        executions = []
    if len(executions) != 1:
        errors.append(f"usage_execution_count:{len(executions)}!=1")
        return {
            "tracking_status": "unavailable",
            "http_calls": 0,
            "post_calls": 0,
            "get_calls": 0,
        }

    execution_id = executions[0].name
    summary = summarize_usage(root, execution_id=execution_id)
    http_calls = _as_int(summary.get("http_calls"), 0)
    if summary.get("tracking_status") != "complete":
        errors.append(f"usage_tracking:{summary.get('tracking_status') or 'missing'}")
    if not 1 <= http_calls <= MAX_HTTP_CALLS:
        errors.append(f"usage_http_calls:{http_calls}")
    if _as_int((summary.get("by_retailer") or {}).get(EXPECTED_RETAILER), 0) != http_calls:
        errors.append("usage_retailer_count_mismatch")
    if _as_int((summary.get("by_product_line") or {}).get(EXPECTED_PRODUCT_LINE), 0) != http_calls:
        errors.append("usage_product_line_count_mismatch")

    events = _read_usage_events(executions[0], errors)
    post_events = [event for event in events if event.get("method") == "POST"]
    get_events = [event for event in events if event.get("method") == "GET"]
    if len(post_events) != 1:
        errors.append(f"usage_post_calls:{len(post_events)}!=1")
    if len(get_events) > 3:
        errors.append(f"usage_get_calls:{len(get_events)}>3")
    if len(events) != len(post_events) + len(get_events):
        errors.append("usage_http_method_invalid")
    if len(events) != http_calls:
        errors.append(f"usage_event_count:{len(events)}!={http_calls}")
    for event in post_events:
        if event.get("profile") != "premium_html":
            errors.append(f"usage_post_profile:{event.get('profile') or 'missing'}")
        if event.get("estimated_multiplier") != "10x":
            errors.append(
                "usage_post_multiplier:"
                f"{event.get('estimated_multiplier') or 'missing'}"
            )
    expected_ladder = [
        ("POST", "premium_html", "10x"),
        ("GET", "premium_html", "10x"),
        ("GET", "listing_next_data_js_wait", "25x"),
        ("GET", "listing_next_data_js_wait", "25x"),
    ]
    actual_ladder = [
        (
            str(event.get("method") or ""),
            str(event.get("profile") or ""),
            str(event.get("estimated_multiplier") or ""),
        )
        for event in events
    ]
    if actual_ladder != expected_ladder[: len(actual_ladder)]:
        errors.append("usage_ladder_order_mismatch")

    return {
        **summary,
        "post_calls": len(post_events),
        "get_calls": len(get_events),
    }


def _validate_no_zenrows_usage(root, errors):
    usage_root = root / "status" / "zenrows_usage"
    events = []
    try:
        executions = sorted(path for path in usage_root.iterdir() if path.is_dir())
    except FileNotFoundError:
        executions = []
    except OSError as exc:
        errors.append(f"usage_directory_error:{type(exc).__name__}")
        executions = []
    for execution in executions:
        events.extend(_read_usage_events(execution, errors))
    if events:
        errors.append(f"usage_unexpected_for_graphql_success:{len(events)}")
    return {
        "scope": "graphql_success_no_zenrows",
        "tracking_status": "not_used",
        "http_calls": len(events),
        "by_retailer": {},
        "by_product_line": {},
        "error": "",
        "post_calls": 0,
        "get_calls": 0,
    }


def _read_usage_events(execution_root, errors):
    events = []
    try:
        shards = sorted(execution_root.glob("*.jsonl"))
    except OSError as exc:
        errors.append(f"usage_shard_list_error:{type(exc).__name__}")
        return events
    for shard in shards:
        try:
            lines = shard.read_text(encoding="ascii", errors="strict").splitlines()
        except Exception as exc:
            errors.append(f"usage_shard_read_error:{type(exc).__name__}")
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                errors.append("usage_event_invalid_json")
                continue
            if not isinstance(event, dict) or event.get("event") != "zenrows_http_request_attempt":
                errors.append("usage_event_invalid")
                continue
            events.append(event)
    return events


def _read_json(path, errors, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{label}_missing")
        return None
    except Exception as exc:
        errors.append(f"{label}_read_error:{type(exc).__name__}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label}_invalid_type")
        return None
    return value


def _read_text(path, errors, label):
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except FileNotFoundError:
        errors.append(f"{label}_missing")
        return ""
    except Exception as exc:
        errors.append(f"{label}_read_error:{type(exc).__name__}")
        return ""
    if not text.strip():
        errors.append(f"{label}_empty")
        return ""
    return text


def _read_csv(path, errors):
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except FileNotFoundError:
        errors.append("parsed_csv_missing")
    except Exception as exc:
        errors.append(f"parsed_csv_read_error:{type(exc).__name__}")
    return []


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate one Magalu TV main-page ZenRows smoke run."
    )
    parser.add_argument("--run-root", required=True)
    parser.add_argument(
        "--transport",
        choices=("zenrows", "production"),
        default="zenrows",
    )
    args = parser.parse_args(argv)

    report = validate_smoke_run(args.run_root, transport=args.transport)
    status = "PASS" if report["passed"] else "FAIL"
    evidence = report["evidence"]
    usage = evidence.get("zenrows_usage") or {}
    print(
        "[seda][magalu-listing-smoke] "
        f"status={status} rows={evidence.get('manifest_rows', 0)} "
        f"http_calls={usage.get('http_calls', 0)} "
        f"post_calls={usage.get('post_calls', 0)} "
        f"get_calls={usage.get('get_calls', 0)} "
        f"report={report['report_path']}",
        flush=True,
    )
    for error in report["errors"]:
        print(f"[seda][magalu-listing-smoke] error={error}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
