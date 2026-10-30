"""Recover a Mode-3 row's quote from its original listing seller only."""
import time

from .detail_price_recovery import PRICE_FIELDS, _positive_id, is_blank
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


class BrowserPriceFetcher:
    """One owned Mode-3 Chrome for this pass; no direct REST or paid fallback."""

    def __init__(self):
        self.session = None
        self.start_failed = False
        self.failures = 0

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
        if self.start_failed or self.failures >= 3:
            return unavailable
        try:
            if self.session is None:
                self.session = api._APISession()
                try:
                    self.session._bootstrap(CASAS_BAHIA_URLS_BY_PRODUCT_LINE[product_line()]["main"], 45)
                except Exception:
                    self.start_failed = True
                    _notice("[seda] casas mode=3 price_recovery bootstrap_failed=true")
                    return unavailable
            product_items, sku_items = price_api._price_items(products)
            # Callers must preserve a known listing seller; no product-only query.
            if not product_items or not sku_items:
                return unavailable
            params = dict(price_api._params())
            params["IdRegiao"] = "126000"
            headers = {k: v for k, v in price_api._headers().items()
                       if k in {"accept", "content-type", "x-origem", "xaplication", "apikey"}}
            headers["content-type"] = "application/json"
            time.sleep(5)
            result, data = api._browser_fetch(
                self.session.browser.driver, price_api.PRICE_URL, params, "POST", headers,
                {"produtos": product_items, "skus": sku_items}, 25, self.session.document_frame)
            status = result.get("status_code", 0)
            good = (status == 200 and not result.get("error") and isinstance(data, dict)
                    and isinstance(data.get("Ofertas"), list))
            self.failures = 0 if good else self.failures + 1
            return {"success": good, "status_code": status,
                    "offers": list(quotes._normalize_offers([data], allow_partial=True).values()) if good else []}
        except Exception:
            self.failures += 1
            return unavailable


def _origin(row):
    if _positive_id(row.get("main_rank")):
        return "main"
    if _positive_id(row.get("bsr_rank")):
        return "bsr"
    return "unknown"


def _recover_row(row, cache, fetcher, append_token):
    """Prepare all updates off-row so errors cannot leave a half-released quote."""
    origin, seller = _origin(row), _positive_id(row.get("seller_id"))
    held = pending(row)
    work, updated = dict(row), dict(row)
    if held:
        for field in ("original_sku_price", "final_sku_price", "savings"):
            work[field] = ""
    if origin == "unknown" or not seller:
        result = _failed_result("missing_listing_origin" if origin == "unknown" else "missing_listing_seller")
    else:
        result = recover_rest_price_fields(work, cache=cache, fetcher=fetcher)
    filled = []
    success = result["verified"] and not is_blank(result["detail"].get("final_sku_price"))
    if success:
        for field in PRICE_FIELDS:
            if held and field in ("original_sku_price", "final_sku_price", "savings"):
                updated[field] = result["detail"].get(field, "")
                filled.append(field)
            elif is_blank(row.get(field)) and not is_blank(result["detail"].get(field)):
                updated[field] = result["detail"][field]
                filled.append(field)
        updated["parse_status"] = "+".join(token for token in str(row.get("parse_status") or "").split("+")
                                          if token not in {PENDING, "casas_listing_price_pending"})
        updated["parse_status"] = append_token(updated["parse_status"], "casas_mode3_price_recovered")
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
    """Main rows retain Main seller/price; BSR-only rows retain BSR seller/price."""
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
            "source=browser_api seller_policy=original_listing max_calls_per_identity=1")
    try:
        for position, (index, row) in enumerate(candidates, 1):
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
            recovered += int(success)
            try:
                record_subcall(trace_rows, row, index, row.get("product_url", ""), "casas_mode3_price_recovery",
                               method=method, success=success, status_code=result["status_code"],
                               attempt=int(result["attempted"]), error="" if success else result["error"], detail=detail)
            except Exception:
                _notice(f"[seda] casas mode=3 price_recovery row={index} trace_failed=true")
            _notice(f"[seda] casas mode=3 price_recovery row={index} {detail} "
                    f"success={str(success).lower()} error={result['error'] or 'none'}")
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
