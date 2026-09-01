import json
import os
import unittest
from unittest.mock import patch

from seda.magalu.detail_api import _detail_from_item
from seda.parsers import (
    magalu_coupon_text,
    magalu_offer_coupon_result,
    parse_detail,
)
from seda.step08_detail_enrichment import (
    _merge_authoritative_detail,
    _merge_magalu_pdp_html,
)


PRODUCT_URL = (
    "https://www.magazineluiza.com.br/smart-tv/p/240147300/et/tvcr/"
    "?seller_id=magazineluiza"
)


def _offer(seller_id, discount_value=None, *, tag_type="coupon"):
    tags = []
    if discount_value is not None:
        tags.append(
            {
                "type": tag_type,
                "discountValue": discount_value,
                "message": f"R$ {discount_value} OFF com cupom",
            }
        )
    return {
        "variationId": "240147300",
        "price": 2399,
        "listPrice": 2499,
        "bestPrice": {"totalAmount": 2279},
        "seller": {
            "id": seller_id,
            "sku": "UN43U8600FGXZD",
            "description": seller_id,
            "tags": tags,
        },
    }


def _item(offers):
    return {
        "id": "240147300",
        "title": 'Smart TV 43" Samsung 4K UHD',
        "description": "",
        "path": "/smart-tv/p/240147300/et/tvcr/",
        "attributes": [],
        "dimensions": {},
        "bundles": [],
        "factsheet": [],
        "offers": offers,
        "rating": {},
        "category": {},
        "subcategory": {},
    }


def _pdp_html(item, body=""):
    payload = {"props": {"pageProps": {"data": {"item": item}}}}
    return (
        "<html><body>"
        + body
        + '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


class MagaluDiscountTypeDetailTest(unittest.TestCase):
    def test_coupon_amount_is_not_hardcoded(self):
        cases = (
            (1, "Cupom R$ 1 OFF"),
            (100, "Cupom R$ 100 OFF"),
            (200, "Cupom R$ 200 OFF"),
            (300, "Cupom R$ 300 OFF"),
            ("1,5", "Cupom R$ 1,5 OFF"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    magalu_coupon_text(
                        [{"type": "Coupon", "discountValue": value}]
                    ),
                    expected,
                )

    def test_item_graphql_uses_the_requested_seller_coupon(self):
        item = _item(
            [
                _offer("other-seller", 300),
                _offer("magazineluiza", 200),
            ]
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}, clear=False):
            detail = _detail_from_item(item, seller_id="magazineluiza")
        self.assertEqual(detail["discount_type"], "Cupom R$ 200 OFF")
        self.assertTrue(detail["_discount_type_checked"])

    def test_item_graphql_rejects_a_different_seller_coupon(self):
        item = _item([_offer("other-seller", 300)])
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}, clear=False):
            detail = _detail_from_item(item, seller_id="magazineluiza")
        self.assertEqual(detail["discount_type"], "")
        self.assertFalse(detail["_discount_type_checked"])

    def test_empty_tags_confirms_that_the_seller_has_no_coupon(self):
        coupon_text, checked = magalu_offer_coupon_result(
            _offer("magazineluiza"),
            seller_id="magazineluiza",
        )
        self.assertEqual(coupon_text, "")
        self.assertTrue(checked)

    def test_missing_tags_means_coupon_check_failed(self):
        offer = _offer("magazineluiza")
        offer["seller"].pop("tags")
        coupon_text, checked = magalu_offer_coupon_result(
            offer,
            seller_id="magazineluiza",
        )
        self.assertEqual(coupon_text, "")
        self.assertFalse(checked)

    def test_pdp_next_data_uses_the_requested_seller_coupon(self):
        item = _item(
            [
                _offer("other-seller", 300),
                _offer("magazineluiza", 100),
            ]
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}, clear=False):
            detail = parse_detail(
                _pdp_html(item),
                "Magalu",
                "https://www.magazineluiza.com.br",
                PRODUCT_URL,
            )
        self.assertEqual(detail["discount_type"], "Cupom R$ 100 OFF")

    def test_pdp_coupon_container_is_the_final_fallback(self):
        item = _item([_offer("magazineluiza")])
        body = (
            '<section data-testid="coupon-code-container">'
            "<span>R$ 1 OFF</span><button>Copiar</button>"
            "</section>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}, clear=False):
            detail = parse_detail(
                _pdp_html(item, body=body),
                "Magalu",
                "https://www.magazineluiza.com.br",
                PRODUCT_URL,
            )
        self.assertEqual(detail["discount_type"], "Cupom R$ 1 OFF")

    def test_no_coupon_remains_blank(self):
        item = _item([_offer("magazineluiza")])
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}, clear=False):
            detail = parse_detail(
                _pdp_html(item),
                "Magalu",
                "https://www.magazineluiza.com.br",
                PRODUCT_URL,
            )
        self.assertEqual(detail.get("discount_type", ""), "")

    def test_detail_coupon_wins_but_blank_does_not_erase_listing_value(self):
        row = {
            "retailer": "Magalu",
            "product_line": "TV",
            "sku": "UN43U8600FGXZD",
            "discount_type": "Cupom R$ 100 OFF",
        }
        _merge_authoritative_detail(
            row,
            {"discount_type": "Cupom R$ 300 OFF"},
            identity_verified=True,
        )
        self.assertEqual(row["discount_type"], "Cupom R$ 300 OFF")

        _merge_authoritative_detail(
            row,
            {"discount_type": ""},
            identity_verified=True,
        )
        self.assertEqual(row["discount_type"], "Cupom R$ 300 OFF")

    def test_pdp_html_runs_when_coupon_check_failed(self):
        row = self._complete_detail_row()
        with patch(
            "seda.step08_detail_enrichment._fetch_magalu_next_html",
            return_value={
                "status_code": 0,
                "text": "",
                "method": "test",
                "label": "pdp",
                "error": "test_failure",
            },
        ) as fetch:
            _merge_magalu_pdp_html(row, PRODUCT_URL)
        fetch.assert_called_once_with(PRODUCT_URL, label="pdp")

    def test_pdp_html_skips_confirmed_no_coupon(self):
        row = self._complete_detail_row()
        row["_discount_type_checked"] = True
        with patch(
            "seda.step08_detail_enrichment._fetch_magalu_next_html"
        ) as fetch:
            _merge_magalu_pdp_html(row, PRODUCT_URL)
        fetch.assert_not_called()

    @staticmethod
    def _complete_detail_row():
        return {
            "retailer": "Magalu",
            "product_line": "TV",
            "product_url": PRODUCT_URL,
            "discount_type": "",
            "screen_size": '43"',
            "estimated_annual_electricity_use": "100 kWh/ano",
            "star_rating": "4.9",
            "count_of_star_ratings": "100",
            "count_of_reviews": "0",
            "summarized_review_content": "Resumo",
            "retailer_sku_name_similar": '["TV similar"]',
        }


if __name__ == "__main__":
    unittest.main()
