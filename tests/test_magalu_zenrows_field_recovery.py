import os
import unittest
from unittest.mock import patch

from seda.step08_detail_enrichment import (
    MAGALU_ZENROWS_FIELD_MAP,
    _backfill_magalu_detail_blanks,
    _backfill_magalu_zenrows_fields,
    _magalu_zenrows_missing_fields,
)


def _failed_row(**values):
    row = {
        "retailer": "Magalu",
        "product_line": "TV",
        "item": "item-1",
        "product_url": "https://www.magazineluiza.com.br/product/p/item-1/et/elit/",
        "screen_size": "",
        "estimated_annual_electricity_use": "",
        "model_year": "",
        "fetch_method": "browser",
        "parse_status": (
            "detail_graphql_failed:item_query_failed+detail_blank_retry_failed"
        ),
    }
    row.update(values)
    return row


class MagaluZenRowsFieldRecoveryTest(unittest.TestCase):
    def _enabled_env(self, **extra):
        values = {
            "SEDA_ALLOW_ZENROWS": "1",
            "SEDA_ZENROWS_DRY_RUN": "0",
            "SEDA_MAGALU_ZENROWS_FIELD_FALLBACK": "1",
            "SEDA_MAGALU_ZENROWS_FIELD_MAX_ITEMS": "25",
            "SEDA_MAGALU_ZENROWS_FIELD_FAILURE_STREAK": "3",
        }
        values.update(extra)
        return patch.dict(os.environ, values, clear=False)

    def _interleaved_default_env(self, **extra):
        values = {
            "SEDA_ACTIVE_RETAILER": "magalu",
            "SEDA_MAGALU_DEFAULT_ALLOW_ZENROWS": "1",
            "SEDA_MAGALU_DEFAULT_ZENROWS_DRY_RUN": "0",
            "SEDA_MAGALU_ZENROWS_FIELD_FALLBACK": "1",
            "SEDA_MAGALU_ZENROWS_FIELD_MAX_ITEMS": "25",
            "SEDA_MAGALU_ZENROWS_FIELD_FAILURE_STREAK": "3",
        }
        values.update(extra)
        return patch.dict(os.environ, values, clear=True)

    def test_exact_product_line_field_contract(self):
        self.assertEqual(
            MAGALU_ZENROWS_FIELD_MAP,
            {
                "TV": (
                    "screen_size",
                    "estimated_annual_electricity_use",
                    "model_year",
                ),
                "REF": ("ref_refrigerator_type", "ref_capacity"),
                "LDY": ("ldy_loading_type", "ldy_capacity"),
            },
        )

    def test_interleaved_retailer_defaults_enable_recovery_without_global_switches(self):
        row = _failed_row()
        response = {
            "success": True,
            "detail": {"screen_size": '65"'},
            "trace": [],
            "zenrows": {"status_code": 200},
        }
        with self._interleaved_default_env(), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
            return_value=response,
        ) as fetch:
            rows = _backfill_magalu_zenrows_fields(
                [row],
                "unused.csv",
                checkpoint_every=0,
            )

        fetch.assert_called_once()
        self.assertEqual(rows[0]["screen_size"], '65"')

    def test_explicit_global_disable_overrides_interleaved_retailer_default(self):
        with self._interleaved_default_env(SEDA_ALLOW_ZENROWS="0"), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
        ) as fetch:
            _backfill_magalu_zenrows_fields(
                [_failed_row()],
                "unused.csv",
                checkpoint_every=0,
            )

        fetch.assert_not_called()

    def test_explicit_dry_run_overrides_interleaved_retailer_default(self):
        with self._interleaved_default_env(SEDA_ZENROWS_DRY_RUN="1"), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
        ) as fetch:
            _backfill_magalu_zenrows_fields(
                [_failed_row()],
                "unused.csv",
                checkpoint_every=0,
            )

        fetch.assert_not_called()

    def test_one_item_result_fills_only_missing_fields_and_is_cached(self):
        first = _failed_row(screen_size='77"')
        second = _failed_row(
            product_url=(
                "https://www.magazineluiza.com.br/other/p/item-1/et/elit/"
                "?seller_id=other"
            )
        )
        response = {
            "success": True,
            "detail": {
                "screen_size": '75"',
                "estimated_annual_electricity_use": "245W",
                "model_year": "2025",
                "sku": "must-not-merge",
            },
            "trace": [],
            "zenrows": {"status_code": 200, "request_cost": "unit"},
        }
        with self._enabled_env(), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
            return_value=response,
        ) as fetch:
            rows = _backfill_magalu_zenrows_fields(
                [first, second],
                "unused.csv",
                checkpoint_every=0,
                trace_rows=[],
            )

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(rows[0]["screen_size"], '77"')
        self.assertEqual(rows[0]["estimated_annual_electricity_use"], "245W")
        self.assertEqual(rows[0]["model_year"], "2025")
        self.assertNotEqual(rows[0].get("sku"), "must-not-merge")
        self.assertEqual(rows[1]["screen_size"], '75"')
        self.assertIn("zenrows_graphql_item", rows[1]["fetch_method"])

    def test_successful_normal_graphql_and_casas_rows_are_not_candidates(self):
        successful = _failed_row(
            parse_status="detail_item_graphql",
            screen_size='55"',
            estimated_annual_electricity_use="100W",
            model_year="",
        )
        casas = _failed_row(retailer="Casas Bahia")
        with self._enabled_env(), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
        ) as fetch:
            rows = _backfill_magalu_zenrows_fields(
                [successful, casas],
                "unused.csv",
                checkpoint_every=0,
            )

        fetch.assert_not_called()
        self.assertEqual(_magalu_zenrows_missing_fields(rows[0]), ("model_year",))

    def test_verified_zenrows_item_result_prefers_title_sku(self):
        row = _failed_row(sku="")
        response = {
            "success": True,
            "detail": {
                "screen_size": '75"',
                "estimated_annual_electricity_use": "245W",
                "model_year": "2025",
                "_magalu_factsheet_reference": "UN75U8600FGXZD",
                "_detail_item_id": "item-1",
                "_detail_identity_verified": True,
                "retailer_sku_name": 'Smart TV Samsung 75" 75U8600F',
            },
            "trace": [],
            "zenrows": {"status_code": 200, "request_cost": "unit"},
        }
        with self._enabled_env(), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
            return_value=response,
        ):
            rows = _backfill_magalu_zenrows_fields(
                [row],
                "unused.csv",
                checkpoint_every=0,
            )

        self.assertEqual(rows[0]["sku"], "75U8600F")
        self.assertIn(
            "sku_title_high_confidence_recovered",
            rows[0]["parse_status"].split("+"),
        )

    def test_title_recovered_sku_skips_reference_only_paid_retry(self):
        complete = {
            "screen_size": '75"',
            "estimated_annual_electricity_use": "245W",
            "model_year": "2025",
        }
        blank = _failed_row(sku="", **complete)
        title_fallback = _failed_row(
            sku="75U8600F",
            retailer_sku_name='Smart TV Samsung 75" 75U8600F',
            parse_status=(
                "detail_graphql_failed:item_query_failed"
                "+detail_blank_retry_failed"
                "+sku_title_fallback_after_detail_retry"
            ),
            **complete,
        )
        response = {
            "success": True,
            "detail": {
                "_magalu_factsheet_reference": "UN75U8600FGXZD",
                "_detail_item_id": "item-1",
                "_detail_identity_verified": True,
                "retailer_sku_name": 'Smart TV Samsung 75" 75U8600F',
            },
            "trace": [],
            "zenrows": {"status_code": 200, "request_cost": "unit"},
        }
        with self._enabled_env(), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
            return_value=response,
        ) as fetch:
            rows = _backfill_magalu_zenrows_fields(
                [blank, title_fallback],
                "unused.csv",
                checkpoint_every=0,
            )

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([row["sku"] for row in rows], [
            "75U8600F",
            "75U8600F",
        ])
        self.assertIn(
            "zenrows_field_recovered:sku",
            rows[0]["parse_status"].split("+"),
        )
        self.assertNotIn(
            "zenrows_field_recovered:sku",
            rows[1]["parse_status"].split("+"),
        )

    def test_synthetic_sku_uses_free_title_recovery_before_zenrows(self):
        row = _failed_row(
            sku='Smart TV Samsung 75" 75U8600F',
            retailer_sku_name='Smart TV Samsung 75" 75U8600F',
            screen_size='75"',
            estimated_annual_electricity_use="245W",
            model_year="2025",
        )
        with patch(
            "seda.step08_detail_enrichment._retry_magalu_detail_blanks",
            return_value=False,
        ):
            rows = _backfill_magalu_detail_blanks(
                [row],
                "unused.csv",
                checkpoint_every=0,
            )
        self.assertEqual(rows[0]["sku"], "75U8600F")
        self.assertIn(
            "sku_title_fallback_after_detail_retry",
            rows[0]["parse_status"].split("+"),
        )

        with self._enabled_env(), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
        ) as fetch:
            _backfill_magalu_zenrows_fields(
                rows,
                "unused.csv",
                checkpoint_every=0,
            )
        fetch.assert_not_called()

    def test_missing_core_fields_take_priority_over_sku_only_within_limit(self):
        sku_only = _failed_row(
            item="sku-only",
            product_url=(
                "https://www.magazineluiza.com.br/product/p/sku-only/et/elit/"
            ),
            sku="",
            screen_size='75"',
            estimated_annual_electricity_use="245W",
            model_year="2025",
        )
        field_missing = _failed_row(
            item="field-missing",
            product_url=(
                "https://www.magazineluiza.com.br/product/p/field-missing/et/elit/"
            ),
            sku="EXISTING-SKU",
        )
        response = {
            "success": True,
            "detail": {
                "screen_size": '65"',
                "estimated_annual_electricity_use": "180W",
                "model_year": "2025",
                "_detail_item_id": "field-missing",
                "_detail_identity_verified": True,
            },
            "trace": [],
            "zenrows": {"status_code": 200},
        }
        with self._enabled_env(
            SEDA_MAGALU_ZENROWS_FIELD_MAX_ITEMS="1"
        ), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
            return_value=response,
        ) as fetch:
            rows = _backfill_magalu_zenrows_fields(
                [sku_only, field_missing],
                "unused.csv",
                checkpoint_every=0,
            )

        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], "field-missing")
        self.assertEqual(rows[1]["screen_size"], '65"')
        self.assertEqual(rows[0]["sku"], "")

    def test_failed_result_is_cached_and_never_merges(self):
        rows = [_failed_row(), _failed_row()]
        response = {
            "success": False,
            "error": "http_403",
            "detail": {},
            "trace": [],
            "zenrows": {"status_code": 403},
        }
        with self._enabled_env(), patch(
            "seda.magalu.detail_api.fetch_item_fields_via_zenrows",
            return_value=response,
        ) as fetch:
            _backfill_magalu_zenrows_fields(
                rows,
                "unused.csv",
                checkpoint_every=0,
            )

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(rows[0]["screen_size"], "")
        self.assertIn("zenrows_field_failed:http_403", rows[1]["parse_status"])


if __name__ == "__main__":
    unittest.main()
