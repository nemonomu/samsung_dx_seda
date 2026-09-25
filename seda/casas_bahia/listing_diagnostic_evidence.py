"""Offline-only, allowlist-based automatic Casas listing evidence/report storage.

No environment loading, networking, browser calls, credential inspection, raw
responses or recursive filesystem access is implemented in this module.
"""

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import stat
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import zipfile


MAX_ITEMS = 2000
MAX_EVENTS = 50000
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
METHODS = {"GET", "POST", "OPTIONS"}
NETWORK_ERRORS = {
    "net::ERR_ABORTED", "net::ERR_TIMED_OUT", "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_CLOSED", "net::ERR_CONNECTION_TIMED_OUT",
    "net::ERR_NETWORK_CHANGED", "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_HTTP2_PROTOCOL_ERROR", "net::ERR_CONTENT_LENGTH_MISMATCH",
    "net::ERR_INCOMPLETE_CHUNKED_ENCODING", "net::ERR_BLOCKED_BY_CLIENT",
    "net::ERR_BLOCKED_BY_RESPONSE", "net::ERR_FAILED",
}
ERRORS = {
    "none", "other_error", "api_http_not_200", "invalid_api_json",
    "request_response_evidence_missing", "document_navigation_during_api_call",
    "invalid_browser_fetch_result", "response_page_mismatch", "response_sort_mismatch",
    "empty_or_invalid_products", "invalid_listing_product", "listing_identity_invalid",
    "conflicting_source_sku_aliases", "duplicate_listing_sku", "bootstrap_frame_missing",
    "not_casas_bahia_listing_url", "outgoing_page_or_sort_mismatch", "empty_price_items",
    "earlier_page_repeated_for_other_page", "document_not_completed", "document_not_200",
    "new_document_navigation_not_observed", "cached_document_rejected",
    "missing_current_price_response", "invalid_requested_page", "invalid_price_payload",
    "invalid_price_offer", "conflicting_price_offers", "missing_next_data",
    "missing_ssr_listing", "requested_page_mismatch", "requested_sort_mismatch",
    "empty_ssr_products", "invalid_ssr_product", "price_or_seller_identity_incomplete",
    "ambiguous_listing_seller", "missing_listing_price", "next_data_script_count_invalid",
    "parsed_listing_identity_invalid", "parsed_price_or_seller_mismatch",
    "chrome_version_unavailable", "chrome_executable_missing", "browser_driver_version_mismatch",
    "isolated_driver_version_unavailable", "isolated_driver_major_mismatch",
    "isolated_driver_path_invalid", "existing_browser_attachment_rejected", "ssr_fallback_failed",
    "empty_products", "missing_product_identity", "duplicate_product_identity", "price_identity_mismatch",
    "missing_price", "no_relevant_parsed_products", "parsed_identity_or_price_mismatch", "invalid_listing_payload",
    "missing_product_url_identity", "optional_price_diagnostics_failed", "price_attach_disabled",
    "price_attach_failed", "price_request_failed", "invalid_price_offers", "invalid_price_json",
    "search_http_not_200", "non_json_response", "invalid_json", "invalid_product_payload",
    "rest_listing_request_failed", "rest_listing_failed", "invalid_price_result", "invalid_price_status",
}
ERRORS |= {f"price_http_{status}" for status in range(100, 600)}
EXCEPTIONS = {
    "TimeoutException", "WebDriverException", "SessionNotCreatedException",
    "InvalidSessionIdException", "NoSuchWindowException", "JavascriptException",
    "ConnectionError", "TimeoutError", "OSError", "PermissionError", "FileNotFoundError",
    "ValueError", "TypeError", "KeyError", "RuntimeError", "ImportError",
    "ModuleNotFoundError", "KeyboardInterrupt", "AssertionError",
}
ERRORS |= EXCEPTIONS | {prefix + name for prefix in ("browser_", "browser_api_") for name in EXCEPTIONS}


def safe_error(value):
    """No arbitrary exception text, even if it looks like a plausible code."""
    if value is None or value == "":
        return "none"
    return value if isinstance(value, str) and value in ERRORS else "other_error"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _integer(value, maximum=10**9, minimum=0):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        value = int(value)
    return value if type(value) is int and minimum <= value <= maximum else None


