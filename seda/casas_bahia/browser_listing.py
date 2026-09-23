"""Casas listing-only Chrome SSR fallback; no direct API or ZenRows calls.

Only completed responses belonging to the current top-frame navigation qualify.
Captured price offers are joined by product/SKU/seller identity, never by position.
The returned HTML retains the real document with enriched NEXT_DATA for the
existing listing parser. The isolated Chrome session is reused until closed.
"""

import atexit
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit


_SESSION = None
_LOCK = threading.RLock()
_HOSTS = {"casasbahia.com.br", "www.casasbahia.com.br"}
_RECOVERABLE_ERRORS = {
    "price_or_seller_identity_incomplete",
    "missing_current_price_response",
    "document_not_completed",
    "document_not_200",
}
_NEXT_SCRIPT = re.compile(
    r'(<script\b(?=[^>]*\bid\s*=\s*[\"\']__NEXT_DATA__[\"\'])[^>]*>)(.*?)(</script\s*>)',
    re.I | re.S,
)


class EvidenceError(ValueError):
    """A deliberately non-sensitive, fixed-code validation failure."""


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value).strip()
    return str(int(value)) if value.isascii() and value.isdigit() and int(value) > 0 else ""


def _same_navigation(params, frame):
    return bool(frame.get("id") and frame.get("loaderId")
                and params.get("frameId") == frame["id"]
                and params.get("loaderId") == frame["loaderId"])


def _sort_value(query):
    for key in ("ordenacao", "sortby", "sortBy"):
        value = query.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        if value not in (None, ""):
            return str(value).replace("-", "").lower()
    return ""


