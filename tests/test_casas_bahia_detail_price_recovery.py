"""Synthetic missing-price recovery tests; all HTTP and headers are mocked."""

from copy import deepcopy
from contextlib import ExitStack
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

_DISABLED_ENV = Path(__file__).with_name("PRICE_RECOVERY_TEST_ENV_DOES_NOT_EXIST")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ["SEDA_ENV_PATH"] = str(_DISABLED_ENV)

from seda import step08_detail_enrichment as enrichment
from seda.casas_bahia import detail_price_recovery as recovery, price_api
from seda.detail_publish import assert_detail_publish_complete


CANARY = "SYNTHETIC_PRIVATE_ERROR_DO_NOT_EXPORT"


def row(**changes):
    value = {"retailer": "Casas Bahia", "product_line": "TV", "retailer_product_id": "10",
             "product_url": "https://www.casasbahia.com.br/smart-tv/p/100", "seller_id": "7",
             "sku": "UN55DU8000", "final_sku_price": "", "original_sku_price": "",
             "savings": "", "discount_type": "", "parse_status": "casas_listing_price_pending"}
    value.update(changes)
    return value


def offer(**changes):
    value = {"productId": 10, "skuId": 100, "sellerId": 7, "currentPrice": 850,
             "oldPrice": 1200, "standardPrice": 900, "discountRate": 25, "savings": "Baixou 25%",
             "discountDescription": "Pagamento Pix", "availability": {"IdSku": 100, "IdLojista": 7}}
    value.update(changes)
    return value


def successful(*offers):
    return {"success": True, "status_code": 200, "offers": list(offers or [offer()])}