def _status(value):
    if type(value) is float and math.isfinite(value) and value.is_integer():
        value = int(value)
    return _integer(value, 599)


def _number(value):
    if type(value) in (int, float) and 0 <= value <= 10**9 and math.isfinite(value):
        return round(value, 6)
    return None


def _enum(value, values, fallback="other"):
    return value if isinstance(value, str) and value in values else fallback


def _numbers(source, fields):
    return {key: number for key in fields if (number := _number(source.get(key))) is not None}


def _flags(source, fields):
    return {key: source[key] for key in fields if type(source.get(key)) is bool}


def request_summary(params):
    params = _mapping(params)
    result = {}
    for key, limit in (("page", 1000), ("resultsperpage", 10000), ("regionid", 10**9), ("configured_rest_page_size", 10000)):
        number = _integer(params.get(key), limit)
        if number is not None:
            result[key] = number
    if "variantconfiguration" in params:
        result["variantconfiguration"] = _enum(params["variantconfiguration"], {"q2"})
    sort = params.get("sortby", params.get("sortBy", params.get("sort")))
    if sort is not None:
        result["sort"] = _enum(sort, {"relevance", "relevancia", "maisvendidos", "topSelling",
                                     "topselling", "priceasc", "pricedesc", "maisVendidos"})
    if "userid" in params:
        result["user_id_empty"] = params["userid"] in (None, "")
    elif type(params.get("user_id_empty")) is bool:
        result["user_id_empty"] = params["user_id_empty"]
    return result


def _evidence_summary(value):
    value = _mapping(value)
    result = _numbers(value, {"matching_request_count", "matching_response_count", "document_requests_observed"})
    result.update(_flags(value, {"cached_response_observed", "request_response_verified", "same_document"}))
    if "request_method" in value:
        result["request_method"] = _enum(value["request_method"], METHODS)
    methods = value.get("matched_http200_methods")
    if isinstance(methods, list):
        result["matched_http200_methods"] = sorted({item for item in methods[:MAX_ITEMS] if isinstance(item, str) and item in METHODS})
    statuses = value.get("response_status_codes")
    if isinstance(statuses, list):
        result["response_status_codes"] = [code for item in statuses[:MAX_ITEMS] if (code := _integer(item, 599)) is not None]
    return result


def api_summary(value):
    value = _mapping(value)
    result = _numbers(value, {"elapsed_seconds"})
    code = _status(value.get("status_code"))
    if code is not None:
        result["status_code"] = code
    result.update(_flags(value, {"ok", "json"}))
    if "network" in value:
        result["network"] = _evidence_summary(value["network"])
    if "error" in value:
        result["error"] = safe_error(value["error"])
    if "error_name" in value:
        result["error_name"] = _enum(value["error_name"], {"AbortError", "FetchError", "InvalidJSON"})
    return result


def _method_network(value):
    value = _mapping(value)
    result = _numbers(value, {"requests", "responses", "finished", "loading_failed", "canceled", "from_disk_cache", "from_service_worker"})
    for key, enums in (
        ("errors", NETWORK_ERRORS | {"other"}),
        ("mime_classes", {"json", "html", "text", "empty", "other"}),
        ("protocols", {"h2", "h3", "http/1.1", "http/1.0", "other"}),
        ("server_families", {"AkamaiGHost", "AkamaiNetStorage", "cloudflare", "nginx", "Apache", "Microsoft-IIS", "other"}),
    ):
        raw = value.get(key)
        if isinstance(raw, list):
            result[key] = sorted({_enum(item, enums) for item in raw[:MAX_ITEMS]})
    statuses = _mapping(value.get("status_counts"))
    result["status_counts"] = {str(code): number for status, count in list(statuses.items())[:600]
                               if (code := _integer(status, 599)) is not None and (number := _integer(count)) is not None}
    retries = value.get("retry_after_seconds")
    if isinstance(retries, list):
        result["retry_after_seconds"] = [number for item in retries[:MAX_ITEMS] if (number := _integer(item, 86400)) is not None]
    return result


def _network_projection(value):
    value = _mapping(value)
    result = _numbers(value, {"document_requests_observed", "messages_seen", "messages_truncated"})
    result["methods"] = {method: _method_network(_mapping(value.get("methods")).get(method)) for method in sorted(METHODS)}
    return result


