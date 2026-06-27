"""Shared retry / throttle helpers for Casas Bahia API calls.

Casas Bahia's ``pdp-api`` and the viavarejo pickup endpoint rate-limit
(HTTP 429) when hit back-to-back, and occasionally drop the TLS connection
mid-handshake (``SSLEOFError``). These helpers add polite per-host throttling
plus exponential-backoff retries so the enrichment pass stays stable without
falling back to ZenRows (which costs credits).

Tunable via env (all optional):
    SEDA_CASAS_BAHIA_MIN_INTERVAL_SECONDS   per-host min spacing (default 1.0)
    SEDA_CASAS_BAHIA_API_RETRIES            retries per call (default 3)
    SEDA_CASAS_BAHIA_BACKOFF_SECONDS        backoff base seconds (default 2)
    SEDA_CASAS_BAHIA_BACKOFF_MAX_SECONDS    backoff cap seconds (default 30)
"""

import os
import random
import time


# Per-host timestamp of the last issued request, used by ``throttle``.
_HOST_LAST_CALL = {}

# Statuses worth retrying: rate limit + transient server-side failures.
RETRYABLE_STATUS = (429, 500, 502, 503, 504)


def throttle(host="casas_bahia"):
    """Sleep so consecutive calls to ``host`` keep a minimum spacing."""
    try:
        min_interval = float(os.getenv("SEDA_CASAS_BAHIA_MIN_INTERVAL_SECONDS", "1.0"))
    except ValueError:
        min_interval = 0.0
    if min_interval <= 0:
        return
    elapsed = time.monotonic() - _HOST_LAST_CALL.get(host, 0.0)
    wait = min_interval - elapsed
    if wait > 0:
        time.sleep(wait + random.uniform(0, min_interval * 0.3))
    _HOST_LAST_CALL[host] = time.monotonic()


def is_retryable_exc(exc):
    """True for transient transport failures (SSL/connection/timeout)."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if any(token in name for token in ("ssl", "connection", "timeout", "chunked", "proxyerror")):
        return True
    return any(
        token in text
        for token in ("ssl", "eof", "connection reset", "timed out", "max retries", "connection aborted")
    )


def retry_after_seconds(response):
    """Parse a numeric ``Retry-After`` header, if the server sent one."""
    value = (getattr(response, "headers", {}) or {}).get("Retry-After", "")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def sleep_backoff(attempt, base=None, retry_after=None, cap=None):
    """Exponential backoff with jitter, honoring ``Retry-After`` when given."""
    try:
        base = float(base if base is not None else os.getenv("SEDA_CASAS_BAHIA_BACKOFF_SECONDS", "2"))
    except ValueError:
        base = 2.0
    try:
        cap = float(cap if cap is not None else os.getenv("SEDA_CASAS_BAHIA_BACKOFF_MAX_SECONDS", "30"))
    except ValueError:
        cap = 30.0
    if retry_after and retry_after > 0:
        delay = retry_after
    else:
        delay = base * (2 ** attempt)
    time.sleep(min(delay + random.uniform(0, base), cap))


def _retries_default():
    try:
        return int(os.getenv("SEDA_CASAS_BAHIA_API_RETRIES", "3"))
    except ValueError:
        return 3


def request_with_retry(do_request, *, retries=None, base_sleep=None,
                       retryable_status=RETRYABLE_STATUS, throttle_host=None):
    """Call ``do_request`` (returns a requests.Response) with backoff retries.

    Retries on transient transport exceptions (SSL/connection/timeout) and on
    rate-limit / server statuses. Returns the final Response; re-raises the last
    exception only if every attempt raised.
    """
    if retries is None:
        retries = _retries_default()
    last_response = None
    last_exc = None
    for attempt in range(retries + 1):
        if throttle_host:
            throttle(host=throttle_host)
        try:
            response = do_request()
        except Exception as exc:  # noqa: BLE001 - transport layer raises many types
            last_exc = exc
            if attempt < retries and is_retryable_exc(exc):
                sleep_backoff(attempt, base=base_sleep)
                continue
            raise
        if response.status_code in retryable_status and attempt < retries:
            last_response = response
            sleep_backoff(attempt, base=base_sleep, retry_after=retry_after_seconds(response))
            continue
        return response
    if last_response is not None:
        return last_response
    if last_exc is not None:
        raise last_exc
    return None
