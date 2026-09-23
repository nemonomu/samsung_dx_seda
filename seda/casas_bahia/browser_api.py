"""Mode 3: one Chrome listing navigation, then browser-only search/price APIs.

UC controls the owned Chrome; JavaScript fetch performs every API request in
that browser. No Python HTTP, ZenRows, automatic refresh, or alternate-mode
fallback is used. REST pages do not necessarily equal the SSR product order.
An absent response page/sort is recorded as request evidence, not server echo.
"""

import atexit
from copy import deepcopy
import json
import math
import os
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit

from seda.casas_bahia import browser_listing
from seda.casas_bahia.browser_probe_session import ProbeBrowserSession


_SESSION = None
_LOCK = threading.RLock()
API_TIMEOUT_SECONDS = 25
MIN_SEARCH_INTERVAL_SECONDS = 5
_FETCH_SCRIPT = r"""
const [url, method, headers, body, seconds] = arguments;
const done = arguments[arguments.length - 1];
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), seconds * 1000);
const start = performance.now();
(async () => {
  try {
    const response = await fetch(url, {
      method, headers, body: body === null ? undefined : JSON.stringify(body),
      mode: 'cors', credentials: 'same-origin', cache: 'no-store',
      signal: controller.signal
    });
    const isJson = (response.headers.get('content-type') || '').toLowerCase().includes('json');
    const result = {status: response.status, ok: response.ok, json: isJson};
    if (response.ok && isJson) result.data = await response.json();
    result.elapsed_seconds = (performance.now() - start) / 1000;
    done(result);
  } catch (error) {
    done({status: 0, ok: false, json: false,
          error_name: error && error.name === 'AbortError' ? 'AbortError' : 'FetchError',
          elapsed_seconds: (performance.now() - start) / 1000});
  } finally { clearTimeout(timer); }
})();
"""


class _BootstrapError(browser_listing.EvidenceError):
    def __init__(self, code, status_code, trace):
        super().__init__(code)
        self.status_code = status_code
        self.trace = trace


def _safe_error(exc):
    if isinstance(exc, browser_listing.EvidenceError) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", str(exc)):
        return str(exc)
    return "browser_api_" + type(exc).__name__


def _bounded_number(value, default, minimum, maximum):
    try:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _attempt_limit():
    try:
        return min(3, max(1, int(os.getenv("SEDA_CASAS_BAHIA_SEARCH_RETRIES", "2")) + 1))
    except (TypeError, ValueError):
        return 3


def _frame(driver):
    return driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]["frame"]


def _same_frame(first, second):
    return bool(first.get("id") and first.get("loaderId")
                and first.get("id") == second.get("id")
                and first.get("loaderId") == second.get("loaderId"))


def _messages(driver):
    result = []
    for entry in driver.get_log("performance"):
        try:
            message = json.loads(entry["message"])["message"]
            if isinstance(message, dict):
                result.append(message)
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _network_evidence(messages, frame, url, method, body):
    """Use the successful local probe's GET/POST HTTP-200 evidence contract.

    Document completion is still required by the initial SSR bootstrap. API
    completion events, full query/body equality and single-response counts were
    not gates in that probe and are deliberately not extra gates here.
    """
    expected = urlsplit(url)
    requested_page = (parse_qs(expected.query).get("page") or [None])[0] if method == "GET" else None
    selected, responses, successful_methods = {}, {}, set()
    navigation_count = 0
    for message in messages:
        kind, params = message.get("method"), message.get("params", {})
        request_id = params.get("requestId")
        if kind == "Network.requestWillBeSent":
            if params.get("type") == "Document":
                navigation_count += 1
            request = params.get("request", {})
            parsed = urlsplit(request.get("url", ""))
            if ((parsed.hostname, parsed.path.rstrip("/"))
                    != (expected.hostname, expected.path.rstrip("/"))):
                continue
            if requested_page is not None and parse_qs(parsed.query).get("page") != [requested_page]:
                continue
            selected[request_id] = request.get("method")
        elif kind == "Network.responseReceived" and request_id in selected:
            response = params.get("response", {})
            status = int(response.get("status") or 0)
            if status == 200:
                successful_methods.add(selected[request_id])
            responses[request_id] = {
                "status_code": status,
                "from_disk_cache": bool(response.get("fromDiskCache")),
                "from_service_worker": bool(response.get("fromServiceWorker")),
            }
        elif kind == "Network.responseReceivedExtraInfo" and request_id in selected:
            status = int(params.get("statusCode") or 0)
            if status == 200:
                successful_methods.add(selected[request_id])
            responses.setdefault(request_id, {"status_code": status})
    matched = [responses[request_id] for request_id in selected if request_id in responses]
    return {
        "request_method": method,
        "matching_request_count": len(selected),
        "matching_response_count": len(matched),
        "matched_http200_methods": sorted(value for value in successful_methods if value in {"GET", "POST", "OPTIONS"}),
        "document_requests_observed": navigation_count,
        "response_status_codes": [item["status_code"] for item in matched],
        "cached_response_observed": any(item.get("from_disk_cache") or item.get("from_service_worker") for item in matched),
        "request_response_verified": method in successful_methods,
    }