def network_summary(messages, url, method):
    """Project captured CDP messages; never issue a browser command or emit IDs."""
    expected = urlsplit(url)
    requested_page = parse_qs(expected.query).get("page") if method == "GET" else None
    selected, responses = {}, {}
    messages = messages if isinstance(messages, list) else []
    bounded = messages[:20000]
    documents = 0
    for message in bounded:
        message = _mapping(message)
        params = _mapping(message.get("params"))
        if message.get("method") != "Network.requestWillBeSent":
            continue
        documents += params.get("type") == "Document"
        request = _mapping(params.get("request"))
        try:
            parsed = urlsplit(request.get("url", ""))
            if (parsed.hostname, parsed.path.rstrip("/")) != (expected.hostname, expected.path.rstrip("/")):
                continue
            if requested_page is not None and parse_qs(parsed.query).get("page") != requested_page:
                continue
        except (TypeError, ValueError, AttributeError):
            continue
        req_id, req_method = params.get("requestId"), request.get("method")
        if isinstance(req_id, str) and isinstance(req_method, str) and req_method in METHODS:
            selected[req_id] = req_method
    summaries = {item: {"requests": sum(value == item for value in selected.values()), "responses": 0,
                       "finished": 0, "loading_failed": 0, "canceled": 0, "from_disk_cache": 0,
                       "from_service_worker": 0, "status_counts": Counter(), "errors": [],
                       "mime_classes": [], "protocols": [], "server_families": [], "retry_after_seconds": []}
                 for item in METHODS}
    for message in bounded:
        message = _mapping(message)
        params = _mapping(message.get("params"))
        req_id = params.get("requestId")
        if not isinstance(req_id, str) or req_id not in selected:
            continue
        target = summaries[selected[req_id]]
        kind = message.get("method")
        if kind == "Network.loadingFinished":
            target["finished"] += 1
        elif kind == "Network.loadingFailed":
            target["loading_failed"] += 1
            target["canceled"] += params.get("canceled") is True
            target["errors"].append(_enum(params.get("errorText"), NETWORK_ERRORS))
        elif kind in {"Network.responseReceived", "Network.responseReceivedExtraInfo"}:
            response = _mapping(params.get("response")) if kind == "Network.responseReceived" else params
            status_code = _status(response.get("status", response.get("statusCode")))
            if status_code is not None:
                responses[req_id] = status_code
            target["from_disk_cache"] += response.get("fromDiskCache") is True
            target["from_service_worker"] += response.get("fromServiceWorker") is True
            mime = response.get("mimeType")
            if isinstance(mime, str):
                mime = mime.lower().split(";", 1)[0].strip()
                target["mime_classes"].append("json" if mime in {"application/json", "text/json", "application/problem+json"}
                                              else "html" if mime == "text/html" else "text" if mime == "text/plain"
                                              else "empty" if not mime else "other")
            if "protocol" in response:
                target["protocols"].append(_enum(response["protocol"], {"h2", "h3", "http/1.1", "http/1.0"}))
            headers = _mapping(response.get("headers"))
            # Only these two named metadata headers are considered; never copy a header map.
            for spelling in ("retry-after", "Retry-After"):
                retry = _integer(headers.get(spelling), 86400)
                if retry is not None:
                    target["retry_after_seconds"].append(retry)
            for spelling in ("server", "Server"):
                if spelling in headers:
                    target["server_families"].append(_enum(headers[spelling],
                        {"AkamaiGHost", "AkamaiNetStorage", "cloudflare", "nginx", "Apache", "Microsoft-IIS"}))
    for req_id, status_code in responses.items():
        target = summaries[selected[req_id]]
        target["responses"] += 1
        target["status_counts"][str(status_code)] += 1
    return _network_projection({"methods": summaries, "document_requests_observed": documents,
                                "messages_seen": len(bounded), "messages_truncated": max(0, len(messages) - len(bounded))})


def _products_projection(value):
    value = _mapping(value)
    result = _numbers(value, {"returned", "inspected", "sponsored", "relevant", "filtered", "predicate_errors", "items_truncated"})
    result["items"] = []
    items = value.get("items")
    for item in items[:MAX_ITEMS] if isinstance(items, list) else []:
        item = _mapping(item)
        safe = _flags(item, {"sponsored", "relevant", "has_is_sponsored", "has_sponsored", "has_ads", "has_advertising", "has_advertisement",
                             "has_advertising_events", "has_advertasing_events", "tag_name_sponsored"})
        for key in ("index", "product_id", "sku_id", "seller_id"):
            number = _integer(item.get(key), 2**53 - 1)
            if number is not None:
                safe[key] = number
        result["items"].append(safe)
    return result


