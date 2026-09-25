"""Mode 1-1 price/source identity regression; synthetic offline inputs only."""

from copy import deepcopy
from contextlib import ExitStack
import inspect
import io
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

_DISABLED_ENV = Path(__file__).with_name("MODE11_TEST_ENV_DOES_NOT_EXIST")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ["SEDA_ENV_PATH"] = str(_DISABLED_ENV)

from seda import step08_detail_enrichment as enrichment
from seda.casas_bahia import detail_api, price_api, rest_detail_price_recovery as recovery
from seda.detail_publish import assert_detail_publish_complete
import test_casas_bahia_detail_price_recovery as legacy

row, offer, successful = legacy.row, legacy.offer, legacy.successful
CANARY = "SYNTHETIC_UNTRUSTED_RESPONSE_NEVER_EXPORT"
_ACTUAL_CASAS_API_MERGE = enrichment._merge_casas_bahia_apis


def complete_row(**changes):
    source = row(final_sku_price="R$850,00", original_sku_price="R$1.200,00",
                 savings="Baixou 25%", discount_type="Cupom existente")
    source.update(changes)
    return source


class Mode11QuoteTests(unittest.TestCase):
    def test_exact_quote_fills_all_source_fields_without_mutating_input(self):
        source = row(seller_id="")
        before = deepcopy(source)
        fetch = Mock(return_value=successful(offer(priceDescription="Desconto especial")))
        result = recovery.recover_rest_price_fields(source, fetcher=fetch)
        self.assertTrue(result["verified"])
        self.assertEqual(result["detail"], {"seller_id": "7", "final_sku_price": "R$850,00",
            "original_sku_price": "R$1.200,00", "savings": "Baixou 25%", "discount_type": "Desconto especial"})
        self.assertEqual(source, before)
        fetch.assert_called_once_with([{"id": "10"}], include_offers=True)

    def test_partial_original_savings_or_discount_is_candidate_even_with_final(self):
        for missing in ("original_sku_price", "savings", "discount_type"):
            source = complete_row(**{missing: ""})
            fetch = Mock(return_value=successful(offer(priceDescription="Desconto especial")))
            result = recovery.recover_rest_price_fields(source, fetcher=fetch)
            self.assertTrue(result["verified"])
            self.assertEqual(set(result["detail"]), {missing})
            fetch.assert_called_once()

    def test_complete_quote_and_other_retailer_do_not_call(self):
        fetch = Mock()
        for source in (complete_row(), row(retailer="Magalu")):
            self.assertFalse(recovery.recover_rest_price_fields(source, fetcher=fetch)["attempted"])
        fetch.assert_not_called()

    def test_missing_seller_with_complete_price_resolves_identity_without_overwriting(self):
        source = complete_row(seller_id="")
        result = recovery.recover_rest_price_fields(source, fetcher=Mock(return_value=successful()))
        self.assertEqual(result["detail"], {"seller_id": "7"})

    def test_source_absent_optional_values_are_verified_not_failed_or_invented(self):
        source = row(final_sku_price="R$850,00")
        quote = offer(oldPrice=None, standardPrice=None, discountRate=0, savings="")
        result = recovery.recover_rest_price_fields(source, fetcher=Mock(return_value=successful(quote)))
        self.assertTrue(result["verified"])
        self.assertEqual(result["detail"], {})
        self.assertEqual(result["quote_absent_fields"], ["original_sku_price", "savings", "discount_type"])

    def test_missing_offer_and_identity_conflicts_have_distinct_evidence(self):
        cases = [([], "missing_price_offer", (0, 0, 0)),
                 ([offer(productId=11)], "price_product_identity_conflict", (0, 0, 0)),
                 ([offer(skuId=101)], "price_sku_identity_conflict", (1, 0, 0)),
                 ([offer(sellerId=8)], "price_seller_identity_conflict", (1, 1, 0)),
                 ([offer(), offer()], "ambiguous_price_offers", (2, 2, 2))]
        for quotes, expected, counts in cases:
            result = recovery.recover_rest_price_fields(row(), fetcher=Mock(return_value={
                "success": True, "status_code": 200, "offers": quotes}))
            self.assertEqual(result["error"], expected)
            self.assertEqual(result["detail"], {})
            self.assertEqual(tuple(result["evidence"][key] for key in
                ("product_matches", "sku_matches", "seller_matches")), counts)

    def test_exact_url_sku_wins_over_other_sku_and_other_seller(self):
        result = recovery.recover_rest_price_fields(row(), fetcher=Mock(return_value=successful(
            offer(skuId=101, currentPrice=990), offer(sellerId=8, currentPrice=980), offer())))
        self.assertEqual(result["detail"]["final_sku_price"], "R$850,00")

    def test_unknown_seller_requires_singular_offer_never_default_or_cheapest(self):
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_DEFAULT_SELLER_ID": "10037"}):
            result = recovery.recover_rest_price_fields(row(seller_id=""), fetcher=Mock(
                return_value=successful(offer(), offer(sellerId=10037, currentPrice=100))))
        self.assertEqual(result["error"], "ambiguous_price_offers")
        self.assertEqual(result["detail"], {})

    def test_url_missing_or_external_and_invalid_ids_never_requested(self):
        fetch = Mock()
        for change in ({"product_url": "https://external.invalid/p/100"},
                       {"product_url": "https://www.casasbahia.com.br/tv"},
                       {"retailer_product_id": ""}, {"seller_id": "bad"}):
            self.assertFalse(recovery.recover_rest_price_fields(row(**change), fetcher=fetch)["attempted"])
        fetch.assert_not_called()

    def test_availability_conflict_rejected_with_safe_field_name(self):
        for field in ("IdProduto", "IdSku", "IdLojista"):
            result = recovery.recover_rest_price_fields(row(), fetcher=Mock(return_value=successful(
                offer(availability={field: 999}))))
            self.assertEqual(result["error"], "price_availability_identity_conflict")
            self.assertEqual(result["evidence"]["conflict_field"], field)

    def test_current_original_savings_conflicts_never_merge_seller_or_fields(self):
        for change, error in (({"final_sku_price": "R$851,00"}, "existing_current_price_quote_conflict"),
                              ({"original_sku_price": "R$1.300,00"}, "existing_original_price_quote_conflict"),
                              ({"savings": "Baixou 26%"}, "existing_savings_quote_conflict")):
            source = row(seller_id="", **change)
            before = deepcopy(source)
            result = recovery.recover_rest_price_fields(source, fetcher=Mock(return_value=successful()))
            self.assertEqual(result["error"], error)
            self.assertEqual(result["detail"], {})
            self.assertEqual(source, before)

    def test_raw_float_rounding_same_as_mode1_is_not_false_conflict(self):
        source = row(final_sku_price="R$850,00", original_sku_price="R$1.200,00", savings="Baixou 25,0%")
        result = recovery.recover_rest_price_fields(source, fetcher=Mock(return_value=successful(
            offer(currentPrice=850.0000000001, oldPrice=1200.0000000001))))
        self.assertTrue(result["verified"])

    def test_real_rounding_difference_and_unrelated_promotion_not_accepted(self):
        for source, quote in ((row(final_sku_price="R$850,00"), offer(currentPrice=850.01)),
                              (row(savings="25% cashback"), offer())):
            self.assertFalse(recovery.recover_rest_price_fields(source, fetcher=Mock(return_value=successful(quote)))["verified"])

    def test_invalid_quote_or_formatter_failure_keeps_request_accounting(self):
        for quote in (offer(currentPrice=0), offer(oldPrice="bad"), offer(currentPrice="1e9999")):
            result = recovery.recover_rest_price_fields(row(), fetcher=Mock(return_value=successful(quote)))
            self.assertFalse(result["success"])
            self.assertTrue(result["attempted"])
            self.assertEqual(result["status_code"], 200)

    def test_transport_error_sanitized_cached_once_including_failures(self):
        for fetch in (Mock(side_effect=RuntimeError(CANARY)), Mock(return_value={
                "success": False, "status_code": 403, "error": CANARY, "headers": {"private": CANARY}})):
            cache = {}
            first = recovery.recover_rest_price_fields(row(), fetcher=fetch, cache=cache)
            second = recovery.recover_rest_price_fields(row(), fetcher=fetch, cache=cache)
            fetch.assert_called_once()
            self.assertTrue(second["cache_hit"])
            self.assertFalse(second["attempted"])
            self.assertNotIn(CANARY, str(first) + str(second))

    def test_product_response_reused_for_each_url_sku_and_discovered_seller(self):
        cache, fetch = {}, Mock(return_value=successful(offer(), offer(skuId=101,
            sellerId=8, availability={"IdSku": 101, "IdLojista": 8})))
        first = recovery.recover_rest_price_fields(row(seller_id=""), cache=cache, fetcher=fetch)
        second = recovery.recover_rest_price_fields(row(seller_id="", product_url="https://www.casasbahia.com.br/tv/p/101"), cache=cache, fetcher=fetch)
        late = recovery.recover_rest_price_fields(row(seller_id=first["detail"]["seller_id"],
            final_sku_price=first["detail"]["final_sku_price"]), cache=cache, fetcher=fetch)
        self.assertEqual(second["detail"]["seller_id"], "8")
        self.assertTrue(late["cache_hit"])
        fetch.assert_called_once()


