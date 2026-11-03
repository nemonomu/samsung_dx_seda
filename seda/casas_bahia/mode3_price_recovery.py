"""Recover Mode-3 quotes using original sellers or verified seller candidates."""
import json
import time
from urllib.parse import urlsplit, urlunsplit

from .detail_price_recovery import PRICE_FIELDS, _positive_id, _identity, _safe_response, is_blank
from .mode3_prices import PENDING, pending
from .rest_detail_price_recovery import recover_rest_price_fields


def _notice(message):
    try:
        print(message, flush=True)
    except Exception:
        pass


def _failed_result(error):
    return {"verified": False, "attempted": False, "cache_hit": False,
            "status_code": 0, "evidence": {}, "detail": {}, "error": error}


def _review_notice(row, result, cache, row_index):
    """Human review context only; never infer page deletion from a price API."""
    try:
        identity, _ = _identity(row)
        product, sku, seller = identity or ("", "", "")
        evidence = result.get("evidence") or {}
        seller = _positive_id(evidence.get("seller_id")) or seller
        discovery = cache.get(("mode3_seller_candidates", product), {})
        candidate_skus = sorted({
            _positive_id(offer.get("skuId")) for offer in discovery.get("offers", [])
            if isinstance(offer, dict) and _positive_id(offer.get("productId")) == product
            and _positive_id(offer.get("skuId"))
        })
        parsed = urlsplit(row.get("product_url") or "")
        url = urlunsplit((parsed.scheme, parsed.hostname, parsed.path, "", "")) if (
            parsed.scheme == "https" and parsed.hostname in {"www.casasbahia.com.br", "casasbahia.com.br"}
        ) else ""
        error = result.get("error", "")
        reason = {
            "missing_price_offer": "사용 가능한 가격 항목 없음(처리 후 0건)",
            "missing_candidate_seller": "판매자 후보 없음",
            "seller_discovery_unavailable": "판매자 조회 응답 확보 실패",
            "price_sku_identity_conflict": "수집 대상 SKU와 가격 응답 SKU 불일치",
            "price_product_identity_conflict": "상품 번호 불일치",
            "price_seller_identity_conflict": "판매자 불일치",
            "ambiguous_price_offers": "가격 후보가 여러 개여서 확정 불가",
        }.get(error, "가격 검증 실패(상세 진단 확인)")
        fields = {"row": row_index, "product_line": row.get("product_line", ""),
                  "product_name": str(row.get("retailer_sku_name") or "")[:300],
                  "product_id": product, "target_sku": sku, "seller_id": seller,
                  "candidate_skus": candidate_skus, "url": url, "reason": reason,
                  "price_api_status": result.get("status_code", 0),
                  "page_status": "미확인(가격 API 결과만으로 페이지 없음 판단 안 함)",
                  "action": "NULL 유지" if pending(row) or is_blank(row.get("final_sku_price")) else "기존 값 유지"}
        _notice("[seda] casas mode=3 price_recovery_review " + json.dumps(fields, ensure_ascii=False))
    except Exception:
        _notice(f"[seda] casas mode=3 price_recovery row={row_index} review_log_failed=true")