def product_summary(data, relevance_predicate, sponsored_predicate):
    products = _mapping(data).get("products")
    if not isinstance(products, list):
        products = []
    result = {"returned": len(products), "inspected": min(MAX_ITEMS, len(products)), "sponsored": 0,
              "relevant": 0, "filtered": 0, "predicate_errors": 0,
              "items_truncated": max(0, len(products) - MAX_ITEMS), "items": []}
    for index, product in enumerate(products[:MAX_ITEMS], 1):
        product = _mapping(product)
        item = {"index": index}
        for key, aliases in (("product_id", ("id", "idProduto")), ("sku_id", ("sku", "idSku")), ("seller_id", ("lojista", "sellerId"))):
            for alias in aliases:
                number = _integer(product.get(alias), 2**53 - 1, 1)
                if number is not None:
                    item[key] = number
                    break
        for key, source in (("has_is_sponsored", "isSponsored"), ("has_sponsored", "sponsored"),
                            ("has_ads", "ads"), ("has_advertising", "advertising"), ("has_advertisement", "advertisement"),
                            ("has_advertising_events", "advertisingEvents"), ("has_advertasing_events", "advertasingEvents")):
            item[key] = source in product
        tag_name = product.get("tagName")
        item["tag_name_sponsored"] = isinstance(tag_name, str) and bool(re.search(r"patrocinado|sponsored", tag_name, re.I))
        for key, predicate in (("relevant", relevance_predicate), ("sponsored", sponsored_predicate)):
            try:
                item[key] = bool(predicate(product))
                result[key] += item[key]
                if key == "relevant":
                    result["filtered"] += not item[key]
            except Exception:
                result["predicate_errors"] += 1
        result["items"].append(item)
    return _products_projection(result)


DIAGNOSTIC_NUMBERS = {
    "browser_prepare_seconds", "navigation_seconds", "evidence_wait_seconds", "page_load_timeout_seconds",
    "evidence_wait_limit_seconds", "fetch_seconds_before_diagnostic", "ready_state_probe_seconds",
    "selected_document_response_count", "current_price_response_count", "performance_event_count",
    "attempt", "status_code", "elapsed_seconds", "interval_wait_seconds", "products", "rows", "parsed_rows",
    "price_response_count", "bootstrap_navigation_count", "source_sku_aliases_added", "reported_page",
    "chrome_major", "document_navigations", "parser_sku_aliases_added",
    "navigation_attempt", "trigger_status_code",
    "price_pending_products", "seller_pending_products", "price_count",
}
DIAGNOSTIC_FLAGS = {
    "navigation_raised", "navigation_timeout", "new_loader_observed", "selected_document_response_seen",
    "selected_document_loading_finished", "selected_document_loading_failed", "selected_document_canceled",
    "selected_document_cached", "selected_document_service_worker", "bootstrap_failure_reused",
    "new_navigation", "new_api_request", "identity_checked", "document_completed",
    "response_queries_is_mapping", "response_page_present", "parser_page_context_injected", "response_sort_present",
    "parser_sort_context_injected",
    "fallback_used", "browser_reused", "recovery_pending",
}


def _trace_projection(value, depth=0):
    items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    result = []
    for item in items[:20]:
        item = _mapping(item)
        safe = _numbers(item, DIAGNOSTIC_NUMBERS)
        safe.update(_flags(item, DIAGNOSTIC_FLAGS))
        for key, values in (("method", {"uc_api", "browser_ssr", "uc_api+browser_ssr", "api_partner", "rest_ssr_hybrid",
                                         "uc_api_url_first", "uc_api_url_first+browser_ssr", "api_partner_url_first"}),
                            ("stage", {"bootstrap", "search", "price", "validation", "complete", "ssr_fallback"}),
                            ("ready_state", {"loading", "interactive", "complete", "unavailable", "not_probed"}),
                            ("document_network_error", NETWORK_ERRORS | {"none", "other"}),
                            ("page_evidence_source", {"response_and_observed_request", "observed_request_only"}),
                            ("sort_evidence_source", {"response_and_observed_request", "observed_request_only", "not_requested"})):
            if key in item:
                safe[key] = _enum(item[key], values)
        for key in ("error", "price_error"):
            if key in item:
                safe[key] = safe_error(item[key])
        for key in ("search", "price"):
            if key in item:
                safe[key] = api_summary(item[key])
        for key in ("browser_diagnostics", "diagnostics"):
            if depth < 2 and isinstance(item.get(key), dict):
                safe[key] = _trace_projection([item[key]], depth + 1)[0]
        if depth < 2 and isinstance(item.get("inner_attempts"), list):
            safe["inner_attempts"] = _trace_projection(item["inner_attempts"], depth + 1)
        result.append(safe)
    return result