def _request_identity(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _HOSTS or not parsed.path.rstrip("/").endswith("/b"):
        raise EvidenceError("not_casas_bahia_listing_url")
    query = parse_qs(parsed.query)
    page = _positive_id((query.get("page") or ["1"])[0])
    if not page:
        raise EvidenceError("invalid_requested_page")
    return parsed.path.rstrip("/"), page, _sort_value(query)


def _document_matches(url, requested):
    try:
        return _request_identity(url) == requested
    except (ValueError, TypeError):
        return False


def _normalize_offers(price_payloads):
    # These existing helpers are pure numeric/formatting functions. No HTTP call.
    from seda.casas_bahia.price_api import _discount_rate, _savings_text

    offers = {}
    for data in price_payloads:
        if not isinstance(data, dict) or not isinstance(data.get("Ofertas"), list):
            raise EvidenceError("invalid_price_payload")
        for offer in data["Ofertas"]:
            if not isinstance(offer, dict):
                raise EvidenceError("invalid_price_offer")
            price = offer.get("PrecoVenda") if isinstance(offer.get("PrecoVenda"), dict) else {}
            discount = offer.get("DescontoFormaPagamento") if isinstance(offer.get("DescontoFormaPagamento"), dict) else {}
            availability = offer.get("Disponibilidade") if isinstance(offer.get("Disponibilidade"), dict) else {}
            identity = (_positive_id(price.get("IdProduto")),
                        _positive_id(price.get("IdSku") or availability.get("IdSku")),
                        _positive_id(price.get("IdLojista") or availability.get("IdLojista")))
            if not all(identity):
                continue
            normalized = {
                "oldPrice": price.get("PrecoDe"),
                "currentPrice": discount.get("PrecoVendaComDesconto") or price.get("Preco"),
                "standardPrice": price.get("Preco"),
                "discountRate": _discount_rate(price, discount),
                "savings": _savings_text(price),
                "discountDescription": discount.get("DescricaoDesconto") or discount.get("FormaPagamento"),
                "installment": price.get("Parcelamento"),
                "availability": availability,
                "sellerId": identity[2], "skuId": identity[1], "productId": identity[0],
            }
            if identity in offers and offers[identity] != normalized:
                raise EvidenceError("conflicting_price_offers")
            offers[identity] = normalized
    return offers


def _price_coverage(raw, price_payloads):
    """Allowlisted identity diagnostics only; never return raw source objects."""
    from seda.parsers import extract_next_data

    payload = extract_next_data(raw)
    try:
        products = payload["props"]["pageProps"]["initialState"]["search"]["results"]["products"]
    except (KeyError, TypeError):
        products = []
    if not isinstance(products, list):
        products = []
    identities, offer_count, invalid_offer_count = set(), 0, 0
    for data in price_payloads:
        offers = data.get("Ofertas") if isinstance(data, dict) else []
        for offer in offers if isinstance(offers, list) else []:
            offer_count += 1
            offer = offer if isinstance(offer, dict) else {}
            price = offer.get("PrecoVenda") if isinstance(offer.get("PrecoVenda"), dict) else {}
            availability = offer.get("Disponibilidade") if isinstance(offer.get("Disponibilidade"), dict) else {}
            identity = (_positive_id(price.get("IdProduto")),
                        _positive_id(price.get("IdSku") or availability.get("IdSku")),
                        _positive_id(price.get("IdLojista") or availability.get("IdLojista")))
            if all(identity):
                identities.add(identity)
            else:
                invalid_offer_count += 1
    missing, ambiguous, seller_mismatches, related = [], [], [], []
    matched = 0
    for product in products:
        product = product if isinstance(product, dict) else {}
        product_id = _positive_id(product.get("id"))
        sku_id = _positive_id(product.get("idSku"))
        seller_id = _positive_id(product.get("lojista") or product.get("sellerId"))
        source = {"product_id": product_id, "sku_id": sku_id, "explicit_seller_id": seller_id}
        pair_matches = sorted(identity for identity in identities if identity[:2] == (product_id, sku_id))
        exact = [identity for identity in pair_matches if not seller_id or identity[2] == seller_id]
        if len(exact) == 1:
            matched += 1
        elif len(exact) > 1:
            ambiguous.append(dict(source, offered_seller_ids=sorted({identity[2] for identity in exact})))
        else:
            missing.append(source)
            if seller_id and pair_matches:
                seller_mismatches.append(dict(source, offered_seller_ids=sorted({identity[2] for identity in pair_matches})))
            near = sorted(identity for identity in identities
                          if (product_id and identity[0] == product_id) or (sku_id and identity[1] == sku_id))
            if near:
                related.append({"source": source, "offer_identities": [
                    {"product_id": identity[0], "sku_id": identity[1], "seller_id": identity[2]} for identity in near]})
    return {"source_products": len(products), "captured_price_batches": len(price_payloads),
            "price_offer_count": offer_count, "valid_offer_identity_count": len(identities),
            "invalid_offer_identity_count": invalid_offer_count, "matched_source_products": matched,
            "missing_price_identity_count": len(missing), "missing_price_identities": missing,
            "ambiguous_identity_count": len(ambiguous), "ambiguous_price_identities": ambiguous,
            "seller_mismatch_count": len(seller_mismatches), "seller_mismatches": seller_mismatches,
            "related_offer_identities": related}


def _enrich_document(raw, url, price_payloads):
    """Pure validation/merge, preserving the established relevance filters."""
    from seda.parsers import extract_next_data, parse_listing, sku_from_url

    _, page, requested_sort = _request_identity(url)
    payload = extract_next_data(raw)
    if not isinstance(payload, dict):
        raise EvidenceError("missing_next_data")
    try:
        search = payload["props"]["pageProps"]["initialState"]["search"]
        products = search["results"]["products"]
        query = search["query"]
    except (KeyError, TypeError):
        raise EvidenceError("missing_ssr_listing") from None
    if not isinstance(query, dict) or _positive_id(query.get("page")) != page:
        raise EvidenceError("requested_page_mismatch")
    if requested_sort and _sort_value(query) != requested_sort:
        raise EvidenceError("requested_sort_mismatch")
    if not isinstance(products, list) or not products:
        raise EvidenceError("empty_ssr_products")
    offers = _normalize_offers(price_payloads)
    copied = deepcopy(products)
    source_ids = []
    for product in copied:
        if not isinstance(product, dict):
            raise EvidenceError("invalid_ssr_product")
        product_id = _positive_id(product.get("id"))
        sku_id = _positive_id(product.get("idSku"))
        url_sku = _positive_id(sku_from_url(product.get("href") or product.get("url") or ""))
        seller_raw = product.get("lojista") or product.get("sellerId")
        seller_id = _positive_id(seller_raw)
        if not product_id or not sku_id or sku_id != url_sku or (seller_raw and not seller_id):
            raise EvidenceError("listing_identity_invalid")
        if sku_id in source_ids:
            raise EvidenceError("duplicate_listing_sku")
        source_ids.append(sku_id)
        candidates = [value for key, value in offers.items()
                      if key[:2] == (product_id, sku_id) and (not seller_id or key[2] == seller_id)]
        if len(candidates) != 1:
            raise EvidenceError("price_or_seller_identity_incomplete" if not candidates else "ambiguous_listing_seller")
        price = candidates[0]
        if price.get("currentPrice") in (None, ""):
            raise EvidenceError("missing_listing_price")
        product["price"] = price
        product["lojista"] = price["sellerId"]
        product["sku"] = price["skuId"]
    payload["props"]["pageProps"]["initialState"]["search"]["results"]["products"] = copied
    # JSON escaping prevents product strings from terminating the script element.
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    enriched, replacements = _NEXT_SCRIPT.subn(lambda m: m[1] + encoded + m[3], raw)
    if replacements != 1:
        raise EvidenceError("next_data_script_count_invalid")
    rows = parse_listing(enriched, "Casas Bahia", "https://www.casasbahia.com.br", url)
    row_ids = [_positive_id(sku_from_url(row.get("product_url") or "")) for row in rows]
    expected_order = [sku for sku in source_ids if sku in set(row_ids)]
    if not rows or row_ids != expected_order or len(row_ids) != len(set(row_ids)):
        raise EvidenceError("parsed_listing_identity_invalid")
    by_sku = {_positive_id(product["idSku"]): product for product in copied}
    for row, sku_id in zip(rows, row_ids):
        product = by_sku.get(sku_id)
        if (not product or not row.get("final_sku_price")
                or _positive_id(row.get("retailer_product_id")) != _positive_id(product["id"])
                or _positive_id(row.get("seller_id")) != product["price"]["sellerId"]):
            raise EvidenceError("parsed_price_or_seller_mismatch")
    return enriched, source_ids, len(rows)


def _chrome_major(executable):
    """Read the selected executable's version without launching a Windows GUI."""
    if os.name == "nt":
        literal = str(executable).replace("'", "''")
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   f"(Get-Item -LiteralPath '{literal}').VersionInfo.ProductVersion"]
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    else:
        command, kwargs = [str(executable), "--version"], {}
    result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True, **kwargs)
    match = re.search(r"\b(\d+)\.\d+\.\d+\.\d+", result.stdout)
    if not match:
        raise EvidenceError("chrome_version_unavailable")
    return int(match[1])