class BrowserPriceFetcher:
    """One owned Mode-3 Chrome for this pass; no direct REST or paid fallback."""

    def __init__(self):
        self.session = None
        self.start_failed = False
        self.failures = 0
        self.request_diagnostics = []

    def _record_diagnostic(self, diagnostic):
        # Never persist request headers, raw payloads, or exception messages.
        try:
            diagnostic["request_index"] = len(self.request_diagnostics) + 1
            self.request_diagnostics.append(diagnostic)
            _notice("[seda] casas mode=3 price_recovery_diagnostic " + json.dumps(diagnostic))
        except Exception:
            pass

    def close(self):
        session, self.session = self.session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                _notice("[seda] casas mode=3 price_recovery browser_close_failed=true")

    def __call__(self, products, *, include_offers=True):
        from seda.step00_config import CASAS_BAHIA_URLS_BY_PRODUCT_LINE, product_line
        from . import browser_api as api, price_api
        from . import browser_listing_url_first as quotes

        unavailable = {"success": False, "status_code": 0, "offers": []}
        diagnostic = {"stage": "prepare", "status": 0, "normalization": []}
        if self.start_failed or self.failures >= 3:
            diagnostic["reason"] = "bootstrap_failure_reused" if self.start_failed else "failure_limit_reached"
            self._record_diagnostic(diagnostic)
            return unavailable
        try:
            if self.session is None:
                diagnostic["stage"] = "bootstrap"
                self.session = api._APISession()
                try:
                    self.session._bootstrap(CASAS_BAHIA_URLS_BY_PRODUCT_LINE[product_line()]["main"], 45)
                except Exception as exc:
                    diagnostic.update(reason="bootstrap_failed", exception_type=type(exc).__name__)
                    self.start_failed = True
                    _notice("[seda] casas mode=3 price_recovery bootstrap_failed=true")
                    return unavailable
            diagnostic["stage"] = "request_prepare"
            product_items, sku_items = price_api._price_items(products)
            diagnostic["request_kind"] = "exact_sku" if sku_items else "product_candidates"
            # Product-only discovers candidates; exact SKU requests verify their quotes.
            if not product_items and not sku_items:
                diagnostic["reason"] = "empty_price_items"
                return unavailable
            params = dict(price_api._params())
            params["IdRegiao"] = "126000"
            headers = {k: v for k, v in price_api._headers().items()
                       if k in {"accept", "content-type", "x-origem", "xaplication", "apikey"}}
            headers["content-type"] = "application/json"
            time.sleep(5)
            diagnostic["stage"] = "price_request"
            result, data = api._browser_fetch(
                self.session.browser.driver, price_api.PRICE_URL, params, "POST", headers,
                {"produtos": [] if sku_items else product_items, "skus": sku_items}, 25, self.session.document_frame)
            status = result.get("status_code", 0)
            good = (status == 200 and not result.get("error") and isinstance(data, dict)
                    and isinstance(data.get("Ofertas"), list))
            diagnostic.update(status=status, response_error=bool(result.get("error")),
                              valid_offer_list=isinstance(data, dict) and isinstance(data.get("Ofertas"), list))
            self.failures = 0 if good else self.failures + 1
            diagnostic["stage"] = "normalize" if good else "price_response"
            offers = list(quotes._normalize_offers(
                [data], allow_partial=True, diagnostics=diagnostic["normalization"]).values()) if good else []
            diagnostic["reason"] = "response_processed" if good else "unusable_response"
            return {"success": good, "status_code": status,
                    "offers": offers}
        except Exception as exc:
            diagnostic.update(reason="request_exception", exception_type=type(exc).__name__)
            self.failures += 1
            return unavailable
        finally:
            self._record_diagnostic(diagnostic)


def _origin(row):
    if _positive_id(row.get("main_rank")):
        return "main"
    if _positive_id(row.get("bsr_rank")):
        return "bsr"
    return "unknown"


def _recover_without_seller(work, cache, fetcher):
    identity, error = _identity(work)
    if error:
        return _failed_result(error)
    product, _, _ = identity
    key = ("mode3_seller_candidates", product)
    discovery_cached = key in cache
    if not discovery_cached:
        try:
            cache[key] = _safe_response(fetcher([{"id": product}], include_offers=True))
        except Exception:
            cache[key] = _safe_response(None)
    response = cache[key]
    if not response["success"] or response["status_code"] != 200:
        result = _failed_result("seller_discovery_unavailable")
        result.update(attempted=not discovery_cached, cache_hit=discovery_cached,
                      status_code=response["status_code"])
        return result
    sellers = list(dict.fromkeys(
        _positive_id(o.get("sellerId")) for o in response["offers"]
        if isinstance(o, dict) and _positive_id(o.get("productId")) == product
        and _positive_id(o.get("sellerId"))))
    result = _failed_result("missing_candidate_seller")
    result.update(attempted=not discovery_cached, cache_hit=discovery_cached,
                  status_code=response["status_code"])
    for seller in sellers:
        candidate = dict(work, seller_id=seller)
        result = recover_rest_price_fields(candidate, cache=cache, fetcher=fetcher)
        result["evidence"]["seller_source"] = "product_offer_candidate"
        result["evidence"]["candidate_seller"] = seller
        if result["verified"]:
            result["detail"]["seller_id"] = seller
            return result
    return result


