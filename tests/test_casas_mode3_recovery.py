"""Offline recovery contracts, including the RDP BSR7 failure and DB NULLs."""
import json
import os
from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from seda.casas_bahia import (browser_recovery as recovery, browser_api_document as document,
                              browser_api, mode3_prices, detail_api, review_api, destaque_api,
                              listing_discount_backfill as badges, search_api)
from seda.parsers import parse_listing
from seda.step08_detail_enrichment import _merge_casas_bahia_apis
from seda.step15_final_output import _format_row
from seda.step14_db_load import _db_value
from test_casas_bahia_browser_api import product, offer, URL


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setenv("SEDA_CASAS_BAHIA_LISTING_MODE", "3")
    monkeypatch.setenv("SEDA_PRODUCT_LINE", "TV")
    monkeypatch.setenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_CACHE", "0")
    monkeypatch.setenv("SEDA_CASAS_BAHIA_TIMING", "0")
    monkeypatch.setattr(recovery, "_SESSION", None)
    monkeypatch.setattr(destaque_api, "_MODE3_DIRECT_BLOCKED", False)
    yield


def raw(products):
    context, _ = browser_api._parser_context({"products": products}, ("/tv/b", "1", ""))
    return search_api._as_next_data_html(context, URL)


def parsed(products, offers):
    html, ids, count = mode3_prices.enrich(raw(products), URL, [{"Ofertas": offers}])
    return parse_listing(html, "Casas Bahia", "https://www.casasbahia.com.br", URL), ids, count


def test_bsr_excluded_furniture_mismatch_does_not_lose_tv():
    furniture = product(200, 20)
    furniture["title"] = "Rack para TV 65 Polegadas Madeira"
    rows, ids, count = parsed([furniture, product()], [offer(), offer(201, 20)])
    assert count == 1 and ids == ["200", "100"]
    assert rows[0]["bsr_rank"] == "" and rows[0]["main_rank"] == 1
    assert rows[0]["final_sku_price"]


def test_pending_quote_survives_output_as_db_null():
    rows, _, _ = parsed([product()], [offer(999)])
    row = rows[0]
    assert row["product_url"].endswith("/100")
    assert row["final_sku_price"] == "" and mode3_prices.pending(row)
    # Later unverified backfills cannot undo quarantine.
    row.update(final_sku_price="999", original_sku_price="1000", savings="1")
    final = _format_row(row, datetime.now())
    for key in ("final_sku_price", "original_sku_price", "savings"):
        assert final[key] == "" and _db_value(key, final[key]) is None


def test_equal_price_needs_independent_exact_quote_and_same_terms():
    source = product(seller=7)
    source["price"] = {"productId": "10", "skuId": "100", "sellerId": "7",
                       "currentPrice": 900, "standardPrice": 900,
                       "discountDescription": None, "installment": None}
    rows, _, _ = parsed([source], [offer(999)])
    assert rows[0]["final_sku_price"] and rows[0]["product_url"].endswith("/100")
    for price, seller in ((901, 7), (900, 8)):
        rows, _, _ = parsed([source], [offer(999, seller=seller, price=price)])
        assert rows[0]["final_sku_price"] == ""
    rows, _, _ = parsed([product()], [offer(999)])
    assert rows[0]["final_sku_price"] == ""


def test_repeated_sku_is_still_rejected():
    with pytest.raises(Exception, match="duplicate_listing_sku"):
        parsed([product(), product()], [offer()])


def test_wait60_only_after_two_listing_403s():
    home = {"success": True, "status_code": 200, "trace": [{}]}
    blocked = {"success": False, "status_code": 403, "trace": [{}]}
    good = dict(home, document_frame={"id": "x", "loaderId": "y"})
    with patch.object(document, "_navigate", side_effect=[home, blocked, blocked, good]) as nav, patch.object(document.time, "sleep") as sleep:
        result = document.prepare(Mock(), URL)
    assert result["success"] and result["document_navigations"] == 4 and nav.call_count == 4
    assert [c.args[0] for c in sleep.call_args_list] == [5, 3, 30, 30]
    with patch.object(document, "_navigate", return_value=blocked) as nav:
        assert not document.prepare(Mock(), URL)["success"]
        assert nav.call_count == 1