class RecoveryTests(unittest.TestCase):
    def test_one_call_exact_identity_and_missing_fields_only(self):
        source = row(discount_type="Cupom existente")
        before = deepcopy(source)
        fetch = Mock(return_value=successful())
        result = recovery.recover_missing_price(source, fetcher=fetch)
        fetch.assert_called_once_with([{"id": "10", "sku": "100", "lojista": "7"}], include_offers=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["detail"]["final_sku_price"], "R$850,00")
        self.assertEqual(result["detail"]["original_sku_price"], "R$1.200,00")
        self.assertEqual(result["detail"]["savings"], "Baixou 25%")
        self.assertNotIn("discount_type", result["detail"])
        self.assertEqual(source, before)

    def test_existing_final_or_other_retailer_never_queried(self):
        fetch = Mock()
        for source in (row(final_sku_price="R$999,00"), row(retailer="Magalu")):
            before = deepcopy(source)
            self.assertFalse(recovery.recover_missing_price(source, fetcher=fetch)["success"])
            self.assertEqual(source, before)
        fetch.assert_not_called()

    def test_missing_product_or_url_sku_is_never_guessed(self):
        fetch = Mock()
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_DEFAULT_SELLER_ID": "10037"}):
            for changes in ({"retailer_product_id": ""}, {"product_url": "https://www.casasbahia.com.br/no-product"}):
                result = recovery.recover_missing_price(row(**changes), fetcher=fetch)
                self.assertFalse(result["success"])
                self.assertFalse(result["attempted"])
        fetch.assert_not_called()

    def test_blank_seller_product_only_query_uses_verified_offer_not_default(self):
        for missing in ("", "  ", None):
            with self.subTest(missing=missing), patch.dict(os.environ, {"SEDA_CASAS_BAHIA_DEFAULT_SELLER_ID": "10037"}):
                source = row(seller_id=missing, discount_type="Cupom existente")
                before = deepcopy(source)
                fetch = Mock(return_value=successful())
                result = recovery.recover_missing_price(source, fetcher=fetch)
                fetch.assert_called_once_with([{"id": "10"}], include_offers=True)
                self.assertTrue(result["success"])
                self.assertTrue(result["attempted"])
                self.assertEqual(result["detail"]["seller_id"], "7")
                self.assertEqual(result["detail"]["final_sku_price"], "R$850,00")
                self.assertNotIn("discount_type", result["detail"])
                self.assertEqual(source, before)

    def test_invalid_nonblank_seller_never_becomes_product_only_query(self):
        fetch = Mock()
        for seller in (0, "0", -1, "-1", "unknown", "7.0", True, False, {}):
            with self.subTest(seller_type=type(seller).__name__):
                result = recovery.recover_missing_price(row(seller_id=seller), fetcher=fetch)
                self.assertEqual(result["error"], "invalid_seller_id")
                self.assertFalse(result["attempted"])
                self.assertEqual(result["detail"], {})
        fetch.assert_not_called()

    def test_product_only_offer_must_match_product_and_url_sku(self):
        for unmatched in (offer(productId=11), offer(skuId=101)):
            fetch = Mock(return_value=successful(unmatched))
            result = recovery.recover_missing_price(row(seller_id=""), fetcher=fetch)
            self.assertEqual(result["error"], "no_matching_price_offer")
            self.assertEqual(result["detail"], {})
            fetch.assert_called_once_with([{"id": "10"}], include_offers=True)
        result = recovery.recover_missing_price(row(seller_id=""), fetcher=Mock(return_value=successful(
            offer(productId=11), offer(skuId=101), offer())))
        self.assertTrue(result["success"])
        self.assertEqual(result["detail"]["seller_id"], "7")

    def test_product_only_requires_single_matching_offer_even_if_another_is_malformed(self):
        alternatives = (offer(), offer(sellerId=8), offer(sellerId=None),
                        offer(sellerId="invalid"), offer(currentPrice=None))
        for other in alternatives:
            fetch = Mock(return_value=successful(offer(), other))
            result = recovery.recover_missing_price(row(seller_id=""), fetcher=fetch)
            self.assertEqual(result["error"], "ambiguous_price_offers")
            self.assertEqual(result["detail"], {})
            fetch.assert_called_once()

    def test_product_only_unknown_or_invalid_offer_seller_keeps_all_updates_empty(self):
        for seller in (None, "", " ", 0, -1, True, "invalid"):
            with self.subTest(seller_type=type(seller).__name__):
                source = row(seller_id="")
                before = deepcopy(source)
                fetch = Mock(return_value=successful(offer(sellerId=seller)))
                result = recovery.recover_missing_price(source, fetcher=fetch)
                self.assertFalse(result["success"])
                self.assertTrue(result["attempted"])
                self.assertEqual(result["detail"], {})
                self.assertEqual(source, before)
                fetch.assert_called_once()

    def test_product_only_availability_identity_must_match_selected_quote(self):
        for availability in ({"IdSku": 101}, {"IdLojista": 8}, {"IdSku": "invalid"}, {"IdLojista": 0}):
            result = recovery.recover_missing_price(row(seller_id=""), fetcher=Mock(
                return_value=successful(offer(availability=availability))))
            self.assertEqual(result["error"], "price_availability_identity_conflict")
            self.assertEqual(result["detail"], {})

    def test_product_only_cannot_fill_seller_when_quote_coherence_fails(self):
        for source, quote in ((row(seller_id="", original_sku_price="R$1.300,00"), offer()),
                              (row(seller_id="", savings="Baixou 30%"), offer()),
                              (row(seller_id=""), offer(currentPrice=0)),
                              (row(seller_id=""), offer(currentPrice="1e9999"))):
            before = deepcopy(source)
            result = recovery.recover_missing_price(source, fetcher=Mock(return_value=successful(quote)))
            self.assertFalse(result["success"])
            self.assertEqual(result["detail"], {})
            self.assertEqual(source, before)

    def test_product_only_cache_reuses_product_response_and_checks_each_url_sku(self):
        variant = offer(skuId=101, sellerId=8, availability={"IdSku": 101, "IdLojista": 8})
        fetch = Mock(return_value=successful(offer(), variant))
        cache = {}
        first = recovery.recover_missing_price(row(seller_id=""), cache=cache, fetcher=fetch)
        second = recovery.recover_missing_price(row(seller_id="", product_url="https://www.casasbahia.com.br/tv/p/101"),
                                               cache=cache, fetcher=fetch)
        missing = recovery.recover_missing_price(row(seller_id="", product_url="https://www.casasbahia.com.br/tv/p/102"),
                                                cache=cache, fetcher=fetch)
        fetch.assert_called_once_with([{"id": "10"}], include_offers=True)
        self.assertEqual(first["detail"]["seller_id"], "7")
        self.assertEqual(second["detail"]["seller_id"], "8")
        self.assertTrue(second["cache_hit"])
        self.assertFalse(second["attempted"])
        self.assertEqual(missing["error"], "no_matching_price_offer")
        self.assertTrue(missing["cache_hit"])

    def test_product_only_and_known_seller_cache_queries_stay_separate(self):
        fetch, cache = Mock(return_value=successful()), {}
        for source in (row(seller_id=""), row(), row(seller_id=""), row()):
            self.assertTrue(recovery.recover_missing_price(source, cache=cache, fetcher=fetch)["success"])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[0].args[0], [{"id": "10"}])
        self.assertEqual(fetch.call_args_list[1].args[0], [{"id": "10", "sku": "100", "lojista": "7"}])

    def test_product_only_failed_response_is_cached_without_guessing_or_retries(self):
        fetch = Mock(return_value={"success": False, "status_code": 403, "error": CANARY})
        cache = {}
        for sku in (100, 101):
            result = recovery.recover_missing_price(row(seller_id="", product_url=f"https://www.casasbahia.com.br/tv/p/{sku}"),
                                                   cache=cache, fetcher=fetch)
            self.assertEqual(result["error"], "price_http_403")
            self.assertEqual(result["detail"], {})
            self.assertNotIn(CANARY, str(result))
        fetch.assert_called_once_with([{"id": "10"}], include_offers=True)

    def test_foreign_url_and_bool_identity_are_rejected(self):
        fetch = Mock()
        for source in (row(product_url="https://example.invalid/p/100"), row(retailer_product_id=True), row(seller_id=0)):
            self.assertFalse(recovery.recover_missing_price(source, fetcher=fetch)["attempted"])
        fetch.assert_not_called()

    def test_product_sku_seller_must_all_match(self):
        for key, bad in (("productId", 11), ("skuId", 101), ("sellerId", 8)):
            result = recovery.recover_missing_price(row(), fetcher=Mock(return_value=successful(offer(**{key: bad}))))
            self.assertEqual(result["error"], "no_matching_price_offer")
        result = recovery.recover_missing_price(row(), fetcher=Mock(return_value=successful(offer(sellerId=8), offer())))
        self.assertTrue(result["success"])

    def test_duplicate_and_conflicting_matching_offers_not_last_wins(self):
        for offers in ((offer(), offer()), (offer(), offer(currentPrice=800))):
            result = recovery.recover_missing_price(row(), fetcher=Mock(return_value=successful(*offers)))
            self.assertEqual(result["error"], "ambiguous_price_offers")
            self.assertEqual(result["detail"], {})

    def test_availability_identity_conflict_not_accepted(self):
        for availability in ({"IdSku": 101}, {"IdLojista": 8}):
            result = recovery.recover_missing_price(row(), fetcher=Mock(return_value=successful(offer(availability=availability))))
            self.assertEqual(result["error"], "price_availability_identity_conflict")

    def test_nonpositive_nonfinite_and_invalid_price_not_accepted(self):
        for value in (0, -1, True, float("nan"), float("inf"), "not-price"):
            result = recovery.recover_missing_price(row(), fetcher=Mock(return_value=successful(offer(currentPrice=value))))
            self.assertEqual(result["error"], "invalid_current_price")
            self.assertEqual(result["detail"], {})

    def test_existing_original_must_match_fresh_quote(self):
        for original in ("R$1.300,00", "invalid"):
            result = recovery.recover_missing_price(row(original_sku_price=original), fetcher=Mock(return_value=successful()))
            self.assertEqual(result["error"], "existing_price_quote_conflict")
        result = recovery.recover_missing_price(row(original_sku_price="R$1.200,00"), fetcher=Mock(return_value=successful()))
        self.assertTrue(result["success"])
        self.assertNotIn("original_sku_price", result["detail"])

    def test_existing_savings_conflict_retains_null(self):
        result = recovery.recover_missing_price(row(savings="Baixou 30%"), fetcher=Mock(return_value=successful()))
        self.assertEqual(result["error"], "existing_savings_quote_conflict")
        self.assertEqual(result["detail"], {})

    def test_cache_deduplicates_calls_but_checks_each_rows_quote(self):
        fetch = Mock(return_value=successful())
        cache = {}
        first = recovery.recover_missing_price(row(), cache=cache, fetcher=fetch)
        second = recovery.recover_missing_price(row(original_sku_price="R$1.300,00"), cache=cache, fetcher=fetch)
        third = recovery.recover_missing_price(row(), cache=cache, fetcher=fetch)
        fetch.assert_called_once()
        self.assertTrue(first["success"])
        self.assertEqual(second["error"], "existing_price_quote_conflict")
        self.assertTrue(third["success"])
        self.assertFalse(third["attempted"])
        self.assertTrue(third["cache_hit"])

    def test_failed_response_cached_without_body_error_text_or_retries(self):
        fetch = Mock(return_value={"success": False, "status_code": 403, "error": CANARY, "body": CANARY, "headers": CANARY})
        cache = {}
        for _ in range(2):
            result = recovery.recover_missing_price(row(), cache=cache, fetcher=fetch)
            self.assertEqual(result["error"], "price_http_403")
            self.assertNotIn(CANARY, json.dumps(result))
        self.assertNotIn(CANARY, str(cache))
        fetch.assert_called_once()

    def test_exception_has_no_raw_text_and_no_retry(self):
        fetch = Mock(side_effect=RuntimeError(CANARY))
        result = recovery.recover_missing_price(row(), fetcher=fetch)
        self.assertEqual(result["error"], "price_response_unavailable")
        self.assertNotIn(CANARY, json.dumps(result))
        fetch.assert_called_once()

    def test_http_success_flag_without_status200_is_insufficient(self):
        result = recovery.recover_missing_price(row(), fetcher=Mock(return_value={"success": True, "offers": [offer()]}))
        self.assertFalse(result["success"])

    def test_unformattable_quote_retains_actual_request_metadata_and_null(self):
        fetch = Mock(return_value=successful(offer(currentPrice="1e9999")))
        result = recovery.recover_missing_price(row(), fetcher=fetch)
        self.assertEqual(result["error"], "price_quote_parse_failed")
        self.assertTrue(result["attempted"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["detail"], {})
        fetch.assert_called_once()


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SEDA_CASAS_BAHIA_API_ENRICH": "1", "SEDA_CASAS_BAHIA_LISTING_MODE": "4"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = patch("sys.stdout", new_callable=io.StringIO)
        self.output.start()
        self.addCleanup(self.output.stop)

    def test_fills_after_detail_preserves_rows_nonempty_prices_and_promotions(self):
        complete = row(final_sku_price="R$777,00", original_sku_price="R$900,00", savings="keep")
        other = row(retailer="Magalu")
        rows = [row(discount_type="Cupom existente"), complete, other, row()]
        complete_before, other_before = deepcopy(complete), deepcopy(other)
        fetch, checkpoint, trace = Mock(return_value=successful()), Mock(), []
        result = enrichment._backfill_casas_listing_prices(rows, fetcher=fetch, checkpoint_writer=checkpoint,
                                                         checkpoint_every=1, trace_rows=trace)
        self.assertIs(result, rows)
        self.assertEqual(len(result), 4)
        self.assertEqual(complete, complete_before)
        self.assertEqual(other, other_before)
        self.assertEqual(rows[0]["discount_type"], "Cupom existente")
        self.assertEqual(rows[0]["final_sku_price"], "R$850,00")
        self.assertIn("casas_detail_price_recovered", rows[0]["parse_status"])
        self.assertIn("casas_bahia_detail_price_cache", rows[3]["fetch_method"])
        fetch.assert_called_once()
        self.assertEqual(checkpoint.call_count, 2)
        self.assertEqual([item["attempt"] for item in trace], [1, 0])

    def test_skip_and_failure_keep_null_with_safe_status_and_trace(self):
        rows = [row(seller_id="invalid"), row()]
        fetch = Mock(side_effect=RuntimeError(CANARY))
        trace = []
        enrichment._backfill_casas_listing_prices(rows, fetcher=fetch, trace_rows=trace)
        self.assertEqual([item["final_sku_price"] for item in rows], ["", ""])
        self.assertIn("casas_detail_price_skipped:invalid_seller_id", rows[0]["parse_status"])
        self.assertIn("casas_detail_price_failed:price_response_unavailable", rows[1]["parse_status"])
        self.assertNotIn(CANARY, str(rows) + str(trace))
        fetch.assert_called_once()

    def test_existing_api_disable_is_respected(self):
        source, fetch = [row()], Mock()
        before = deepcopy(source)
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_API_ENRICH": "0"}):
            self.assertIs(enrichment._backfill_casas_listing_prices(source, fetcher=fetch), source)
        self.assertEqual(source, before)
        fetch.assert_not_called()

    def test_legacy_modes_do_not_add_price_calls_or_change_rows(self):
        for mode in ("1", "2", "3"):
            source, fetch = [row(), row(seller_id="")], Mock()
            before = deepcopy(source)
            with self.subTest(mode=mode), patch.dict(os.environ, {"SEDA_CASAS_BAHIA_LISTING_MODE": mode}):
                self.assertIs(enrichment._backfill_casas_listing_prices(source, fetcher=fetch), source)
            self.assertEqual(before, source)
            fetch.assert_not_called()

    def test_product_only_fills_seller_with_price_and_never_seller_alone(self):
        sources = [row(seller_id=""), row(seller_id="", original_sku_price="R$1.300,00"),
                   row(seller_id="", final_sku_price="R$777,00")]
        original_complete = deepcopy(sources[2])
        fetch, snapshots, traces = Mock(return_value=successful()), [], []
        enrichment._backfill_casas_listing_prices(sources, fetcher=fetch, checkpoint_every=1,
            checkpoint_writer=lambda rows: snapshots.append(deepcopy(rows)), trace_rows=traces)
        fetch.assert_called_once_with([{"id": "10"}], include_offers=True)
        self.assertEqual(sources[0]["seller_id"], "7")
        self.assertEqual(sources[0]["final_sku_price"], "R$850,00")
        self.assertEqual(sources[1]["seller_id"], "")
        self.assertEqual(sources[1]["final_sku_price"], "")
        self.assertEqual(sources[1]["original_sku_price"], "R$1.300,00")
        self.assertEqual(sources[2], original_complete)
        self.assertEqual(len(snapshots), 2)
        for snapshot in snapshots:
            self.assertEqual(snapshot[0]["seller_id"], "7")
            self.assertEqual(snapshot[0]["final_sku_price"], "R$850,00")
            self.assertEqual(snapshot[1]["seller_id"], "")
            self.assertEqual(snapshot[1]["final_sku_price"], "")
        self.assertEqual([item["attempt"] for item in traces], [1, 0])

    def test_product_only_ambiguous_offer_preserves_row_and_pending_fields(self):
        source, traces = [row(seller_id="")], []
        fetch = Mock(return_value=successful(offer(), offer(sellerId=8)))
        enrichment._backfill_casas_listing_prices(source, fetcher=fetch, trace_rows=traces)
        self.assertEqual(len(source), 1)
        self.assertEqual(source[0]["seller_id"], "")
        self.assertEqual(source[0]["final_sku_price"], "")
        self.assertIn("casas_detail_price_failed:ambiguous_price_offers", source[0]["parse_status"])
        self.assertEqual(traces[0]["attempt"], 1)
        fetch.assert_called_once()

    def test_post_request_parse_error_counts_call_and_preserves_row(self):
        rows, trace = [row()], []
        fetch = Mock(return_value=successful(offer(currentPrice="1e9999")))
        enrichment._backfill_casas_listing_prices(rows, fetcher=fetch, trace_rows=trace)
        self.assertEqual(rows[0]["final_sku_price"], "")
        self.assertIn("casas_detail_price_failed:price_quote_parse_failed", rows[0]["parse_status"])
        self.assertEqual(trace[0]["attempt"], 1)
        self.assertEqual(trace[0]["status_code"], 200)
        fetch.assert_called_once()

    def test_hook_follows_existing_detail_paths_and_precedes_final_publish(self):
        source = inspect.getsource(enrichment._run_detail_main)
        hook = source.index("enriched = _backfill_casas_listing_prices(")
        self.assertGreater(hook, source.index("enriched = _backfill_casas_zenrows_fields("))
        self.assertGreater(hook, source.index("enriched = _backfill_magalu_shipping_blanks("))
        self.assertLess(hook, source.rindex("_checkpoint_detail_state("))


class PriceOffersContractTests(unittest.TestCase):
    def setUp(self):
        for target in (patch.object(price_api, "_headers", return_value={}), patch.object(price_api, "_params", return_value={})):
            target.start()
            self.addCleanup(target.stop)

    def raw_offer(self, seller=7, price=900):
        return {"PrecoVenda": {"IdProduto": 10, "IdSku": 100, "IdLojista": seller, "PrecoDe": 1200, "Preco": price},
                "Disponibilidade": {"IdSku": 100, "IdLojista": seller}}

    def test_opt_in_keeps_all_offers_without_changing_old_result_shape(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"Ofertas": [self.raw_offer(), self.raw_offer(8)]})
        with patch.object(price_api.requests, "post", return_value=response) as post:
            result = price_api.fetch_listing_prices([{"id": 10, "sku": 100, "lojista": 7}], include_offers=True)
        self.assertEqual(len(result["offers"]), 2)
        self.assertEqual(result["status_code"], 200)
        post.assert_called_once()
        with patch.object(price_api.requests, "post", return_value=response):
            old = price_api.fetch_listing_prices([{"id": 10}])
        self.assertEqual(set(old), {"success", "prices", "count"})

    def test_opt_in_http_failure_never_reads_response_body(self):
        class ForbiddenBody:
            status_code = 403

            @property
            def text(self):
                raise AssertionError("must not read response body")

        with patch.object(price_api.requests, "post", return_value=ForbiddenBody()) as post:
            result = price_api.fetch_listing_prices([{"id": 10}], include_offers=True)
        self.assertEqual(result["error"], "price_http_403")
        self.assertEqual(result["offers"], [])
        post.assert_called_once()

    def test_opt_in_exception_does_not_serialize_exception(self):
        with patch.object(price_api.requests, "post", side_effect=RuntimeError(CANARY)) as post:
            result = price_api.fetch_listing_prices([{"id": 10}], include_offers=True)
        self.assertEqual(result["error"], "price_request_failed")
        self.assertNotIn(CANARY, str(result))
        post.assert_called_once()

    def test_opt_in_invalid_offer_container_rejected(self):
        for payload in ([], {"Ofertas": {}}):
            response = SimpleNamespace(status_code=200, json=lambda: payload)
            with patch.object(price_api.requests, "post", return_value=response):
                result = price_api.fetch_listing_prices([{"id": 10}], include_offers=True)
            self.assertFalse(result["success"])
            self.assertEqual(result["offers"], [])

    def test_opt_in_malformed_offer_does_not_discard_valid_other_offer(self):
        for offers in ([None, self.raw_offer(), "invalid"], [None]):
            response = SimpleNamespace(status_code=200, json=lambda: {"Ofertas": offers})
            with patch.object(price_api.requests, "post", return_value=response) as post:
                result = price_api.fetch_listing_prices([{"id": 10}], include_offers=True)
            self.assertTrue(result["success"])
            self.assertEqual(len(result["offers"]), 1 if len(offers) == 3 else 0)
            if result["offers"]:
                self.assertEqual(result["offers"][0]["sellerId"], 7)
                self.assertEqual(result["offers"][0]["currentPrice"], 900)
            post.assert_called_once()


