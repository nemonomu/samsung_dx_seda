"""Passive, automatic diagnostics for the existing Casas listing process.

Never launches a collector, changes parameters/retries, redirects normal logs,
or makes additional network/browser requests. Only owned safe event files are
archived, never a log directory, raw response, configuration or Chrome profile.
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import uuid

from . import listing_diagnostic_evidence as evidence


_ACTIVE = None


def _notice(message):
    try:
        print(message, flush=True)
    except Exception:
        pass


def _discard(diagnostic):
    if diagnostic is None:
        return
    for cleanup in (
        diagnostic.observer.restore if diagnostic.observer else None,
        diagnostic.writer.close if diagnostic.writer else None,
    ):
        if cleanup is not None:
            try:
                cleanup()
            except BaseException:
                # Already handling an outcome: cleanup must not replace it.
                pass


class _Observer:
    def __init__(self, api, parsers, writer):
        self.api, self.parsers, self.writer = api, parsers, writer
        self.page = 0
        self.call_number = 0
        self.errors = 0
        self.previous_finished = None
        self.started = time.perf_counter()
        self.original_fetch = api._browser_fetch
        self.original_network = api._network_evidence

    def _observe(self, callback):
        try:
            callback()
        except Exception:
            self.errors += 1

    def install(self):
        self.api._browser_fetch = self._fetch
        self.api._network_evidence = self._network

    def restore(self):
        self.api._browser_fetch = self.original_fetch
        self.api._network_evidence = self.original_network

    def _network(self, messages, frame, url, method, body):
        result = self.original_network(messages, frame, url, method, body)
        self._observe(lambda: self.writer.emit("network", {
            "page": self.page, "call_number": self.call_number, "method": method,
            "network": evidence.network_summary(messages, url, method),
        }))
        return result

    def _fetch(self, driver, endpoint, params, method, headers, body, timeout, document_frame):
        self.call_number += 1
        started = time.perf_counter()
        self._observe(lambda: self.writer.emit("api_start", {
            "page": self.page, "call_number": self.call_number, "method": method,
            "request": evidence.request_summary(params),
            "elapsed_since_previous_call_seconds": (
                None if self.previous_finished is None else started - self.previous_finished),
            "session_elapsed_seconds": started - self.started,
        }))
        try:
            original = self.original_fetch(driver, endpoint, params, method, headers, body, timeout, document_frame)
        except BaseException as exc:
            finished = time.perf_counter()
            self.previous_finished = finished
            self._observe(lambda: self.writer.emit("api_end", {
                "page": self.page, "call_number": self.call_number, "method": method,
                "elapsed_seconds": finished - started,
                "result": {"status_code": 0, "error": "browser_api_" + type(exc).__name__},
            }))
            raise
        finished = time.perf_counter()
        self.previous_finished = finished

        def record():
            result, data = original
            payload = {"page": self.page, "call_number": self.call_number, "method": method,
                       "elapsed_seconds": finished - started, "result": evidence.api_summary(result)}
            if method == "GET" and isinstance(data, dict):
                payload["products"] = evidence.product_summary(
                    data, self.parsers._casas_bahia_is_relevant_product,
                    lambda product: self.parsers._casas_bahia_sku_status(product) == "Sponsored",
                )
            self.writer.emit("api_end", payload)

        self._observe(record)
        return original


def _trace(attempts):
    """Unwrap transport trace only; output is projected again by the writer."""
    result = []
    for attempt in attempts[:20] if isinstance(attempts, list) else []:
        if not isinstance(attempt, dict):
            continue
        inner = attempt.get("inner_attempts")
        result.extend(inner[:20] if isinstance(inner, list) else [attempt])
    return result[:40]


class AutomaticListingDiagnostics:
    def __init__(self, *, mode, run_id, pages, product_line, log_dir=None):
        self.mode, self.run_id, self.product_line = mode, run_id, product_line
        self.pages = pages
        self.observer = None
        self.writer = None
        self.errors = 0
        self.attempted = 0
        self.completed = 0
        self.failed_pages = []
        self.unique = 0
        self.calls_before = 0
        self.bootstrap_reused = False
        self.manifest = None
        self.exception_type = None
        self.close_failed = False
        self.finished = False
        self.log_dir = Path(log_dir) if log_dir is not None else Path(__file__).resolve().parent / "log"
        label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex
        self.directory = self.log_dir / "diagnostics" / label

    def start(self):
        from seda import parsers, step00_config as config
        from . import search_api

        self.writer = evidence.ReportWriter(self.directory)
        initial_page = self.pages[0] if self.pages else 1
        # Existing parameter construction is pure; it performs no API request.
        url = config.page_url(config.RETAILERS["casas_bahia"], initial_page, self.run_id)
        rest_params = evidence.request_summary(search_api._params(url))
        payload = {
            "product_line": self.product_line, "run_id": self.run_id, "pages": len(self.pages),
            "mode": int(self.mode), "python_version": ".".join(map(str, sys.version_info[:3])),
            "configured_rest_page_size": rest_params.get("resultsperpage"),
        }
        if self.mode == "3":
            from . import browser_api

            self.observer = _Observer(browser_api, parsers, self.writer)
            payload.update({"effective_mode3_page_size": 20,
                            "api_timeout_seconds": browser_api.API_TIMEOUT_SECONDS,
                            "min_search_interval_seconds": browser_api.MIN_SEARCH_INTERVAL_SECONDS,
                            "attempt_limit": browser_api._attempt_limit()})
            self.observer.install()
        self.writer.emit("run_start", payload)
        _notice(f"[seda] casas_bahia diagnostic_started mode={self.mode} product_line={self.product_line} "
                f"stage={self.run_id} automatic=true")

    def page_start(self, page):
        self.attempted += 1
        self.calls_before = self.observer.call_number if self.observer else 0
        self.bootstrap_reused = False
        if self.observer:
            self.observer.page = page
            session = self.observer.api._SESSION
            self.bootstrap_reused = bool(session and session.bootstrap_attempted and session.bootstrap_error)
        self.writer.emit("page_start", {"page": page})

    def page_end(self, page, success, rows, unique, error, attempts):
        if success:
            self.completed += 1
            self.unique = unique
        else:
            self.failed_pages.append(page)
        trace = _trace(attempts)
        status = next((entry.get("status_code") for entry in reversed(trace)
                       if isinstance(entry, dict) and entry.get("status_code")), None)
        payload = {
            "page": page, "success": success, "rows": rows, "filtered_unique_count": self.unique,
            "error": error, "trace": trace,
        }
        if status is not None:
            payload["status_code"] = status
        if self.observer:
            payload["new_api_calls_in_page"] = self.observer.call_number - self.calls_before
            payload["bootstrap_failure_reused"] = self.bootstrap_reused
        self.writer.emit("page_end", payload)

    def manifest_ready(self, manifest):
        # Keep only facts needed at finish, never retain the URL-bearing manifest.
        self.manifest = {
            "coverage_complete": manifest.get("complete") is True,
            "downstream_allowed": manifest.get("downstream_allowed", manifest.get("complete")) is True,
            "accepted_with_failures": manifest.get("accepted_with_failures") is True,
            "filtered_unique_count": manifest.get("filtered_unique_count", self.unique),
            "required_unique": manifest.get("minimum_unique_required"),
        }

    def finish(self):
        if self.finished:
            return
        self.finished = True
        if self.observer:
            self.observer.restore()
        if self.writer is None:
            return
        policy = self.manifest or {}
        if self.exception_type == "KeyboardInterrupt":
            outcome = "interrupted"
        elif self.exception_type and (self.exception_type != "SystemExit" or policy.get("downstream_allowed")):
            outcome = "tool_error"
        elif policy.get("accepted_with_failures"):
            outcome = "accepted_with_failures"
        elif policy.get("coverage_complete") and not self.exception_type:
            outcome = "completed"
        elif self.failed_pages or self.manifest is not None:
            outcome = "partial_failed"
        else:
            outcome = "tool_error"
        self.writer.emit("run_end", {
            "outcome": outcome, "requested_pages": len(self.pages), "attempted_pages": self.attempted,
            "completed_pages": self.completed, "failed_pages": len(self.failed_pages),
            "failed_page_numbers": self.failed_pages, "filtered_unique_count": self.unique,
            "diagnostic_errors": self.errors + (self.observer.errors if self.observer else 0),
            "close_failed": self.close_failed, "tool_error_type": self.exception_type or "none", **policy,
        })
        paths = self.writer.finish(archive_dir=self.log_dir)
        _notice(f"[seda] casas_bahia diagnostic_zip={paths['share_zip']}")


def start_diagnostics(mode, run_id, pages, product_line):
    global _ACTIVE
    if _ACTIVE is not None:
        return _ACTIVE
    diagnostic = AutomaticListingDiagnostics(mode=mode, run_id=run_id, pages=pages, product_line=product_line)
    try:
        diagnostic.start()
    except BaseException as exc:
        _discard(diagnostic)
        if not isinstance(exc, Exception):
            raise
        _notice("[seda] casas_bahia diagnostic_start_failed collection_unchanged=true")
        return None
    _ACTIVE = diagnostic
    return diagnostic


def record_event(name, *args):
    if _ACTIVE is None:
        return
    try:
        if name == "page_start":
            _ACTIVE.page_start(*args)
        elif name == "page_end":
            _ACTIVE.page_end(*args)
        elif name == "manifest_ready":
            _ACTIVE.manifest_ready(*args)
    except Exception:
        _ACTIVE.errors += 1


def finish_diagnostics(diagnostic, exception_type=None, close_failed=False):
    global _ACTIVE
    if diagnostic is None:
        return
    try:
        diagnostic.exception_type = exception_type
        diagnostic.close_failed = close_failed
        diagnostic.finish()
    except Exception:
        _discard(diagnostic)
        _notice("[seda] casas_bahia diagnostic_zip_failed collection_unchanged=true")
    finally:
        if _ACTIVE is diagnostic:
            _ACTIVE = None
