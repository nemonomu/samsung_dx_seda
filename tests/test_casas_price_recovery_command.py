"""Standalone recovery: historical metadata, exact identity and DB update boundaries."""
from copy import deepcopy
import json
from unittest.mock import MagicMock, Mock

import pytest

from seda.casas_bahia import price_recovery as recovery
from seda.step15_final_output import final_output_columns


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("SEDA_PRODUCT_LINE", "REF")
    monkeypatch.setenv("SEDA_ACTIVE_RETAILER", "casas_bahia")
    monkeypatch.setenv("SEDA_CASAS_BAHIA_LISTING_MODE", "3")


def final_row(line="REF", **changes):
    row = dict.fromkeys(final_output_columns(line), "")
    row.update(country="SEDA", product=line, item="55065274", sku="GN-B392PXGB",
               account_name="Casas Bahia", retailer_sku_name="LG refrigerator",
               product_url="https://www.casasbahia.com.br/refrigerator/p/55065274",
               crawl_strdatetime="2026-11-01 06:00:00", batch_id="c_20261101_060000",
               main_rank="3", calendar_week="w44", summarized_review_content="keep review")
    row.update(changes)
    return row


def target(line="REF", seller="10037", **changes):
    row = {"retailer": "Casas Bahia", "product_line": line, "retailer_product_id": "74187836",
           "product_url": final_row(line)["product_url"], "seller_id": seller, "main_rank": "3"}
    row.update(changes)
    return row


def offer(**changes):
    result = {"productId": "74187836", "skuId": "55065274", "sellerId": "10037",
              "oldPrice": 2899.9, "standardPrice": 2599, "currentPrice": 2399,
              "availability": {}, "discountDescription": "PIX", "savings": "Baixou 17%"}
    result.update(changes)
    return result


def reply(*offers):
    return {"success": True, "status_code": 200, "offers": list(offers)}


class MemoryStore:
    def __init__(self, row):
        self.prices = {f: row[f] for f in recovery.PRICE_FIELDS}
        self.updates = []

    def snapshot(self, row):
        return dict(self.prices), ""

    def update(self, row, before, changes):
        assert before == self.prices
        assert set(changes) <= set(recovery.PRICE_FIELDS)
        self.updates.append((deepcopy(row), dict(changes)))
        self.prices.update(changes)
        return "updated"


@pytest.mark.parametrize("line", ["TV", "REF", "LDY"])
def test_recovers_prices_only_preserves_original_time_and_all_other_columns(tmp_path, line):
    row = final_row(line)
    original = deepcopy(row)
    store = MemoryStore(row)
    stats, reports = recovery.recover_rows([row], [target(line)], tmp_path, store=store,
                                         fetcher=Mock(return_value=reply(offer())))
    saved = recovery.config.read_csv(tmp_path / "final_output_recovered.csv")[0]
    assert row == original
    assert {f: saved[f] for f in saved if f not in recovery.PRICE_FIELDS} == {
        f: original[f] for f in original if f not in recovery.PRICE_FIELDS}
    assert saved["final_sku_price"] == "R$2.399,00" and saved["original_sku_price"] == "R$2.899,90"
    assert stats["updated"] == 1 and len(store.updates) == 1
    assert "crawl_strdatetime" not in store.updates[0][1]
    assert reports[0]["quoted_prices"] == {"원가": "2899.9", "일반 판매가": "2599", "최종 결제가격": "2399"}


def test_original_price_equal_to_final_stays_blank_as_existing_contract(tmp_path):
    row = final_row()
    stats, _ = recovery.recover_rows([row], [target()], tmp_path,
        fetcher=Mock(return_value=reply(offer(oldPrice=2399, standardPrice=2399))))
    saved = recovery.config.read_csv(tmp_path / "final_output_recovered.csv")[0]
    assert saved["original_sku_price"] == "" and saved["final_sku_price"] == "R$2.399,00"
    assert stats["csv_only"] == 1


