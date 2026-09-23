from copy import deepcopy
import unittest

from seda.casas_bahia import browser_listing as legacy_browser
from seda.casas_bahia import browser_listing_url_first as browser


def product(**values):
    item = {"id": 10, "idSku": 100, "href": "/smart-tv/p/100", "title": "Smart TV Samsung"}
    item.update(values)
    return item


def normalized_offer(**values):
    item = {
        "productId": 10,
        "skuId": 100,
        "sellerId": 7,
        "oldPrice": 1200,
        "currentPrice": 900,
        "availability": {},
    }
    item.update(values)
    return item


class ProductUrlIdentityTests(unittest.TestCase):
    def test_mode4_isolated_but_reuses_safe_evidence_error_type(self):
        self.assertIs(browser.EvidenceError, legacy_browser.EvidenceError)
        self.assertFalse(hasattr(legacy_browser, "_attach_optional_prices"))

    def test_relative_and_casas_absolute_urls(self):
        self.assertEqual("100", browser._product_url_sku({"href": "/smart-tv/p/100"}))
        self.assertEqual("100", browser._product_url_sku(
            {"url": "https://casasbahia.com.br/smart-tv/p/100"},
        ))

    def test_conflicting_or_untrusted_url_aliases_are_rejected(self):
        invalid = (
            {"href": "/smart-tv/p/100", "url": "/smart-tv/p/200"},
            {"href": "https://evil.example/smart-tv/p/100"},
            {"href": "https://user@www.casasbahia.com.br/smart-tv/p/100"},
            {"href": "https://www.casasbahia.com.br:444/smart-tv/p/100"},
            {"href": "/smart-tv/p/100\nignored"},
        )
        for item in invalid:
            with self.subTest(item=item):
                self.assertEqual("", browser._product_url_sku(item))