class Mode11BackfillTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.dict(os.environ, {"SEDA_CASAS_BAHIA_LISTING_MODE": "1-1",
            "SEDA_CASAS_BAHIA_API_ENRICH": "1"})
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.silence = patch("sys.stdout", new_callable=io.StringIO)
        self.silence.start()
        self.addCleanup(self.silence.stop)

    def test_row_order_existing_values_trace_and_checkpoint(self):
        sources = [complete_row(), row(retailer="Magalu"), row(), row()]
        before = deepcopy(sources[:2])
        traces, snapshots, fetch = [], [], Mock(return_value=successful())
        result = enrichment._backfill_casas_listing_prices(sources, fetcher=fetch, trace_rows=traces,
            checkpoint_every=1, checkpoint_writer=lambda rows: snapshots.append(deepcopy(rows)), row_index_offset=5)
        self.assertIs(result, sources)
        self.assertEqual(sources[:2], before)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual([trace["row_index"] for trace in traces], [8, 9])
        self.assertEqual([trace["attempt"] for trace in traces], [1, 0])
        fetch.assert_called_once()

    def test_optional_blank_is_verified_and_failure_is_explicit(self):
        sources = [row(final_sku_price="R$850,00"), row(final_sku_price="R$851,00")]
        traces = []
        enrichment._backfill_casas_listing_prices(sources, trace_rows=traces, fetcher=Mock(return_value=successful(
            offer(oldPrice=None, standardPrice=None, discountRate=0, savings=""))))
        self.assertIn("casas_mode11_price_verified", sources[0]["parse_status"])
        self.assertIn("casas_mode11_price_failed:existing_current_price_quote_conflict", sources[1]["parse_status"])
        self.assertIn("quote_absent:original_sku_price,savings,discount_type", traces[0]["detail"])

    def test_mode1_2_3_unchanged_and_mode4_still_ignores_partial_quote(self):
        for mode in ("1", "2", "3", "4"):
            source, fetch = [row(final_sku_price="R$850,00")], Mock()
            before = deepcopy(source)
            with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_LISTING_MODE": mode}):
                enrichment._backfill_casas_listing_prices(source, fetcher=fetch)
            self.assertEqual(source, before)
            fetch.assert_not_called()

    def test_api_disabled_preserves_rows_and_no_requests(self):
        source, fetch = [row()], Mock()
        before = deepcopy(source)
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_API_ENRICH": "0"}):
            enrichment._backfill_casas_listing_prices(source, fetcher=fetch)
        self.assertEqual(source, before)
        fetch.assert_not_called()