def _browser_fetch(driver, endpoint, params, method, headers, body, timeout, document_frame):
    """Return only parsed API data and allowlisted network diagnostics."""
    driver.get_log("performance")
    before = _frame(driver)
    url = endpoint + "?" + urlencode(params)
    driver.set_script_timeout(API_TIMEOUT_SECONDS + 5)
    value = driver.execute_async_script(_FETCH_SCRIPT, url, method, headers, body, API_TIMEOUT_SECONDS)
    if not isinstance(value, dict):
        raise browser_listing.EvidenceError("invalid_browser_fetch_result")
    messages = _messages(driver)
    evidence = _network_evidence(messages, before, url, method, body)
    evidence["same_document"] = _same_frame(before, _frame(driver))
    safe = {"status_code": int(value.get("status") or 0), "ok": value.get("ok") is True,
            "json": value.get("json") is True,
            "elapsed_seconds": round(_bounded_number(value.get("elapsed_seconds"), 0, 0, API_TIMEOUT_SECONDS + 5), 3),
            "network": evidence}
    if value.get("error_name") in {"AbortError", "FetchError", "InvalidJSON"}:
        safe["error_name"] = value["error_name"]
    if not evidence["same_document"] or evidence["document_requests_observed"]:
        safe["error"] = "document_navigation_during_api_call"
    elif safe["status_code"] != 200 or not safe["ok"]:
        safe["error"] = "api_http_not_200"
    elif not safe["json"] or not isinstance(value.get("data"), dict):
        safe["error"] = "invalid_api_json"
    elif not evidence["request_response_verified"]:
        safe["error"] = "request_response_evidence_missing"
    return safe, value.get("data")


def _parser_context(data, requested):
    """Map REST aliases and label injected parser context honestly."""
    from seda.parsers import sku_from_url

    page, requested_sort = requested[1], requested[2]
    query = data.get("queries")
    query_is_mapping = isinstance(query, dict)
    # The verified REST probe accepted a missing/non-mapping queries member.
    # It supplies no page/sort echo and cannot be promoted to server evidence.
    query = deepcopy(query) if query_is_mapping else {}
    raw_page = query.get("page")
    has_page = raw_page not in (None, "")
    if has_page and browser_listing._positive_id(raw_page) != page:
        raise browser_listing.EvidenceError("response_page_mismatch")
    sorts = [str(query[key]).replace("-", "").lower() for key in ("ordenacao", "sortby", "sortBy")
             if query.get(key) not in (None, "")]
    if requested_sort and sorts and any(value != requested_sort for value in sorts):
        raise browser_listing.EvidenceError("response_sort_mismatch")
    copied = deepcopy(data)
    copied["queries"] = query
    query["page"] = page
    if requested_sort and not sorts:
        query["sortby"] = requested_sort
    products = copied.get("products")
    if not isinstance(products, list) or not products:
        raise browser_listing.EvidenceError("empty_or_invalid_products")
    aliases, seen = 0, set()
    for product in products:
        if not isinstance(product, dict):
            raise browser_listing.EvidenceError("invalid_listing_product")
        raw_sku, raw_alias = product.get("sku"), product.get("idSku")
        sku, alias = browser_listing._positive_id(raw_sku), browser_listing._positive_id(raw_alias)
        if raw_sku not in (None, "") and not sku or raw_alias not in (None, "") and not alias:
            raise browser_listing.EvidenceError("listing_identity_invalid")
        if sku and alias and sku != alias:
            raise browser_listing.EvidenceError("conflicting_source_sku_aliases")
        if sku and not alias:
            product["idSku"] = raw_sku
            aliases += 1
        elif alias and not sku:
            product["sku"] = raw_alias
        identity = sku or alias
        url_sku = browser_listing._positive_id(sku_from_url(product.get("href") or product.get("url") or ""))
        seller_raw = product.get("lojista") or product.get("sellerId")
        if (not identity or not browser_listing._positive_id(product.get("id")) or identity != url_sku
                or (seller_raw and not browser_listing._positive_id(seller_raw))):
            raise browser_listing.EvidenceError("listing_identity_invalid")
        if identity in seen:
            raise browser_listing.EvidenceError("duplicate_listing_sku")
        seen.add(identity)
    return copied, {
        "response_queries_is_mapping": query_is_mapping,
        "response_page_present": has_page,
        "reported_page": int(page) if has_page else None,
        "page_evidence_source": "response_and_observed_request" if has_page else "observed_request_only",
        "parser_page_context_injected": not has_page,
        "response_sort_present": bool(sorts),
        "sort_evidence_source": "response_and_observed_request" if sorts else "observed_request_only",
        "parser_sort_context_injected": bool(requested_sort and not sorts),
        "parser_sku_aliases_added": aliases,
    }


