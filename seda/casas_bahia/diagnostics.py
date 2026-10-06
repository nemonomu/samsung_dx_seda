"""Immediate, allowlisted timing events; never store request/response contents."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_context = contextvars.ContextVar('casas_timing_context', default={})
_lock = threading.Lock()
_warned = False
_TEXT_VALUES = {
    'reason': {'completed', 'returned_failure', 'exception', 'timeout', 'cancelled',
               'out_of_stock', 'http_404', 'http_error', 'calculation_error',
               'empty_delivery', 'invalid_json', 'body_read_error', 'budget_exceeded',
               'retry_timeout', 'transient_http', 'retry_exception', 'retry_wait',
               'skipped_404', 'missing_url', 'stage_failed', 'blocked_response',
               'identity_mismatch', 'invalid_payload', 'configuration_error', 'incomplete_fields'},
    'product_line': {'tv', 'ref', 'ldy'},
    'profile': {'premium_html', 'pdp_js_full'},
    'transport': {'requests', 'curl_cffi', 'zenrows', 'direct'},
    'exception_type': {'TimeoutError', 'ReadTimeout', 'ConnectTimeout', 'ConnectionError',
                       'HTTPError', 'JSONDecodeError', 'ValueError', 'TypeError',
                       'OSError', 'RuntimeError', 'Error', 'CancelledError'},
}
_NUMBERS = {'status_code', 'attempt', 'page', 'row_index', 'wait_seconds', 'timeout_ms',
            'elapsed_ms', 'http_elapsed_ms', 'batch_size', 'exit_code'}
_IDS = {'sku_id', 'seller_id', 'product_id'}


def enabled():
    return os.getenv('SEDA_CASAS_BAHIA_TIMING', '1').lower() not in {'0', 'false', 'off', 'no'}


def _fields(values):
    result = {}
    for key, value in values.items():
        if key in _IDS and re.fullmatch(r'\d{1,20}', str(value or '')):
            result[key] = str(value)
        elif key in _NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            result[key] = value
        elif key in {'success', 'skipped'} and isinstance(value, bool):
            result[key] = value
        elif key in _TEXT_VALUES:
            result[key] = value if isinstance(value, str) and value in _TEXT_VALUES[key] else 'other'
        elif key in {'http_started_at_utc', 'http_finished_at_utc'} and isinstance(value, str):
            try:
                result[key] = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    return result


def _write(event, root=None):
    global _warned
    if not enabled():
        return
    try:
        if root is None:
            from seda.step00_config import run_root
            root = run_root()
        directory = Path(root) / 'status' / 'casas_bahia_timing'
        directory.mkdir(parents=True, exist_ok=True)
        # Each process has its own file; threads share a lock. Flush every event.
        with _lock, (directory / f'timing_{os.getpid()}.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=True) + '\n')
            handle.flush()
    except Exception:
        if not _warned:
            _warned = True
            print('[casas_timing] timing write failed; collection continues', flush=True)


class Span:
    def __init__(self, operation, root=None, **fields):
        # Operation is supplied by code, never derived from a URL or exception.
        self.operation = operation if re.fullmatch(r'[a-z][a-z0-9_]{0,79}', operation) else 'other'
        self.root = root
        self.values = _fields(fields)
        self.request_id = uuid.uuid4().hex

    def __enter__(self):
        self.started = time.perf_counter()
        parent = _context.get()
        self.parent_id = parent.get('request_id')
        self.identity = {**_fields(parent), **self.values}
        self.token = _context.set({**self.identity, 'request_id': self.request_id})
        self.emit('start')
        return self

    def emit(self, event):
        payload = {
            'schema': 1, 'event': event, 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
            'operation': self.operation, 'request_id': self.request_id,
            'parent_request_id': self.parent_id, 'pid': os.getpid(),
            **self.identity, **self.values,
        }
        line = os.getenv('SEDA_PRODUCT_LINE', '').lower()
        if line in {'tv', 'ref', 'ldy'} and 'product_line' not in payload:
            payload['product_line'] = line
        _write(payload, self.root)

    def update(self, **fields):
        self.values.update(_fields(fields))

    def observe(self, result):
        if isinstance(result, dict):
            self.update(status_code=result.get('status_code', result.get('status')),
                        success=result.get('success', result.get('ok')),
                        skipped=result.get('skipped', False))
            if 'reason' in result:
                self.update(reason=result['reason'])
            elif result.get('success', result.get('ok')) is False:
                self.update(reason='returned_failure')
            error = result.get('error')
            if isinstance(error, str) and result.get('success', result.get('ok')) is False:
                lowered = error.lower()
                reason = None
                for marker, classification in (
                    ('json_decode', 'invalid_json'), ('identity', 'identity_mismatch'),
                    ('blocked', 'blocked_response'), ('key_missing', 'configuration_error'),
                    ('disabled', 'configuration_error'), ('fields_incomplete', 'incomplete_fields'),
                    ('request_timeout', 'timeout'),
                ):
                    if marker in lowered:
                        reason = classification
                        break
                if reason:
                    self.update(reason=reason)
        else:
            self.update(status_code=getattr(result, 'status_code', None),
                        success=getattr(result, 'success', None))
        # A HTTP error can have no explicit success field (requests.Response).
        code = self.values.get('status_code')
        if isinstance(code, int) and code >= 400:
            self.update(success=False, reason='http_404' if code == 404 else 'http_error')

    def __exit__(self, kind, error, traceback):
        try:
            self.update(elapsed_ms=round((time.perf_counter() - self.started) * 1000, 3))
            if kind:
                reason = 'cancelled' if issubclass(kind, asyncio.CancelledError) else (
                    'timeout' if 'timeout' in kind.__name__.lower() else 'exception')
                self.update(reason=reason, exception_type=kind.__name__, success=False)
            elif 'reason' not in self.values:
                self.update(reason='completed')
            self.emit('finish')
        finally:
            _context.reset(self.token)
        return False


def _identity(function, args, kwargs):
    try:
        bound = inspect.signature(function).bind_partial(*args, **kwargs).arguments
        fields = {key: bound[key] for key in _IDS | {'page', 'attempt', 'transport'} if key in bound}
        target = bound.get('target')
        if isinstance(target, dict):
            fields.update({key: target.get(key) for key in _IDS | {'row_index'}})
        url = bound.get('product_url', bound.get('url', ''))
        # Only a numeric product path component is retained, never a URL/query.
        match = re.search(r'/p/(\d+)(?:[/?#]|$)', str(url or ''))
        if match:
            fields.setdefault('sku_id', match.group(1))
        return _fields(fields)
    except Exception:
        return {}


def trace(operation):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                if not enabled():
                    return await function(*args, **kwargs)
                with Span(operation, **_identity(function, args, kwargs)) as span:
                    result = await function(*args, **kwargs)
                    span.observe(result)
                    return result
            return asynchronous
        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            if not enabled():
                return function(*args, **kwargs)
            with Span(operation, **_identity(function, args, kwargs)) as span:
                result = function(*args, **kwargs)
                span.observe(result)
                return result
        return synchronous
    return decorate


def timed_request(operation, do_request, *args, **kwargs):
    attempt = kwargs.pop('_timing_attempt', None)
    if not enabled():
        return do_request(*args, **kwargs)
    fields = {'profile': kwargs['profile']} if 'profile' in kwargs else {}
    if attempt is not None:
        fields['attempt'] = attempt
    with Span(operation, **fields) as span:
        result = do_request(*args, **kwargs)
        span.observe(result)
        return result


def timed_sleep(seconds):
    if not enabled():
        return time.sleep(seconds)
    with Span('retry_wait', reason='retry_wait', wait_seconds=seconds):
        return time.sleep(seconds)


@contextmanager
def stage_span(module_name, env):
    # Applied only by the Casas runner; other retailers keep their existing path.
    from seda.step00_config import run_root
    root = env.get('SEDA_RUN_ROOT') or run_root()
    operation = module_name.rsplit('.', 1)[-1]
    with Span(operation, root=root, product_line=env.get('SEDA_PRODUCT_LINE', '').lower()) as span:
        yield span