class Mode11SellerOrderingTests(unittest.TestCase):
    setUp = Mode11BackfillTests.setUp

    def api_stack(self, source_response=None, quote_response=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {"SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API": "1",
            "SEDA_CASAS_BAHIA_FREIGHT_API": "1", "SEDA_CASAS_BAHIA_PICKUP_API": "1",
            "SEDA_CASAS_BAHIA_RECS_API": "0", "SEDA_CASAS_BAHIA_REVIEW_API": "0",
            "SEDA_CASAS_BAHIA_DEFAULT_SELLER_ID": "10037"}))
        stack.enter_context(patch.object(enrichment, "_skip_casas_sku_api", return_value=False))
        stack.enter_context(patch.object(detail_api, "fetch_product_source", return_value=source_response or {
            "success": True, "detail": {"retailer_sku_name": "Fixture TV", "retailer_product_id": "10"}}))
        stack.enter_context(patch.object(enrichment, "_merge_casas_bahia_authoritative_detail",
                                        side_effect=lambda row, detail, token: row.update(detail)))
        price = stack.enter_context(patch.object(price_api, "fetch_listing_prices", return_value=quote_response or successful()))
        freight = stack.enter_context(patch.object(detail_api, "fetch_freight", return_value={"success": True, "detail": {}}))
        pickup = stack.enter_context(patch.object(detail_api, "fetch_pickup", return_value={"success": True, "detail": {}}))
        return price, freight, pickup

    def test_product_source_id_and_verified_seller_precede_freight_pickup(self):
        price, freight, pickup = self.api_stack()
        source, trace, cache = row(retailer_product_id="", seller_id=""), [], {}
        enrichment._merge_casas_bahia_apis(source, trace_rows=trace, row_index=3, price_cache=cache)
        self.assertEqual(source["seller_id"], "7")
        freight.assert_called_once_with("100", "7", referer_url=source["product_url"])
        pickup.assert_called_once_with("100", "7")
        enrichment._backfill_casas_listing_prices([source], trace_rows=trace, price_cache=cache)
        price.assert_called_once()
        self.assertEqual(sum(int(item["attempt"] or 0) for item in trace), 1)

    def test_unknown_seller_no_freight_or_pickup_with_default_on_api_failure(self):
        price, freight, pickup = self.api_stack(quote_response={"success": False, "status_code": 403})
        source, trace, cache = row(seller_id=""), [], {}
        enrichment._merge_casas_bahia_apis(source, trace_rows=trace, row_index=1, price_cache=cache)
        enrichment._backfill_casas_listing_prices([source], trace_rows=trace, price_cache=cache)
        freight.assert_not_called()
        pickup.assert_not_called()
        price.assert_called_once()
        self.assertEqual(source["seller_id"], "")
        self.assertTrue(any(item["error"] == "verified_seller_unavailable" for item in trace))

    def test_known_seller_still_used_not_conflicting_offer_seller(self):
        price, freight, pickup = self.api_stack(quote_response=successful(offer(sellerId=8)))
        source = row()
        enrichment._merge_casas_bahia_apis(source, price_cache={})
        freight.assert_called_once_with("100", "7", referer_url=source["product_url"])
        pickup.assert_called_once_with("100", "7")
        self.assertEqual(source["final_sku_price"], "")


