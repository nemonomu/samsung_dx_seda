import os
import unittest
from datetime import datetime
from unittest.mock import patch

from seda.casas_bahia.detail_api import _product_source_detail
from seda.casas_bahia.detail_html_backfill import _merge as merge_html_backfill
from seda.casas_bahia.ldy_sku_contract import (
    BRAND_FIELD,
    EVIDENCE_FIELD,
    LAST_KNOWN_SELECTED_TOKEN,
    SHORT_DERIVED_TOKEN,
    casas_ldy_sku_for_output,
    casas_ldy_short_for_output,
    derive_samsung_short,
    extract_product_source_evidence,
    is_valid_ldy_manufacturer_sku,
    ldy_title_sku_candidates,
    resolve_ldy_sku,
)
from seda.parsers import _listing_row
from seda.step08_detail_enrichment import (
    _merge_casas_bahia_apis,
    _merge_generic_product_detail,
)
from seda.step14_db_load import _db_value
from seda.step15_final_output import _format_row


class CasasBahiaLdySkuContractTests(unittest.TestCase):
    def test_strict_validator_accepts_models_and_rejects_noise(self):
        valid = (
            "NA-F170B7W",
            "BWF16AB",
            "MA512",
            "LCA18",
            "MF201W110WB/WK-01",
            "CWN15ABANA",
            "LEC15",
            "WW11T",
            "WW11T4040BXFAZ",
        )
        rejected = (
            "1570578247",
            "7891234567890",
            "110V",
            "Twin",
            "Petit",
            "Tradicional",
            "Wave",
            "Lavamax",
            "15KGWAVEAGITATORBRANCA",
            "220VLIBELL",
            "BCO-127V",
            "TURBO-220V",
            "ADVANCED-BWD16A9",
            "MLA13110VBRANCO",
        )
        for value in valid:
            with self.subTest(valid=value):
                self.assertTrue(is_valid_ldy_manufacturer_sku(value))
        for value in rejected:
            with self.subTest(rejected=value):
                self.assertFalse(is_valid_ldy_manufacturer_sku(value))

    def test_title_wins_and_product_source_conflict_is_recorded(self):
        resolution = resolve_ldy_sku(
            "PLR14A",
            "Maquina de Lavar Philco 14kg PLR14A",
            [{"value": "PRL14A", "source": "product_source_description:modelo"}],
        )
        self.assertEqual(resolution.sku, "PLR14A")
        self.assertTrue(
            any(
                token.startswith("casas_ldy_sku_conflict:product_source:")
                for token in resolution.status_tokens
            )
        )

    def test_accented_word_cannot_create_a_partial_model_candidate(self):
        self.assertEqual(
            ldy_title_sku_candidates(
                "Maquina Mueller 15kg com Ciclo R\u00e1pido-127v"
            ),
            [],
        )

    def test_matching_title_evidence_ignores_secondary_labeled_tokens(self):
        resolution = resolve_ldy_sku(
            "MLA11",
            "Maquina de Lavar Mueller MLA11 11kg",
            [
                {"value": "MLA11", "source": "product_source_description:modelo"},
                {"value": "ABA6I", "source": "product_source_description:modelo"},
            ],
        )
        self.assertEqual(resolution.sku, "MLA11")
        self.assertFalse(
            any(
                "conflict:product_source" in token
                for token in resolution.status_tokens
            )
        )

    def test_null_is_rescued_by_one_explicit_product_source_candidate(self):
        cases = (
            "MF201W110WB/WK-01",
            "MA512W130A/WK",
            "CWN15ABANA",
            "LEC15",
            "BWJ14ABANA",
            "BWF18ABANA",
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                resolution = resolve_ldy_sku(
                    "",
                    "Lavadora de Roupas 13kg",
                    [{"value": candidate, "source": "product_source_description:modelo"}],
                )
                self.assertEqual(resolution.sku, candidate)
                self.assertIn(
                    "casas_ldy_sku_product_source_selected",
                    resolution.status_tokens,
                )

    def test_multiple_candidates_stay_unresolved_without_variant_proof(self):
        evidence = extract_product_source_evidence(
            "Modelo: MF201W110WB/WK-01 / MF201W110WB/WK-02",
            {},
        )
        self.assertEqual(
            {entry["value"] for entry in evidence},
            {"MF201W110WB/WK-01", "MF201W110WB/WK-02"},
        )
        resolution = resolve_ldy_sku("", "Lavadora Midea 11kg", evidence)
        self.assertEqual(resolution.sku, "")
        self.assertIn(
            "casas_ldy_sku_product_source_ambiguous",
            resolution.status_tokens,
        )

    def test_explicit_voltage_mapping_selects_only_matching_model(self):
        evidence = extract_product_source_evidence(
            "Modelo: 110v - LE1021BR / 220v - LE1022BR",
            {},
            variant_text="Lavadora 10kg - 110V",
        )
        self.assertEqual([entry["value"] for entry in evidence], ["LE1021BR"])

    def test_modelo_referencia_and_ref_labels_only(self):
        evidence = extract_product_source_evidence(
            "Texto livre MF201W110WB; Referencia: CWN15ABANA; Ref.: 123456",
            {"modelo": ["MA512W130A/WK"]},
        )
        self.assertEqual(
            {entry["value"] for entry in evidence},
            {"CWN15ABANA", "MA512W130A/WK"},
        )

    def test_samsung_short_is_derived_only_from_resolved_sku(self):
        cases = (
            ("WW11T4040BXFAZ", "WW11T"),
            ("WW13CGC04DAEBZ", "WW13CG"),
            ("WF90F20ADSBZ", "WF90F"),
            ("WD13FG6B34BBAZ", "WD13FG"),
            ("DV12B6800EW/AZ", "DV12B"),
            ("DV90F20CDSBZ", "DV90F"),
            ("DVG20A6470V/AZ", "DVG20"),
            ("WW11T", "WW11T"),
            ("DV316LGS/XAZ", ""),
        )
        for sku, expected in cases:
            with self.subTest(sku=sku):
                self.assertEqual(
                    derive_samsung_short(
                        sku,
                        title=f"Lavadora Samsung {sku}",
                    ),
                    expected,
                )
        self.assertEqual(
            derive_samsung_short(
                "WW11T4040BXFAZ",
                title="Lavadora de Roupas sem marca",
            ),
            "",
        )

    def test_product_source_detail_exposes_evidence_not_an_independent_sku(self):
        data = {
            "product": {
                "id": "product-1",
                "name": "Lavadora Midea 13kg",
                "description": (
                    "<p>Especificacoes Tecnicas Modelo: "
                    "MF201W110WB/WK-01 Cor: Branco</p>"
                ),
                "brand": {"name": "Midea"},
                "specGroups": [],
            },
            "sku": {
                "id": "1577377527",
                "name": "Lavadora Midea 13kg - 110V",
            },
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            detail = _product_source_detail(data)
        self.assertNotIn("sku", detail)
        self.assertNotIn("sku_short_version", detail)
        self.assertEqual(detail[BRAND_FIELD], "Midea")
        self.assertEqual(
            [entry["value"] for entry in detail[EVIDENCE_FIELD]],
            ["MF201W110WB/WK-01"],
        )

    def test_product_source_merge_rescues_and_reaches_db_value(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "product_url": "https://www.casasbahia.com.br/lavadora/p/1577377527",
            "retailer_sku_name": "Lavadora Midea 13kg",
            "sku": "1577377527",
            "sku_short_version": "STALE",
            "parse_status": "",
        }
        result = {
            "success": True,
            "detail": {
                "retailer_sku_name": row["retailer_sku_name"],
                "ldy_capacity": "13kg",
                EVIDENCE_FIELD: [
                    {
                        "value": "MF201W110WB/WK-01",
                        "source": "product_source_description:modelo",
                    }
                ],
                BRAND_FIELD: "Midea",
            },
            "method": "casas_bahia_product_source_direct",
            "headers": {},
        }
        env = self._api_env()
        with patch.dict(os.environ, env), patch(
            "seda.casas_bahia.detail_api.fetch_product_source",
            return_value=result,
        ) as fetch:
            _merge_casas_bahia_apis(row)
            formatted = _format_row(row, datetime(2026, 7, 30, 12, 0, 0))
        fetch.assert_called_once_with("1577377527")
        self.assertEqual(row["sku"], "MF201W110WB/WK-01")
        self.assertEqual(row["sku_short_version"], "MF201W110WB/WK-01")
        self.assertEqual(formatted["sku"], "MF201W110WB/WK-01")
        self.assertEqual(
            formatted["sku_short_version"],
            "MF201W110WB/WK-01",
        )
        self.assertEqual(_db_value("sku", formatted["sku"]), "MF201W110WB/WK-01")

    def test_product_source_merge_clears_stale_short_atomically(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "product_url": "https://www.casasbahia.com.br/lavadora/p/123",
            "retailer_sku_name": "Lavadora Panasonic 17kg NA-F170B7W",
            "sku": "NA-F170B7W",
            "sku_short_version": "WW11T",
            "parse_status": "",
        }
        result = {
            "success": True,
            "detail": {
                "retailer_sku_name": row["retailer_sku_name"],
                EVIDENCE_FIELD: [],
                BRAND_FIELD: "Panasonic",
            },
            "method": "test",
            "headers": {},
        }
        with patch.dict(os.environ, self._api_env()), patch(
            "seda.casas_bahia.detail_api.fetch_product_source",
            return_value=result,
        ):
            _merge_casas_bahia_apis(row)
        self.assertEqual(row["sku"], "NA-F170B7W")
        self.assertEqual(row["sku_short_version"], "NA-F170B7W")

    def test_missing_url_item_never_uses_manufacturer_sku_as_api_id(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "product_url": "https://www.casasbahia.com.br/lavadora-sem-item",
            "retailer_sku_name": "Lavadora Brastemp BWF16AB",
            "sku": "BWF16AB",
            "parse_status": "",
        }
        with patch.dict(os.environ, self._api_env()), patch(
            "seda.casas_bahia.detail_api.fetch_product_source",
        ) as fetch:
            _merge_casas_bahia_apis(row)
        fetch.assert_not_called()
        self.assertIn(
            "product_source_skipped:missing_url_item",
            row["parse_status"].split("+"),
        )

    def test_verified_generic_html_preserves_valid_title_sku_atomically(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "retailer_sku_name": "Maquina Philco 14kg PLR14A",
            "sku": "PLR14A",
            "sku_short_version": "",
            "ldy_capacity": "",
            "parse_status": "casas_ldy_sku_title_selected",
        }
        detail = {
            "retailer_sku_name": row["retailer_sku_name"],
            "sku": "PRL14A",
            "sku_short_version": "BAD",
            "ldy_capacity": "14kg",
            "_detail_identity_verified": True,
            "parse_status": "detail_casas_bahia_html",
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            self.assertTrue(_merge_generic_product_detail(row, detail))
        self.assertEqual(row["sku"], "PLR14A")
        self.assertEqual(row["sku_short_version"], "PLR14A")
        self.assertEqual(row["ldy_capacity"], "14kg")
        self.assertIn(
            "casas_ldy_sku_title_selected",
            row["parse_status"].split("+"),
        )
        self.assertIn(
            "detail_casas_bahia_html",
            row["parse_status"].split("+"),
        )

    def test_verified_generic_html_uses_reference_not_marketing_modelo(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "retailer_sku_name": "Lavadora Midea 13kg",
            "sku": "",
            "sku_short_version": "",
            "ldy_capacity": "",
            "parse_status": "",
        }
        detail = {
            "retailer_sku_name": row["retailer_sku_name"],
            "sku": "Wave Agitator",
            EVIDENCE_FIELD: (
                {
                    "value": "MA512W130A/WK-05",
                    "source": "pdp_spec:referencia",
                },
            ),
            "ldy_capacity": "13kg",
            "_detail_identity_verified": True,
            "parse_status": "detail_casas_bahia_html",
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            self.assertTrue(_merge_generic_product_detail(row, detail))
        self.assertEqual(row["sku"], "MA512W130A/WK-05")
        self.assertEqual(row["sku_short_version"], "MA512W130A/WK-05")
        self.assertEqual(row["ldy_capacity"], "13kg")

    def test_standalone_html_backfill_does_not_overwrite_ldy_sku(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "retailer_sku_name": "Maquina Philco 14kg PLR14A",
            "sku": "PLR14A",
            "ldy_capacity": "",
            "parse_status": "",
        }
        detail = {
            "retailer_sku_name": row["retailer_sku_name"],
            "sku": "PRL14A",
            "ldy_capacity": "14kg",
            "_detail_identity_verified": True,
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            self.assertTrue(merge_html_backfill(row, detail))
        self.assertEqual(row["sku"], "PLR14A")
        self.assertEqual(row["ldy_capacity"], "14kg")

    def test_generic_listing_uses_title_model_not_numeric_url_item(self):
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            row = _listing_row(
                "Casas Bahia",
                "Lavadora Brastemp 16kg BWF16AB R$ 2.999,00",
                "https://www.casasbahia.com.br/lavadora/p/1570578247",
                "https://www.casasbahia.com.br/lavadoras",
                "main",
                1,
            )
        self.assertEqual(row["sku"], "BWF16AB")
        self.assertNotEqual(row["sku"], "1570578247")

    def test_final_output_uses_resolved_sku_for_short_and_db(self):
        cases = (
            (
                {
                    "retailer_sku_name": "Lavadora Samsung WW11T4040BXFAZ",
                    "sku": "WW11T4040BXFAZ",
                    "sku_short_version": "WRONG",
                    "parse_status": "",
                },
                "WW11T4040BXFAZ",
                "WW11T",
            ),
            (
                {
                    "retailer_sku_name": "Lavadora Samsung WW11T",
                    "sku": "",
                    "sku_short_version": "",
                    "parse_status": "",
                },
                "WW11T",
                "WW11T",
            ),
            (
                {
                    "retailer_sku_name": "Lavadora Panasonic NA-F170B7W",
                    "sku": "NA-F170B7W",
                    "sku_short_version": "WW11T",
                    "parse_status": SHORT_DERIVED_TOKEN,
                },
                "NA-F170B7W",
                "NA-F170B7W",
            ),
        )
        env = {
            "SEDA_PRODUCT_LINE": "LDY",
            "SEDA_ACTIVE_RETAILER": "casas_bahia",
        }
        for base, expected_sku, expected_short in cases:
            row = {
                "retailer": "Casas Bahia",
                "product_line": "LDY",
                "product_url": "https://www.casasbahia.com.br/lavadora/p/123",
                **base,
            }
            with self.subTest(expected_sku=expected_sku), patch.dict(
                os.environ,
                env,
            ):
                formatted = _format_row(
                    row,
                    datetime(2026, 7, 30, 12, 0, 0),
                )
            self.assertEqual(formatted["sku"], expected_sku)
            self.assertEqual(formatted["sku_short_version"], expected_short)
            self.assertEqual(_db_value("sku", formatted["sku"]), expected_sku)

    def test_invalid_final_sku_becomes_db_null(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "product_url": "https://www.casasbahia.com.br/lavadora/p/1570578247",
            "retailer_sku_name": "Lavadora 13kg Tradicional",
            "sku": "1570578247",
            "sku_short_version": "WW11T",
            "parse_status": "",
        }
        with patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "LDY",
                "SEDA_ACTIVE_RETAILER": "casas_bahia",
            },
        ):
            formatted = _format_row(row, datetime(2026, 7, 30, 12, 0, 0))
        self.assertEqual(formatted["sku"], "")
        self.assertEqual(formatted["sku_short_version"], "")
        self.assertIsNone(_db_value("sku", formatted["sku"]))

    def test_final_does_not_reverse_a_step08_resolved_sku(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "product_url": "https://www.casasbahia.com.br/lavadora/p/123",
            "retailer_sku_name": "Maquina Philco 14kg PRL14A",
            "sku": "PLR14A",
            "sku_short_version": "",
            "parse_status": "casas_ldy_sku_title_selected",
        }
        with patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "LDY",
                "SEDA_ACTIVE_RETAILER": "casas_bahia",
            },
        ):
            formatted = _format_row(row, datetime(2026, 7, 30, 12, 0, 0))
        self.assertEqual(formatted["sku"], "PLR14A")

    def test_verified_short_token_cannot_validate_unrelated_stored_short(self):
        row = {
            "retailer_sku_name": "Lavadora sem marca",
            "sku_short_version": "WW13CG",
            "parse_status": SHORT_DERIVED_TOKEN,
        }
        self.assertEqual(
            casas_ldy_short_for_output(row, "WW11T4040BXFAZ"),
            "WW11T4040BXFAZ",
        )

    def test_last_known_db_token_preserves_only_a_valid_final_sku(self):
        valid = {
            "retailer_sku_name": "Lavadora 13kg",
            "sku": "MF200D130WB/WK-02",
            "parse_status": LAST_KNOWN_SELECTED_TOKEN,
        }
        invalid = {
            "retailer_sku_name": "Lavadora 13kg",
            "sku": "1570578247",
            "parse_status": LAST_KNOWN_SELECTED_TOKEN,
        }
        self.assertEqual(
            casas_ldy_sku_for_output(valid, "1570578247"),
            "MF200D130WB/WK-02",
        )
        self.assertEqual(
            casas_ldy_short_for_output(valid, "MF200D130WB/WK-02"),
            "MF200D130WB/WK-02",
        )
        self.assertEqual(
            casas_ldy_sku_for_output(invalid, "1570578247"),
            "",
        )
        self.assertEqual(
            casas_ldy_short_for_output(invalid, "1570578247"),
            "",
        )

    def test_samsung_short_is_computed_from_final_sku_not_stale_storage(self):
        row = {
            "retailer_sku_name": "Lavadora Samsung WW11T4040BXFAZ",
            "sku_short_version": "WRONG",
            "parse_status": SHORT_DERIVED_TOKEN,
        }
        self.assertEqual(
            casas_ldy_short_for_output(row, "WW11T4040BXFAZ"),
            "WW11T",
        )

    @staticmethod
    def _api_env():
        return {
            "SEDA_PRODUCT_LINE": "LDY",
            "SEDA_ACTIVE_RETAILER": "casas_bahia",
            "SEDA_CASAS_BAHIA_API_ENRICH": "1",
            "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API": "1",
            "SEDA_CASAS_BAHIA_FREIGHT_API": "0",
            "SEDA_CASAS_BAHIA_PICKUP_API": "0",
            "SEDA_CASAS_BAHIA_RECS_API": "0",
            "SEDA_CASAS_BAHIA_REVIEW_API": "0",
        }


if __name__ == "__main__":
    unittest.main()
