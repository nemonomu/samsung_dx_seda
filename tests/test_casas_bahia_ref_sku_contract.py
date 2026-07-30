import os
import unittest
from datetime import datetime
from unittest.mock import patch

from seda.casas_bahia.ref_sku_contract import (
    BRAND_FIELD,
    EVIDENCE_FIELD,
    casas_ref_short_for_output,
    casas_ref_title_sku,
    resolve_casas_ref_sku,
)
from seda.step00_config import OUTPUT_COLUMNS
from seda.step08_detail_enrichment import _merge_casas_bahia_ref_detail
from seda.step15_final_output import _format_row


class CasasBahiaRefSkuContractTests(unittest.TestCase):
    def test_models_without_short_rule_keep_same_full_value(self):
        for title, expected in (
            ("Geladeira Consul CRM44MK 377L", "CRM44MK"),
            ("Geladeira HQ HQ-140RDF 140L", "HQ-140RDF"),
        ):
            with self.subTest(title=title):
                sku = casas_ref_title_sku(title)
                self.assertEqual(sku, expected)
                self.assertEqual(
                    casas_ref_short_for_output(
                        {"retailer_sku_name": title},
                        sku,
                    ),
                    expected,
                )

    def test_panasonic_short_requires_separate_title_alias(self):
        title = "Geladeira Panasonic BB64 NR-BB64PV2B"
        sku = casas_ref_title_sku(title)
        self.assertEqual(sku, "NR-BB64PV2B")
        self.assertEqual(
            casas_ref_short_for_output(
                {"retailer_sku_name": title},
                sku,
            ),
            "BB64",
        )

        title = "Geladeira Panasonic NR-BT41PD2WA"
        sku = casas_ref_title_sku(title)
        self.assertEqual(sku, "NR-BT41PD2WA")
        self.assertEqual(
            casas_ref_short_for_output(
                {"retailer_sku_name": title},
                sku,
            ),
            "NR-BT41PD2WA",
        )

    def test_samsung_short_rule_is_brand_gated(self):
        cases = (
            ("Geladeira Samsung RF49A5202S9", "RF49A5202S9", "RF49A"),
            (
                "Geladeira Midea MD-RT411FGF01",
                "MD-RT411FGF01",
                "MD-RT411FGF01",
            ),
            (
                "Geladeira Hisense RB422P3ESA1",
                "RB422P3ESA1",
                "RB422P3ESA1",
            ),
        )
        for title, full, short in cases:
            with self.subTest(title=title):
                self.assertEqual(casas_ref_title_sku(title), full)
                self.assertEqual(
                    casas_ref_short_for_output(
                        {"retailer_sku_name": title},
                        full,
                    ),
                    short,
                )

    def test_noise_and_ambiguous_titles_do_not_guess(self):
        self.assertEqual(casas_ref_title_sku("Geladeira 1Porta 260L"), "")
        title = "Geladeira Electrolux IM8S FF3P"
        self.assertEqual(casas_ref_title_sku(title), "")
        self.assertEqual(resolve_casas_ref_sku("", title).sku, "")
        resolved = resolve_casas_ref_sku("", title, ("FF3P",))
        self.assertEqual(resolved.sku, "FF3P")

    def test_electrolux_three_character_models_are_context_limited(self):
        for model in ("IM7", "IB7", "IB6"):
            self.assertEqual(
                casas_ref_title_sku(f"Geladeira Electrolux {model}"),
                model,
            )
            self.assertEqual(casas_ref_title_sku(f"Geladeira Outra {model}"), "")

    def test_title_model_precedes_stale_existing_value(self):
        resolved = resolve_casas_ref_sku(
            "STALE99",
            "Geladeira Consul CRM44MK 377L",
        )
        self.assertEqual(resolved.sku, "CRM44MK")

    def test_product_source_merge_keeps_full_and_short_atomic(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "REF",
            "product_url": "https://www.casasbahia.com.br/geladeira/p/123",
            "item": "123",
            "retailer_sku_name": "Geladeira Midea MD-RT411FGF01",
            "sku": "",
            "sku_short_version": "RT41",
            "ref_capacity": "",
        }
        detail = {
            "retailer_sku_name": "Geladeira Midea MD-RT411FGF01",
            "ref_capacity": "411L",
            EVIDENCE_FIELD: ("MD-RT411FGF01",),
            BRAND_FIELD: "Midea",
        }
        self.assertTrue(
            _merge_casas_bahia_ref_detail(
                row,
                detail,
                identity_verified=True,
            )
        )
        self.assertEqual(row["sku"], "MD-RT411FGF01")
        self.assertEqual(row["sku_short_version"], "MD-RT411FGF01")
        self.assertEqual(row["ref_capacity"], "411L")

    def test_final_output_uses_title_full_and_derived_short(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "REF",
            "product_url": "https://www.casasbahia.com.br/geladeira/p/123",
            "item": "123",
            "retailer_sku_name": "Geladeira Consul CRM44MK 377L",
            "sku": "",
            "sku_short_version": "STALE",
        }
        with patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "REF",
                "SEDA_ACTIVE_RETAILER": "casas_bahia",
            },
            clear=False,
        ):
            formatted = _format_row(
                row,
                datetime(2026, 7, 30, 12, 0, 0),
            )
        self.assertEqual(formatted["sku"], "CRM44MK")
        self.assertEqual(formatted["sku_short_version"], "CRM44MK")

    def test_trusted_product_source_pair_survives_output_columns_boundary(self):
        cases = (
            (
                "Samsung",
                "Geladeira Frost Free 385L Inox",
                "RT38K5530S8",
                "RT38K",
            ),
            (
                "Electrolux",
                "Geladeira Bottom Freezer 480L Inox",
                "IM7",
                "IM7",
            ),
        )
        with patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "REF",
                "SEDA_ACTIVE_RETAILER": "casas_bahia",
            },
            clear=False,
        ):
            for brand, title, full, short in cases:
                with self.subTest(brand=brand, full=full):
                    row = {
                        "retailer": "Casas Bahia",
                        "product_line": "REF",
                        "product_url": (
                            "https://www.casasbahia.com.br/geladeira/p/123"
                        ),
                        "item": "123",
                        "retailer_sku_name": title,
                        "sku": "",
                        "sku_short_version": "",
                        "parse_status": "",
                    }
                    self.assertTrue(
                        _merge_casas_bahia_ref_detail(
                            row,
                            {
                                "retailer_sku_name": title,
                                EVIDENCE_FIELD: (full,),
                                BRAND_FIELD: brand,
                            },
                            identity_verified=True,
                        )
                    )
                    projected = {
                        column: row.get(column, "")
                        for column in OUTPUT_COLUMNS
                    }
                    self.assertNotIn(BRAND_FIELD, projected)

                    before = _format_row(
                        row,
                        datetime(2026, 7, 30, 12, 0, 0),
                    )
                    after = _format_row(
                        projected,
                        datetime(2026, 7, 30, 12, 0, 0),
                    )
                    self.assertEqual(
                        (before["sku"], before["sku_short_version"]),
                        (full, short),
                    )
                    self.assertEqual(
                        (after["sku"], after["sku_short_version"]),
                        (full, short),
                    )


if __name__ == "__main__":
    unittest.main()