class Mode11PipelineTests(unittest.TestCase):
    def setUp(self):
        legacy.DetailPipelineIntegrationTests.setUp(self)
        self.stack.enter_context(patch.dict(os.environ, {"SEDA_CASAS_BAHIA_LISTING_MODE": "1-1"}))

    write_targets = legacy.DetailPipelineIntegrationTests.write_targets

    def test_final_publication_partial_quotes_and_order(self):
        sources = [complete_row(), row(final_sku_price="R$850,00"), row(seller_id="")]
        self.write_targets(sources)
        with patch.object(price_api, "fetch_listing_prices", return_value=successful()) as fetch:
            enrichment._run_detail_main(self.root, is_worker=False)
        saved = enrichment.read_csv(self.output)
        self.assertEqual([source["item"] for source in saved], ["1", "2", "3"])
        self.assertTrue(all(source["final_sku_price"] == "R$850,00" for source in saved))
        self.assertTrue(all(source["original_sku_price"] == "R$1.200,00" for source in saved))
        self.assertEqual(fetch.call_count, 2)
        assert_detail_publish_complete(self.root)

    def test_resume_only_late_recovery_preserves_completed_quote_and_order(self):
        sources = [complete_row(), row(seller_id="")]
        self.write_targets(sources)
        prior = [{key: source.get(key, "") for key in enrichment.OUTPUT_COLUMNS} for source in sources]
        traces = []
        for index, source in enumerate(prior, 1):
            enrichment._record_subcall(traces, source, index, source["product_url"], "detail_row", success=True)
        enrichment._publish_detail_snapshot(self.root, self.output, prior, traces, [], final_complete=True,
            expected_total=2, target_sha256=enrichment.file_sha256(self.target), target_path=str(self.target.resolve()))
        before = enrichment.read_csv(self.output)
        with patch.dict(os.environ, {"SEDA_DETAIL_SKIP": "2"}), \
                patch.object(enrichment, "_merge_casas_bahia_apis", side_effect=AssertionError("must_not_restart")) as normal, \
                patch.object(price_api, "fetch_listing_prices", return_value=successful()) as fetch:
            enrichment._run_detail_main(self.root, is_worker=False)
        normal.assert_not_called()
        fetch.assert_called_once_with([{"id": "10"}], include_offers=True)
        saved = enrichment.read_csv(self.output)
        self.assertEqual(saved[0], before[0])
        self.assertEqual([source["item"] for source in saved], ["1", "2"])
        self.assertEqual(saved[1]["seller_id"], "7")
        assert_detail_publish_complete(self.root)

    def test_failures_save_all_rows_but_never_report_price_verified(self):
        self.write_targets([row()])
        with patch.object(price_api, "fetch_listing_prices", return_value={"success": False, "status_code": 403}):
            enrichment._run_detail_main(self.root, is_worker=False)
        saved = enrichment.read_csv(self.output)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["final_sku_price"], "")
        self.assertIn("casas_mode11_price_failed:price_http_403", saved[0]["parse_status"])
        self.assertNotIn("casas_mode11_price_verified", saved[0]["parse_status"])
        # This is the row-count publication contract, not a claim of field completeness.
        assert_detail_publish_complete(self.root)

    def test_early_hook_before_freight_and_late_hook_before_publish(self):
        merge = inspect.getsource(_ACTUAL_CASAS_API_MERGE)
        self.assertLess(merge.index("_backfill_casas_listing_prices("), merge.index("freight = fetch_freight("))
        source = inspect.getsource(enrichment._run_detail_main)
        self.assertLess(source.index("enriched = _backfill_casas_listing_prices("), source.rindex("_checkpoint_detail_state("))


if __name__ == "__main__":
    unittest.main()