EVENTS = {"run_start", "page_start", "api_start", "network", "api_end", "page_end", "run_end"}


def project_event(event, payload):
    if not isinstance(event, str) or event not in EVENTS:
        return None
    payload = _mapping(payload)
    safe = {}
    if event == "run_start":
        safe["product_line"] = _enum(payload.get("product_line"), {"TV", "REF", "LDY"})
        safe["run_id"] = _enum(payload.get("run_id"), {"main", "bsr"})
        # Keep the legacy numeric modes in reports; the new branch is an exact string.
        raw_mode = payload.get("mode")
        mode = "1-1" if raw_mode == "1-1" else _integer(raw_mode, 4, 1)
        if mode is not None:
            safe["mode"] = mode
        safe.update(_flags(payload, {"browser_session_reused", "bootstrap_previously_completed"}))
        for key in ("pages", "configured_rest_page_size", "effective_mode3_page_size", "effective_mode4_page_size",
                    "api_timeout_seconds", "min_search_interval_seconds", "attempt_limit"):
            number = _integer(payload.get(key), 1000 if key == "pages" else 10000)
            if number is not None:
                safe[key] = number
        version = payload.get("python_version")
        if isinstance(version, str) and re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", version):
            safe["python_version"] = version
        revision = payload.get("code_revision")
        if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision):
            safe["code_revision"] = revision
    if event in {"page_start", "api_start", "network", "api_end", "page_end"}:
        page = _integer(payload.get("page"), 1000, 1)
        if page is not None:
            safe["page"] = page
    if event in {"api_start", "network", "api_end"}:
        safe["method"] = _enum(payload.get("method"), METHODS)
        call_number = _integer(payload.get("call_number"))
        if call_number is not None:
            safe["call_number"] = call_number
    if event == "api_start":
        safe["request"] = request_summary(payload.get("request"))
        safe.update(_numbers(payload, {"elapsed_since_previous_call_seconds", "session_elapsed_seconds"}))
    elif event == "network":
        safe["network"] = _network_projection(payload.get("network"))
    elif event == "api_end":
        safe.update(_numbers(payload, {"elapsed_seconds"}))
        safe["result"] = api_summary(payload.get("result"))
        if "products" in payload:
            safe["products"] = _products_projection(payload["products"])
    elif event == "page_end":
        safe.update(_numbers(payload, {"products", "rows", "status_code", "chrome_major", "new_api_calls_in_page", "filtered_unique_count", "ssr_navigation_attempts"}))
        safe.update(_flags(payload, {"success", "bootstrap_failure_reused", "fallback_used", "browser_reused", "recovery_pending"}))
        if "actual_method" in payload:
            safe["actual_method"] = _enum(payload["actual_method"], {"uc_api", "browser_ssr", "uc_api+browser_ssr", "api_partner", "rest_ssr_hybrid",
                                                                    "uc_api_url_first", "uc_api_url_first+browser_ssr", "api_partner_url_first"})
        safe["error"] = safe_error(payload.get("error"))
        safe["trace"] = _trace_projection(payload.get("trace"))
    elif event == "run_end":
        safe["outcome"] = _enum(payload.get("outcome"), {"completed", "partial_failed", "accepted_with_failures", "interrupted", "tool_error"})
        safe.update(_numbers(payload, {"requested_pages", "completed_pages", "failed_pages", "attempted_pages", "diagnostic_errors",
                                        "filtered_unique_count", "required_unique"}))
        safe.update(_flags(payload, {"close_failed", "downstream_allowed", "coverage_complete", "accepted_with_failures"}))
        failed_page_numbers = payload.get("failed_page_numbers")
        if isinstance(failed_page_numbers, list):
            safe["failed_page_numbers"] = sorted({number for item in failed_page_numbers[:1000]
                                                   if (number := _integer(item, 1000, 1)) is not None})
        if "tool_error_type" in payload:
            safe["tool_error_type"] = _enum(payload["tool_error_type"], EXCEPTIONS | {"none"})
    return safe


