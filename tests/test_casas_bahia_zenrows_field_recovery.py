import json
import os
import unittest
from unittest.mock import Mock, patch

from seda.casas_bahia import pdp_field_recovery
from seda.casas_bahia.detail_api import fetch_product_source
from seda.magalu.zenrows_client import ZenRowsResult
from seda.parsers import parse_detail
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
                "ref_capacity": "377L",
                "ref_refrigerator_type": "",
                "sku": "KEEP",
            },
            {
                "retailer": "Casas Bahia",
                "product_line": "REF",
                "item": "123",
                "product_url": "https://www.casasbahia.com.br/a/p/123",
                "ref_capacity": "",
                "ref_refrigerator_type": "",
                "sku": "KEEP2",
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
        self.assertEqual(rows[0]["sku"], "KEEP")
        self.assertEqual(rows[1]["sku"], "KEEP2")

    def test_standalone_dryer_intentional_blanks_do_not_spend(self):
        row = {
            "retailer": "Casas Bahia",
            "product_line": "LDY",
            "item": "123",
            "product_url": "https://www.casasbahia.com.br/a/p/123",
            "retailer_sku_name": "Secadora de Roupas Electrolux 11kg",
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
