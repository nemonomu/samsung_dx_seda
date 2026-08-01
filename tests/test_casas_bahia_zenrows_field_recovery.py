import json
import os
import unittest
from unittest.mock import Mock, patch

from seda.casas_bahia import pdp_field_recovery
from seda.casas_bahia.detail_api import fetch_product_source
from seda.casas_bahia.ldy_sku_contract import (
    BRAND_FIELD as LDY_BRAND_FIELD,
    EVIDENCE_FIELD as LDY_EVIDENCE_FIELD,
)
from seda.casas_bahia.ref_sku_contract import (
    BRAND_FIELD as REF_BRAND_FIELD,
    EVIDENCE_FIELD as REF_EVIDENCE_FIELD,
)
from seda.casas_bahia.recovery_contract import (
    CASAS_LAST_KNOWN_DB_FIELD_MAP,
    CASAS_ZENROWS_FIELD_MAP,
)
from seda.casas_bahia.sku_contract import PDP_HTML_MODEL_TOKEN
from seda.magalu.zenrows_client import ZenRowsResult
from seda.parsers import CASAS_TV_EXACT_MODELO_FIELD, parse_detail
from seda.step08_detail_enrichment import (
    _backfill_casas_zenrows_fields,
)


class CasasBahiaZenRowsFieldRecoveryTests(unittest.TestCase):
    def _result(self, profile, text="<html>ok</html>"):
        return ZenRowsResult(
            success=True,
            url="https://www.casasbahia.com.br/produto/p/123",
            profile=profile,
            status_code=200,
            text=text,
            headers={"X-Request-Cost": "10"},
            estimated_multiplier="10x",
        )

    def test_10x_success_stops_before_25x_and_forces_br(self):
        request = Mock(return_value=self._result("premium_html"))
        detail = {
            "_detail_identity_verified": True,
            "_casas_pdp_safe_recovery": {"ref_capacity": "377L"},
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("ref_capacity",),
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["detail"]["ref_capacity"], "377L")
        self.assertEqual(request.call_args.kwargs["profile"], "premium_html")
        self.assertEqual(
            request.call_args.kwargs["extra"],
            {"proxy_country": "br"},
        )

    def test_25x_runs_only_after_unverified_10x(self):
        request = Mock(
            side_effect=(
                self._result("premium_html"),
                self._result("pdp_js_full"),
            )
        )
        parsed = (
            {"_detail_identity_verified": False},
            {
                "_detail_identity_verified": True,
                "_casas_pdp_safe_recovery": {"ldy_color": "Branco"},
            },
        )
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", side_effect=parsed):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("ldy_color",),
            )
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(
            [call.kwargs["profile"] for call in request.call_args_list],
            ["premium_html", "pdp_js_full"],
        )

    def test_tv_sku_10x_accepts_only_exact_modelo_and_forces_br(self):
        request = Mock(return_value=self._result("premium_html"))
        detail = {
            "_detail_identity_verified": True,
            CASAS_TV_EXACT_MODELO_FIELD: True,
            "sku": "QLED 50 4K P7K",
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/1582420985",
                ("sku",),
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["detail"]["sku"], "QLED 50 4K P7K")
        self.assertIs(result["detail"][CASAS_TV_EXACT_MODELO_FIELD], True)
        self.assertEqual(
            request.call_args.kwargs["extra"],
            {"proxy_country": "br"},
        )

    def test_tv_sku_without_exact_modelo_tries_25x_then_rejects(self):
        request = Mock(
            side_effect=(
                self._result("premium_html"),
                self._result("pdp_js_full"),
            )
        )
        detail = {
            "_detail_identity_verified": True,
            CASAS_TV_EXACT_MODELO_FIELD: False,
            "sku": "1582420985",
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/1582420985",
                ("sku",),
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["request_count"], 2)
        self.assertNotIn("sku", result["detail"])

    def test_ref_sku_uses_only_ref_contract_validated_private_evidence(self):
        request = Mock(return_value=self._result("premium_html"))
        detail = {
            "_detail_identity_verified": True,
            "retailer_sku_name": "Geladeira HQ Frost Free 140 litros",
            "sku": "220V",
            REF_EVIDENCE_FIELD: ("HQ-140RDF",),
            REF_BRAND_FIELD: "HQ",
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("sku",),
                product_line_value="REF",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["detail"], {"sku": "HQ-140RDF"})

    def test_ref_sku_invalid_raw_modelo_tries_25x_then_rejects(self):
        request = Mock(
            side_effect=(
                self._result("premium_html"),
                self._result("pdp_js_full"),
            )
        )
        detail = {
            "_detail_identity_verified": True,
            "retailer_sku_name": "Geladeira HQ Frost Free 140 litros",
            "sku": "220V",
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("sku",),
                product_line_value="REF",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["request_count"], 2)
        self.assertNotIn("sku", result["detail"])

    def test_ldy_sku_uses_only_ldy_contract_validated_private_evidence(self):
        request = Mock(return_value=self._result("premium_html"))
        detail = {
            "_detail_identity_verified": True,
            "retailer_sku_name": "Lavadora Midea 13kg",
            "sku": "220V",
            LDY_EVIDENCE_FIELD: (
                {
                    "value": "MF200D130WB/WK-02",
                    "source": "pdp_modelo",
                },
            ),
            LDY_BRAND_FIELD: "Midea",
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("sku",),
                product_line_value="LDY",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["detail"], {"sku": "MF200D130WB/WK-02"})

    def test_ldy_sku_falls_back_to_valid_raw_modelo(self):
        request = Mock(return_value=self._result("premium_html"))
        detail = {
            "_detail_identity_verified": True,
            "retailer_sku_name": "Lavadora Philco 14kg",
            "sku": "PLR14A",
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("sku",),
                product_line_value="LDY",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["detail"], {"sku": "PLR14A"})

    def test_ldy_sku_invalid_raw_modelo_tries_25x_then_rejects(self):
        request = Mock(
            side_effect=(
                self._result("premium_html"),
                self._result("pdp_js_full"),
            )
        )
        detail = {
            "_detail_identity_verified": True,
            "retailer_sku_name": "Lavadora Philco 14kg",
            "sku": "220V",
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("sku",),
                product_line_value="LDY",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["request_count"], 2)
        self.assertNotIn("sku", result["detail"])

    def test_partial_10x_preserves_25x_configuration_error(self):
        failed = ZenRowsResult(
            success=False,
            url="https://www.casasbahia.com.br/produto/p/123",
            profile="pdp_js_full",
            error="unknown_profile:pdp_js_full",
        )
        request = Mock(
            side_effect=(
                self._result("premium_html"),
                failed,
            )
        )
        detail = {
            "_detail_identity_verified": True,
            "_casas_pdp_safe_recovery": {"ref_capacity": "377L"},
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(pdp_field_recovery, "parse_detail", return_value=detail):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("ref_capacity", "ref_refrigerator_type"),
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["detail"], {"ref_capacity": "377L"})
        self.assertEqual(result["error"], "unknown_profile:pdp_js_full")
        self.assertEqual(result["request_count"], 2)

    def test_identity_conflict_stops_without_25x(self):
        request = Mock(return_value=self._result("premium_html"))
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(
            pdp_field_recovery,
            "parse_detail",
            return_value={"_detail_identity_conflict": True},
        ):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("screen_size",),
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "identity_conflict")
        self.assertEqual(request.call_count, 1)

    def test_parse_exception_preserves_completed_paid_request_count(self):
        request = Mock(return_value=self._result("premium_html"))
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(
            pdp_field_recovery,
            "parse_detail",
            side_effect=ValueError("malformed payload"),
        ):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("screen_size",),
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "parse_exception:ValueError")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(request.call_count, 1)

    def test_verified_pdp_builds_ref_type_and_ldy_color_internal_map(self):
        cases = (
            (
                "REF",
                "Geladeira Consul CRM44MK 377L",
                (
                    ("Quantidade de Portas", "4"),
                    ("Capacidade total", "377L"),
                ),
                {
                    "ref_refrigerator_type": "4 portas",
                    "ref_capacity": "377L",
                },
            ),
            (
                "LDY",
                "Lavadora Midea 13kg Branca",
                (
                    ("Capacidade de lavagem", "13kg"),
                    ("Tipo de abertura", "Superior"),
                    ("Cor", "Branca"),
                ),
                {
                    "ldy_capacity": "13kg",
                    "ldy_loading_type": "Top load",
                    "ldy_color": "Branca",
                },
            ),
        )
        for line, title, specs, expected in cases:
            product = {
                "id": 1,
                "name": title,
                "sku": {"id": "123"},
                "specGroups": [
                    {
                        "name": "Especificacoes",
                        "specs": [
                            {"name": label, "value": value}
                            for label, value in specs
                        ],
                    }
                ],
            }
            payload = {"props": {"pageProps": {"product": product}}}
            html = (
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload)
                + "</script>"
            )
            with self.subTest(line=line), patch.dict(
                os.environ,
                {"SEDA_PRODUCT_LINE": line},
                clear=False,
            ):
                detail = parse_detail(
                    html,
                    "Casas Bahia",
                    "https://www.casasbahia.com.br",
                    "https://www.casasbahia.com.br/produto/p/123",
                )
                self.assertIs(detail["_detail_identity_verified"], True)
                safe = detail["_casas_pdp_safe_recovery"]
                for field, value in expected.items():
                    self.assertEqual(safe[field], value)

    def test_verified_pdp_exposes_reference_evidence_to_each_sku_validator(self):
        cases = (
            (
                "REF",
                "Geladeira HQ Frost Free 140 litros",
                (
                    ("Marca", "HQ"),
                    ("Modelo", "220V"),
                    ("Refer\u00eancia", "HQ-140RDF"),
                ),
                REF_EVIDENCE_FIELD,
                "HQ-140RDF",
            ),
            (
                "LDY",
                "Lavadora Midea 13kg",
                (
                    ("Marca", "Midea"),
                    ("Modelo", "Wave Agitator"),
                    ("Refer\u00eancia", "MA512W130A/WK-05"),
                ),
                LDY_EVIDENCE_FIELD,
                "MA512W130A/WK-05",
            ),
        )
        for line, title, specs, evidence_field, expected in cases:
            product = {
                "id": 1,
                "name": title,
                "sku": {"id": "123"},
                "specGroups": [
                    {
                        "name": "Especificacoes",
                        "specs": [
                            {"name": label, "value": value}
                            for label, value in specs
                        ],
                    }
                ],
            }
            payload = {"props": {"pageProps": {"product": product}}}
            html = (
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload)
                + "</script>"
            )
            with self.subTest(line=line), patch.dict(
                os.environ,
                {"SEDA_PRODUCT_LINE": line},
                clear=False,
            ):
                detail = parse_detail(
                    html,
                    "Casas Bahia",
                    "https://www.casasbahia.com.br",
                    "https://www.casasbahia.com.br/produto/p/123",
                )
                self.assertTrue(detail[evidence_field])
                self.assertEqual(
                    pdp_field_recovery._validated_sku(detail, line),
                    expected,
                )

    def test_standalone_dryer_pdp_does_not_create_capacity_or_loading(self):
        product = {
            "id": 1,
            "name": "Secadora de Roupas Electrolux 11kg Branca",
            "sku": {"id": "123"},
            "specGroups": [
                {
                    "name": "Especificacoes",
                    "specs": [
                        {"name": "Capacidade", "value": "11kg"},
                        {"name": "Tipo de abertura", "value": "Frontal"},
                        {"name": "Cor", "value": "Branca"},
                    ],
                }
            ],
        }
        payload = {"props": {"pageProps": {"product": product}}}
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "LDY"},
            clear=False,
        ):
            detail = parse_detail(
                html,
                "Casas Bahia",
                "https://www.casasbahia.com.br",
                "https://www.casasbahia.com.br/produto/p/123",
            )
        safe = detail["_casas_pdp_safe_recovery"]
        self.assertEqual(safe["ldy_capacity"], "")
        self.assertEqual(safe["ldy_loading_type"], "")
        self.assertEqual(safe["ldy_color"], "Branca")

    def test_unrequested_safe_field_does_not_report_success(self):
        request = Mock(
            side_effect=(
                self._result("premium_html"),
                self._result("pdp_js_full"),
            )
        )
        detail = {
            "_detail_identity_verified": True,
            "_casas_pdp_safe_recovery": {
                "ref_capacity": "377L",
                "ldy_color": "Branca",
            },
        }
        with patch(
            "seda.magalu.zenrows_client.request_url",
            request,
        ), patch.object(
            pdp_field_recovery,
            "parse_detail",
            return_value=detail,
        ):
            result = pdp_field_recovery.fetch_pdp_fields_via_zenrows(
                "https://www.casasbahia.com.br/produto/p/123",
                ("screen_size",),
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["detail"], {})
        self.assertEqual(result["request_count"], 2)

    def test_product_source_paid_attempt_is_not_retried_by_direct_setting(self):
        failed = {
            "success": False,
            "error": "zenrows_status_403",
            "method": "casas_bahia_product_source_zenrows",
        }
        zenrows = Mock(return_value=failed)
        with patch.dict(
            os.environ,
            {
                "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_RETRIES": "3",
                "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_ZENROWS_RETRIES": "0",
            },
            clear=False,
        ), patch(
            "seda.casas_bahia.detail_api._read_product_source_cache",
            return_value=None,
        ), patch(
            "seda.casas_bahia.detail_api._product_source_attempts",
            return_value=["zenrows"],
        ), patch(
            "seda.casas_bahia.detail_api._fetch_product_source_zenrows",
            zenrows,
        ):
            result = fetch_product_source("123", timeout=1)
        self.assertFalse(result["success"])
        self.assertEqual(zenrows.call_count, 1)

    def test_parent_backfill_caches_item_and_merges_missing_only(self):
        rows = [
            {
                "retailer": "Casas Bahia",
                "product_line": "REF",
                "item": "123",
                "product_url": "https://www.casasbahia.com.br/a/p/123",
                "retailer_sku_name": "Geladeira Consul CRM44MK 377L",
                "ref_capacity": "377L",
                "ref_refrigerator_type": "",
                "sku": "CRM44MK",
            },
            {
                "retailer": "Casas Bahia",
                "product_line": "REF",
                "item": "123",
                "product_url": "https://www.casasbahia.com.br/a/p/123",
                "retailer_sku_name": "Geladeira Consul CRM44MK 377L",
                "ref_capacity": "",
                "ref_refrigerator_type": "",
                "sku": "CRM44MK",
            },
        ]
        result = {
            "success": True,
            "detail": {
                "ref_capacity": "400L",
                "ref_refrigerator_type": "Duplex",
                "sku": "MUST_NOT_MERGE",
            },
            "error": "",
            "request_count": 1,
            "attempts": [],
        }
        fetch = Mock(return_value=result)
        env = {
            "SEDA_CASAS_BAHIA_ZENROWS_FIELD_FAILURE_STREAK": "3",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            fetch,
        ):
            _backfill_casas_zenrows_fields(rows, "unused.csv")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(rows[0]["ref_capacity"], "377L")
        self.assertEqual(rows[1]["ref_capacity"], "400L")
        self.assertEqual(rows[0]["ref_refrigerator_type"], "Duplex")
        self.assertEqual(rows[0]["sku"], "CRM44MK")
        self.assertEqual(rows[1]["sku"], "CRM44MK")
        self.assertEqual(
            fetch.call_args.kwargs["product_line_value"],
            "REF",
        )

    def test_parent_backfill_merges_only_ref_validated_sku_and_short(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "REF",
            "item": "123",
            "product_url": "https://www.casasbahia.com.br/a/p/123",
            "retailer_sku_name": "Geladeira HQ Frost Free 140 litros",
            "sku": "123",
            "sku_short_version": "STALE",
            "ref_capacity": "140L",
            "ref_refrigerator_type": "1 porta",
        }
        fetch = Mock(
            return_value={
                "success": True,
                "detail": {"sku": "HQ-140RDF"},
                "identity_verified": True,
                "error": "",
                "request_count": 1,
                "attempts": [],
            }
        )
        with patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            fetch,
        ):
            _backfill_casas_zenrows_fields([row], "unused.csv")
        self.assertEqual(row["sku"], "HQ-140RDF")
        self.assertEqual(row["sku_short_version"], "HQ-140RDF")
        self.assertEqual(
            fetch.call_args.kwargs["product_line_value"],
            "REF",
        )

    def test_parent_backfill_merges_only_ldy_validated_sku_and_short(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "item": "123",
            "product_url": "https://www.casasbahia.com.br/a/p/123",
            "retailer_sku_name": "Lavadora Midea 13kg",
            "sku": "123",
            "sku_short_version": "STALE",
            "ldy_capacity": "13kg",
            "ldy_loading_type": "Front load",
            "ldy_color": "Branco",
        }
        fetch = Mock(
            return_value={
                "success": True,
                "detail": {"sku": "MF200D130WB/WK-02"},
                "identity_verified": True,
                "error": "",
                "request_count": 1,
                "attempts": [],
            }
        )
        with patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            fetch,
        ):
            _backfill_casas_zenrows_fields([row], "unused.csv")
        self.assertEqual(row["sku"], "MF200D130WB/WK-02")
        self.assertEqual(
            row["sku_short_version"],
            "MF200D130WB/WK-02",
        )
        self.assertEqual(
            fetch.call_args.kwargs["product_line_value"],
            "LDY",
        )

    def test_parent_backfill_replaces_unverified_tv_listing_sku_with_modelo(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "TV",
            "item": "1582420985",
            "product_url": (
                "https://www.casasbahia.com.br/smart-tv/p/1582420985"
            ),
            "retailer_sku_name": 'Smart TV TCL QLED 50" 4K P7K',
            "sku": "P7K",
            "screen_size": '50"',
            "estimated_annual_electricity_use": "100 W",
            "model_year": "2025",
            "parse_status": (
                "listing_casas_bahia_partner_api+"
                "product_source_failed:sku_mismatch:999"
            ),
        }
        result = {
            "success": True,
            "detail": {
                "sku": "QLED 50 4K P7K",
                CASAS_TV_EXACT_MODELO_FIELD: True,
            },
            "identity_verified": True,
            "error": "",
            "request_count": 1,
            "attempts": [],
        }
        fetch = Mock(return_value=result)
        with patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            fetch,
        ):
            _backfill_casas_zenrows_fields([row], "unused.csv")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(row["sku"], "QLED 50 4K P7K")
        self.assertIn(PDP_HTML_MODEL_TOKEN, row["parse_status"].split("+"))
        self.assertIn(
            "casas_zenrows_field_recovered:sku",
            row["parse_status"].split("+"),
        )

    def test_high_confidence_tv_title_model_does_not_spend_zenrows(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "TV",
            "item": "123",
            "product_url": "https://www.casasbahia.com.br/tv/p/123",
            "retailer_sku_name": (
                'Smart TV LG 55" QNED Processador AI A7 55QNED73ASA'
            ),
            "sku": "",
            "screen_size": "55 inches",
            "estimated_annual_electricity_use": "26,5",
            "model_year": "2025",
        }
        fetch = Mock()
        with patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            fetch,
        ):
            _backfill_casas_zenrows_fields([row], "unused.csv")
        fetch.assert_not_called()

    def test_complete_valid_ref_and_ldy_rows_do_not_spend_zenrows(self):
        rows = (
            {
                "retailer": "Casas Bahia",
                "product_line": "REF",
                "item": "123",
                "product_url": "https://www.casasbahia.com.br/a/p/123",
                "retailer_sku_name": "Geladeira Consul CRM44MK 377L",
                "sku": "CRM44MK",
                "ref_capacity": "377L",
                "ref_refrigerator_type": "Duplex",
            },
            {
                "retailer": "Casas Bahia",
                "product_line": "LDY",
                "item": "124",
                "product_url": "https://www.casasbahia.com.br/a/p/124",
                "retailer_sku_name": "Lavadora Philco PLR14A 14kg",
                "sku": "PLR14A",
                "ldy_capacity": "14kg",
                "ldy_loading_type": "Top load",
                "ldy_color": "Preto",
            },
        )
        for row in rows:
            fetch = Mock()
            with self.subTest(line=row["product_line"]), patch(
                "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
                return_value=True,
            ), patch(
                "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
                fetch,
            ):
                _backfill_casas_zenrows_fields([row], "unused.csv")
            fetch.assert_not_called()

    def test_standalone_dryer_intentional_blanks_do_not_spend(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "item": "123",
            "product_url": "https://www.casasbahia.com.br/a/p/123",
            "retailer_sku_name": "Secadora de Roupas Electrolux STH11 11kg",
            "sku": "STH11",
            "ldy_capacity": "",
            "ldy_loading_type": "",
            "ldy_color": "Branco",
        }
        fetch = Mock()
        with patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            fetch,
        ):
            _backfill_casas_zenrows_fields([row], "unused.csv")
        fetch.assert_not_called()

    def test_ref_ldy_sku_recovery_maps_remain_separate(self):
        self.assertEqual(
            CASAS_ZENROWS_FIELD_MAP["REF"],
            ("sku", "ref_refrigerator_type", "ref_capacity"),
        )
        self.assertEqual(
            CASAS_ZENROWS_FIELD_MAP["LDY"],
            ("sku", "ldy_loading_type", "ldy_color", "ldy_capacity"),
        )
        self.assertEqual(
            CASAS_LAST_KNOWN_DB_FIELD_MAP["LDY"],
            ("sku", "ldy_loading_type", "ldy_color", "ldy_capacity"),
        )

    def test_item_url_identity_mismatch_never_spends_zenrows(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "item": "123",
            "product_url": "https://www.casasbahia.com.br/a/p/999",
            "retailer_sku_name": "Lavadora Midea 13kg",
            "sku": "",
            "ldy_capacity": "13kg",
            "ldy_loading_type": "Front load",
            "ldy_color": "Branco",
        }
        fetch = Mock()
        with patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            fetch,
        ):
            _backfill_casas_zenrows_fields([row], "unused.csv")
        fetch.assert_not_called()
        self.assertIn(
            "casas_zenrows_field_skipped:input_item_identity_mismatch",
            row["parse_status"].split("+"),
        )

    def test_unexpected_paid_fetch_exception_does_not_abort_pipeline(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "TV",
            "item": "123",
            "product_url": "https://www.casasbahia.com.br/a/p/123",
            "screen_size": "",
            "estimated_annual_electricity_use": "",
            "model_year": "",
        }
        with patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            side_effect=RuntimeError("unexpected"),
        ):
            result = _backfill_casas_zenrows_fields([row], "unused.csv")
        self.assertIs(result[0], row)
        self.assertIn("exception:RuntimeError", row["parse_status"])

    def test_more_than_25_unique_items_are_not_capped(self):
        rows = [
            {
                "retailer": "Casas Bahia",
                "product_line": "TV",
                "item": item,
                "product_url": f"https://www.casasbahia.com.br/a/p/{item}",
                "screen_size": "",
                "estimated_annual_electricity_use": "",
                "model_year": "",
            }
            for item in (str(100 + index) for index in range(26))
        ]
        result = {
            "success": True,
            "detail": {"screen_size": '55"'},
            "error": "",
            "request_count": 1,
            "attempts": [],
        }
        fetch = Mock(return_value=result)
        with patch.dict(
            os.environ,
            {"SEDA_CASAS_BAHIA_ZENROWS_FIELD_FAILURE_STREAK": "3"},
            clear=False,
        ), patch(
            "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
            return_value=True,
        ), patch(
            "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
            fetch,
        ):
            _backfill_casas_zenrows_fields(
                rows,
                "unused.csv",
                checkpoint_every=0,
            )
        self.assertEqual(fetch.call_count, 26)
        self.assertTrue(all(row["screen_size"] == '55"' for row in rows))

    def test_failure_and_config_guards_stop_next_unique_item(self):
        cases = (
            (
                "failure_streak",
                {
                    "SEDA_CASAS_BAHIA_ZENROWS_FIELD_FAILURE_STREAK": "1",
                },
                {
                    "success": False,
                    "detail": {},
                    "error": "temporary_failure",
                    "request_count": 1,
                    "attempts": [],
                },
            ),
            (
                "configuration_error",
                {
                    "SEDA_CASAS_BAHIA_ZENROWS_FIELD_FAILURE_STREAK": "3",
                },
                {
                    "success": False,
                    "detail": {},
                    "error": "key_missing",
                    "request_count": 0,
                    "attempts": [],
                },
            ),
        )
        for label, env, result in cases:
            rows = [
                {
                    "retailer": "Casas Bahia",
                    "product_line": "TV",
                    "item": item,
                    "product_url": (
                        f"https://www.casasbahia.com.br/a/p/{item}"
                    ),
                    "screen_size": "",
                    "estimated_annual_electricity_use": "",
                    "model_year": "",
                }
                for item in ("123", "124")
            ]
            fetch = Mock(return_value=result)
            with self.subTest(label=label), patch.dict(
                os.environ,
                env,
                clear=False,
            ), patch(
                "seda.step08_detail_enrichment._casas_zenrows_field_recovery_enabled",
                return_value=True,
            ), patch(
                "seda.casas_bahia.pdp_field_recovery.fetch_pdp_fields_via_zenrows",
                fetch,
            ):
                _backfill_casas_zenrows_fields(rows, "unused.csv")
            self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
