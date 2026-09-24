"""Offline URL-first listing contracts: missing quotes are not missing products."""

from copy import deepcopy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_DISABLED_ENV = Path(__file__).with_name("PENDING_PRICE_TEST_ENV_DOES_NOT_EXIST")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ["SEDA_ENV_PATH"] = str(_DISABLED_ENV)

from seda import parsers, step00_config as config
from seda.casas_bahia import listing_url_first as hybrid, listing_modes


BASE = "https://www.casasbahia.com.br"
URL = BASE + "/tv/b?page=1"


def source_product(number=201, priced=False):
    product = {
        "id": number + 1000, "idSku": number, "sku": number,
        "href": f"/smart-tv-samsung/p/{number}", "url": f"/smart-tv-samsung/p/{number}",
        "title": 'Smart TV Samsung 43" DU7700', "rating": 4.5, "ratingCount": 12,
        "isSponsored": True, "flags": [{"description": "Cupom fixture"}, {"description": "Retira Rapido"}],
        "_casas_listing_url_first": True,
        "price": {}, "_casas_listing_price_pending": True, "_casas_listing_seller_pending": True,
    }
    if priced:
        product.update({"lojista": 7, "sellerId": 7,
                        "price": {"currentPrice": 1500, "oldPrice": 1800, "productId": number + 1000,
                                  "skuId": number, "sellerId": 7},
                        "_casas_listing_price_pending": False, "_casas_listing_seller_pending": False})
    return product