def test_discovery_other_option_cannot_fill_original_price_and_log_has_human_identity(tmp_path, capsys):
    row = final_row()
    store = MemoryStore(row)
    fetch = Mock(side_effect=[reply(offer(skuId="55065275")), reply()])
    stats, reports = recovery.recover_rows([row], [target(seller="")], tmp_path, store=store, fetcher=fetch)
    log = capsys.readouterr().out
    assert "[사람 확인 필요]" in log and "다른 SKU" in log and "가격 항목 없음" in log
    assert "item: 55065274" in log and "sku: GN-B392PXGB" in log and row["product_url"] in log
    assert reports[0]["candidate_skus"] == ["55065275"] and reports[0]["target_sku"] == "55065274"
    assert not store.updates and stats["review"] == 1
    assert recovery.config.read_csv(tmp_path / "final_output_recovered.csv")[0] == row


def test_new_seller_is_verified_but_not_written_as_unrequested_db_field(tmp_path):
    row = final_row()
    fetch = Mock(side_effect=[reply(offer(sellerId="24506", skuId="55065275")), reply(offer(sellerId="24506"))])
    store = MemoryStore(row)
    stats, _ = recovery.recover_rows([row], [target(seller="")], tmp_path, store=store, fetcher=fetch)
    assert stats["updated"] == 1 and fetch.call_args_list[1].args[0][0]["lojista"] == "24506"
    assert "seller_id" not in store.updates[0][1]


def test_repeat_run_checks_db_and_does_not_query_or_update_twice(tmp_path):
    row = final_row()
    store = MemoryStore(row)
    fetch = Mock(return_value=reply(offer()))
    recovery.recover_rows([row], [target()], tmp_path / "first", store=store, fetcher=fetch)
    stats, _ = recovery.recover_rows([row], [target()], tmp_path / "second", store=store, fetcher=fetch)
    assert fetch.call_count == 1 and len(store.updates) == 1 and stats["already_present"] == 1


@pytest.mark.parametrize("error", ["db_row_missing", "db_row_ambiguous"])
def test_missing_or_duplicate_db_row_does_not_collect_or_insert(tmp_path, error):
    store, fetch = Mock(), Mock()
    store.snapshot.return_value = (None, error)
    stats, reports = recovery.recover_rows([final_row()], [target()], tmp_path, store=store, fetcher=fetch)
    fetch.assert_not_called()
    store.update.assert_not_called()
    assert stats["review"] == 1 and reports[0]["error"] == error


@pytest.mark.parametrize("targets", [[], [target(), target()], [target(retailer_product_id="")]])
def test_ambiguous_or_invalid_original_identity_not_guessed(tmp_path, targets):
    fetch = Mock()
    stats, _ = recovery.recover_rows([final_row()], targets, tmp_path, fetcher=fetch)
    assert stats["review"] == 1
    fetch.assert_not_called()


def test_nonempty_values_and_unrelated_fields_are_not_overwritten(tmp_path):
    row = final_row(original_sku_price="R$2.899,90", discount_type="existing discount")
    store = MemoryStore(row)
    recovery.recover_rows([row], [target()], tmp_path, store=store, fetcher=Mock(return_value=reply(offer())))
    assert "original_sku_price" not in store.updates[0][1] and "discount_type" not in store.updates[0][1]


def test_db_commit_failure_keeps_proposed_csv_and_logs_no_success(tmp_path, capsys):
    row = final_row()
    store = MemoryStore(row)
    store.update = Mock(side_effect=RuntimeError("private connection marker"))
    stats, reports = recovery.recover_rows([row], [target()], tmp_path, store=store, fetcher=Mock(return_value=reply(offer())))
    assert stats["db_update_failed"] == 1 and not stats["updated"]
    assert recovery.config.read_csv(tmp_path / "final_output_recovered.csv")[0]["final_sku_price"] == "R$2.399,00"
    log = capsys.readouterr().out
    assert "[복구 성공]" not in log and "private connection marker" not in log + json.dumps(reports)