def _response_body(driver, request_id):
    data = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
    return base64.b64decode(data["body"]).decode("utf-8") if data.get("base64Encoded") else data["body"]


def _owned_chrome_type(base):
    """Guard cleanup only for our driver, including partially initialized ones."""
    class OwnedChrome(base):
        def quit(self):
            if getattr(self, "_casas_listing_quit_started", False):
                return
            self._casas_listing_quit_started = True
            return super().quit()

        def __del__(self):
            # UC's destructor calls quit again after an explicit quit. On Windows
            # that can emit an ignored invalid-handle error during interpreter exit.
            try:
                self.quit()
            except Exception:
                pass

    return OwnedChrome


def _network_evidence(messages, frame, requested):
    requests, documents, prices, completed = {}, [], [], set()
    for message in messages:
        method, params = message.get("method"), message.get("params", {})
        if method == "Network.loadingFinished":
            completed.add(params.get("requestId"))
        if method == "Network.requestWillBeSent" and _same_navigation(params, frame):
            request = params.get("request", {})
            parsed = urlsplit(request.get("url", ""))
            if parsed.hostname == "api.casasbahia.com.br" and parsed.path.rstrip("/") == "/merchandising/oferta/v1/Preco/Oferta/PrecoVenda":
                requests[params.get("requestId")] = request.get("method")
        if method != "Network.responseReceived" or not _same_navigation(params, frame):
            continue
        response = params.get("response", {})
        parsed = urlsplit(response.get("url", ""))
        if params.get("type") == "Document" and _document_matches(response.get("url", ""), requested):
            documents.append((params.get("requestId"), response))
        if (parsed.hostname == "api.casasbahia.com.br"
                and parsed.path.rstrip("/") == "/merchandising/oferta/v1/Preco/Oferta/PrecoVenda"
                and params.get("type") != "Preflight" and response.get("status") == 200
                and not response.get("fromDiskCache") and not response.get("fromServiceWorker")):
            prices.append(params.get("requestId"))
    prices = list(dict.fromkeys(request_id for request_id in prices
                              if request_id in completed and requests.get(request_id) == "POST"))
    return documents, prices, completed