class DetailPipelineIntegrationTests(unittest.TestCase):
    """Run the actual detail/checkpoint/publish flow on synthetic local files."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.target = self.root / "output" / "seda_final_targets.csv"
        self.output = self.root / "output" / "final_output_enriched.csv"
        self.stack.enter_context(patch.dict(os.environ, {
            "SEDA_DETAIL_TARGET_CSV": str(self.target),
            "SEDA_DETAIL_OUTPUT_CSV": str(self.output),
            "SEDA_DETAIL_SKIP": "0", "SEDA_DETAIL_LIMIT": "0",
            "SEDA_DETAIL_TRACE": "1", "SEDA_DETAIL_TRACE_TAG": "",
            "SEDA_DETAIL_CHECKPOINT_EVERY": "1", "SEDA_DETAIL_FALLBACK_FETCH": "0",
            "SEDA_MAGALU_SHIPPING_BACKFILL_ONLY": "0", "SEDA_MAGALU_DETAIL_WORKERS": "1",
            "SEDA_ACTIVE_RETAILER": "casas_bahia", "SEDA_TRANSLATE_OUTPUT": "0",
            "SEDA_CASAS_BAHIA_API_ENRICH": "1",
            "SEDA_CASAS_BAHIA_LISTING_MODE": "4",
        }))
        self.stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.stack.enter_context(patch.object(enrichment, "fetch_url", side_effect=AssertionError("live_fetch_forbidden")))
        for name in ("_magalu_graphql_detail", "_merge_magalu_reviews", "_merge_magalu_pdp_html",
                     "_merge_magalu_similar", "_merge_magalu_review_pages", "_merge_casas_bahia_apis"):
            self.stack.enter_context(patch.object(enrichment, name, return_value=None))
        for name in ("_backfill_magalu_detail_blanks", "_backfill_magalu_tv_skus_from_title_url",
                     "_backfill_magalu_zenrows_fields", "_backfill_casas_zenrows_fields",
                     "_backfill_magalu_shipping_blanks"):
            self.stack.enter_context(patch.object(enrichment, name, side_effect=lambda rows, *args, **kwargs: rows))

    def write_targets(self, rows):
        for index, source in enumerate(rows, 1):
            source["item"] = str(index)
        enrichment.write_csv(self.target, rows)

    def test_normal_detail_then_recovery_publish_preserves_every_target_and_order(self):
        sources = [row(final_sku_price="R$777,00"), row(), row(seller_id=""), row()]
        self.write_targets(sources)
        with patch.object(price_api, "fetch_listing_prices", return_value=successful()) as fetch:
            enrichment._run_detail_main(self.root, is_worker=False)
        self.assertEqual(fetch.call_count, 2)
        saved = enrichment.read_csv(self.output)
        self.assertEqual([source["item"] for source in saved], ["1", "2", "3", "4"])
        self.assertEqual([source["final_sku_price"] for source in saved], ["R$777,00", "R$850,00", "R$850,00", "R$850,00"])
        self.assertEqual(saved[2]["seller_id"], "7")
        self.assertIn("casas_detail_price_recovered", saved[2]["parse_status"])
        assert_detail_publish_complete(self.root)
        traces = enrichment.read_csv(self.root / "detail" / "trace" / "subcall_trace.csv")
        recoveries = [trace for trace in traces if trace["subcall"] == "casas_missing_price_recovery"]
        self.assertEqual([trace["row_index"] for trace in recoveries], ["2", "3", "4"])

    def test_price_filled_by_prior_detail_recovery_does_not_trigger_new_request(self):
        self.write_targets([row()])

        def prior_recovery(rows, *args, **kwargs):
            rows[0]["final_sku_price"] = "R$999,00"
            return rows

        with patch.object(enrichment, "_backfill_casas_zenrows_fields", side_effect=prior_recovery), \
                patch.object(price_api, "fetch_listing_prices") as fetch:
            enrichment._run_detail_main(self.root, is_worker=False)
        fetch.assert_not_called()
        self.assertEqual(enrichment.read_csv(self.output)[0]["final_sku_price"], "R$999,00")
        assert_detail_publish_complete(self.root)

    def test_price_failure_still_publishes_all_rows_complete_with_nulls(self):
        self.write_targets([row(), row(seller_id="")])
        with patch.object(price_api, "fetch_listing_prices", side_effect=RuntimeError(CANARY)) as fetch:
            enrichment._run_detail_main(self.root, is_worker=False)
        self.assertEqual(fetch.call_count, 2)
        saved = enrichment.read_csv(self.output)
        self.assertEqual(len(saved), 2)
        self.assertEqual([source["final_sku_price"] for source in saved], ["", ""])
        self.assertNotIn(CANARY, str(saved))
        assert_detail_publish_complete(self.root)

    def test_resume_full_308_prefix_preserves_224_existing_prices_and_recovers_only_84_blanks(self):
        sources = []
        for index in range(308):
            priced = index < 224
            sources.append(row(retailer_product_id=str(1000 + index),
                product_url=f"https://www.casasbahia.com.br/tv/p/{2000 + index}",
                seller_id="7" if priced else "", final_sku_price="R$777,00" if priced else "",
                original_sku_price="R$900,00" if priced else "", savings="keep" if priced else "",
                discount_type="Cupom existente" if priced else "",
                parse_status="previous_detail_complete" if priced else
                    "casas_listing_price_pending+casas_listing_seller_pending+casas_detail_price_skipped:missing_seller_id"))
        self.write_targets(sources)
        prior_rows = [{key: source.get(key, "") for key in enrichment.OUTPUT_COLUMNS} for source in sources]
        traces = []
        for index, source in enumerate(prior_rows, 1):
            enrichment._record_subcall(traces, source, index, source["product_url"], "detail_row", success=True)
        enrichment._publish_detail_snapshot(self.root, self.output, prior_rows, traces, [],
            final_complete=True, expected_total=308, target_sha256=enrichment.file_sha256(self.target),
            target_path=str(self.target.resolve()))
        previous = enrichment.read_csv(self.output)

        def return_product_quote(products, **kwargs):
            self.assertEqual(kwargs, {"include_offers": True})
            self.assertEqual(len(products), 1)
            self.assertEqual(set(products[0]), {"id"})
            product_id = int(products[0]["id"])
            self.assertGreaterEqual(product_id, 1224)
            sku_id = product_id + 1000
            return successful(offer(productId=product_id, skuId=sku_id,
                                    availability={"IdSku": sku_id, "IdLojista": 7}))

        with patch.dict(os.environ, {"SEDA_DETAIL_SKIP": "308", "SEDA_DETAIL_CHECKPOINT_EVERY": "25"}), \
                patch.object(price_api, "fetch_listing_prices", side_effect=return_product_quote) as fetch, \
                patch.object(enrichment, "_merge_casas_bahia_apis", side_effect=AssertionError("normal_detail_must_not_restart")) as normal:
            enrichment._run_detail_main(self.root, is_worker=False)
        normal.assert_not_called()
        self.assertEqual(fetch.call_count, 84)
        saved = enrichment.read_csv(self.output)
        self.assertEqual(len(saved), 308)
        self.assertEqual(saved[:224], previous[:224])
        self.assertEqual([item["item"] for item in saved], [item["item"] for item in previous])
        self.assertTrue(all(item["final_sku_price"] == "R$850,00" and item["seller_id"] == "7" for item in saved[224:]))
        self.assertTrue(all("casas_detail_price_recovered" in item["parse_status"] for item in saved[224:]))
        assert_detail_publish_complete(self.root)

    def test_interrupted_recovery_checkpoint_is_incomplete_and_resume_keeps_completed_price(self):
        second = row(retailer_product_id="11", product_url="https://www.casasbahia.com.br/tv/p/101")
        self.write_targets([row(), second])
        with patch.object(price_api, "fetch_listing_prices", side_effect=[successful(), KeyboardInterrupt]) as fetch:
            with self.assertRaises(KeyboardInterrupt):
                enrichment._run_detail_main(self.root, is_worker=False)
        self.assertEqual(fetch.call_count, 2)
        saved = enrichment.read_csv(self.output)
        self.assertEqual([source["final_sku_price"] for source in saved], ["R$850,00", ""])
        with self.assertRaisesRegex(RuntimeError, "detail_publish_incomplete"):
            assert_detail_publish_complete(self.root)
        second_offer = offer(productId=11, skuId=101, availability={"IdSku": 101, "IdLojista": 7})
        with patch.dict(os.environ, {"SEDA_DETAIL_SKIP": "2"}), \
                patch.object(price_api, "fetch_listing_prices", return_value=successful(second_offer)) as resumed:
            enrichment._run_detail_main(self.root, is_worker=False)
        resumed.assert_called_once_with([{"id": "11", "sku": "101", "lojista": "7"}], include_offers=True)
        self.assertEqual([source["final_sku_price"] for source in enrichment.read_csv(self.output)], ["R$850,00", "R$850,00"])
        assert_detail_publish_complete(self.root)


if __name__ == "__main__":
    unittest.main()