def test_browser_reused_and_closed_without_restarting_each_request():
    session = recovery._Session()
    with patch("seda.casas_bahia.browser_probe_session.ProbeBrowserSession") as browser, patch.object(
        document, "_navigate", return_value={"success": True, "document_frame": {}}), patch.object(
        recovery.time, "sleep"), patch.object(browser_api, "_browser_fetch", return_value=({"status_code": 200}, {"ok": 1})):
        for _ in range(3):
            assert session.fetch(detail_api.PRODUCT_SOURCE_URL, {}, "GET", {}, None)["success"]
        browser.assert_called_once()
        session.close()
        browser.return_value.close.assert_called_once()


def test_browser_failures_open_circuit():
    session = recovery._Session()
    session.browser, session.frame = Mock(), {}
    with patch.object(browser_api, "_browser_fetch", return_value=({"status_code": 403, "error": "blocked"}, None)) as fetch:
        for _ in range(4):
            result = session.fetch(detail_api.PRODUCT_SOURCE_URL, {}, "GET", {}, None)
    assert fetch.call_count == 3 and result["error"] == "browser_recovery_circuit_open"
    with patch.object(browser_api, "_browser_fetch", return_value=({"status_code": 200}, {"ok": 1})) as fetch:
        assert session.fetch(detail_api.RECS_API, {}, "POST", {}, {}, "recs")["success"]
        fetch.assert_called_once()


def test_listing_retry_stops_after_final_403_and_does_not_retry_other_errors():
    home = {"success": True, "status_code": 200, "trace": [{}]}
    blocked = {"success": False, "status_code": 403, "trace": [{}]}
    with patch.object(document, "_navigate", side_effect=[home, blocked, blocked, blocked]) as nav, patch.object(document.time, "sleep"):
        result = document.prepare(Mock(), URL)
        assert not result["success"] and nav.call_count == 4
    with patch.object(document, "_navigate", side_effect=[home, dict(blocked, status_code=500)]) as nav, patch.object(document.time, "sleep"):
        assert not document.prepare(Mock(), URL)["success"]
        assert nav.call_count == 2


def test_other_modes_never_enter_browser_recovery(monkeypatch):
    for mode in ("1", "1-1", "2", "4"):
        monkeypatch.setenv("SEDA_CASAS_BAHIA_LISTING_MODE", mode)
        assert not recovery.fetch(detail_api.PRODUCT_SOURCE_URL)["success"]
        assert recovery._SESSION is None


def test_product_source_zenrows_then_browser_identity_check():
    good = {"product": {"id": 10, "name": "Smart TV", "description": "TV description"}, "sku": {"id": 100}}
    with patch.object(detail_api, "_fetch_product_source_zenrows", return_value={"success": False, "error": "http_403"}), patch.object(
        recovery, "fetch", return_value={"success": True, "data": good, "method": "browser"}) as browser:
        assert detail_api.fetch_product_source("100")["success"]
        assert browser.call_count == 1
        assert not detail_api.fetch_product_source("101")["success"]


def page(number, texts, last=False):
    return {"success": True, "method": "browser", "data": {"review": {
        "page": number, "userReviews": [{"id": f"{number}-{i}", "text": text} for i, text in enumerate(texts)],
        "lastPage": last, "aiSummary": {"aiSummaryText": "Official summary"}}}}


def test_review_browser_pagination_and_summary():
    with patch.object(review_api, "_fetch_review_page_direct", return_value={"success": False}), patch.object(
        review_api, "_fetch_review_page_zenrows", return_value={"success": False}), patch.object(
        recovery, "fetch", side_effect=[page(1, ["good"] * 18 + ["", ""]), page(2, ["more"] * 2, True)]) as fetch:
        result = review_api.fetch_reviews("10")
    assert result["success"] and len(result["reviews"]) == 20
    assert result["summary"] == "Official summary" and fetch.call_count == 2


def test_review_later_failure_retains_valid_summary_and_reviews():
    with patch.object(review_api, "_fetch_review_page_direct", return_value={"success": False}), patch.object(
        review_api, "_fetch_review_page_zenrows", return_value={"success": False}), patch.object(
        recovery, "fetch", side_effect=[page(1, ["good"] * 18 + ["", ""]), {"success": False, "error": "blocked"}]):
        result = review_api.fetch_reviews("10")
    assert not result["success"] and result["partial_validated"] and len(result["reviews"]) == 18
    assert result["general"]["summary"] == "Official summary"


