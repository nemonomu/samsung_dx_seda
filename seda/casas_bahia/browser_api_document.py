"""Mode-3 document preparation, independent of SSR offer admission.

These documents establish a browser context, never collected output. The API
collector still validates every returned product/SKU/seller/price before saving.
"""

import json
import time
from urllib.parse import urlsplit

from . import browser_listing as listing


HOME_URL = "https://www.casasbahia.com.br/"


def _notice(message):
    try:
        print(message, flush=True)
    except Exception:
        pass


def _frame(driver):
    return driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]["frame"]


def _matches(url, requested):
    if requested is not None:
        return listing._document_matches(url, requested)
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in listing._HOSTS and parsed.path in {"", "/"}


def _validate_document(raw, requested):
    from seda.parsers import extract_next_data, sku_from_url

    payload = extract_next_data(raw)
    if not isinstance(payload, dict) or payload.get("page") in {"/_error", "/404", "/500"}:
        raise listing.EvidenceError("initial_page_not_verified")
    props = payload.get("props")
    if not isinstance(props, dict) or not props:
        raise listing.EvidenceError("initial_page_not_verified")
    if requested is None:
        return 0
    page_props = props.get("pageProps", {})
    state = page_props.get("initialState") if isinstance(page_props, dict) else None
    if state is None:
        state = props.get("initialState")
    search = state.get("search") if isinstance(state, dict) else None
    query = search.get("query") if isinstance(search, dict) else None
    results = search.get("results") if isinstance(search, dict) else None
    products = results.get("products") if isinstance(results, dict) else None
    if not isinstance(query, dict) or listing._positive_id(query.get("page")) != requested[1]:
        raise listing.EvidenceError("requested_page_mismatch")
    if requested[2] and listing._sort_value(query) != requested[2]:
        raise listing.EvidenceError("requested_sort_mismatch")
    if not isinstance(products, list) or not products:
        raise listing.EvidenceError("empty_ssr_products")
    seen = set()
    for product in products:
        if not isinstance(product, dict):
            raise listing.EvidenceError("invalid_ssr_product")
        sku = listing._positive_id(product.get("idSku") or product.get("sku"))
        other_sku = listing._positive_id(product.get("sku"))
        url_sku = listing._positive_id(sku_from_url(product.get("href") or product.get("url") or ""))
        if (not listing._positive_id(product.get("id")) or not sku or sku != url_sku
                or (other_sku and other_sku != sku)):
            raise listing.EvidenceError("listing_identity_invalid")
        if sku in seen:
            raise listing.EvidenceError("duplicate_listing_sku")
        seen.add(sku)
    return len(products)


def _navigate(driver, url, requested):
    """Accept only a completed, uncached response from the new top document."""
    trace = {"method": "uc_api", "stage": "bootstrap", "status_code": 0,
             "document_navigations": 1}
    driver.set_page_load_timeout(45)
    driver.get_log("performance")
    prior = _frame(driver)
    started = time.monotonic()
    try:
        driver.get(url)
    except Exception as exc:
        trace["navigation_raised"] = True
        trace["navigation_timeout"] = type(exc).__name__ == "TimeoutException"
    trace["navigation_seconds"] = round(time.monotonic() - started, 3)
    deadline = time.monotonic() + 30
    messages, bodies = [], {}
    failure = "missing_current_document"
    while True:
        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
                if isinstance(message, dict):
                    messages.append(message)
            except (KeyError, TypeError, ValueError):
                continue
        frame = _frame(driver)
        if not frame.get("loaderId") or frame.get("loaderId") == prior.get("loaderId"):
            failure = "new_document_navigation_not_observed"
        else:
            documents = [m["params"] for m in messages
                         if m.get("method") == "Network.responseReceived"
                         and m.get("params", {}).get("type") == "Document"
                         and listing._same_navigation(m.get("params", {}), frame)]
            completed = {m.get("params", {}).get("requestId") for m in messages
                         if m.get("method") == "Network.loadingFinished"}
            if documents:
                document = documents[-1]
                response = document.get("response", {})
                request_id = document.get("requestId")
                trace["status_code"] = int(response.get("status") or 0)
                if not _matches(response.get("url", ""), requested) or not _matches(frame.get("url", ""), requested):
                    failure = "requested_page_mismatch"
                elif trace["status_code"] != 200:
                    failure = "document_not_200"
                elif response.get("fromDiskCache") or response.get("fromServiceWorker"):
                    failure = "cached_document_rejected"
                elif request_id not in completed:
                    failure = "document_not_completed"
                else:
                    try:
                        if request_id not in bodies:
                            bodies[request_id] = listing._response_body(driver, request_id)
                        count = _validate_document(bodies[request_id], requested)
                        current = _frame(driver)
                        if (current.get("id") != frame.get("id") or current.get("loaderId") != frame.get("loaderId")
                                or not _matches(current.get("url", ""), requested)):
                            raise listing.EvidenceError("document_navigation_during_api_call")
                        trace.update(products=count, document_completed=True)
                        return {"success": True, "status_code": 200, "trace": [trace],
                                "document_frame": {"id": current["id"], "loaderId": current["loaderId"]}}
                    except listing.EvidenceError as exc:
                        failure = str(exc)
                    except Exception as exc:
                        failure = "response_validation_" + type(exc).__name__
        if time.monotonic() >= deadline:
            trace["error"] = failure
            return {"success": False, "status_code": trace["status_code"], "error": failure, "trace": [trace]}
        time.sleep(0.5)


def prepare(browser, url):
    """Homepage once; listing retries after 3s then 60s, only for HTTP 403."""
    requested = listing._request_identity(url)
    browser.start()
    trace = []
    _notice("[seda] casas_bahia mode=3 api_document stage=homepage start")
    home = _navigate(browser.driver, HOME_URL, None)
    trace.extend(home["trace"])
    _notice(f"[seda] casas_bahia mode=3 api_document stage=homepage status={home['status_code']} "
            f"success={str(home['success']).lower()}")
    if not home["success"]:
        return dict(home, trace=trace, document_navigations=len(trace), homepage_ready=False)
    time.sleep(5)
    for attempt in (1, 2, 3):
        _notice(f"[seda] casas_bahia mode=3 api_document stage=listing attempt={attempt} start")
        result = _navigate(browser.driver, url, requested)
        trace.extend(result["trace"])
        _notice(f"[seda] casas_bahia mode=3 api_document stage=listing status={result['status_code']} "
                f"success={str(result['success']).lower()} error={result.get('error', 'none')}")
        if result["success"] or result["status_code"] != 403 or attempt == 3:
            return dict(result, trace=trace, document_navigations=len(trace), homepage_ready=True)
        if attempt == 1:
            time.sleep(3)
        else:
            for remaining in (60, 30):
                _notice(f"[seda] casas_bahia mode=3 api_document stage=listing recovery_wait_seconds={remaining}")
                time.sleep(30)