class OptionalPriceAttachmentTests(unittest.TestCase):
    def test_no_offer_keeps_url_and_marks_optional_fields_pending(self):
        items = [product()]
        counts = browser._attach_optional_prices(items, [])
        self.assertEqual({"price_pending_products": 1, "seller_pending_products": 1}, counts)
        self.assertEqual(("100", "100", 10), (items[0]["idSku"], items[0]["sku"], items[0]["id"]))
        self.assertEqual({}, items[0]["price"])
        self.assertTrue(items[0]["_casas_listing_url_first"])

    def test_unique_offer_attaches_price_and_seller(self):
        items = [product()]
        counts = browser._attach_optional_prices(items, [normalized_offer()])
        self.assertEqual({"price_pending_products": 0, "seller_pending_products": 0}, counts)
        self.assertEqual(900, items[0]["price"]["currentPrice"])
        self.assertEqual(("7", "7"), (items[0]["lojista"], items[0]["sellerId"]))

    def test_source_sku_conflict_is_canonical_and_durably_quarantined(self):
        items = [product(idSku=999, sku=999)]
        first = browser._attach_optional_prices(items, [])
        second = browser._attach_optional_prices(items, [normalized_offer()])
        self.assertEqual(1, first["price_pending_products"])
        self.assertEqual(1, second["price_pending_products"])
        self.assertEqual(("100", "100"), (items[0]["idSku"], items[0]["sku"]))
        self.assertNotIn("id", items[0])
        self.assertTrue(items[0]["_casas_listing_product_id_quarantined"])

    def test_mismatched_external_product_offer_does_not_remove_trusted_source_id(self):
        items = [product()]
        counts = browser._attach_optional_prices(items, [normalized_offer(productId=999)])
        self.assertEqual(1, counts["price_pending_products"])
        self.assertEqual(10, items[0]["id"])

    def test_ambiguous_sellers_leave_price_and_seller_pending(self):
        items = [product()]
        counts = browser._attach_optional_prices(
            items, [normalized_offer(sellerId=7), normalized_offer(sellerId=8)],
        )
        self.assertEqual({"price_pending_products": 1, "seller_pending_products": 1}, counts)
        self.assertNotIn("lojista", items[0])

    def test_source_seller_conflict_is_durably_quarantined(self):
        items = [product(lojista=7, sellerId=8)]
        first = browser._attach_optional_prices(items, [])
        second = browser._attach_optional_prices(items, [normalized_offer(sellerId=7)])
        self.assertEqual({"price_pending_products": 1, "seller_pending_products": 1}, first)
        self.assertEqual({"price_pending_products": 1, "seller_pending_products": 1}, second)
        self.assertTrue(items[0]["_casas_listing_seller_quarantined"])
        self.assertNotIn("lojista", items[0])
        self.assertNotIn("sellerId", items[0])

    def test_legacy_price_dict_without_identity_is_preserved_but_scalar_is_not(self):
        quoted = [product(price={"currentPrice": 900})]
        scalar = [product(price=900)]
        self.assertEqual(0, browser._attach_optional_prices(quoted, [])["price_pending_products"])
        self.assertEqual(900, quoted[0]["price"]["currentPrice"])
        self.assertEqual(1, browser._attach_optional_prices(scalar, [])["price_pending_products"])
        self.assertEqual({}, scalar[0]["price"])

    def test_embedded_nonfinite_huge_or_availability_conflict_quote_is_pending(self):
        bad_prices = (
            {"currentPrice": "NaN"},
            {"currentPrice": "9" * 5000},
            {"currentPrice": 900, "skuId": 100, "availability": {"IdSku": 200}},
            {"currentPrice": 900, "sellerId": 7, "availability": {"IdLojista": 8}},
        )
        for price in bad_prices:
            with self.subTest(price_kind=list(price)):
                items = [product(price=price)]
                counts = browser._attach_optional_prices(items, [])
                self.assertEqual(1, counts["price_pending_products"])
                self.assertEqual({}, items[0]["price"])

    def test_malformed_or_conflicting_offer_identity_is_not_a_wildcard(self):
        invalid = (
            normalized_offer(productId="bad"),
            normalized_offer(sellerId="bad"),
            normalized_offer(availability={"IdSku": 200}),
            normalized_offer(availability={"IdLojista": 8}),
            normalized_offer(currentPrice="NaN"),
            normalized_offer(currentPrice="Infinity"),
            normalized_offer(currentPrice="not a price"),
        )
        for quote in invalid:
            with self.subTest(quote=quote):
                items = [product()]
                counts = browser._attach_optional_prices(items, [quote])
                self.assertEqual(1, counts["price_pending_products"])

    def test_mapping_values_and_repeat_attachment_are_supported(self):
        items = [product()]
        quote = normalized_offer()
        browser._attach_optional_prices(items, {("10", "100", "7"): quote})
        counts = browser._attach_optional_prices(items, {("10", "100", "7"): deepcopy(quote)})
        self.assertEqual({"price_pending_products": 0, "seller_pending_products": 0}, counts)

    def test_pathological_optional_id_never_raises(self):
        items = [product(id="9" * 5000)]
        counts = browser._attach_optional_prices(items, [])
        self.assertEqual(1, counts["price_pending_products"])
        self.assertEqual(5000, len(items[0]["id"]))


class TolerantRawOfferTests(unittest.TestCase):
    @staticmethod
    def raw_offer(product_id, sku, seller, price):
        return {
            "PrecoVenda": {"IdProduto": product_id, "IdSku": sku, "IdLojista": seller, "Preco": price},
            "DescontoFormaPagamento": {},
            "Disponibilidade": {},
        }

    def test_partial_normalization_drops_only_conflicted_key(self):
        conflict_a = self.raw_offer(10, 100, 7, 900)
        conflict_b = self.raw_offer(10, 100, 7, 950)
        valid = self.raw_offer(20, 200, 8, 800)
        values = browser._normalize_offers(
            [{"Ofertas": [conflict_a, conflict_b, valid, "invalid"]}], allow_partial=True,
        )
        self.assertNotIn(("10", "100", "7"), values)
        self.assertIn(("20", "200", "8"), values)

    def test_bad_numeric_quote_does_not_block_other_url_or_good_quote(self):
        bad = self.raw_offer(10, 100, 7, 900)
        bad["PrecoVenda"]["PrecoDe"] = "NaN"
        good = self.raw_offer(20, 200, 8, 800)
        values = browser._normalize_offers([{"Ofertas": [bad, good]}], allow_partial=True)
        items = [product(), product(id=20, idSku=200, href="/other-tv/p/200")]
        counts = browser._attach_optional_prices(items, values)
        self.assertEqual(1, counts["price_pending_products"])
        self.assertEqual({}, items[0]["price"])
        self.assertEqual(800, items[1]["price"]["currentPrice"])


if __name__ == "__main__":
    unittest.main()