def test_existing_reviews_do_not_skip_missing_summary(monkeypatch):
    for name in ("PRODUCT_SOURCE", "FREIGHT", "PICKUP", "RECS"):
        monkeypatch.setenv("SEDA_CASAS_BAHIA_" + name + "_API", "0")
    row = {"retailer": "Casas Bahia", "retailer_product_id": "10", "product_url": URL,
           "detailed_review_content": json.dumps(["old"] * 20)}
    with patch.object(review_api, "fetch_reviews", return_value={"success": True, "reviews": ["new"], "summary": "Official summary"}) as fetch:
        _merge_casas_bahia_apis(row)
    assert fetch.call_args.kwargs["limit"] == 1
    assert len(json.loads(row["detailed_review_content"])) == 20
    assert row["summarized_review_content"] == "Official summary"


def test_discount_positive_empty_invalid_and_missing_seller():
    with patch.object(destaque_api.requests, "Session") as factory, patch.object(recovery, "fetch") as fetch:
        factory.return_value.__enter__.return_value.get.return_value.status_code = 403
        fetch.return_value = {"success": True, "data": {"value": {"destaques": [{"titulo": "Use o cupom 10%"}]}}, "method": "browser"}
        assert destaque_api.fetch_discount_type("100", "7")["discount_type"] == "USE O CUPOM DESCONTO 10%"
        fetch.return_value["data"]["value"]["destaques"] = []
        empty = destaque_api.fetch_discount_type("100", "7")
        assert empty["success"] and not empty["discount_type"]
        fetch.return_value["data"] = {}
        assert not destaque_api.fetch_discount_type("100", "7")["success"]
        assert factory.call_count == 1
    assert badges._seller_id({}) == ""


def test_recommendations_browser_retains_real_titles_and_distinguishes_empty():
    with patch.object(detail_api.requests, "Session") as factory, patch.object(recovery, "fetch") as fetch:
        factory.return_value.post.return_value.status_code = 403
        fetch.return_value = {"success": True, "method": "browser", "data": {"data": {"products": [{"title": "Smart TV LG"}]}}}
        result = detail_api.fetch_similar_names("10", sku_id="100")
        assert result["names"] == ["Smart TV LG"] and result["method"] == "browser"
        fetch.return_value["data"]["data"]["products"] = []
        assert detail_api.fetch_similar_names("10")["success"]
        fetch.return_value["data"] = {}
        assert not detail_api.fetch_similar_names("10")["success"]


def test_discount_failure_stop_keeps_all_input_rows(tmp_path):
    from seda.step00_config import write_csv, read_csv
    from types import SimpleNamespace
    source, output = tmp_path / "input.csv", tmp_path / "output.csv"
    rows = [{"retailer_sku_id": str(i), "seller_id": "7", "discount_type": "",
             "parse_status": "", "fetch_method": ""} for i in range(1, 7)]
    write_csv(source, rows, columns=list(rows[0]))
    args = SimpleNamespace(input=str(source), output=str(output), limit=0, force=False, timeout=0)
    with patch.object(badges, "fetch_discount_type", return_value={"success": False, "error": "blocked"}) as fetch:
        manifest = badges.run(args)
    assert fetch.call_count == 3
    assert manifest["stats"]["not_attempted_after_stop"] == 3
    assert len(read_csv(output)) == 6


def test_review_repeated_page_is_partial_not_infinite():
    response = page(1, ["body"] * 18 + ["", ""])
    repeated = deepcopy(response)
    repeated["data"]["review"]["page"] = 2
    with patch.object(review_api, "_fetch_review_page_direct", return_value={"success": False}), patch.object(
        review_api, "_fetch_review_page_zenrows", return_value={"success": False}), patch.object(
        recovery, "fetch", side_effect=[response, repeated]) as fetch:
        result = review_api.fetch_reviews("10")
    assert result["error"] == "review_page_repeated" and result["partial_validated"]
    assert fetch.call_count == 2