def _recover_row(row, cache, fetcher, append_token):
    """Prepare all updates off-row so errors cannot leave a half-released quote."""
    origin, seller = _origin(row), _positive_id(row.get("seller_id"))
    held = pending(row)
    work, updated = dict(row), dict(row)
    if held:
        for field in ("original_sku_price", "final_sku_price", "savings"):
            work[field] = ""
    if origin == "unknown":
        result = _failed_result("missing_listing_origin")
    elif not seller:
        result = _recover_without_seller(work, cache, fetcher)
    else:
        result = recover_rest_price_fields(work, cache=cache, fetcher=fetcher)
    filled = []
    success = result["verified"] and not is_blank(result["detail"].get("final_sku_price"))
    if success:
        if not seller:
            updated["seller_id"] = result["detail"]["seller_id"]
            filled.append("seller_id")
        for field in PRICE_FIELDS:
            if held and field in ("original_sku_price", "final_sku_price", "savings"):
                updated[field] = result["detail"].get(field, "")
                filled.append(field)
            elif is_blank(row.get(field)) and not is_blank(result["detail"].get(field)):
                updated[field] = result["detail"][field]
                filled.append(field)
        updated["parse_status"] = "+".join(token for token in str(row.get("parse_status") or "").split("+")
                                          if token not in {PENDING, "casas_listing_price_pending", "casas_listing_seller_pending"}
                                          and not token.startswith("casas_mode3_price_failed:"))
        updated["parse_status"] = append_token(updated["parse_status"], "casas_mode3_price_recovered")
        if not seller:
            updated["parse_status"] = append_token(updated["parse_status"], "casas_mode3_seller_discovered")
        method = "casas_bahia_mode3_price_cache" if result["cache_hit"] else "casas_bahia_mode3_price_api"
        updated["fetch_method"] = append_token(row.get("fetch_method", ""), method)
    else:
        updated["parse_status"] = append_token(row.get("parse_status", ""),
                                               "casas_mode3_price_failed:" + result["error"])
        method = "casas_bahia_mode3_price_api"
    detail = (f"origin:{origin};listing_seller:{seller};cache_hit:{int(result['cache_hit'])}"
              f";filled:{','.join(filled)};quote_verified:{int(success)}"
              + "".join(f";{k}:{v}" for k, v in result["evidence"].items()))
    return updated, success, method, result, detail


def backfill_mode3_prices(rows, *, checkpoint_every=25, trace_rows=None,
                          row_index_offset=0, checkpoint_writer=None,
                          fetcher=None, cache=None, append_token, record_subcall):
    """Preserve known listing sellers; discover and verify only missing sellers."""
    candidates = [(index, row) for index, row in enumerate(rows, row_index_offset + 1)
                  if row.get("retailer") == "Casas Bahia"
                  and (pending(row) or is_blank(row.get("final_sku_price")))]
    if not candidates:
        return rows
    owned = BrowserPriceFetcher() if fetcher is None else None
    fetcher = owned if owned is not None else fetcher
    cache = {} if cache is None else cache
    recovered = 0
    _notice(f"[seda] casas mode=3 price_recovery candidates={len(candidates)} "
            "source=browser_api seller_policy=original_listing_or_verified_candidate max_calls_per_identity=1")
    try:
        for position, (index, row) in enumerate(candidates, 1):
            diagnostics = getattr(fetcher, "request_diagnostics", None)
            diagnostic_start = len(diagnostics) if isinstance(diagnostics, list) else 0
            try:
                updated, success, method, result, detail = _recover_row(row, cache, fetcher, append_token)
                row.update(updated)
            except Exception:
                # Optional quote recovery cannot abort the remaining detail rows.
                result = _failed_result("price_recovery_error")
                success, method = False, "casas_bahia_mode3_price_api"
                detail = "quote_verified:0;row_preserved:1"
                token = "casas_mode3_price_failed:price_recovery_error"
                previous = str(row.get("parse_status") or "")
                if token not in previous.split("+"):
                    row["parse_status"] = previous + ("+" if previous else "") + token
            try:
                if isinstance(diagnostics, list) and len(diagnostics) > diagnostic_start:
                    detail += ";price_diagnostics_json:" + json.dumps(diagnostics[diagnostic_start:])
            except Exception:
                _notice(f"[seda] casas mode=3 price_recovery row={index} diagnostic_failed=true")
            recovered += int(success)
            try:
                record_subcall(trace_rows, row, index, row.get("product_url", ""), "casas_mode3_price_recovery",
                               method=method, success=success, status_code=result["status_code"],
                               attempt=int(result["attempted"]), error="" if success else result["error"],
                               detail=detail, detail_limit=max(500, len(detail)))
            except Exception:
                _notice(f"[seda] casas mode=3 price_recovery row={index} trace_failed=true")
            _notice(f"[seda] casas mode=3 price_recovery row={index} {detail} "
                    f"success={str(success).lower()} error={result['error'] or 'none'}")
            if not success:
                _review_notice(row, result, cache, index)
            if checkpoint_writer is not None and checkpoint_every and position % checkpoint_every == 0:
                checkpoint_writer(rows)
        if checkpoint_writer is not None and (not checkpoint_every or len(candidates) % checkpoint_every):
            checkpoint_writer(rows)
    finally:
        if owned is not None:
            try:
                owned.close()
            except Exception:
                _notice("[seda] casas mode=3 price_recovery browser_close_failed=true")
    _notice(f"[seda] casas mode=3 price_recovery recovered={recovered}/{len(candidates)}")
    return rows
