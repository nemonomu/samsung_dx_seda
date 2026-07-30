"""Run-scoped accounting for actual ZenRows HTTP request attempts."""

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


EXECUTION_ID_ENV = "SEDA_ZENROWS_USAGE_EXECUTION_ID"
REQUIRED_ENV = "SEDA_ZENROWS_USAGE_REQUIRED"
_SAFE_EXECUTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PROCESS_SHARD = f"{os.getpid()}_{uuid.uuid4().hex}.jsonl"
_WRITE_LOCK = threading.Lock()
_TRUE_VALUES = {"1", "true", "yes", "y"}


def start_execution():
    """Start an isolated accounting scope for one orchestrator invocation."""
    execution_id = uuid.uuid4().hex
    os.environ[EXECUTION_ID_ENV] = execution_id
    os.environ[REQUIRED_ENV] = "1"
    return execution_id


def usage_required():
    return os.getenv(REQUIRED_ENV, "0").strip().lower() in _TRUE_VALUES


def record_http_request_attempt(method, profile, estimated_multiplier):
    """Persist one receipt before an actual requests.get/post call."""
    execution_id = _execution_id()
    if not execution_id:
        return False
    root_value = os.getenv("SEDA_RUN_ROOT", "").strip()
    if not root_value:
        raise RuntimeError("missing_run_root")
    retailer = _safe_segment(
        os.getenv("SEDA_ACTIVE_RETAILER")
        or os.getenv("SEDA_RETAILERS", "").split(",", 1)[0],
        "unknown",
    )
    line = _safe_segment(os.getenv("SEDA_PRODUCT_LINE", ""), "unknown")
    event = {
        "event": "zenrows_http_request_attempt",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "retailer": retailer,
        "product_line": line,
        "method": str(method or "GET").strip().upper()[:8],
        "profile": str(profile or "").strip()[:128],
        "estimated_multiplier": str(estimated_multiplier or "").strip()[:128],
    }
    shard = (
        Path(root_value)
        / "status"
        / "zenrows_usage"
        / execution_id
        / _PROCESS_SHARD
    )
    payload = json.dumps(
        event,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with _WRITE_LOCK:
        shard.parent.mkdir(parents=True, exist_ok=True)
        with shard.open("a", encoding="ascii", newline="\n") as handle:
            handle.write(payload + "\n")
    return True


def summarize_usage(root, execution_id=None):
    raw_execution_id = (
        execution_id
        if execution_id is not None
        else os.getenv(EXECUTION_ID_ENV, "")
    )
    raw_execution_id = str(raw_execution_id or "").strip()
    if not raw_execution_id:
        return _empty_summary(
            tracking_status="unavailable",
            error="execution_id_missing",
        )
    if not _SAFE_EXECUTION_ID.fullmatch(raw_execution_id):
        return _empty_summary(
            tracking_status="error",
            error="invalid_execution_id",
        )

    usage_root = (
        Path(root)
        / "status"
        / "zenrows_usage"
        / raw_execution_id
    )
    total = 0
    by_retailer = {}
    by_product_line = {}
    errors = []
    if usage_root.exists():
        try:
            shards = tuple(sorted(usage_root.glob("*.jsonl")))
        except OSError as exc:
            return _empty_summary(
                execution_id=raw_execution_id,
                tracking_status="error",
                error=f"ledger_list_error:{type(exc).__name__}",
            )
        for shard in shards:
            try:
                lines = shard.read_text(
                    encoding="ascii",
                    errors="strict",
                ).splitlines()
            except Exception as exc:
                errors.append(f"ledger_read_error:{type(exc).__name__}")
                continue
            for line in lines:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    errors.append("invalid_ledger_json")
                    continue
                if (
                    not isinstance(event, dict)
                    or event.get("event") != "zenrows_http_request_attempt"
                ):
                    errors.append("invalid_ledger_event")
                    continue
                total += 1
                retailer = _safe_segment(event.get("retailer"), "unknown")
                product = _safe_segment(event.get("product_line"), "unknown")
                by_retailer[retailer] = by_retailer.get(retailer, 0) + 1
                by_product_line[product] = by_product_line.get(product, 0) + 1

    return {
        "scope": "current_orchestrator_execution",
        "execution_id": raw_execution_id,
        "tracking_status": "error" if errors else "complete",
        "http_calls": total,
        "by_retailer": dict(sorted(by_retailer.items())),
        "by_product_line": dict(sorted(by_product_line.items())),
        "error": ",".join(sorted(set(errors))),
    }


def _execution_id():
    value = os.getenv(EXECUTION_ID_ENV, "").strip()
    if not value:
        return ""
    if not _SAFE_EXECUTION_ID.fullmatch(value):
        raise ValueError("invalid_execution_id")
    return value


def _safe_segment(value, fallback):
    value = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(value or "").strip().lower(),
    ).strip("_")
    return value[:64] or fallback


def _empty_summary(
    *,
    execution_id="",
    tracking_status,
    error,
):
    return {
        "scope": "current_orchestrator_execution",
        "execution_id": execution_id,
        "tracking_status": tracking_status,
        "http_calls": 0,
        "by_retailer": {},
        "by_product_line": {},
        "error": error,
    }
