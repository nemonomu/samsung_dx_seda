"""Offline original-listing seller recovery, publication and browser contracts."""
from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from seda import step07_final_targets as targets, step08_detail_enrichment as detail
from seda.casas_bahia import mode3_price_recovery as recovery, mode3_prices
from seda.casas_bahia import browser_api, price_api
from seda.step00_config import read_csv, write_csv, OUTPUT_COLUMNS
from seda.step15_final_output import _format_row
from seda.step14_db_load import _db_value


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("SEDA_CASAS_BAHIA_LISTING_MODE", "3")
    monkeypatch.setenv("SEDA_PRODUCT_LINE", "TV")
    monkeypatch.setenv("SEDA_CASAS_BAHIA_API_ENRICH", "1")
    monkeypatch.setattr(price_api, "_headers", lambda: {})
    monkeypatch.setattr(price_api, "_params", lambda: {"cep": "01010010"})
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)


def row(seller="22161", origin="main", **changes):
    result = {"retailer": "Casas Bahia", "product_line": "TV", "retailer_product_id": "87459673",
              "product_url": "https://www.casasbahia.com.br/smart-tv-hisense/p/55071153",
              "sku": "65Q6QV", "seller_id": seller, "final_sku_price": "", "original_sku_price": "",
              "savings": "", "discount_type": "", "main_rank": "20" if origin == "main" else "",
              "bsr_rank": "5" if origin == "bsr" else "", "parse_status":
              "listing_casas_bahia_partner_api+casas_mode3_price_pending+casas_listing_price_pending"}
    result.update(changes)
    return result


def offer(seller="22161", price=3558.9, **changes):
    result = {"productId": "87459673", "skuId": "55071153", "sellerId": seller,
              "currentPrice": price, "standardPrice": price, "oldPrice": None,
              "availability": {"IdSku": "55071153", "IdLojista": seller}}
    result.update(changes)
    return result


def reply(*offers):
    return {"success": True, "status_code": 200, "offers": list(offers)}


def test_main_and_bsr_keep_their_seller_and_distinct_prices():
    rows = [row(bsr_rank="5"), row("10037", "bsr")]
    fetch = Mock(return_value=reply(offer("10037", 3779.11), offer()))
    trace = []
    detail._backfill_casas_listing_prices(rows, fetcher=fetch, trace_rows=trace)
    assert [r["final_sku_price"] for r in rows] == ["R$3.558,90", "R$3.779,11"]
    assert [r["seller_id"] for r in rows] == ["22161", "10037"]
    assert fetch.call_count == 2
    assert [c.args[0][0]["lojista"] for c in fetch.call_args_list] == ["22161", "10037"]
    assert "origin:main;listing_seller:22161" in trace[0]["detail"]
    assert "origin:bsr;listing_seller:10037" in trace[1]["detail"]
    assert all(not mode3_prices.pending(r) for r in rows)


def test_merge_main_priority_and_bsr_only_provenance_are_preserved():
    main = row()
    same_bsr = row("10037", "bsr", final_sku_price="R$3.779,11", parse_status="listing")
    bsr_only = row("10037", "bsr", product_url="https://www.casasbahia.com.br/tv/p/55071154")
    original_main, original_bsr = deepcopy(main), deepcopy(bsr_only)
    with patch.object(targets, "read_csv", side_effect=[[main], [same_bsr, bsr_only], [same_bsr, bsr_only]]), \
            patch.object(targets, "write_csv") as write:
        targets.main()
    merged = write.call_args.args[1]
    assert len(merged) == 2
    assert merged[0]["seller_id"] == original_main["seller_id"] and merged[0]["final_sku_price"] == ""
    assert merged[0]["main_rank"] == "20" and merged[0]["bsr_rank"] == "5"
    assert merged[1] == original_bsr


def test_recovery_survives_checkpoint_csv_reload_final_output_and_db_conversion(tmp_path):
    rows, trace = [row()], []
    checkpoint = tmp_path / "enriched.csv"
    detail._backfill_casas_listing_prices(rows, fetcher=Mock(return_value=reply(offer())), trace_rows=trace,
        checkpoint_every=1, checkpoint_writer=lambda values: write_csv(checkpoint, values, columns=OUTPUT_COLUMNS))
    saved = read_csv(checkpoint)[0]
    assert not mode3_prices.pending(saved) and "casas_listing_price_pending" not in saved["parse_status"]
    output = _format_row(saved, datetime(2026, 10, 30))
    assert output["final_sku_price"] == "R$3.558,90"
    assert _db_value("final_sku_price", output["final_sku_price"]) is not None
    fetch = Mock()
    detail._backfill_casas_listing_prices([saved], fetcher=fetch)
    fetch.assert_not_called()