class _APISession:
    def __init__(self):
        self.browser = ProbeBrowserSession()
        self.request_session_id = str(uuid.uuid4())
        self.last_search_finished = None
        print("[seda] casas_bahia mode=3 uc_api execution_profile=successful_local_probe "
              "api_timeout_seconds=25 search_interval_seconds=5", flush=True)
        self.document_frame = None
        self.bootstrap_attempted = False
        self.bootstrap_error = ""
        self.bootstrap_status = 0
        self.bootstrap_trace = []
        self.page_ids = {}

    def close(self):
        self.browser.close()

    def _bootstrap(self, url, timeout):
        if self.bootstrap_attempted:
            if self.bootstrap_error:
                try:
                    print("[seda] casas_bahia mode=3 uc_api bootstrap_failure_reused=true "
                          "new_navigation=false new_api_request=false", flush=True)
                except Exception:
                    pass
                raise _BootstrapError(self.bootstrap_error, self.bootstrap_status, self.bootstrap_trace)
            return None
        self.bootstrap_attempted = True
        started = time.monotonic()
        item = {"method": "uc_api", "stage": "bootstrap", "status_code": 0, "document_navigations": 1}
        print("[seda] casas_bahia mode=3 uc_api bootstrap_navigation=1", flush=True)
        try:
            # The established SSR validator verifies this first real page only.
            # Its products are never returned as mode-3 listing output.
            result = self.browser.fetch(url, timeout=45)
            item["status_code"] = int(_bounded_number(result.get("status_code"), 0, 0, 599))
            try:
                browser_trace = result.get("trace") or []
                if isinstance(browser_trace, list) and browser_trace and isinstance(browser_trace[-1], dict):
                    diagnostics = browser_listing._public_browser_diagnostics(browser_trace[-1].get("diagnostics"))
                    if diagnostics:
                        item["diagnostics"] = diagnostics
            except Exception:
                pass
            if not result.get("success"):
                reason = result.get("error")
                if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,100}", reason):
                    reason = "initial_page_not_verified"
                raise browser_listing.EvidenceError(reason)
            self.document_frame = _frame(self.browser.driver)
            if not self.document_frame.get("id") or not self.document_frame.get("loaderId"):
                raise browser_listing.EvidenceError("bootstrap_frame_missing")
            self.last_search_finished = time.monotonic()
        except Exception as exc:
            self.bootstrap_error = _safe_error(exc)
            self.bootstrap_status = item["status_code"]
            item.update({"error": self.bootstrap_error, "elapsed_seconds": round(time.monotonic() - started, 3)})
            self.bootstrap_trace = [item]
            print(f"[seda] casas_bahia mode=3 uc_api stage=bootstrap status={self.bootstrap_status} "
                  f"error={self.bootstrap_error} elapsed_seconds={item['elapsed_seconds']}", flush=True)
            raise _BootstrapError(self.bootstrap_error, self.bootstrap_status, self.bootstrap_trace) from None
        return {"method": "uc_api", "stage": "bootstrap", "status_code": 200,
                **({"diagnostics": item["diagnostics"]} if "diagnostics" in item else {}),
                "chrome_major": self.browser.major, "document_navigations": 1,
                "elapsed_seconds": round(time.monotonic() - started, 3)}

    def fetch(self, url, timeout=None):
        from seda.casas_bahia import price_api, search_api

        requested = browser_listing._request_identity(url)
        if not search_api._supported_listing_path(requested[0]):
            raise browser_listing.EvidenceError("not_casas_bahia_listing_url")
        seconds = API_TIMEOUT_SECONDS
        trace = []
        started = time.monotonic()
        initial = self._bootstrap(url, 45)
        if initial:
            trace.append(initial)
        params = dict(search_api._params(url))
        # Match the successful probe without changing process-wide settings used
        # by mode 1/2 or detail. Never log the generated session identifier.
        params.update({"resultsperpage": "20", "variantconfiguration": "q2", "regionid": "126000",
                       "sessionid": self.request_session_id, "userid": ""})
        price_params = dict(price_api._params())
        price_params["IdRegiao"] = "126000"
        if (str(params.get("page")) != requested[1]
                or str(params.get("sortby") or "").replace("-", "").lower() != requested[2]):
            raise browser_listing.EvidenceError("outgoing_page_or_sort_mismatch")
        search_headers = {key: value for key, value in search_api._headers(url).items()
                          if key in {"accept", "x-origem", "xaplication"}}
        limit = _attempt_limit()
        delay = _bounded_number(os.getenv("SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS", "3"), 3, 0, 30)
        for attempt in range(1, limit + 1):
            if attempt > 1:
                time.sleep(delay * (attempt - 1))
            item = {"method": "uc_api", "page": int(requested[1]), "attempt": attempt,
                    "stage": "search", "status_code": 0}
            trace.append(item)
            try:
                interval_wait = max(0, MIN_SEARCH_INTERVAL_SECONDS - (time.monotonic() - self.last_search_finished))
                time.sleep(interval_wait)
                item["interval_wait_seconds"] = round(interval_wait, 3)
                try:
                    search_result, data = _browser_fetch(self.browser.driver, search_api.SEARCH_URL, params,
                                                        "GET", search_headers, None, seconds, self.document_frame)
                finally:
                    self.last_search_finished = time.monotonic()
                item["search"] = search_result
                item["status_code"] = search_result["status_code"]
                if search_result.get("error"):
                    raise browser_listing.EvidenceError(search_result["error"])
                parser_data, context = _parser_context(data, requested)
                item.update(context)
                products = parser_data["products"]
                product_items, sku_items = price_api._price_items(products)
                if not product_items and not sku_items:
                    raise browser_listing.EvidenceError("empty_price_items")
                price_headers = {key: value for key, value in price_api._headers().items()
                                 if key in {"accept", "content-type", "x-origem", "xaplication", "apikey"}}
                price_headers["content-type"] = "application/json"
                item["stage"] = "price"
                price_result, prices = _browser_fetch(self.browser.driver, price_api.PRICE_URL, price_params,
                                                      "POST", price_headers,
                                                      {"produtos": product_items, "skus": sku_items},
                                                      seconds, self.document_frame)
                item["price"] = price_result
                item["status_code"] = price_result["status_code"]
                if price_result.get("error"):
                    raise browser_listing.EvidenceError(price_result["error"])
                item["stage"] = "validation"
                raw = search_api._as_next_data_html(parser_data, url)
                html, ids, row_count = browser_listing._enrich_document(raw, url, [prices])
                group = (requested[0], requested[2])
                previous = self.page_ids.setdefault(group, {})
                if any(page != requested[1] and old_ids == set(ids) for page, old_ids in previous.items()):
                    raise browser_listing.EvidenceError("earlier_page_repeated_for_other_page")
                previous[requested[1]] = set(ids)
                item.update({"stage": "complete", "status_code": 200, "products": len(ids),
                             "parsed_rows": row_count, "identity_checked": True,
                             "elapsed_seconds": round(time.monotonic() - started, 3)})
                print(f"[seda] casas_bahia mode=3 uc_api page={requested[1]} attempt={attempt}/{limit} "
                      f"products={len(ids)} rows={row_count} "
                      f"search_seconds={search_result.get('elapsed_seconds', 0)} "
                      f"price_seconds={price_result.get('elapsed_seconds', 0)} "
                      f"elapsed_seconds={item['elapsed_seconds']}", flush=True)
                return {"success": True, "text": html, "status_code": 200, "method": "uc_api",
                        "products": len(ids), "trace": trace}
            except Exception as exc:
                item["error"] = _safe_error(exc)
                print(f"[seda] casas_bahia mode=3 uc_api page={requested[1]} attempt={attempt}/{limit} "
                      f"stage={item['stage']} status={item['status_code']} error={item['error']}", flush=True)
        return {"success": False, "text": "", "status_code": trace[-1]["status_code"],
                "method": "uc_api", "error": trace[-1]["error"], "trace": trace}


def fetch_listing(url, timeout=None):
    """Max three search/price attempts; preserve the Chrome session on failure."""
    global _SESSION
    with _LOCK:
        try:
            browser_listing._request_identity(url)
            if _SESSION is None:
                _SESSION = _APISession()
            return _SESSION.fetch(url, timeout=timeout)
        except Exception as exc:
            error = _safe_error(exc)
            print(f"[seda] casas_bahia mode=3 uc_api error={error}", flush=True)
            status = exc.status_code if isinstance(exc, _BootstrapError) else 0
            trace = exc.trace if isinstance(exc, _BootstrapError) else [{"method": "uc_api", "status_code": 0, "error": error}]
            return {"success": False, "text": "", "status_code": status, "method": "uc_api",
                    "error": error, "trace": trace}


def close_browser():
    """Close only the browser owned by this mode, never a user's Chrome."""
    global _SESSION
    with _LOCK:
        session, _SESSION = _SESSION, None
        if session is not None:
            session.close()


atexit.register(close_browser)