def connection(prices, rowcount=1):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [tuple(prices[f] for f in recovery.PRICE_FIELDS)]
    cursor.rowcount = rowcount
    return conn, cursor


def test_db_sql_updates_only_allowlisted_prices_and_uses_original_batch_time():
    row = final_row()
    before = {f: None for f in recovery.PRICE_FIELDS}
    conn, cursor = connection(before)
    store = recovery.PriceStore(conn, "public.ref_retail_com_seda")
    assert store.update(row, before, {"final_sku_price": "R$2.399,00"}) == "updated"
    sql, params = cursor.execute.call_args.args
    assert sql.startswith('UPDATE "public"."ref_retail_com_seda" SET "final_sku_price" = %s WHERE')
    assert '"crawl_strdatetime" IS NOT DISTINCT FROM %s' in sql
    assert row["crawl_strdatetime"] in params and row["batch_id"] in params
    assert any("FOR UPDATE" in call.args[0] for call in cursor.execute.call_args_list)
    conn.__exit__.assert_called_with(None, None, None)


def test_db_rejects_metadata_update_and_invalid_table():
    with pytest.raises(recovery.RecoveryError):
        recovery.PriceStore(Mock(), "table;DELETE")
    store = recovery.PriceStore(Mock(), "ref_retail_com_seda")
    with pytest.raises(recovery.RecoveryError, match="invalid_update_fields"):
        store.update(final_row(), {}, {"crawl_strdatetime": "new"})


def test_db_concurrent_price_change_skips_update():
    before = {f: None for f in recovery.PRICE_FIELDS}
    conn, cursor = connection(dict(before, final_sku_price="R$1,00"))
    result = recovery.PriceStore(conn, "ref_retail_com_seda").update(final_row(), before, {"final_sku_price": "R$2.399,00"})
    assert result == "db_changed_during_recovery"
    assert not any(c.args[0].startswith("UPDATE") for c in cursor.execute.call_args_list)


@pytest.mark.parametrize("count", [0, 2])
def test_db_wrong_affected_count_raises_inside_transaction_for_rollback(count):
    before = {f: None for f in recovery.PRICE_FIELDS}
    conn, cursor = connection(before, rowcount=count)
    with pytest.raises(recovery.RecoveryError, match="db_update_count_mismatch"):
        recovery.PriceStore(conn, "ref_retail_com_seda").update(final_row(), before, {"final_sku_price": "R$2.399,00"})
    assert conn.__exit__.call_args.args[0] is recovery.RecoveryError