@pytest.mark.parametrize("bad", [offer("10037", 3779.11), offer(skuId="999"), offer(productId="999"),
                                   offer(currentPrice=0), offer(availability={"IdLojista": "10037"})])
def test_wrong_identity_or_invalid_price_cannot_release_pending(bad):
    source = row()
    detail._backfill_casas_listing_prices([source], fetcher=Mock(return_value=reply(bad)))
    assert source["seller_id"] == "22161" and source["final_sku_price"] == ""
    assert mode3_prices.pending(source) and "casas_mode3_price_failed:" in source["parse_status"]
    assert _format_row(source, datetime(2026, 10, 30))["final_sku_price"] == ""


def test_unverified_backfill_price_is_replaced_only_after_exact_recovery():
    source = row(final_sku_price="999", original_sku_price="1000", savings="unknown")
    detail._backfill_casas_listing_prices([source], fetcher=Mock(return_value=reply(offer())))
    assert source["final_sku_price"] == "R$3.558,90" and source["original_sku_price"] == ""
    assert not mode3_prices.pending(source)


def test_duplicate_identity_cached_but_sellers_never_share_quotes():
    sources = [row(), row(), row("10037", "bsr")]
    fetch = Mock(return_value=reply(offer(), offer("10037", 3779.11)))
    trace = []
    detail._backfill_casas_listing_prices(sources, fetcher=fetch, trace_rows=trace)
    assert fetch.call_count == 2
    assert [t["attempt"] for t in trace] == [1, 0, 1]


@pytest.mark.parametrize("changes", [{"seller_id": ""}, {"seller_id": "bad"}, {"main_rank": "", "bsr_rank": ""}])
def test_missing_listing_context_is_not_guessed(changes):
    source, fetch = row(**changes), Mock()
    detail._backfill_casas_listing_prices([source], fetcher=fetch)
    fetch.assert_not_called()
    assert mode3_prices.pending(source) and source["final_sku_price"] == ""


def test_complete_quotes_api_disabled_and_other_retailers_are_untouched(monkeypatch):
    sources = [row(final_sku_price="R$42,00", parse_status="listing"), row(retailer="Magalu")]
    original, fetch = deepcopy(sources), Mock()
    detail._backfill_casas_listing_prices(sources, fetcher=fetch)
    assert sources == original
    monkeypatch.setenv("SEDA_CASAS_BAHIA_API_ENRICH", "0")
    detail._backfill_casas_listing_prices([row()], fetcher=fetch)
    fetch.assert_not_called()


def test_transport_uses_mode3_browser_request_and_returns_all_seller_offers():
    session = Mock()
    raw = {"Ofertas": [{"PrecoVenda": {"IdProduto": 87459673, "IdSku": 55071153,
                         "IdLojista": seller, "Preco": price},
                        "Disponibilidade": {"IdSku": 55071153, "IdLojista": seller}}
                       for seller, price in ((10037, 3779.11), (22161, 3558.9))]}
    with patch.object(browser_api, "_APISession", return_value=session) as create, \
            patch.object(browser_api, "_browser_fetch", return_value=({"status_code": 200}, raw)) as api, \
            patch.object(price_api, "fetch_listing_prices", side_effect=AssertionError("no direct REST")):
        fetcher = recovery.BrowserPriceFetcher()
        try:
            for seller in (22161, 10037):
                result = fetcher([{"id": 87459673, "sku": 55071153, "lojista": seller}])
                assert result["success"] and len(result["offers"]) == 2
                args = api.call_args.args
                assert args[1] == price_api.PRICE_URL and args[2]["IdRegiao"] == "126000"
                assert args[2]["cep"] == "01010010" and args[3] == "POST" and args[6] == 25
                assert args[5]["skus"] == [{"idLojista": seller, "idSku": 55071153}]
            create.assert_called_once()
            session._bootstrap.assert_called_once_with("https://www.casasbahia.com.br/tv/b", 45)
        finally:
            fetcher.close()
        session.close.assert_called_once()


