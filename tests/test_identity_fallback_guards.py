import json
import unittest
from unittest.mock import patch

from seda.casas_bahia.detail_html_backfill import (
    _merge as merge_casas_html,
    _needs_backfill as needs_casas_html_backfill,
)
from seda.casas_bahia.sku_contract import (
    PDP_HTML_MODEL_TOKEN,
    PRODUCT_SOURCE_MODEL_TOKEN,
)
from seda.magalu.ai_summary_curl_backfill import _trusted_summary
from seda.parsers import CASAS_TV_EXACT_MODELO_FIELD
from seda.step08_detail_enrichment import _merge_magalu_review_pages


class IdentityFallbackGuardTests(unittest.TestCase):
    def test_casas_html_backfill_blocks_conflict_and_allows_exact_name(self):
        conflict_row = {
            'retailer_sku_name': 'Geladeira Principal',
            'estimated_annual_electricity_use': '',
        }
        conflict = {
            'retailer_sku_name': 'Geladeira Errada',
            'estimated_annual_electricity_use': '999 W',
            '_detail_identity_conflict': True,
        }
        self.assertFalse(merge_casas_html(conflict_row, conflict))
        self.assertEqual(conflict_row['estimated_annual_electricity_use'], '')
        self.assertIn('identity_conflict', conflict_row['parse_status'])

        same_name_row = {
            'retailer_sku_name': 'Geladeira Principal',
            'estimated_annual_electricity_use': '',
        }
        same_name = {
            'retailer_sku_name': '  Geladeira---Principal  ',
            'estimated_annual_electricity_use': '130 W',
        }
        self.assertTrue(merge_casas_html(same_name_row, same_name))
        self.assertEqual(same_name_row['estimated_annual_electricity_use'], '130 W')

    def test_casas_html_backfill_covers_ref_and_ldy_semantic_fields(self):
        ref_row = {
            "account_name": "CasasBahia",
            "product": "REF",
            "retailer_sku_name": "Geladeira Principal",
            "estimated_annual_electricity_use": "130 W",
            "ref_capacity": "",
        }
        self.assertTrue(needs_casas_html_backfill(ref_row))
        self.assertFalse(
            needs_casas_html_backfill(
                {
                    **ref_row,
                    "account_name": "Magalu",
                }
            )
        )
        self.assertTrue(
            merge_casas_html(
                ref_row,
                {
                    "retailer_sku_name": "Geladeira Principal",
                    "ref_capacity": "305 L",
                    "_detail_identity_verified": True,
                },
            )
        )
        self.assertEqual(ref_row["ref_capacity"], "305 L")
        self.assertFalse(needs_casas_html_backfill(ref_row))

        ldy_row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "retailer_sku_name": "Lavadora Principal",
            "estimated_annual_electricity_use": "0,4 kWh/ciclo",
            "ldy_capacity": "14 kg",
            "ldy_loading_type": "",
        }
        self.assertTrue(needs_casas_html_backfill(ldy_row))
        self.assertTrue(
            merge_casas_html(
                ldy_row,
                {
                    "retailer_sku_name": "Lavadora Principal",
                    "ldy_loading_type": "Top load",
                    "_detail_identity_verified": True,
                },
            )
        )
        self.assertEqual(ldy_row["ldy_loading_type"], "Top load")
        self.assertFalse(needs_casas_html_backfill(ldy_row))

    def test_casas_tv_html_backfill_preserves_rest_and_requires_verified_modelo(self):
        base_row = {
            "retailer": "Casas Bahia",
            "product_line": "TV",
            "product_url": "https://www.casasbahia.com.br/smart-tv/p/123",
            "retailer_sku_name": "Smart TV Principal",
            "sku": "55REST",
            "parse_status": PRODUCT_SOURCE_MODEL_TOKEN,
        }
        same_name = {
            "retailer_sku_name": "smart-tv principal",
            "sku": "BAD-SAME-NAME",
            CASAS_TV_EXACT_MODELO_FIELD: True,
        }
        self.assertTrue(merge_casas_html(base_row, same_name))
        self.assertEqual(base_row["sku"], "55REST")
        self.assertIn(
            PRODUCT_SOURCE_MODEL_TOKEN,
            base_row["parse_status"].split("+"),
        )

        verified_html = {
            "retailer_sku_name": "Smart TV Principal",
            "sku": "55HTML",
            CASAS_TV_EXACT_MODELO_FIELD: True,
            "_detail_identity_verified": True,
            "parse_status": "detail_casas_bahia_html",
        }
        self.assertTrue(merge_casas_html(base_row, verified_html))
        self.assertEqual(base_row["sku"], "55REST")
        self.assertIn(
            PRODUCT_SOURCE_MODEL_TOKEN,
            base_row["parse_status"].split("+"),
        )
        self.assertNotIn(PDP_HTML_MODEL_TOKEN, base_row["parse_status"].split("+"))

        html_only_row = {
            **base_row,
            "sku": "123",
            "parse_status": PRODUCT_SOURCE_MODEL_TOKEN,
        }
        self.assertTrue(merge_casas_html(html_only_row, verified_html))
        self.assertEqual(html_only_row["sku"], "55HTML")
        self.assertIn(PDP_HTML_MODEL_TOKEN, html_only_row["parse_status"].split("+"))
        self.assertNotIn(
            PRODUCT_SOURCE_MODEL_TOKEN,
            html_only_row["parse_status"].split("+"),
        )

    def test_magalu_review_pages_block_conflict_and_allow_exact_name(self):
        response = {
            'status_code': 200,
            'text': '<script id="__NEXT_DATA__">{}</script>',
            'method': 'test',
            'error': '',
        }
        conflict_row = {
            'retailer': 'Magalu',
            'retailer_sku_name': 'Smart TV Principal',
            'count_of_reviews': '1',
            'detailed_review_content': '',
        }
        conflict = {
            'retailer_sku_name': 'Smart TV Errada',
            'star_rating': '5',
            'count_of_star_ratings': '99',
            'count_of_reviews': '1',
            'detailed_review_content': json.dumps(['review errada']),
            '_detail_identity_conflict': True,
        }
        with patch(
            'seda.step08_detail_enrichment._fetch_magalu_next_html',
            return_value=response,
        ), patch(
            'seda.step08_detail_enrichment.parse_detail',
            return_value=conflict,
        ):
            result = _merge_magalu_review_pages(
                conflict_row,
                'https://www.magazineluiza.com.br/item/p/sample/et/tv4k/',
            )
        self.assertFalse(result['success'])
        self.assertFalse(conflict_row.get('star_rating'))
        self.assertFalse(conflict_row.get('detailed_review_content'))
        self.assertIn('identity_conflict', conflict_row['parse_status'])

        same_name_row = {
            'retailer': 'Magalu',
            'retailer_sku_name': 'Smart TV Principal',
            'count_of_reviews': '1',
            'detailed_review_content': '',
        }
        same_name = {
            'retailer_sku_name': 'smart-tv principal',
            'star_rating': '4.8',
            'count_of_star_ratings': '10',
            'count_of_reviews': '1',
            'detailed_review_content': json.dumps(['review correta']),
            'total_review_pages': '1',
        }
        with patch(
            'seda.step08_detail_enrichment._fetch_magalu_next_html',
            return_value=response,
        ), patch(
            'seda.step08_detail_enrichment.parse_detail',
            return_value=same_name,
        ):
            result = _merge_magalu_review_pages(
                same_name_row,
                'https://www.magazineluiza.com.br/item/p/sample/et/tv4k/',
            )
        self.assertTrue(result['success'])
        self.assertEqual(same_name_row['star_rating'], '4.8')
        self.assertIn('review correta', same_name_row['detailed_review_content'])

    def test_ai_summary_requires_verified_or_exact_name_identity(self):
        row = {'retailer_sku_name': 'Smart TV Principal'}
        conflict = {
            'retailer_sku_name': 'Smart TV Principal',
            'summarized_review_content': 'wrong',
            '_detail_identity_conflict': True,
        }
        self.assertEqual(_trusted_summary(row, conflict), '')
        self.assertEqual(
            _trusted_summary(
                row,
                {
                    'retailer_sku_name': 'smart-tv principal',
                    'summarized_review_content': 'same-name summary',
                },
            ),
            'same-name summary',
        )
        self.assertEqual(
            _trusted_summary(
                row,
                {
                    'retailer_sku_name': 'Different Product',
                    'summarized_review_content': 'verified summary',
                    '_detail_identity_verified': True,
                },
            ),
            'verified summary',
        )


if __name__ == '__main__':
    unittest.main()