def document(products=None, query=None, suffix=""):
    payload = {"props": {"pageProps": {"initialState": {"search": {
        "query": {"page": "1", "strbusca": "tv"} if query is None else query,
        "searchTerm": "tv", "results": {"products": products if products is not None else [source_product()]},
    }}}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>" + suffix


class PendingPriceContracts(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.environment = {"SEDA_PRODUCT_LINE": "TV", "SEDA_TRANSLATE_OUTPUT": "0"}
        self.stack.enter_context(patch.object(parsers.os, "environ", self.environment))

    def rows(self, text=None, run_id="main"):
        return parsers.parse_listing(text or document(), "Casas Bahia", BASE, URL, run_id=run_id)

    def test_missing_price_and_seller_keep_url_product_and_sku_identity(self):
        text = document()
        self.assertEqual(hybrid._validation_error(text, URL), "")
        row = self.rows(text)[0]
        self.assertEqual(row["product_url"], BASE + "/smart-tv-samsung/p/201")
        self.assertEqual(row["retailer_product_id"], "1201")
        self.assertEqual(row["seller_id"], "")
        self.assertEqual(row["final_sku_price"], "")
        self.assertEqual(row["original_sku_price"], "")
        self.assertEqual(row["savings"], "")
        self.assertIn("casas_listing_price_pending", row["parse_status"].split("+"))
        self.assertIn("casas_listing_seller_pending", row["parse_status"].split("+"))

    def test_missing_price_preserves_explicit_seller_without_pending_seller_token(self):
        product = source_product()
        product.update(lojista=7, sellerId=7, _casas_listing_seller_pending=False)
        text = document([product])
        self.assertEqual(hybrid._validation_error(text, URL), "")
        row = self.rows(text)[0]
        self.assertEqual(row["seller_id"], "7")
        self.assertNotIn("casas_listing_seller_pending", row["parse_status"].split("+"))

    def test_pending_flag_blanks_stale_quote_values_and_ignores_reason_text(self):
        product = source_product(priced=True)
        product.update(_casas_listing_price_pending=True, _casas_listing_price_pending_reason="RAW_REASON_CANARY")
        row = self.rows(document([product]))[0]
        self.assertEqual([row[key] for key in ("original_sku_price", "final_sku_price", "savings")], ["", "", ""])
        self.assertNotIn("RAW_REASON_CANARY", json.dumps(row))
        self.assertEqual(row["seller_id"], "7")

    def test_price_dict_without_current_price_never_becomes_a_csv_price_string(self):
        product = source_product(priced=True)
        product["price"].pop("currentPrice")
        text = document([product])
        self.assertEqual(hybrid._validation_error(text, URL), "")
        row = self.rows(text)[0]
        self.assertEqual(row["final_sku_price"], "")
        self.assertEqual(row["original_sku_price"], "R$1.800,00")
        self.assertNotIn("{", row["final_sku_price"])

    def test_pending_keeps_nonprice_rating_promotion_pickup_name_and_ranks(self):
        row = self.rows(run_id="bsr")[0]
        self.assertEqual(row["star_rating"], "4.5")
        self.assertEqual(row["count_of_reviews"], "12")
        self.assertEqual(row["sku_status"], "Sponsored")
        self.assertIn("Cupom fixture", row["discount_type"])
        self.assertEqual(row["pick_up_availability"], "Retira Rapido")
        self.assertEqual(row["retailer_sku_name"], 'Smart TV Samsung 43" DU7700')
        self.assertEqual(row["main_rank"], "")
        self.assertEqual(row["bsr_rank"], 1)

    def test_pending_does_not_use_unverified_quote_promotion_or_availability(self):
        product = source_product()
        product["flags"] = []
        product["price"] = {"priceDescription": "Cupom unverified", "availability": {"Retira": True}}
        row = self.rows(document([product]))[0]
        self.assertEqual(row["discount_type"], "")
        self.assertEqual(row["pick_up_availability"], "")

    def test_pending_or_missing_final_price_blocks_dom_quote_backfill(self):
        for flagged in (True, False):
            with self.subTest(flagged=flagged):
                product = source_product()
                product["_casas_listing_price_pending"] = flagged
                snapshots = {BASE + "/smart-tv-samsung/p/201": {
                    "original_sku_price": "R$9.999,00", "final_sku_price": "R$8.888,00",
                    "savings": "Baixou 20%", "seller_id": "999",
                    "discount_type": "Cupom visible", "pick_up_availability": "Retira Rapido",
                }}
                with patch.object(parsers, "_casas_bahia_card_snapshots", return_value=snapshots):
                    row = self.rows(document([product]))[0]
                self.assertEqual([row[key] for key in ("original_sku_price", "final_sku_price", "savings", "seller_id")],
                                 ["", "", "", ""])
                self.assertIn("Cupom", row["discount_type"])

    def test_only_exact_boolean_flags_append_pending_tokens(self):
        for flag in ("true", "1", 1, False, None):
            with self.subTest(flag=flag):
                product = source_product(priced=True)
                product["_casas_listing_price_pending"] = flag
                product["_casas_listing_seller_pending"] = flag
                row = self.rows(document([product]))[0]
                self.assertNotIn("casas_listing_price_pending", row["parse_status"].split("+"))
                self.assertEqual(row["final_sku_price"], "R$1.500,00")
                self.assertEqual(row["seller_id"], "7")

    def test_tokens_and_blank_prices_survive_actual_csv_roundtrip(self):
        with tempfile.TemporaryDirectory(prefix="pending_price_csv_", dir=Path(__file__).parent) as directory:
            output = Path(directory) / "fixture.csv"
            config.write_csv(output, self.rows())
            row = config.read_csv(output)[0]
        self.assertIn("casas_listing_price_pending", row["parse_status"].split("+"))
        self.assertIn("casas_listing_seller_pending", row["parse_status"].split("+"))
        self.assertEqual(row["final_sku_price"], "")
        self.assertEqual(row["seller_id"], "")

    def test_legacy_next_product_mapping_uses_same_pending_tokens(self):
        row = parsers._casas_bahia_product_row(source_product(), BASE, URL, "main", 1)
        self.assertIn("casas_listing_price_pending", row["parse_status"].split("+"))
        self.assertEqual(row["final_sku_price"], "")

    def test_complete_quote_stays_unchanged_without_pending_tokens(self):
        text = document([source_product(priced=True)])
        self.assertEqual(hybrid._validation_error(text, URL), "")
        row = self.rows(text)[0]
        self.assertEqual(row["original_sku_price"], "R$1.800,00")
        self.assertEqual(row["final_sku_price"], "R$1.500,00")
        self.assertEqual(row["seller_id"], "7")
        self.assertEqual(row["parse_status"], "listing_casas_bahia_ssr")

    def test_partial_quote_identity_fields_are_optional_but_not_inferred(self):
        product = source_product(priced=True)
        product["price"] = {"currentPrice": 1500}
        self.assertEqual(hybrid._validation_error(document([product]), URL), "")
        self.assertEqual(self.rows(document([product]))[0]["final_sku_price"], "R$1.500,00")
        product.pop("lojista")
        product.pop("sellerId")
        self.assertEqual(hybrid._validation_error(document([product]), URL), "")
        self.assertEqual(self.rows(document([product]))[0]["seller_id"], "")
        self.assertEqual(self.rows(document([product]))[0]["final_sku_price"], "R$1.500,00")

    def test_optional_price_identity_contradictions_do_not_block_valid_url(self):
        for field in ("productId", "skuId", "sellerId"):
            with self.subTest(field=field):
                product = source_product(priced=True)
                product["price"][field] = 999
                self.assertEqual(hybrid._validation_error(document([product]), URL), "")
                row = self.rows(document([product]))[0]
                self.assertEqual(row["product_url"], BASE + "/smart-tv-samsung/p/201")
                self.assertEqual([row[key] for key in ("original_sku_price", "final_sku_price", "savings")],
                                 ["", "", ""])
                self.assertIn("casas_listing_price_pending", row["parse_status"].split("+"))
                self.assertEqual(row["seller_id"], "7")
                self.assertNotIn("casas_listing_seller_pending", row["parse_status"].split("+"))

    def test_raw_cache_quote_conflict_cannot_be_repopulated_by_dom_snapshot(self):
        product = source_product(priced=True)
        product["price"]["skuId"] = 999
        snapshot = {BASE + "/smart-tv-samsung/p/201": {
            "original_sku_price": "R$9.999,00", "final_sku_price": "R$8.888,00", "savings": "Baixou 20%",
        }}
        with patch.object(parsers, "_casas_bahia_card_snapshots", return_value=snapshot):
            row = self.rows(document([product]))[0]
        self.assertEqual([row[key] for key in ("original_sku_price", "final_sku_price", "savings")], ["", "", ""])
        self.assertEqual(row["star_rating"], "4.5")
        self.assertEqual(row["main_rank"], 1)
        self.assertEqual(row["sku_status"], "Sponsored")

    def test_raw_cache_invalid_optional_metadata_is_quarantined_without_losing_row(self):
        for key, value in (("id", "invalid"), ("idSku", 999), ("sku", False),
                           ("lojista", -1), ("sellerId", 999)):
            with self.subTest(key=key, value=value):
                product = source_product(priced=True)
                product[key] = value
                text = document([product])
                self.assertEqual(hybrid._validation_error(text, URL), "")
                row = self.rows(text)[0]
                self.assertEqual(row["product_url"], BASE + "/smart-tv-samsung/p/201")
                self.assertEqual(row["final_sku_price"], "")
                self.assertIn("casas_listing_price_pending", row["parse_status"].split("+"))
                if key in {"lojista", "sellerId"}:
                    self.assertEqual(row["seller_id"], "")
                if key in {"id", "idSku", "sku"}:
                    self.assertEqual(row["retailer_product_id"], "")

    def test_raw_cache_quote_guard_does_not_mutate_input_product(self):
        product = source_product(priced=True)
        product["price"]["productId"] = 999
        before = deepcopy(product)
        row = parsers._casas_bahia_ssr_product_row(product, BASE, URL, "main", 1)
        self.assertEqual(product, before)
        self.assertEqual(row["final_sku_price"], "")

    def test_raw_cache_legacy_scalar_price_with_missing_quote_ids_remains_valid(self):
        product = source_product(priced=True)
        product["price"] = 1500
        product["oldPrice"] = 1800
        row = parsers._casas_bahia_product_row(product, BASE, URL, "main", 1)
        self.assertEqual(row["final_sku_price"], "R$1.500,00")
        self.assertEqual(row["original_sku_price"], "R$1.800,00")
        self.assertNotIn("casas_listing_price_pending", row["parse_status"].split("+"))

    def test_unknown_seller_flag_alone_does_not_discard_nonconflicting_quote(self):
        product = source_product(priced=True)
        product.pop("lojista")
        product.pop("sellerId")
        product["price"].pop("sellerId")
        product["_casas_listing_seller_pending"] = True
        row = self.rows(document([product]))[0]
        self.assertEqual(row["seller_id"], "")
        self.assertEqual(row["final_sku_price"], "R$1.500,00")
        self.assertNotIn("casas_listing_price_pending", row["parse_status"].split("+"))
        self.assertIn("casas_listing_seller_pending", row["parse_status"].split("+"))

    def test_pending_flag_quarantines_bad_price_without_losing_valid_url(self):
        product = source_product(priced=True)
        product["_casas_listing_price_pending"] = True
        product["price"]["skuId"] = 999
        self.assertEqual(hybrid._validation_error(document([product]), URL), "")
        self.assertEqual(self.rows(document([product]))[0]["final_sku_price"], "")

    def test_missing_invalid_optional_product_ids_do_not_block_valid_url(self):
        for value in (0, -1, True, False, 1.2, "", "abc", "-2"):
            with self.subTest(value=value):
                product = source_product()
                product["id"] = value
                self.assertEqual(hybrid._validation_error(document([product]), URL), "")

    def test_optional_sku_alias_conflict_does_not_override_valid_url_identity(self):
        for key in ("idSku", "sku"):
            for value in (0, -1, True, "abc", 999):
                with self.subTest(key=key, value=value):
                    product = source_product()
                    product[key] = value
                    self.assertEqual(hybrid._validation_error(document([product]), URL), "")

    def test_missing_or_invalid_url_sku_still_fails(self):
        for suffix in ("", "/smart-tv", "/smart-tv/p/0", "/smart-tv/p/not-a-number"):
            with self.subTest(suffix=suffix):
                product = source_product()
                product["href"] = suffix
                product["url"] = suffix
                self.assertEqual(hybrid._validation_error(document([product]), URL), "missing_product_identity")

    def test_foreign_unsafe_and_credential_bearing_product_links_are_rejected(self):
        unsafe = (
            "https://example.invalid/smart-tv/p/201", "//example.invalid/smart-tv/p/201",
            "javascript:alert('/p/201')", "file:///p/201", "data:text/plain,/p/201",
            "https://www.casasbahia.com.br.evil.invalid/p/201",
            "https://user:fixture@www.casasbahia.com.br/p/201",
            "https://www.casasbahia.com.br:1234/p/201", "https://www.casasbahia.com.br:bad/p/201",
            "https://www.casasbahia.com.br/\n/p/201",
        )
        for link in unsafe:
            with self.subTest(link=link):
                product = source_product()
                product.update(href=link, url=link)
                self.assertEqual(hybrid._validation_error(document([product]), URL), "missing_product_identity")

    def test_relative_absolute_and_normal_web_ports_preserve_valid_casas_links(self):
        links = (
            "/smart-tv/p/201", "smart-tv/p/201", "//www.casasbahia.com.br/smart-tv/p/201",
            "https://casasbahia.com.br/smart-tv/p/201", "https://www.casasbahia.com.br:443/smart-tv/p/201",
            "http://www.casasbahia.com.br:80/smart-tv/p/201",
        )
        for link in links:
            with self.subTest(link=link):
                product = source_product()
                product.update(href=link, url=link)
                self.assertEqual(hybrid._validation_error(document([product]), URL), "")

    def test_conflicting_or_foreign_second_link_alias_is_rejected(self):
        for link in ("/smart-tv/p/999", "https://example.invalid/p/201"):
            with self.subTest(link=link):
                product = source_product()
                product["url"] = link
                self.assertEqual(hybrid._validation_error(document([product]), URL), "missing_product_identity")

    def test_parser_cannot_replace_a_valid_url_with_foreign_same_sku(self):
        text = document()
        rows = self.rows(text)
        rows[0]["product_url"] = "https://example.invalid/p/201"
        with patch.object(parsers, "parse_listing", return_value=rows):
            self.assertEqual(hybrid._validation_error(text, URL), "parsed_identity_or_price_mismatch")

    def test_optional_seller_invalid_or_conflicting_does_not_block_valid_url(self):
        for seller in (0, -1, True, "invalid"):
            with self.subTest(seller=seller):
                product = source_product()
                product["lojista"] = seller
                self.assertEqual(hybrid._validation_error(document([product]), URL), "")
        product = source_product(priced=True)
        product["sellerId"] = 9
        self.assertEqual(hybrid._validation_error(document([product]), URL), "")

    def test_duplicate_url_sku_not_excused_by_missing_seller(self):
        self.assertEqual(hybrid._validation_error(document([source_product(), source_product()]), URL),
                         "duplicate_product_identity")

    def test_explicit_page_and_sort_mismatches_still_fail(self):
        self.assertEqual(hybrid._validation_error(document(query={"page": "2"}), URL), "requested_page_mismatch")
        self.assertEqual(hybrid._validation_error(document(query={"page": "1", "sortby": "menorpreco"}),
                                                URL + "&ordenacao=maisvendidos"), "requested_sort_mismatch")

    def test_filtered_source_order_and_identities_must_survive_parser(self):
        products = [source_product(201), source_product(202)]
        text = document(products)
        actual_rows = self.rows(text)
        self.assertEqual(hybrid._validation_error(text, URL), "")
        for bad_rows in (list(reversed(actual_rows)), actual_rows[:1], actual_rows + actual_rows[:1]):
            with self.subTest(row_count=len(bad_rows)):
                with patch.object(parsers, "parse_listing", return_value=bad_rows):
                    self.assertEqual(hybrid._validation_error(text, URL), "parsed_identity_or_price_mismatch")
        bad_rows = deepcopy(actual_rows)
        bad_rows[0]["retailer_product_id"] = "999"
        bad_rows[0]["seller_id"] = "unknown"
        with patch.object(parsers, "parse_listing", return_value=bad_rows):
            self.assertEqual(hybrid._validation_error(text, URL), "")

    def test_existing_relevance_filter_drops_accessories_without_reordering_tvs(self):
        accessory = source_product(202)
        accessory["title"] = "Suporte para TV fixture"
        text = document([source_product(201), accessory, source_product(203)])
        self.assertEqual(hybrid._validation_error(text, URL), "")
        self.assertEqual([parsers.sku_from_url(row["product_url"]) for row in self.rows(text)], ["201", "203"])

    def test_no_relevant_products_still_fails(self):
        accessory = source_product()
        accessory["title"] = "Suporte para TV fixture"
        self.assertEqual(hybrid._validation_error(document([accessory]), URL), "no_relevant_parsed_products")

    def test_all_modes_retained_and_mode_one_is_default(self):
        self.assertEqual(set(listing_modes.MODES), {"1", "2", "3", "4"})
        self.assertEqual(listing_modes.DEFAULT_MODE, "1")

    def test_legacy_modes_ignore_all_new_pending_and_quote_guard_behavior(self):
        for mode in ("1", "2", "3"):
            with self.subTest(mode=mode):
                self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = mode
                product = source_product(priced=True)
                product.pop("_casas_listing_url_first")
                product.update(idSku=999, _casas_listing_price_pending=True, _casas_listing_seller_pending=True)
                product["price"]["skuId"] = 999
                row = self.rows(document([product]))[0]
                self.assertEqual(row["final_sku_price"], "R$1.500,00")
                self.assertEqual(row["original_sku_price"], "R$1.800,00")
                self.assertEqual(row["retailer_product_id"], "1201")
                self.assertEqual(row["seller_id"], "7")
                self.assertEqual(row["parse_status"], "listing_casas_bahia_ssr")
                self.assertIs(parsers._casas_bahia_guard_listing_quote(product, BASE), product)

    def test_legacy_missing_price_mapping_and_dom_backfill_remain_unchanged(self):
        product = source_product(priced=True)
        product.pop("_casas_listing_url_first")
        product["price"].pop("currentPrice")
        row = self.rows(document([product]))[0]
        self.assertEqual(row["final_sku_price"], parsers.format_brl(product["price"]))
        product["price"] = None
        product["_casas_listing_price_pending"] = True
        snapshot = {BASE + "/smart-tv-samsung/p/201": {"final_sku_price": "R$8.888,00"}}
        with patch.object(parsers, "_casas_bahia_card_snapshots", return_value=snapshot):
            row = self.rows(document([product]))[0]
        self.assertEqual(row["final_sku_price"], "R$8.888,00")

    def test_url_first_marker_requires_exact_boolean_true_for_both_row_mappers(self):
        for marker in (None, False, "true", "1", 1):
            for mapper in (parsers._casas_bahia_ssr_product_row, parsers._casas_bahia_product_row):
                with self.subTest(marker=marker, mapper=mapper.__name__):
                    product = source_product(priced=True)
                    product.update(_casas_listing_url_first=marker, _casas_listing_price_pending=True,
                                   _casas_listing_seller_pending=True)
                    if mapper is parsers._casas_bahia_product_row:
                        product.update(price=1500, oldPrice=1800)
                    row = mapper(product, BASE, URL, "main", 1)
                    self.assertEqual(row["final_sku_price"], "R$1.500,00")
                    self.assertEqual(row["seller_id"], "7")
                    self.assertNotIn("casas_listing_price_pending", row["parse_status"])


if __name__ == "__main__":
    unittest.main()