class _BrowserSession:
    def __init__(self):
        self.driver = None
        self.major = None
        self.page_ids = {}

    def start(self):
        import undetected_chromedriver as uc

        executable = os.getenv("SEDA_CASAS_BAHIA_CHROME_PATH") or uc.find_chrome_executable()
        if not executable or not Path(executable).is_file():
            raise EvidenceError("chrome_executable_missing")
        self.major = _chrome_major(executable)
        print(f"[seda] casas_bahia browser_ssr detected_browser_major={self.major}", flush=True)
        options = uc.ChromeOptions()
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        if os.getenv("SEDA_CASAS_BAHIA_BROWSER_HEADLESS", "0").lower() in {"1", "true", "yes"}:
            options.add_argument("--headless=new")
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        # UC provisions a matching driver. No fixed major or user browser profile.
        self.driver = _owned_chrome_type(uc.Chrome)(options=options, version_main=self.major, browser_executable_path=executable)
        browser_major = str(self.driver.capabilities.get("browserVersion", "")).split(".")[0]
        driver_version = self.driver.capabilities.get("chrome", {}).get("chromedriverVersion", "")
        if browser_major != str(self.major) or (driver_version and driver_version.split(".")[0] != str(self.major)):
            raise EvidenceError("browser_driver_version_mismatch")
        driver_major = driver_version.split(".")[0] if driver_version else "unavailable"
        print(f"[seda] casas_bahia browser_ssr ready browser_major={int(browser_major)} driver_major={driver_major}", flush=True)
        self.driver.execute_cdp_cmd("Network.enable", {"maxTotalBufferSize": 60000000, "maxResourceBufferSize": 10000000})
        self.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        self.driver.execute_cdp_cmd("Network.setBypassServiceWorker", {"bypass": True})

    def close(self):
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def fetch(self, url, timeout=None):
        requested = _request_identity(url)
        trace = {"method": "browser_ssr", "page": int(requested[1]), "status_code": 0,
                 "chrome_major": self.major, "cache_disabled": True, "service_worker_bypassed": True}
        if self.driver is None:
            self.start()
            trace["chrome_major"] = self.major
        self.driver.set_page_load_timeout(min(float(timeout or 45), 45))
        self.driver.get_log("performance")
        prior_frame = self.driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]["frame"]
        try:
            self.driver.get(url)
        except Exception as exc:
            trace["navigation_error_type"] = type(exc).__name__
        wait_seconds = max(1.0, min(float(os.getenv("SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS", "30")), 120.0))
        deadline = time.monotonic() + wait_seconds
        messages, body_cache = [], {}
        failure = "missing_current_document"
        while True:
            for entry in self.driver.get_log("performance"):
                try:
                    messages.append(json.loads(entry["message"])["message"])
                except (KeyError, ValueError, TypeError):
                    continue
            frame = self.driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]["frame"]
            documents, price_ids, completed = _network_evidence(messages, frame, requested)
            # Draining the log alone cannot exclude late prior-navigation events.
            # A real document navigation must establish a new loader identity.
            if not frame.get("loaderId") or frame.get("loaderId") == prior_frame.get("loaderId"):
                documents = []
                failure = "new_document_navigation_not_observed"
            if documents:
                document_id, response = documents[-1]
                trace["status_code"] = int(response.get("status") or 0)
                if trace["status_code"] != 200:
                    failure = "document_not_200"
                elif response.get("fromDiskCache") or response.get("fromServiceWorker"):
                    failure = "cached_document_rejected"
                elif document_id not in completed:
                    failure = "document_not_completed"
                elif not price_ids:
                    failure = "missing_current_price_response"
                else:
                    try:
                        for request_id in [document_id] + price_ids:
                            if request_id not in body_cache:
                                body_cache[request_id] = _response_body(self.driver, request_id)
                        price_payloads = [json.loads(body_cache[request_id]) for request_id in price_ids]
                        trace["price_response_count"] = len(price_ids)
                        trace["price_coverage"] = _price_coverage(body_cache[document_id], price_payloads)
                        html, ids, row_count = _enrich_document(body_cache[document_id], url, price_payloads)
                        group = (requested[0], requested[2])
                        previous = self.page_ids.setdefault(group, {})
                        if any(old_page != requested[1] and old_ids == set(ids) for old_page, old_ids in previous.items()):
                            raise EvidenceError("earlier_page_repeated_for_other_page")
                        previous[requested[1]] = set(ids)
                        trace.update({"products": len(ids), "parsed_rows": row_count, "price_response_count": len(price_ids),
                                      "identity_checked": True, "document_completed": True})
                        return {"success": True, "text": html, "status_code": 200, "method": "browser_ssr",
                                "products": len(ids), "trace": [trace]}
                    except EvidenceError as exc:
                        failure = str(exc)
                    except Exception as exc:
                        failure = "response_validation_" + type(exc).__name__
            if time.monotonic() >= deadline:
                trace["error"] = failure
                return {"success": False, "text": "", "status_code": trace["status_code"],
                        "method": "browser_ssr", "error": failure, "trace": [trace]}
            time.sleep(0.5)