def _check_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("linked_path_rejected")
        if part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("linked_path_rejected")
    return path


def _record_metadata(record, safe):
    elapsed = _number(record.get("elapsed_seconds"))
    if elapsed is not None:
        safe["elapsed_seconds"] = elapsed
    utc = record.get("utc")
    if isinstance(utc, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", utc):
        try:
            datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")
            safe["utc"] = utc
        except ValueError:
            pass
    return safe


class ReportWriter:
    def __init__(self, output_dir):
        self.output_dir = _check_path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._file = (self.output_dir / "events.jsonl").open("x", encoding="utf-8", newline="\n")
        self._started = time.monotonic()

    def emit(self, event, payload):
        safe = project_event(event, payload)
        if safe is None:
            return
        record = {"event": event, "payload": safe}
        record["elapsed_seconds"] = round(time.monotonic() - self._started, 6)
        record["utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._file.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
        self._file.flush()

    def finish(self, *, archive_dir=None):
        if not self._file.closed:
            self._file.close()
        return build_report(self.output_dir, archive_dir=archive_dir)

    def close(self):
        if not self._file.closed:
            self._file.close()


def _read_events(output_dir):
    path = _check_path(Path(output_dir) / "events.jsonl")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES or info.st_nlink != 1:
        raise ValueError("events_file_rejected")
    records, ignored, partial = [], 0, False
    with path.open("rb") as stream:
        for index in range(MAX_EVENTS + 1):
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES:
                raise ValueError("events_line_too_large")
            if index >= MAX_EVENTS:
                raise ValueError("too_many_events")
            try:
                value = json.loads(line)
                value = _mapping(value)
                safe = project_event(value.get("event"), value.get("payload"))
                if safe is not None:
                    records.append(_record_metadata(value, {"event": value["event"], "payload": safe}))
                else:
                    ignored += 1
            except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
                ignored += 1
                if not line.endswith(b"\n"):
                    partial = True
    return records, ignored, partial


def build_report(output_dir, *, archive_dir=None):
    """Read only events.jsonl, sanitize again, and ZIP only in-memory projections."""
    output_dir = _check_path(output_dir)
    records, ignored, partial = _read_events(output_dir)
    starts = [r["payload"] for r in records if r["event"] == "run_start"]
    pages = [r["payload"] for r in records if r["event"] == "page_end"]
    calls = [dict(r["payload"], session_elapsed_seconds=r.get("elapsed_seconds", 0)) for r in records if r["event"] == "api_end"]
    endings = [r["payload"] for r in records if r["event"] == "run_end"]
    first_403 = next(({key: call.get(key) for key in ("page", "call_number", "method", "session_elapsed_seconds")}
                      for call in calls if call.get("result", {}).get("status_code") == 403), None)
    report = {"schema_version": 1, "scope": "automatic_listing_run", "run": starts[-1] if starts else {},
              "outcome": endings[-1] if endings else {"outcome": "interrupted"},
              "page_outcomes": pages, "api_timeline": calls, "first_api_403": first_403,
              "event_count": len(records), "ignored_lines": ignored, "partial_tail": partial}
    effective_mode = 4 if report["run"].get("mode") == 4 else 3
    if report["run"].get("mode") != "1-1":
        effective_page_size = report["run"].get(f"effective_mode{effective_mode}_page_size", "not recorded")
        effective_page_note = (
            f"The configured REST value and the effective mode {effective_mode} value may differ; this tool does not change either.")
    else:
        effective_mode = "1-1"
        effective_page_size = "not independently recorded (REST configured size is shown above)"
        effective_page_note = "This REST listing run has no browser-observed API page size."
    lines = ["# Casas Bahia automatic listing diagnostic report", "",
             "Evidence from the normal listing run; this report does not cover detail collection.",
             "No separate diagnostic requests are issued. Missing API detail does not establish that no traffic occurred.",
             "403 proves an HTTP denial, not its underlying IP, session, rate or browser cause.",
             "GET/POST results and OPTIONS preflight evidence are recorded separately in events.jsonl.",
             "Sponsorship counts do not prove API pagination or offset semantics.", "",
             f"Outcome: {report['outcome'].get('outcome', 'interrupted')}",
             f"Listing mode: {report['run'].get('mode', 'not recorded')}",
             f"Existing browser reused at run start: {report['run'].get('browser_session_reused', 'not recorded')}",
             f"Bootstrap already validated at run start: {report['run'].get('bootstrap_previously_completed', 'not recorded')}",
             f"Configured REST page size: {report['run'].get('configured_rest_page_size', 'not recorded')}",
             f"Effective mode {effective_mode} page size: {effective_page_size}",
             effective_page_note,
             f"Filtered unique products: {report['outcome'].get('filtered_unique_count', 'not recorded')}",
             f"Required unique products: {report['outcome'].get('required_unique', 'not recorded')}",
             f"Coverage complete: {report['outcome'].get('coverage_complete', 'not recorded')}",
             f"Downstream allowed: {report['outcome'].get('downstream_allowed', 'not recorded')}",
             f"Accepted with failed pages: {report['outcome'].get('accepted_with_failures', 'not recorded')}",
             f"Failed page numbers: {report['outcome'].get('failed_page_numbers', [])}",
             "A passed minimum-count policy is not proof that every requested page succeeded.",
             f"Re-sanitized events: {len(records)}; ignored lines: {ignored}; partial final line: {partial}", "",
             "## Page outcomes", "", "| Page | Success | Status | Method | SSR fallback | SSR navigations | Rows | Filtered unique | New API calls | Bootstrap failure reused | Error |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for page in pages:
        lines.append(f"| {page.get('page', 0)} | {page.get('success', False)} | {page.get('status_code', 0)} | {page.get('actual_method', 'not recorded')} | {page.get('fallback_used', False)} | {page.get('ssr_navigation_attempts', 'not recorded')} | {page.get('rows', 0)} | {page.get('filtered_unique_count', 'not recorded')} | {page.get('new_api_calls_in_page', 'not recorded')} | {page.get('bootstrap_failure_reused', False)} | {page.get('error', 'none')} |")
    lines.extend(["", "## API timeline", "", "| Call | Page | Method | Status | Seconds | Returned | Sponsored | Filtered |", "| --- | --- | --- | --- | --- | --- | --- | --- |"])
    for call in calls:
        products = call.get("products", {})
        lines.append(f"| {call.get('call_number', 0)} | {call.get('page', 0)} | {call.get('method', 'other')} | {call.get('result', {}).get('status_code', 0)} | {call.get('elapsed_seconds', 0)} | {products.get('returned', 0)} | {products.get('sponsored', 0)} | {products.get('filtered', 0)} |")
    if first_403:
        lines.extend(["", f"First API 403: call {first_403['call_number']}, page {first_403['page']}, method {first_403['method']}, session elapsed {first_403['session_elapsed_seconds']} seconds."])
    markdown = "\n".join(lines) + "\n"
    report_json = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
    safe_events = "".join(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n" for record in records)
    for index in range(10000):
        suffix = "" if index == 0 else f"_{index}"
        names = (f"REPORT{suffix}.md", f"report{suffix}.json", f"share{suffix}.zip")
        if not any((output_dir / name).exists() for name in names):
            break
    else:
        raise ValueError("too_many_reports")
    with (output_dir / names[0]).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(markdown)
    with (output_dir / names[1]).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(report_json)
    archive_path = output_dir / names[2]
    if archive_dir is not None:
        archive_dir = _check_path(archive_dir)
        archive_dir.mkdir(parents=True, exist_ok=True)
        product_line = _enum(report["run"].get("product_line"), {"TV", "REF", "LDY"})
        run_id = _enum(report["run"].get("run_id"), {"main", "bsr"})
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = archive_dir / f"casas_listing_{product_line}_{run_id}_{stamp}_{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(archive_path, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("REPORT.md", markdown)
        archive.writestr("report.json", report_json)
        archive.writestr("events.jsonl", safe_events)
    return {"report_md": str(output_dir / names[0]), "report_json": str(output_dir / names[1]), "share_zip": str(archive_path)}