def test_cli_csv_only_writes_log_and_preserves_source_files(tmp_path, monkeypatch):
    row = final_row()
    root = tmp_path / "data" / "casas_bahia" / "ref" / "20261101"
    final = root / "output" / "final_output.csv"
    targets = root / "output" / "seda_final_targets.csv"
    recovery.save_csv(final, [row], list(row))
    recovery.save_csv(targets, [target()], list(target()))
    before = (final.read_bytes(), targets.read_bytes())
    monkeypatch.setattr(recovery.config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(recovery.config, "DEFAULT_RUNS_BASE", tmp_path / "data")
    monkeypatch.setattr(recovery.config, "load_env", lambda: None)
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(recovery.config, "db_connect", connect)
    fetch = Mock(return_value=reply(offer()))
    monkeypatch.setattr(recovery, "BrowserPriceFetcher", lambda: fetch)
    assert recovery.main(["REF", "20261101", "--csv-only"]) == 0
    assert before == (final.read_bytes(), targets.read_bytes())
    log = next((tmp_path / "seda" / "casas_bahia" / "log").glob("casas_bahia_price_recovery_REF_*.log"))
    assert "최초 수집 시간(유지): 2026-11-01 06:00:00" in log.read_text(encoding="utf-8")
    assert "CSV에만 복구" in log.read_text(encoding="utf-8")
    connect.assert_not_called()
    fetch.close.assert_called_once()


def test_cli_rejects_invalid_date_before_loading_env(monkeypatch):
    load = Mock()
    monkeypatch.setattr(recovery.config, "load_env", load)
    with pytest.raises(SystemExit):
        recovery.main(["REF", "20260230"])
    load.assert_not_called()


def test_menu_lists_each_batch_with_full_id_and_counts_only_null_prices(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(recovery.config, "DEFAULT_RUNS_BASE", tmp_path)
    root = tmp_path / "casas_bahia" / "ref" / "20261101"
    rows = [final_row(), final_row(final_sku_price="R$100,00"),
            final_row(batch_id="c_second", crawl_strdatetime="2026-11-01 07:00:00"),
            final_row(account_name="Magalu", batch_id="wrong_retailer")]
    recovery.save_csv(root / "output" / "final_output.csv", rows, list(rows[0]))
    recovery.save_csv(root / "recovery" / "price" / "copy" / "final_output_recovered.csv", rows, list(rows[0]))
    choices = recovery.available_batches()
    assert len(choices) == 2 and [b["missing"] for b in choices] == [1, 1]
    assert choices[0]["batch_id"] == "c_second"
    monkeypatch.setattr("builtins.input", Mock(return_value="2"))
    selected = recovery.choose_batch()
    assert selected["batch_id"] == "c_20261101_060000"
    output = capsys.readouterr().out
    assert "배치 ID" in output and "c_20261101_060000" in output and "2026-11-01 06:00:00" in output
    assert "CSV 기준" in output and "바로 복구 후 DB에 반영" in output


def test_interactive_selection_immediately_updates_only_chosen_batch(tmp_path, monkeypatch, capsys):
    row = final_row()
    other = final_row(batch_id="c_other", crawl_strdatetime="2026-11-01 07:00:00")
    root = tmp_path / "data" / "casas_bahia" / "ref" / "20261101"
    path = root / "output" / "final_output.csv"
    recovery.save_csv(path, [row, other], list(row))
    recovery.save_csv(root / "output" / "seda_final_targets.csv", [target()], list(target()))
    before = path.read_bytes()
    monkeypatch.setattr(recovery.config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(recovery.config, "DEFAULT_RUNS_BASE", tmp_path / "data")
    monkeypatch.setattr(recovery.config, "load_env", lambda: None)
    connection_mock = Mock()
    monkeypatch.setattr(recovery.config, "db_connect", lambda: connection_mock)
    store = MemoryStore(row)
    monkeypatch.setattr(recovery, "PriceStore", lambda *_: store)
    fetch = Mock(return_value=reply(offer()))
    monkeypatch.setattr(recovery, "BrowserPriceFetcher", lambda: fetch)
    prompt = Mock(return_value="2")
    monkeypatch.setattr("builtins.input", prompt)
    assert recovery.main([]) == 0
    prompt.assert_called_once_with("복구할 번호: ")
    assert len(store.updates) == 1 and store.updates[0][0]["batch_id"] == row["batch_id"]
    assert path.read_bytes() == before
    saved = recovery.config.read_csv(next((root / "recovery").glob("price/*/final_output_recovered.csv")))
    assert len(saved) == 1 and saved[0]["batch_id"] == row["batch_id"]
    assert saved[0]["crawl_strdatetime"] == row["crawl_strdatetime"]
    connection_mock.close.assert_called_once()


def test_menu_cancel_and_no_batches_never_connect_or_start_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.config, "DEFAULT_RUNS_BASE", tmp_path)
    connect, browser, prompt = Mock(), Mock(), Mock(return_value="0")
    monkeypatch.setattr(recovery.config, "db_connect", connect)
    monkeypatch.setattr(recovery, "BrowserPriceFetcher", browser)
    monkeypatch.setattr("builtins.input", prompt)
    assert recovery.main([]) == 0
    prompt.assert_not_called()
    row = final_row()
    recovery.save_csv(tmp_path / "casas_bahia" / "ref" / "20261101" / "output" / "final_output.csv", [row], list(row))
    assert recovery.main([]) == 0
    prompt.assert_called_once()
    connect.assert_not_called()
    browser.assert_not_called()