def test_browser_stops_after_three_network_failures_and_does_not_retry_start():
    with patch.object(browser_api, "_APISession") as create, \
            patch.object(browser_api, "_browser_fetch", return_value=({"status_code": 403}, None)) as api:
        fetch = recovery.BrowserPriceFetcher()
        for _ in range(5):
            assert not fetch([{"id": 1, "sku": 2, "lojista": 3}])["success"]
        assert api.call_count == 3
        fetch.close()
        create.return_value._bootstrap.side_effect = RuntimeError("private-marker")
        fetch = recovery.BrowserPriceFetcher()
        for _ in range(2):
            assert not fetch([{"id": 1, "sku": 2, "lojista": 3}])["success"]
        assert api.call_count == 3
        fetch.close()


def test_owned_browser_closes_if_checkpoint_fails():
    with patch.object(recovery, "BrowserPriceFetcher") as create:
        create.return_value.return_value = reply(offer())
        with pytest.raises(RuntimeError):
            detail._backfill_casas_listing_prices([row()], checkpoint_every=1,
                checkpoint_writer=Mock(side_effect=RuntimeError("checkpoint failed")))
        create.return_value.close.assert_called_once()


def test_exception_is_safe_and_failure_remains_null(capsys):
    source, trace = row(), []
    detail._backfill_casas_listing_prices([source], fetcher=Mock(side_effect=RuntimeError("private-marker")), trace_rows=trace)
    assert source["final_sku_price"] == "" and mode3_prices.pending(source)
    assert "private-marker" not in str(trace) + str(source) + capsys.readouterr().out


def test_failed_price_request_does_not_skip_next_row_or_final_checkpoint():
    sources, snapshots = [row(), row("10037", "bsr")], []
    fetch = Mock(side_effect=[RuntimeError("private-marker"), reply(offer("10037", 3779.11))])
    result = detail._backfill_casas_listing_prices(sources, fetcher=fetch, checkpoint_every=25,
        checkpoint_writer=lambda values: snapshots.append(deepcopy(values)))
    assert result is sources and fetch.call_count == 2
    assert mode3_prices.pending(sources[0]) and sources[0]["final_sku_price"] == ""
    assert sources[1]["final_sku_price"] == "R$3.779,11" and not mode3_prices.pending(sources[1])
    assert len(snapshots) == 1 and snapshots[0] == sources


def test_row_update_exception_is_atomic_and_next_row_still_recovers():
    sources, trace = [row(), row("10037", "bsr")], []
    original_append = detail._append_token
    calls = 0

    def append(value, token):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("private-marker")
        return original_append(value, token)

    with patch.object(detail, "_append_token", side_effect=append):
        detail._backfill_casas_listing_prices(sources,
            fetcher=Mock(return_value=reply(offer(), offer("10037", 3779.11))), trace_rows=trace)
    assert sources[0]["final_sku_price"] == "" and mode3_prices.pending(sources[0])
    assert "casas_mode3_price_recovered" not in sources[0]["parse_status"]
    assert trace[0]["error"] == "price_recovery_error"
    assert sources[1]["final_sku_price"] == "R$3.779,11" and trace[1]["success"] == 1


def test_log_and_trace_failures_do_not_interrupt_rows_or_checkpoint():
    sources, checkpoint = [row(), row("10037", "bsr")], Mock()
    with patch("builtins.print", side_effect=OSError("private-marker")), \
            patch.object(detail, "_record_subcall", side_effect=OSError("private-marker")):
        result = detail._backfill_casas_listing_prices(sources,
            fetcher=Mock(return_value=reply(offer(), offer("10037", 3779.11))),
            checkpoint_writer=checkpoint, checkpoint_every=1)
    assert result is sources and checkpoint.call_count == 2
    assert [r["final_sku_price"] for r in sources] == ["R$3.558,90", "R$3.779,11"]


def test_owned_browser_close_failure_does_not_replace_successful_result():
    sources, checkpoint = [row()], Mock()
    with patch.object(recovery, "BrowserPriceFetcher") as create:
        create.return_value.return_value = reply(offer())
        create.return_value.close.side_effect = RuntimeError("private-marker")
        with patch("builtins.print", side_effect=OSError("private-marker")):
            result = detail._backfill_casas_listing_prices(sources, checkpoint_writer=checkpoint)
        create.return_value.close.assert_called_once()
    assert result is sources and not mode3_prices.pending(sources[0])
    checkpoint.assert_called_once_with(sources)


def test_browser_close_and_its_error_log_are_both_nonfatal():
    fetcher = recovery.BrowserPriceFetcher()
    session = fetcher.session = Mock()
    session.close.side_effect = RuntimeError("private-marker")
    with patch("builtins.print", side_effect=OSError("private-marker")):
        fetcher.close()
        fetcher.close()
    session.close.assert_called_once()
    assert fetcher.session is None