def fetch_page(url, timeout=None):
    """Fetch a fallback page with at most one fresh-navigation recovery attempt."""
    global _SESSION
    with _LOCK:
        trace = []
        try:
            requested = _request_identity(url)
            if _SESSION is None:
                _SESSION = _BrowserSession()
            for attempt in (1, 2):
                result = _SESSION.fetch(url, timeout=timeout)
                trace.extend(dict(item, navigation_attempt=attempt) for item in result.get("trace", []))
                result = dict(result, trace=list(trace))
                if result.get("success") or attempt == 2 or result.get("error") not in _RECOVERABLE_ERRORS:
                    return result
                print(f"[seda] casas_bahia browser_ssr page={requested[1]} navigation_attempt=1 "
                      f"error={result['error']} retry_navigation=2 wait_seconds=3", flush=True)
                time.sleep(3)
        except Exception as exc:
            error = str(exc) if isinstance(exc, EvidenceError) else "browser_" + type(exc).__name__
            close_browser()
            return {"success": False, "text": "", "status_code": 0, "method": "browser_ssr",
                    "error": error, "trace": trace + [{"method": "browser_ssr", "status_code": 0, "error": error}]}


def close_browser():
    """Close only the dedicated fallback browser owned by this module."""
    global _SESSION
    with _LOCK:
        session, _SESSION = _SESSION, None
        if session is not None:
            session.close()


atexit.register(close_browser)
