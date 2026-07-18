import csv
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from seda.casas_bahia.detail_api import (
    _fetch_product_source_direct,
    _product_source_detail,
    _product_source_payload_error,
    fetch_product_source,
)
from seda.casas_bahia.field_extraction import extract_fields as extract_casas_fields
from seda.common.field_rules import extract_ldy_capacity_from_title, extract_ref_capacity_from_title
from seda.common.translations import translate_row
from seda.magalu import detail_api as magalu_detail_api
from seda.magalu.field_extraction import extract_fields as extract_magalu_fields
from seda.magalu.zenrows_client import ZenRowsResult
from seda.parsers import _casas_bahia_next_product, parse_detail
from seda import step14_db_load
from seda.step00_config import OUTPUT_COLUMNS, write_csv
from seda.step08_detail_enrichment import (
    _magalu_graphql_detail,
    _detail_raw_filename,
    _merge_authoritative_detail,
    _merge_casas_bahia_apis,
    _merge_generic_product_detail,
    _merge_magalu_exact_html_specs,
    _merge_magalu_pdp_html,
    _merge_magalu_shipping_from_next_data,
    _merge_magalu_zenrows_detail,
    _merge_magalu_zenrows_pdp_html,
    _parallel_part_error,
    _relevant_audited_fields,
    _resume_prefix,
    _run_parallel,
)
from seda.step14_db_load import _db_value
from seda.step15_final_output import (
    _format_row,
    _source_completeness_error,
    _source_path,
    _validate_internal_source_schema,
    _validate_source_context,
    final_output_columns,
)


class FieldPipelineContractTests(unittest.TestCase):
    def formatted(self, line, row):
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": line, "SEDA_ACTIVE_RETAILER": "magalu"},
        ):
            return _format_row(row, datetime(2026, 7, 17, 9, 30, 0))

    def test_raw_nonstandard_values_survive_final_format_and_db_conversion(self):
        ref_value = "0,95 pés cúbicos (aprox. 26,9 litros)"
        formatted = self.formatted("REF", {"product_url": "https://example/p/ref", "ref_capacity": ref_value})
        self.assertEqual(formatted["ref_capacity"], ref_value)
        self.assertEqual(_db_value("ref_capacity", formatted["ref_capacity"]), ref_value)

        energy_value = "<165W"
        formatted = self.formatted(
            "TV",
            {"product_url": "https://example/p/tv", "estimated_annual_electricity_use": energy_value},
        )
        self.assertEqual(formatted["estimated_annual_electricity_use"], energy_value)
        self.assertEqual(_db_value("estimated_annual_electricity_use", energy_value), energy_value)

        ldy_value = "8,8 libras"
        formatted = self.formatted("LDY", {"product_url": "https://example/p/ldy", "ldy_capacity": ldy_value})
        self.assertEqual(formatted["ldy_capacity"], ldy_value)
        self.assertEqual(_db_value("ldy_capacity", ldy_value), ldy_value)

    def test_loading_merge_is_idempotent_through_translation_and_csv(self):
        row = {"ldy_loading_type": "Top load,Front load", "ldy_capacity": "De 11 a 15kg"}
        self.assertEqual(translate_row(row)["ldy_loading_type"], "Top load,Front load")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.csv"
            with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY", "SEDA_TRANSLATE_OUTPUT": "1"}):
                write_csv(path, [row], columns=["ldy_loading_type", "ldy_capacity"])
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                saved = next(csv.DictReader(handle))
        self.assertEqual(saved["ldy_loading_type"], "Top load,Front load")
        self.assertEqual(saved["ldy_capacity"], "De 11 a 15kg")

    def test_blank_only_becomes_database_null(self):
        self.assertIsNone(_db_value("ref_capacity", ""))
        self.assertEqual(_db_value("ref_capacity", "0"), "0")
        self.assertIn("ref_capacity", final_output_columns("REF"))
        self.assertIn("ldy_capacity", final_output_columns("LDY"))

    def test_authoritative_detail_clears_explicit_audited_blanks_and_overwrites_values(self):
        row = {
            "screen_size": "10-40 polegadas",
            "estimated_annual_electricity_use": "Bivolt",
            "ref_capacity": "wrong",
            "ldy_capacity": "wrong",
            "ldy_loading_type": "wrong",
            "model_year": "2024",
        }
        _merge_authoritative_detail(
            row,
            {
                "screen_size": "",
                "estimated_annual_electricity_use": None,
                "ref_capacity": [],
                "ldy_capacity": {},
                "ldy_loading_type": "",
                "model_year": "",
            },
        )
        for key in (
            "screen_size",
            "estimated_annual_electricity_use",
            "ref_capacity",
            "ldy_capacity",
            "ldy_loading_type",
        ):
            self.assertEqual(row[key], "", key)
        self.assertEqual(row["model_year"], "2024")

        _merge_authoritative_detail(row, {"screen_size": "55 polegadas", "ref_capacity": "305"})
        self.assertEqual(row["screen_size"], "55 polegadas")
        self.assertEqual(row["ref_capacity"], "305")

    def test_verified_site_parsers_preserve_explicit_blank_capacity_keys(self):
        cases = []
        for line, title, fields in (
            ("REF", "Geladeira Principal", ("ref_capacity",)),
            ("LDY", "Maquina de Lavar Principal", ("ldy_capacity", "ldy_loading_type")),
        ):
            magalu_item = {
                "id": "sample",
                "title": title,
                "factsheet": [],
                "attributes": [],
                "offers": [],
            }
            magalu_payload = {"props": {"pageProps": {"data": {"item": magalu_item}}}}
            cases.append(
                (
                    "Magalu",
                    "https://www.magazineluiza.com.br/item/p/sample/",
                    "https://www.magazineluiza.com.br",
                    magalu_payload,
                    line,
                    fields,
                )
            )
            casas_product = {
                "id": "product-id",
                "name": title,
                "sku": {"id": "sample"},
                "specGroups": [],
            }
            casas_payload = {"props": {"pageProps": {"product": casas_product}}}
            cases.append(
                (
                    "Casas Bahia",
                    "https://www.casasbahia.com.br/produto/p/123",
                    "https://www.casasbahia.com.br",
                    casas_payload,
                    line,
                    fields,
                )
            )

        for retailer, product_url, base_url, payload, line, fields in cases:
            if retailer == "Casas Bahia":
                product_url = "https://www.casasbahia.com.br/produto/p/sample"
            html = (
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload)
                + "</script>"
            )
            with self.subTest(retailer=retailer, line=line), patch.dict(
                os.environ,
                {"SEDA_PRODUCT_LINE": line},
            ):
                detail = parse_detail(html, retailer, base_url, product_url)
                self.assertIs(detail["_detail_identity_verified"], True)
                row = {field: "WRONG" for field in fields}
                _merge_generic_product_detail(row, detail)
                for field in fields:
                    self.assertIn(field, detail)
                    self.assertEqual(detail[field], "")
                    self.assertEqual(row[field], "")

    def test_magalu_graphql_success_clears_listing_screen_false_positive(self):
        row = {
            "retailer": "Magalu",
            "product_url": "https://www.magazineluiza.com.br/item/p/sample/et/tv4k/",
            "screen_size": "10-40 polegadas",
        }
        result = {
            "success": True,
            "detail": {"retailer_sku_name": "STPA 45 Suporte para TV", "screen_size": ""},
            "trace": [],
        }
        with patch("seda.magalu.detail_api.fetch_detail", return_value=result):
            _magalu_graphql_detail(row, row["product_url"])
        self.assertEqual(row["screen_size"], "")

        result["detail"]["screen_size"] = "55 polegadas"
        with patch("seda.magalu.detail_api.fetch_detail", return_value=result):
            _magalu_graphql_detail(row, row["product_url"])
        self.assertEqual(row["screen_size"], "55 polegadas")

    def test_magalu_rendered_detail_success_clears_listing_false_positive(self):
        row = {
            "retailer": "Magalu",
            "product_url": "https://www.magazineluiza.com.br/item/p/sample/et/tv4k/",
            "screen_size": "10-40 polegadas",
        }
        result = ZenRowsResult(
            success=True,
            url=row["product_url"],
            profile="test",
            status_code=200,
            text="<html>product</html>",
            estimated_multiplier="1x",
        )
        parsed = {
            "retailer_sku_name": "STPA 45 Suporte para TV",
            "screen_size": "",
            "_detail_identity_verified": True,
        }
        with patch.dict(os.environ, {"SEDA_MAGALU_ZENROWS_DETAIL_FALLBACK": "1"}), patch(
            "seda.magalu.zenrows_client.fetch_pdp_rendered_html",
            return_value=result,
        ), patch("seda.step08_detail_enrichment.parse_detail", return_value=parsed):
            self.assertTrue(_merge_magalu_zenrows_detail(row, row["product_url"]))
        self.assertEqual(row["screen_size"], "")

    def test_magalu_rendered_partial_shell_cannot_clear_listing_value(self):
        row = {
            "retailer": "Magalu",
            "product_url": "https://www.magazineluiza.com.br/item/p/sample/et/tv4k/",
            "screen_size": "55 polegadas",
        }
        result = ZenRowsResult(
            success=True,
            url=row["product_url"],
            profile="test",
            status_code=200,
            text="<html>partial shell</html>",
            estimated_multiplier="1x",
        )
        parsed = {"retailer_sku_name": "Magazine Luiza", "final_sku_price": "1000", "screen_size": ""}
        with patch.dict(os.environ, {"SEDA_MAGALU_ZENROWS_DETAIL_FALLBACK": "1"}), patch(
            "seda.magalu.zenrows_client.fetch_pdp_rendered_html",
            return_value=result,
        ), patch("seda.step08_detail_enrichment.parse_detail", return_value=parsed):
            self.assertFalse(_merge_magalu_zenrows_detail(row, row["product_url"]))
        self.assertEqual(row["screen_size"], "55 polegadas")
        self.assertIn("missing_product_identity", row["parse_status"])

    def test_magalu_jsonld_only_rendered_detail_is_not_authoritative(self):
        row = {
            "retailer": "Magalu",
            "product_url": "https://www.magazineluiza.com.br/item/p/sample/et/tv4k/",
            "retailer_sku_name": "Listing TV",
            "screen_size": "55 polegadas",
        }
        html = (
            '<script type="application/ld+json">'
            + json.dumps({"@type": "Product", "name": "JSON-LD Product", "offers": {"price": "1000"}})
            + "</script>"
        )
        result = ZenRowsResult(
            success=True,
            url=row["product_url"],
            profile="test",
            status_code=200,
            text=html,
            estimated_multiplier="1x",
        )
        with patch.dict(os.environ, {"SEDA_MAGALU_ZENROWS_DETAIL_FALLBACK": "1"}), patch(
            "seda.magalu.zenrows_client.fetch_pdp_rendered_html",
            return_value=result,
        ):
            self.assertFalse(_merge_magalu_zenrows_detail(row, row["product_url"]))
        self.assertEqual(row["retailer_sku_name"], "Listing TV")
        self.assertEqual(row["screen_size"], "55 polegadas")
        self.assertIn("missing_product_identity", row["parse_status"])

    def test_magalu_main_next_identity_requires_matching_url_id(self):
        product_url = "https://www.magazineluiza.com.br/item/p/sample/et/tv4k/"

        def parsed(item_id):
            item = {"id": item_id, "title": "Smart TV", "factsheet": [], "attributes": [], "offers": []}
            payload = {"props": {"pageProps": {"data": {"item": item}}}}
            html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"
            return parse_detail(html, "Magalu", "https://www.magazineluiza.com.br", product_url)

        matched = parsed("sample")
        self.assertIs(matched["_detail_identity_verified"], True)
        self.assertIsNot(matched.get("_detail_identity_conflict"), True)

        missing = parsed("")
        self.assertIs(missing["_detail_identity_verified"], False)
        self.assertIsNot(missing.get("_detail_identity_conflict"), True)

        mismatched = parsed("different")
        self.assertIs(mismatched["_detail_identity_verified"], False)
        self.assertIs(mismatched["_detail_identity_conflict"], True)

    def test_casas_product_source_success_clears_listing_capacity_false_positive(self):
        row = {
            "retailer": "Casas Bahia",
            "product_url": "https://www.casasbahia.com.br/produto/p/123",
            "sku": "123",
            "ref_capacity": "84",
        }
        result = {
            "success": True,
            "detail": {"retailer_sku_name": "Acessorio para geladeira", "ref_capacity": ""},
            "method": "test_product_source",
            "headers": {},
        }
        env = {
            "SEDA_CASAS_BAHIA_API_ENRICH": "1",
            "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API": "1",
            "SEDA_CASAS_BAHIA_FREIGHT_API": "0",
            "SEDA_CASAS_BAHIA_PICKUP_API": "0",
            "SEDA_CASAS_BAHIA_RECS_API": "0",
            "SEDA_CASAS_BAHIA_REVIEW_API": "0",
        }
        with patch.dict(os.environ, env), patch(
            "seda.casas_bahia.detail_api.fetch_product_source",
            return_value=result,
        ):
            _merge_casas_bahia_apis(row)
        self.assertEqual(row["ref_capacity"], "")

    def test_casas_product_source_payload_requires_identity_name_and_content(self):
        valid = {
            "isValid": True,
            "product": {"id": 10, "name": "Geladeira 305L", "description": "Capacidade 305L"},
            "sku": {"id": "123", "name": "."},
        }
        self.assertEqual(_product_source_payload_error(valid, expected_sku_id="123"), "")
        self.assertEqual(_product_source_payload_error({}, expected_sku_id="123"), "empty_data")
        self.assertIn("sku_mismatch", _product_source_payload_error(valid, expected_sku_id="999"))
        no_content = {"product": {"id": 10, "name": "Geladeira"}, "sku": {"id": "123", "name": "."}}
        self.assertEqual(_product_source_payload_error(no_content, expected_sku_id="123"), "missing_product_content")
        image_and_highlight_only = {
            "product": {"id": 10, "name": "Geladeira"},
            "sku": {
                "id": "123",
                "name": "Geladeira",
                "images": [{"url": "https://example.test/image.jpg"}],
                "highlights": {"items": ["Entrega rapida"]},
            },
        }
        self.assertEqual(
            _product_source_payload_error(image_and_highlight_only, expected_sku_id="123"),
            "missing_product_content",
        )
        specs_only = {
            "product": {
                "id": 10,
                "name": "Geladeira",
                "specGroups": [
                    {"name": "Especificacoes", "specs": [{"name": "Capacidade", "value": "305L"}]}
                ],
            },
            "sku": {"id": "123", "name": "Geladeira"},
        }
        self.assertEqual(_product_source_payload_error(specs_only, expected_sku_id="123"), "")

    def test_casas_image_only_product_source_cache_cannot_clear_listing(self):
        partial = {
            "product": {"id": 10, "name": "Geladeira"},
            "sku": {
                "id": "123",
                "name": "Geladeira",
                "images": [{"url": "https://example.test/image.jpg"}],
                "highlights": {"items": ["Entrega rapida"]},
            },
        }
        row = {
            "retailer": "Casas Bahia",
            "product_url": "https://www.casasbahia.com.br/produto/p/123",
            "sku": "123",
            "ref_capacity": "305",
        }
        env = {
            "SEDA_CASAS_BAHIA_API_ENRICH": "1",
            "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API": "1",
            "SEDA_CASAS_BAHIA_FREIGHT_API": "0",
            "SEDA_CASAS_BAHIA_PICKUP_API": "0",
            "SEDA_CASAS_BAHIA_RECS_API": "0",
            "SEDA_CASAS_BAHIA_REVIEW_API": "0",
        }
        with patch.dict(os.environ, env), patch(
            "seda.casas_bahia.detail_api._read_product_source_cache",
            return_value=partial,
        ), patch(
            "seda.casas_bahia.detail_api._product_source_attempts",
            return_value=[],
        ):
            _merge_casas_bahia_apis(row)
        self.assertEqual(row["ref_capacity"], "305")
        self.assertIn("product_source_failed:cache_invalid_payload:missing_product_content", row["parse_status"])

    def test_casas_product_source_name_falls_back_to_sku(self):
        data = {
            "product": {"id": 10, "name": "", "rawName": "", "description": "Especificacoes"},
            "sku": {"id": "123", "name": "Geladeira SKU 305L"},
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
            detail = _product_source_detail(data)
        self.assertEqual(detail["retailer_sku_name"], "Geladeira SKU 305L")

    def test_casas_product_source_adjacent_html_cells_keep_specs(self):
        def source(name, description):
            return {
                "product": {"id": 10, "name": name, "description": description, "specGroups": []},
                "sku": {"id": "123", "name": name},
            }

        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            detail = _product_source_detail(
                source("Smart TV 55 polegadas", "<div>Consumo de energia</div><div>130W</div>")
            )
        self.assertEqual(detail["estimated_annual_electricity_use"], "130W")

        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
            detail = _product_source_detail(source("Geladeira", "<div>Capacidade</div><div>305L</div>"))
        self.assertEqual(detail["ref_capacity"], "305L")

        description = (
            "<table><tr><td>Capacidade de lavagem</td><td>13kg</td>"
            "<th>Acesso ao cesto</th><td>Superior</td></tr></table>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            detail = _product_source_detail(source("Lavadora automatica", description))
        self.assertEqual(detail["ldy_capacity"], "13kg")
        self.assertEqual(detail["ldy_loading_type"], "Top load")

    def test_casas_product_source_energy_stops_at_next_html_spec_row(self):
        data = {
            "product": {
                "id": 10,
                "name": "Smart TV",
                "description": (
                    "<div>Consumo de energia</div><div>36W</div>"
                    "<div>Entradas</div><div>3xHDMI</div>"
                ),
                "specGroups": [],
            },
            "sku": {"id": "123", "name": "Smart TV"},
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            detail = _product_source_detail(data)
        self.assertEqual(detail["estimated_annual_electricity_use"], "36W")

    def test_casas_tv_title_screen_fallback_is_shared_by_product_source_and_next_html(self):
        cases = (
            ('Smart TV Aiwa 43" Android AWS-TV-43-BL-02-A', '43"'),
            ('REEMBALADO: Smart TV Samsung 55 Polegadas 4K UHD', '55 Polegadas'),
            ('Smart TV Samsung 55 Polegadas 4K Wi-Fi Tizen', '55 Polegadas'),
            ('SMART TV LG 43" ThinQ AI HDR 10', '43"'),
            ('Smart Tv Lg Hd 32 32Lr600b Preto', '32"'),
            ('Smart TV AIWA 55” Android 4K Borda Ultrafina', '55"'),
            ('Smart TV TCL 50 Polegadas QLED Mini LED 4K', '50 Polegadas'),
            ('Smart TV QLED 40 Full HD Wi-Fi Android', '40"'),
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            for index, (title, expected) in enumerate(cases):
                with self.subTest(title=title):
                    source = {
                        "product": {
                            "id": index + 1,
                            "name": title,
                            "description": "Produto",
                            "specGroups": [],
                        },
                        "sku": {"id": "123", "name": title},
                    }
                    self.assertEqual(_product_source_detail(source)["screen_size"], expected)

                    product = {
                        "id": index + 1,
                        "name": title,
                        "sku": {"id": "123"},
                        "specGroups": [],
                    }
                    payload = {"props": {"pageProps": {"product": product}}}
                    html = (
                        '<script id="__NEXT_DATA__" type="application/json">'
                        + json.dumps(payload)
                        + "</script>"
                    )
                    parsed = parse_detail(
                        html,
                        "Casas Bahia",
                        "https://www.casasbahia.com.br",
                        "https://www.casasbahia.com.br/produto/p/123",
                    )
                    self.assertIs(parsed["_detail_identity_verified"], True)
                    self.assertEqual(parsed["screen_size"], expected)

    def test_qled_oled_title_screen_fallback_keeps_common_guards(self):
        from seda.common.field_rules import extract_screen_size_from_title

        self.assertEqual(extract_screen_size_from_title("QLED 40 Full HD"), '40"')
        self.assertEqual(extract_screen_size_from_title("OLED 55 4K"), '55"')
        for title in ("QLED 120 Hz", "QLED 130 W", "QLED 10-40 polegadas"):
            with self.subTest(title=title):
                self.assertEqual(extract_screen_size_from_title(title), "")

    def test_casas_title_screen_fallback_rejects_tv_accessories_in_both_producers(self):
        accessories = (
            "Controle Remoto para Smart TV Samsung 55 4K",
            "Suporte Articulado para TV 55 polegadas",
            "Smart TV Stick 55 4K Wi-Fi",
            "TV Stick 43 Full HD",
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            for index, title in enumerate(accessories):
                with self.subTest(title=title):
                    screen_specs = [
                        {
                            "name": "Especificações",
                            "specs": [{"name": "Tamanho da Tela", "value": "55 polegadas"}],
                        }
                    ]
                    source = {
                        "product": {
                            "id": index + 1,
                            "name": title,
                            "description": "Acessório",
                            "specGroups": screen_specs,
                        },
                        "sku": {"id": "123", "name": title},
                    }
                    self.assertEqual(_product_source_detail(source)["screen_size"], "")

                    product = {
                        "id": index + 1,
                        "name": title,
                        "sku": {"id": "123"},
                        "specGroups": screen_specs,
                    }
                    payload = {"props": {"pageProps": {"product": product}}}
                    html = (
                        '<script id="__NEXT_DATA__" type="application/json">'
                        + json.dumps(payload)
                        + "</script>"
                        + "<div>Tamanho da Tela: 55 polegadas</div>"
                    )
                    parsed = parse_detail(
                        html,
                        "Casas Bahia",
                        "https://www.casasbahia.com.br",
                        "https://www.casasbahia.com.br/produto/p/123",
                    )
                    self.assertEqual(parsed["screen_size"], "")
                    magalu = extract_magalu_fields(
                        {
                            "title": title,
                            "factsheet": [
                                {"keyName": "Tamanho da Tela", "value": "55 polegadas"}
                            ],
                        },
                        "TV",
                    )
                    self.assertEqual(magalu["screen_size"], "")

    def test_casas_product_source_rejects_invalid_http_and_cache_payloads(self):
        response = Mock(status_code=200, headers={"content-type": "application/json"}, text="{}")
        response.json.return_value = {}
        with patch("seda.casas_bahia.detail_api.requests.get", return_value=response):
            result = _fetch_product_source_direct("https://example.test/source", timeout=1)
        self.assertFalse(result["success"])
        self.assertIn("invalid_payload", result["error"])

        with patch("seda.casas_bahia.detail_api._read_product_source_cache", return_value={}), patch(
            "seda.casas_bahia.detail_api._product_source_attempts",
            return_value=[],
        ):
            result = fetch_product_source("123", timeout=1)
        self.assertFalse(result["success"])
        self.assertIn("cache_invalid_payload", result["error"])

    def test_casas_product_source_success_without_name_cannot_clear_listing(self):
        row = {
            "retailer": "Casas Bahia",
            "product_url": "https://www.casasbahia.com.br/produto/p/123",
            "sku": "123",
            "ref_capacity": "305",
        }
        result = {"success": True, "detail": {"retailer_sku_name": "", "ref_capacity": ""}, "headers": {}}
        env = {
            "SEDA_CASAS_BAHIA_API_ENRICH": "1",
            "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API": "1",
            "SEDA_CASAS_BAHIA_FREIGHT_API": "0",
            "SEDA_CASAS_BAHIA_PICKUP_API": "0",
            "SEDA_CASAS_BAHIA_RECS_API": "0",
            "SEDA_CASAS_BAHIA_REVIEW_API": "0",
        }
        with patch.dict(os.environ, env), patch(
            "seda.casas_bahia.detail_api.fetch_product_source",
            return_value=result,
        ):
            _merge_casas_bahia_apis(row)
        self.assertEqual(row["ref_capacity"], "305")
        self.assertIn("product_source_missing_identity", row["parse_status"])

    def test_generic_detail_requires_verified_structured_identity_before_clearing(self):
        row = {"retailer_sku_name": "Listing TV", "screen_size": "10-40 polegadas"}
        _merge_generic_product_detail(row, {"screen_size": ""})
        self.assertEqual(row["screen_size"], "10-40 polegadas")
        _merge_generic_product_detail(
            row,
            {
                "retailer_sku_name": "Unverified meta name",
                "screen_size": "",
                "estimated_annual_electricity_use": "130W",
                "final_sku_price": "1000",
            },
        )
        self.assertEqual(row["retailer_sku_name"], "Listing TV")
        self.assertEqual(row["screen_size"], "10-40 polegadas")
        self.assertNotIn("estimated_annual_electricity_use", row)
        self.assertNotIn("final_sku_price", row)

        self.assertTrue(
            _merge_generic_product_detail(
                row,
                {
                    "retailer_sku_name": "listing-tv",
                    "screen_size": "55 polegadas",
                    "estimated_annual_electricity_use": "130W",
                    "final_sku_price": "1000",
                },
            )
        )
        self.assertEqual(row["retailer_sku_name"], "Listing TV")
        self.assertEqual(row["screen_size"], "10-40 polegadas")
        self.assertEqual(row["estimated_annual_electricity_use"], "130W")
        self.assertEqual(row["final_sku_price"], "1000")

        conflict_row = {"retailer_sku_name": "Listing TV"}
        self.assertFalse(
            _merge_generic_product_detail(
                conflict_row,
                {
                    "retailer_sku_name": "Listing TV",
                    "final_sku_price": "999",
                    "_detail_identity_conflict": True,
                },
            )
        )
        self.assertNotIn("final_sku_price", conflict_row)
        _merge_generic_product_detail(
            row,
            {
                "retailer_sku_name": "Smart TV",
                "screen_size": "",
                "_detail_identity_verified": True,
            },
        )
        self.assertEqual(row["screen_size"], "")

    def test_casas_meta_and_jsonld_only_details_are_not_authoritative(self):
        cases = (
            (
                "meta",
                '<meta property="og:title" content="Casas Bahia">'
                '<div data-testid="product-price-value">R$ 1.999,00</div>',
            ),
            (
                "jsonld",
                '<script type="application/ld+json">'
                + json.dumps({"@type": "Product", "name": "Geladeira JSON-LD", "offers": {"price": "1999"}})
                + "</script>",
            ),
        )
        for label, html in cases:
            with self.subTest(source=label), patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
                detail = parse_detail(
                    html,
                    "Casas Bahia",
                    "https://www.casasbahia.com.br",
                    "https://www.casasbahia.com.br/produto/p/123",
                )
            self.assertIsNot(detail.get("_detail_identity_verified"), True)
            row = {"retailer_sku_name": "Listing Geladeira", "ref_capacity": "305L"}
            _merge_generic_product_detail(row, detail)
            self.assertEqual(row["retailer_sku_name"], "Listing Geladeira")
            self.assertEqual(row["ref_capacity"], "305L")
            self.assertFalse(row.get("final_sku_price"))

    def test_casas_recommendation_only_next_product_is_not_main_identity(self):
        recommendation = {
            "id": "999",
            "name": "Geladeira Recomendada 300L",
            "specGroups": [{"specs": [{"name": "Capacidade total", "value": "300L"}]}],
        }
        payload = {"props": {"pageProps": {"recommendations": [{"product": recommendation}]}}}
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"
        self.assertEqual(_casas_bahia_next_product(html), {})
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
            detail = parse_detail(
                html,
                "Casas Bahia",
                "https://www.casasbahia.com.br",
                "https://www.casasbahia.com.br/produto/p/123",
            )
        self.assertIsNot(detail.get("_detail_identity_verified"), True)
        self.assertNotEqual(detail.get("retailer_sku_name"), recommendation["name"])
        self.assertEqual(detail.get("ref_capacity") or "", "")

        main_payload = {
            "props": {
                "pageProps": {
                    "product": {
                        **recommendation,
                        "id": "product-id-is-not-the-sku",
                        "sku": {"id": "123"},
                    }
                }
            }
        }
        main_html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(main_payload) + "</script>"
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
            main_detail = parse_detail(
                main_html,
                "Casas Bahia",
                "https://www.casasbahia.com.br",
                "https://www.casasbahia.com.br/produto/p/123",
            )
        self.assertIs(main_detail["_detail_identity_verified"], True)
        self.assertEqual(main_detail["ref_capacity"], "300L")

    def test_exact_html_compatibility_path_cannot_bypass_validators(self):
        item = {
            "id": "sample",
            "title": "Smart TV",
            "factsheet": [
                {"keyName": "Tamanho da Tela", "value": "10-40 polegadas"},
                {"keyName": "Consumo Aproximado de Energia", "value": "Bivolt"},
            ],
            "attributes": [],
            "offers": [],
            "rating": {},
        }
        payload = {"props": {"pageProps": {"data": {"item": item}}}}
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
            + "<div><span>Tamanho Da Tela</span><span>10-40 polegadas</span></div>"
            + "<div><span>Consumo Aproximado de Energia</span><span>Bivolt</span></div>"
        )
        row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Smart TV",
            "product_url": "https://example/p/sample",
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            detail = parse_detail(html, "Magalu", "https://example", row["product_url"])
            updated = _merge_magalu_exact_html_specs(row, html, detail)
        self.assertFalse(updated)
        self.assertNotIn("screen_size", row)
        self.assertNotIn("estimated_annual_electricity_use", row)

    def test_exact_html_path_keeps_valid_dom_only_specs(self):
        item = {"id": "sample", "title": "Smart TV", "factsheet": [], "attributes": [], "offers": []}
        payload = {"props": {"pageProps": {"data": {"item": item}}}}
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
            "<div><span>Tamanho Da Tela</span><span>55 polegadas</span></div>"
            "<div><span>Consumo Aproximado de Energia</span><span>130W</span></div>"
        )
        row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Smart TV",
            "product_url": "https://example/p/sample",
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            detail = parse_detail(html, "Magalu", "https://example", row["product_url"])
            self.assertTrue(_merge_magalu_exact_html_specs(row, html, detail))
        self.assertEqual(row["screen_size"], "55 polegadas")
        self.assertEqual(row["estimated_annual_electricity_use"], "130W")

    def test_exact_html_energy_uses_labeled_target_numeric_contract(self):
        for html_value, expected in (("130", "130"), ("&lt;165", "<165")):
            with self.subTest(value=expected):
                html = (
                    '<script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps(
                        {
                            "props": {
                                "pageProps": {
                                    "data": {
                                        "item": {
                                            "id": "sample",
                                            "title": "Smart TV",
                                            "factsheet": [],
                                            "attributes": [],
                                            "offers": [],
                                        }
                                    }
                                }
                            }
                        }
                    )
                    + "</script>"
                    "<div><span>Consumo Aproximado de Energia</span>"
                    f"<span>{html_value}</span></div>"
                )
                row = {
                    "retailer": "Magalu",
                    "retailer_sku_name": "Smart TV",
                    "product_url": "https://example/p/sample",
                }
                detail = parse_detail(html, "Magalu", "https://example", row["product_url"])
                self.assertTrue(_merge_magalu_exact_html_specs(row, html, detail))
                self.assertEqual(row["estimated_annual_electricity_use"], expected)

    def test_zenrows_pdp_fallback_merges_semantic_capacity(self):
        item = {
            "id": "sample",
            "title": "Geladeira 395L",
            "factsheet": [
                {"keyName": "Capacidade do Refrigerador (L)", "value": "305"},
                {"keyName": "Capacidade do Freezer (L)", "value": "84"},
            ],
            "attributes": [],
            "offers": [],
            "rating": {},
        }
        payload = {"props": {"pageProps": {"data": {"item": item}}}}
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"
        result = ZenRowsResult(
            success=True,
            url="https://example/p/sample",
            profile="test",
            status_code=200,
            text=html,
            estimated_multiplier="1x",
        )
        row = {"retailer": "Magalu", "product_url": result.url}
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "REF", "SEDA_MAGALU_ZENROWS_PDP_FALLBACK": "1"},
        ), patch("seda.magalu.zenrows_client.fetch_next_data_html", return_value=result):
            self.assertTrue(_merge_magalu_zenrows_pdp_html(row, result.url))
        self.assertEqual(row["ref_capacity"], "305")

    def test_zenrows_pdp_auxiliary_fallback_keeps_blank_only_policy(self):
        result = ZenRowsResult(
            success=True,
            url="https://example/p/sample",
            profile="test",
            status_code=200,
            text='<script id="__NEXT_DATA__" type="application/json">{}</script>',
            estimated_multiplier="1x",
        )
        row = {
            "retailer": "Magalu",
            "product_url": result.url,
            "screen_size": "listing value",
        }
        with patch.dict(os.environ, {"SEDA_MAGALU_ZENROWS_PDP_FALLBACK": "1"}), patch(
            "seda.magalu.zenrows_client.fetch_next_data_html",
            return_value=result,
        ), patch("seda.step08_detail_enrichment.parse_detail", return_value={"screen_size": ""}):
            _merge_magalu_zenrows_pdp_html(row, result.url)
        self.assertEqual(row["screen_size"], "listing value")

    def test_casas_fixed_main_identity_uses_sku_not_product_id(self):
        product_url = "https://www.casasbahia.com.br/produto/p/123"

        def parsed(product, canonical_url=""):
            payload = {"props": {"pageProps": {"product": product}}}
            html = (
                (f'<link rel="canonical" href="{canonical_url}">' if canonical_url else "")
                + '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload)
                + "</script>"
            )
            with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
                return parse_detail(
                    html,
                    "Casas Bahia",
                    "https://www.casasbahia.com.br",
                    product_url,
                )

        base = {"id": "123", "name": "Geladeira", "specGroups": []}
        matched = parsed({**base, "id": "different-product-id", "sku": {"id": "123"}})
        self.assertIs(matched["_detail_identity_verified"], True)
        self.assertIsNot(matched.get("_detail_identity_conflict"), True)

        missing = parsed(base)
        self.assertIs(missing["_detail_identity_verified"], False)
        self.assertIsNot(missing.get("_detail_identity_conflict"), True)

        mismatched = parsed({**base, "sku": {"id": "999"}})
        self.assertIs(mismatched["_detail_identity_verified"], False)
        self.assertIs(mismatched["_detail_identity_conflict"], True)

        canonical_fallback = parsed(base, product_url)
        self.assertIs(canonical_fallback["_detail_identity_verified"], True)
        self.assertIsNot(canonical_fallback.get("_detail_identity_conflict"), True)

        canonical_must_not_mask_conflict = parsed(
            {**base, "sku": {"id": "999"}},
            product_url,
        )
        self.assertIs(canonical_must_not_mask_conflict["_detail_identity_verified"], False)
        self.assertIs(canonical_must_not_mask_conflict["_detail_identity_conflict"], True)

    def test_verified_main_skips_mismatched_recommendation_jsonld(self):
        item = {
            "id": "sample",
            "title": "Smart TV Main",
            "factsheet": [{"keyName": "Modelo", "value": "MAIN-1"}],
            "attributes": [],
            "offers": [{"price": 2000}],
        }
        payload = {"props": {"pageProps": {"data": {"item": item}}}}
        recommendation = {
            "@type": "Product",
            "name": "Smart TV Recommendation",
            "sku": "REC-9",
            "offers": {"price": "1"},
            "aggregateRating": {"ratingValue": "5", "ratingCount": "999"},
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
            + '<script type="application/ld+json">'
            + json.dumps(recommendation)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            detail = parse_detail(
                html,
                "Magalu",
                "https://www.magazineluiza.com.br",
                "https://www.magazineluiza.com.br/item/p/sample/et/tv4k/",
            )
        self.assertIs(detail["_detail_identity_verified"], True)
        self.assertEqual(detail["retailer_sku_name"], "Smart TV Main")
        self.assertEqual(detail["sku"], "MAIN-1")
        self.assertEqual(detail["final_sku_price"], "R$2.000,00")
        self.assertFalse(detail.get("star_rating"))
        self.assertFalse(detail.get("count_of_star_ratings"))

    def test_verified_main_skips_anonymous_jsonld_without_positive_identity(self):
        item = {
            "id": "sample",
            "title": "Smart TV Main",
            "factsheet": [],
            "attributes": [],
            "offers": [],
        }
        payload = {"props": {"pageProps": {"data": {"item": item}}}}
        anonymous = {
            "@type": "Product",
            "offers": {"price": "1"},
            "aggregateRating": {"ratingValue": "5", "ratingCount": "999"},
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
            + '<script type="application/ld+json">'
            + json.dumps(anonymous)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            detail = parse_detail(
                html,
                "Magalu",
                "https://www.magazineluiza.com.br",
                "https://www.magazineluiza.com.br/item/p/sample/et/tv4k/",
            )
        self.assertIs(detail["_detail_identity_verified"], True)
        self.assertFalse(detail.get("final_sku_price"))
        self.assertFalse(detail.get("star_rating"))
        self.assertFalse(detail.get("count_of_star_ratings"))

    def test_unverified_same_name_main_skips_anonymous_jsonld_without_positive_identity(self):
        item = {
            "title": "Listing TV",
            "factsheet": [],
            "attributes": [],
            "offers": [],
        }
        payload = {"props": {"pageProps": {"data": {"item": item}}}}
        anonymous = {
            "@type": "Product",
            "offers": {"price": "1"},
            "aggregateRating": {"ratingValue": "5", "ratingCount": "999"},
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
            + '<script type="application/ld+json">'
            + json.dumps(anonymous)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            detail = parse_detail(
                html,
                "Magalu",
                "https://www.magazineluiza.com.br",
                "https://www.magazineluiza.com.br/item/p/sample/et/tv4k/",
            )
        self.assertIs(detail["_detail_identity_verified"], False)
        self.assertEqual(detail["retailer_sku_name"], "Listing TV")
        row = {"retailer": "Magalu", "retailer_sku_name": "Listing TV"}
        _merge_generic_product_detail(row, detail)
        self.assertFalse(row.get("final_sku_price"))
        self.assertFalse(row.get("star_rating"))
        self.assertFalse(row.get("count_of_star_ratings"))

    def test_magalu_graphql_item_identity_blocks_detail_and_shipping(self):
        def requested(actual_id):
            def request(_item_id, _timeout, trace, context_url=None):
                trace.append({"label": "item", "method": "test"})
                return {"id": actual_id, "title": "Smart TV"}

            return request

        for actual_id, expected_error in (
            ("", "item_identity_missing"),
            ("different", "item_identity_mismatch"),
        ):
            with self.subTest(error=expected_error), patch.object(
                magalu_detail_api,
                "_request_item",
                side_effect=requested(actual_id),
            ), patch.object(magalu_detail_api, "_detail_from_item") as detail_from_item, patch.object(
                magalu_detail_api,
                "fetch_shipping",
            ) as fetch_shipping:
                result = magalu_detail_api.fetch_detail("sample")
                shipping = magalu_detail_api.fetch_shipping_for_item_id("sample")
            self.assertFalse(result["success"])
            self.assertEqual(result["error"], expected_error)
            self.assertEqual(result["detail"], {})
            self.assertEqual(result["trace"][0]["label"], "item")
            self.assertFalse(shipping["success"])
            self.assertEqual(shipping["error"], expected_error)
            detail_from_item.assert_not_called()
            fetch_shipping.assert_not_called()

        with patch.object(
            magalu_detail_api,
            "_request_item",
            side_effect=requested("sample"),
        ), patch.object(
            magalu_detail_api,
            "_detail_from_item",
            return_value={"retailer_sku_name": "Smart TV"},
        ), patch.object(
            magalu_detail_api,
            "fetch_shipping",
            return_value={"success": True, "delivery": "Entrega", "pickup": "", "trace": []},
        ) as fetch_shipping, patch.dict(
            os.environ,
            {"SEDA_MAGALU_SHIPPING_GRAPHQL": "0", "SEDA_MAGALU_SIMILAR_GRAPHQL": "0"},
        ):
            detail_result = magalu_detail_api.fetch_detail("sample")
            shipping_result = magalu_detail_api.fetch_shipping_for_item_id("sample")
        self.assertTrue(detail_result["success"])
        self.assertTrue(shipping_result["success"])
        fetch_shipping.assert_called_once()

    def test_magalu_ssr_shipping_validates_next_item_id(self):
        def html(item):
            payload = {"props": {"pageProps": {"data": {"item": item}}}}
            return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"

        product_url = "https://example/p/sample"
        for item, token in (
            ({"id": "", "title": "Smart TV"}, "item_identity_missing"),
            ({"id": "different", "title": "Smart TV"}, "item_identity_mismatch"),
        ):
            row = {"retailer": "Magalu"}
            with patch("seda.magalu.detail_api.fetch_shipping") as fetch_shipping:
                self.assertFalse(_merge_magalu_shipping_from_next_data(row, html(item), product_url))
            fetch_shipping.assert_not_called()
            self.assertIn(token, row["parse_status"])

        row = {"retailer": "Magalu"}
        with patch(
            "seda.magalu.detail_api.fetch_shipping",
            return_value={"success": True, "delivery": "Entrega", "pickup": "", "trace": []},
        ) as fetch_shipping:
            self.assertTrue(
                _merge_magalu_shipping_from_next_data(
                    row,
                    html({"id": "sample", "title": "Smart TV"}),
                    product_url,
                )
            )
        fetch_shipping.assert_called_once()
        self.assertEqual(row["delivery_availability"], "Entrega")

    def test_magalu_pdp_identity_gate_blocks_conflict_and_allows_exact_name(self):
        product_url = "https://example/p/sample"

        def html(item, dom):
            payload = {"props": {"pageProps": {"data": {"item": item}}}}
            return (
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload)
                + "</script>"
                + dom
            )

        conflict_html = html(
            {"id": "different", "title": "Smart TV", "factsheet": [], "attributes": [], "offers": []},
            "<div><span>Consumo (máximo)</span><span>130 W</span></div>",
        )
        row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Smart TV",
            "product_url": product_url,
        }
        with patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "TV",
                "SEDA_MAGALU_PDP_HTML_FETCH": "1",
                "SEDA_MAGALU_ZENROWS_PDP_FALLBACK": "0",
            },
        ), patch(
            "seda.step08_detail_enrichment._fetch_magalu_next_html",
            return_value={"status_code": 200, "text": conflict_html, "method": "test", "error": ""},
        ):
            _merge_magalu_pdp_html(row, product_url)
        self.assertNotIn("estimated_annual_electricity_use", row)
        self.assertIn("identity_conflict", row["parse_status"])

        same_name_html = html(
            {"title": "smart-tv", "factsheet": [], "attributes": [], "offers": []},
            "<div><span>Consumo de energia em funcionamento</span><span>&lt;165W</span></div>",
        )
        row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Smart TV",
            "product_url": product_url,
        }
        with patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "TV",
                "SEDA_MAGALU_PDP_HTML_FETCH": "1",
                "SEDA_MAGALU_ZENROWS_PDP_FALLBACK": "0",
                "SEDA_MAGALU_SHIPPING_FROM_SSR_ITEM": "0",
            },
        ), patch(
            "seda.step08_detail_enrichment._fetch_magalu_next_html",
            return_value={"status_code": 200, "text": same_name_html, "method": "test", "error": ""},
        ):
            _merge_magalu_pdp_html(row, product_url)
        self.assertNotIn("estimated_annual_electricity_use", row)

    def test_magalu_dom_aliases_use_semantic_priority_and_missing_only(self):
        verified = {"retailer_sku_name": "Smart TV", "_detail_identity_verified": True}
        row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Smart TV",
            "product_line": "TV",
            "screen_size": "65 polegadas",
        }
        html = (
            "<div><span>Polegada</span><span>55</span></div>"
            "<div><span>Consumo (máximo)</span><span>130 W</span></div>"
        )
        self.assertTrue(_merge_magalu_exact_html_specs(row, html, verified))
        self.assertEqual(row["screen_size"], "65 polegadas")
        self.assertEqual(row["estimated_annual_electricity_use"], "130 W")

        for dom, expected in (
            (
                "<div><span>Capacidade do Freezer</span><span>84</span></div>"
                "<div><span>Capacidade do Refrigerador</span><span>305</span></div>"
                "<div><span>Capacidade total</span><span>389</span></div>",
                "389",
            ),
            (
                "<div><span>Capacidade do Freezer</span><span>84</span></div>"
                "<div><span>Capacidade do Refrigerador</span><span>305</span></div>",
                "305",
            ),
            ("<div><span>Capacidade do Freezer</span><span>84</span></div>", "84"),
        ):
            ref_row = {
                "retailer": "Magalu",
                "retailer_sku_name": "Geladeira",
                "product_line": "REF",
            }
            ref_detail = {"retailer_sku_name": "Geladeira", "_detail_identity_verified": True}
            self.assertTrue(_merge_magalu_exact_html_specs(ref_row, dom, ref_detail))
            self.assertEqual(ref_row["ref_capacity"], expected)

        ldy_row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Lavadora automatica",
            "product_line": "LDY",
        }
        ldy_html = (
            "<div><span>Capacidade de lavagem</span><span>13 kg</span></div>"
            "<div><span>Abertura da Tampa</span><span>Superior</span></div>"
            "<div><span>Tipo</span><span>Front Loading automática</span></div>"
        )
        self.assertTrue(
            _merge_magalu_exact_html_specs(
                ldy_row,
                ldy_html,
                {"retailer_sku_name": "Lavadora automatica", "_detail_identity_verified": True},
            )
        )
        self.assertEqual(ldy_row["ldy_capacity"], "13 kg")
        self.assertEqual(ldy_row["ldy_loading_type"], "Top load,Front load")

    def test_dom_fallback_stops_at_general_label_like_sibling(self):
        verified = {"retailer_sku_name": "Geladeira", "_detail_identity_verified": True}
        row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Geladeira",
            "product_line": "REF",
        }
        html = "<div><span>Capacidade</span><span>Modelo: 305X</span></div>"
        self.assertFalse(_merge_magalu_exact_html_specs(row, html, verified))
        self.assertNotIn("ref_capacity", row)

        row.pop("ref_capacity", None)
        html = "<div><span>Capacidade</span><span>Modelo 305X</span></div>"
        self.assertFalse(_merge_magalu_exact_html_specs(row, html, verified))
        self.assertNotIn("ref_capacity", row)

        tv_row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Smart TV",
            "product_line": "TV",
        }
        tv_detail = {"retailer_sku_name": "Smart TV", "_detail_identity_verified": True}
        html = (
            "<div><span>Consumo Aproximado de Energia</span>"
            "<span>Potência de áudio: 130W</span></div>"
        )
        self.assertFalse(_merge_magalu_exact_html_specs(tv_row, html, tv_detail))
        self.assertNotIn("estimated_annual_electricity_use", tv_row)

    def test_dom_inline_values_stop_at_next_general_label(self):
        cases = (
            (
                "REF",
                "Geladeira",
                "<div>Capacidade:305L Modelo:ABC</div>",
                "ref_capacity",
                "305L",
            ),
            (
                "REF",
                "Geladeira",
                "<div>Capacidade:305L, modelo:ABC</div>",
                "ref_capacity",
                "305L",
            ),
            (
                "REF",
                "Geladeira",
                "<div>Capacidade total:389L Cor:Branco</div>",
                "ref_capacity",
                "389L",
            ),
            (
                "TV",
                "Smart TV",
                "<div>Tamanho da Tela:55 polegadas Resolução:4K</div>",
                "screen_size",
                "55 polegadas",
            ),
            (
                "TV",
                "Smart TV",
                "<div>Consumo de energia em funcionamento:&lt;165W Modelo:X</div>",
                "estimated_annual_electricity_use",
                "<165W",
            ),
            (
                "TV",
                "Smart TV",
                "<div>Consumo:130W, Standby:&lt;0.5W</div>",
                "estimated_annual_electricity_use",
                "130W, Standby:<0.5W",
            ),
            (
                "TV",
                "Smart TV",
                "<div>Consumo:130W, Consumo em standby:&lt;0.5W</div>",
                "estimated_annual_electricity_use",
                "130W, Consumo em standby:<0.5W",
            ),
        )
        for line, title, html, field, expected in cases:
            with self.subTest(field=field, expected=expected):
                row = {
                    "retailer": "Magalu",
                    "retailer_sku_name": title,
                    "product_line": line,
                }
                detail = {"retailer_sku_name": title, "_detail_identity_verified": True}
                self.assertTrue(_merge_magalu_exact_html_specs(row, html, detail))
                self.assertEqual(row[field], expected)

    def test_casas_dom_aliases_are_parsed_behind_fixed_identity(self):
        def detail(line, name, dom):
            product = {"id": "product", "name": name, "sku": {"id": "123"}, "specGroups": []}
            payload = {"props": {"pageProps": {"product": product}}}
            html = (
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload)
                + "</script>"
                + dom
            )
            with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": line}):
                return parse_detail(
                    html,
                    "Casas Bahia",
                    "https://www.casasbahia.com.br",
                    "https://www.casasbahia.com.br/produto/p/123",
                )

        tv = detail(
            "TV",
            "Smart TV",
            "<div><span>Consumo (máximo)</span><span>130 W</span></div>",
        )
        self.assertIs(tv["_detail_identity_verified"], True)
        self.assertEqual(tv["estimated_annual_electricity_use"], "130 W")

        ref = detail(
            "REF",
            "Geladeira",
            "<div><span>Capacidade total líquida</span><span>389 L</span></div>",
        )
        self.assertEqual(ref["ref_capacity"], "389 L")

        ldy = detail(
            "LDY",
            "Lavadora automatica",
            "<div><span>Capacidade -kg-</span><span>14</span></div>"
            "<div><span>Tipo</span><span>Front Loading automática</span></div>",
        )
        self.assertEqual(ldy["ldy_capacity"], "14")
        self.assertEqual(ldy["ldy_loading_type"], "Front load")

    def test_energy_suffix_trimming_preserves_compound_consumption(self):
        cases = (
            ("36W Entradas:3xHDMI", "36W"),
            ("4,65 kWh/mês Memória interna:2GB", "4,65 kWh/mês"),
            ("135W Código:ABC", "135W"),
            ("0,19 kWh/ciclo, Motor Inverter", "0,19 kWh/ciclo"),
            ("0,39 kWh/ciclo Sistema Eco", "0,39 kWh/ciclo"),
            (
                "115 W, Sensor Ecológico, Desligamento Automático, Economia de energia automática.",
                "115 W",
            ),
            ("65W em uso e menos de 0,5W em standby", "65W em uso e menos de 0,5W em standby"),
            ("130W (Pico) / <0.5W (Standby)", "130W (Pico) / <0.5W (Standby)"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                magalu = extract_magalu_fields(
                    {
                        "title": "Smart TV",
                        "factsheet": [{"keyName": "Consumo Aproximado de Energia", "value": raw}],
                    },
                    "TV",
                )
                casas = extract_casas_fields(
                    {"consumo de energia": [raw]},
                    "Smart TV",
                    "",
                    "TV",
                )
                self.assertEqual(magalu["estimated_annual_electricity_use"], expected)
                self.assertEqual(casas["estimated_annual_electricity_use"], expected)

        contaminated = "Classe A Motor: 670W"
        self.assertEqual(
            extract_magalu_fields(
                {
                    "title": "Smart TV",
                    "factsheet": [
                        {"keyName": "Consumo Aproximado de Energia", "value": contaminated}
                    ],
                },
                "TV",
            )["estimated_annual_electricity_use"],
            "",
        )

    def test_energy_watts_and_terminal_decimal_contracts(self):
        magalu = extract_magalu_fields(
            {
                "title": "Smart TV",
                "factsheet": [{"keyName": "Consumo maximo", "value": "300 watts"}],
            },
            "TV",
        )
        casas = extract_casas_fields(
            {"consumo maximo": ["300 watts"]},
            "Smart TV",
            "",
            "TV",
        )
        self.assertEqual(magalu["estimated_annual_electricity_use"], "300 watts")
        self.assertEqual(casas["estimated_annual_electricity_use"], "300 watts")

        for raw in ("Potência de áudio 130W", "Potencia de audio 130 watts"):
            with self.subTest(raw=raw):
                magalu = extract_magalu_fields(
                    {
                        "title": "Smart TV",
                        "factsheet": [
                            {"keyName": "Consumo Aproximado de Energia", "value": raw}
                        ],
                    },
                    "TV",
                )
                casas = extract_casas_fields(
                    {"consumo de energia": [raw]},
                    "Smart TV",
                    "",
                    "TV",
                )
                self.assertEqual(magalu["estimated_annual_electricity_use"], "")
                self.assertEqual(casas["estimated_annual_electricity_use"], "")

        self.assertEqual(
            extract_magalu_fields(
                {"title": "Smart TV", "description": "Potência: 750 Watts"},
                "TV",
            )["estimated_annual_electricity_use"],
            "",
        )
        self.assertEqual(
            extract_casas_fields({}, "Smart TV", "Potência: 750 Watts", "TV")[
                "estimated_annual_electricity_use"
            ],
            "",
        )

        casas_priority = extract_casas_fields(
            {},
            "Smart TV",
            "Consumo de energia: 14,4 kWh/mes; Consumo maximo: 185 watts",
            "TV",
        )
        self.assertEqual(
            casas_priority["estimated_annual_electricity_use"],
            "14,4 kWh/mes,185 watts",
        )

        repeated = (
            "Consumo de Energia - Água Fria (kWh/ciclo) (127v): 26; "
            "Consumo de Energia - Água Fria (kWh/ciclo) (220v): 27; "
            "Consumo de Energia - Água Quente (kWh/ciclo) (127v): 1,49; "
            "Consumo de Energia - Água Quente (kWh/ciclo) (220v): 1,67."
        )
        magalu = extract_magalu_fields(
            {"title": "Lavadora", "description": repeated},
            "LDY",
        )
        casas = extract_casas_fields({}, "Lavadora", repeated, "LDY")
        self.assertEqual(
            magalu["estimated_annual_electricity_use"],
            "26,27,1,49,1,67",
        )
        self.assertEqual(
            casas["estimated_annual_electricity_use"],
            "26,27,1,49,1,67",
        )

    def test_description_energy_direct_token_fallbacks_stay_bounded(self):
        cases = (
            (
                "Pot\u00eancia de Sa\u00edda de Audio: 5W + 5WTipo de Auto Falante: "
                "2CHConsumo de Energia: 117WTens\u00e3o: AC100-240V",
                "117W",
            ),
            ("Consumo de Energia:117WTensão: Bivolt", "117W"),
            ("Consumo no modo stand by; Abaixo de 0,5W", "Abaixo de 0,5W"),
            (
                "consumo máximo de 80 W, standby de menos de 0,5 W",
                "de 80 W,menos de 0,5 W",
            ),
            ("consumo em espera <0,5 W", "<0,5 W"),
            (
                "Consumo em espera 0,5 W. Consumo/mês 5,97 kWh",
                "0,5 W,5,97 kWh",
            ),
            ("Consumo de energia 120W, Padrão de recepção: NTSC", "120W"),
            ("Consumo de energia:180W\n- Alimentação: Bivolt", "180W"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                magalu = extract_magalu_fields(
                    {"title": "Smart TV", "description": raw},
                    "TV",
                )
                casas = extract_casas_fields({}, "Smart TV", raw, "TV")
                self.assertEqual(magalu["estimated_annual_electricity_use"], expected)
                self.assertEqual(casas["estimated_annual_electricity_use"], expected)

        false_positive = "Consumo reduzido. Potência: 750 Watts"
        self.assertEqual(
            extract_magalu_fields(
                {"title": "Smart TV", "description": false_positive},
                "TV",
            )["estimated_annual_electricity_use"],
            "",
        )
        self.assertEqual(
            extract_casas_fields({}, "Smart TV", false_positive, "TV")[
                "estimated_annual_electricity_use"
            ],
            "",
        )

        for word_internal in (
            "superconsumo de Energia: 117W",
            "SUPERCONSUMO de Energia: 117W",
            "Consumo de Energia: 117Water",
            "Consumo de Energia: 15Weight",
        ):
            with self.subTest(word_internal=word_internal):
                self.assertEqual(
                    extract_magalu_fields(
                        {"title": "Smart TV", "description": word_internal},
                        "TV",
                    )["estimated_annual_electricity_use"],
                    "",
                )
                self.assertEqual(
                    extract_casas_fields({}, "Smart TV", word_internal, "TV")[
                        "estimated_annual_electricity_use"
                    ],
                    "",
                )

    def test_consumo_kwh_labeled_target_raw_acceptance_and_rejections(self):
        accepted = (
            ("54.0-54.0", "54.0"),
            ("42.9-42.9", "42.9"),
            ("24,62-24,62", "24,62"),
            ("0.36-0.36", "0.36"),
            ("0,47/0,48", "0,47/0,48"),
            (
                "110V:39,8 (kWh/mês). 220V:41,7 (kWh/mês)",
                "110V:39,8 (kWh/mês). 220V:41,7 (kWh/mês)",
            ),
            ("34,4?kWh/mês", "34,4?kWh/mês"),
            ("0,24.", "0,24."),
            ("510W", "510W"),
            ("750", "750"),
            (
                "0,32 kW/h (Ciclo Normal Água Fria)-0,32 kW/h (Ciclo Normal Água Fria)",
                "0,32 kW/h (Ciclo Normal Água Fria)",
            ),
        )
        for raw, expected in accepted:
            with self.subTest(accepted=raw):
                magalu = extract_magalu_fields(
                    {
                        "title": "Geladeira",
                        "factsheet": [{"keyName": "Consumo (kWh)", "value": raw}],
                    },
                    "REF",
                )
                casas = extract_casas_fields(
                    {"Consumo (kWh)": [raw]},
                    "Geladeira",
                    "",
                    "REF",
                )
                self.assertEqual(magalu["estimated_annual_electricity_use"], expected)
                self.assertEqual(casas["estimated_annual_electricity_use"], expected)

        for raw in (
            "143 L",
            "250 ml",
            "730RPM",
            "Superior",
            "20% economia",
            "Classe A eficiência",
            "110",
            "127V",
            "110V / 220V",
            "Bivolt",
        ):
            with self.subTest(rejected=raw):
                magalu = extract_magalu_fields(
                    {
                        "title": "Lavadora",
                        "factsheet": [{"keyName": "Consumo (kWh)", "value": raw}],
                    },
                    "LDY",
                )
                casas = extract_casas_fields(
                    {"Consumo (kWh)": [raw]},
                    "Lavadora",
                    "",
                    "LDY",
                )
                self.assertEqual(magalu["estimated_annual_electricity_use"], "")
                self.assertEqual(casas["estimated_annual_electricity_use"], "")

    def test_portuguese_accessory_title_guards_cover_singular_and_plural(self):
        actual_ldy_accessories = (
            "Painel Decorativo Lavadora Brastemp 9Kg Bivolt",
            "Agitador Batedor Lavadora Brastemp 13kg",
            "Vedação de porta de lavadora Front Loader",
        )
        ldy_accessory_tokens = (
            "painel",
            "paineis",
            "anel",
            "aneis",
            "atuador",
            "atuadores",
            "interruptor",
            "interruptores",
            "vedacao",
            "vedacoes",
            "coletor",
            "coletores",
            "desembaracador",
            "desembaracadores",
            "aparador",
            "aparadores",
            "agitador",
            "agitadores",
            "batedor",
            "batedores",
            "motor",
            "motores",
            "puxador",
            "puxadores",
        )
        ldy_titles = actual_ldy_accessories + tuple(
            f"{token} para lavadora 13kg Front Loader"
            for token in ldy_accessory_tokens
        )
        for title in ldy_titles:
            with self.subTest(line="LDY", title=title):
                magalu = extract_magalu_fields(
                    {"title": title, "factsheet": []},
                    "LDY",
                )
                casas = extract_casas_fields({}, title, "", "LDY")
                for detail in (magalu, casas):
                    self.assertEqual(detail["ldy_capacity"], "")
                    self.assertEqual(detail["ldy_loading_type"], "")

        for title in (
            "Máquina de Lavar Brastemp 13kg Front Loading",
            "Lavadora Brastemp 13kg com Motor Inverter Front Loading",
        ):
            with self.subTest(line="LDY", normal_title=title):
                magalu = extract_magalu_fields(
                    {"title": title, "factsheet": []},
                    "LDY",
                )
                casas = extract_casas_fields({}, title, "", "LDY")
                for detail in (magalu, casas):
                    self.assertEqual(detail["ldy_capacity"], "13kg")
                    self.assertEqual(detail["ldy_loading_type"], "Front load")

        tv_accessory_tokens = (
            "painel",
            "paineis",
            "pedestal",
            "pedestais",
            "conversor",
            "conversores",
            "adaptador",
            "adaptadores",
        )
        for token in tv_accessory_tokens:
            title = f"{token} para TV 55 polegadas"
            with self.subTest(line="TV", title=title):
                magalu = extract_magalu_fields(
                    {
                        "title": title,
                        "factsheet": [
                            {"keyName": "Consumo de energia", "value": "130W"}
                        ],
                    },
                    "TV",
                )
                casas = extract_casas_fields(
                    {"Consumo de energia": ["130W"]},
                    title,
                    "",
                    "TV",
                )
                self.assertEqual(magalu["screen_size"], "")
                self.assertEqual(magalu["estimated_annual_electricity_use"], "")
                self.assertEqual(casas["estimated_annual_electricity_use"], "")

        normal_tv = "Smart TV 55 polegadas com pedestal"
        self.assertEqual(
            extract_magalu_fields(
                {
                    "title": normal_tv,
                    "factsheet": [
                        {"keyName": "Consumo de energia", "value": "130W"}
                    ],
                },
                "TV",
            )["estimated_annual_electricity_use"],
            "130W",
        )
        self.assertEqual(
            extract_casas_fields(
                {"Consumo de energia": ["130W"]},
                normal_tv,
                "",
                "TV",
            )["estimated_annual_electricity_use"],
            "130W",
        )

        ref_accessory_tokens = (
            "sensor",
            "sensores",
            "organizador",
            "organizadores",
            "motor",
            "motores",
            "restaurador",
            "restauradores",
            "compressor",
            "compressores",
            "puxador",
            "puxadores",
        )
        for token in ref_accessory_tokens:
            title = f"{token} para geladeira 305L"
            with self.subTest(line="REF", title=title):
                magalu = extract_magalu_fields(
                    {
                        "title": title,
                        "factsheet": [
                            {"keyName": "Capacidade total", "value": "305L"}
                        ],
                    },
                    "REF",
                )
                casas = extract_casas_fields(
                    {"Capacidade total": ["305L"]},
                    title,
                    "",
                    "REF",
                )
                self.assertEqual(magalu["ref_capacity"], "")
                self.assertEqual(casas["ref_capacity"], "")

        normal_ref = "Geladeira 305L com sensor de temperatura"
        self.assertEqual(
            extract_magalu_fields(
                {
                    "title": normal_ref,
                    "factsheet": [{"keyName": "Capacidade total", "value": "305L"}],
                },
                "REF",
            )["ref_capacity"],
            "305L",
        )
        self.assertEqual(
            extract_casas_fields(
                {"Capacidade total": ["305L"]},
                normal_ref,
                "",
                "REF",
            )["ref_capacity"],
            "305L",
        )

    def test_requested_capacity_targets_extract_exact_values(self):
        magalu_ref_cases = (
            ("Capacidade (L)", "290 L"),
            ("Capacidade", "26,9 litros"),
            ("Capacidade total de", "30 litros"),
        )
        for label, raw in magalu_ref_cases:
            with self.subTest(retailer="Magalu", line="REF", label=label):
                detail = extract_magalu_fields(
                    {
                        "title": "Geladeira",
                        "factsheet": [{"keyName": label, "value": raw}],
                    },
                    "REF",
                )
                self.assertEqual(detail["ref_capacity"], raw)

        with self.subTest(retailer="Magalu", line="LDY", label="Capacidade"):
            detail = extract_magalu_fields(
                {
                    "title": "Lavadora",
                    "factsheet": [{"keyName": "Capacidade", "value": "13 kg"}],
                },
                "LDY",
            )
            self.assertEqual(detail["ldy_capacity"], "13 kg")

        casas_ref_cases = (
            ("Capacidade de armazenagem total (L)", "490 litros"),
            ("CAPACIDADE", "332 L"),
        )
        for label, raw in casas_ref_cases:
            with self.subTest(retailer="Casas Bahia", line="REF", label=label):
                detail = extract_casas_fields(
                    {label: [raw]},
                    "Geladeira",
                    "",
                    "REF",
                )
                self.assertEqual(detail["ref_capacity"], raw)

        casas_ldy_cases = (
            ("Capacidade total", "15"),
            ("Capacidade -kg-", "14"),
        )
        for label, raw in casas_ldy_cases:
            with self.subTest(retailer="Casas Bahia", line="LDY", label=label):
                detail = extract_casas_fields(
                    {label: [raw]},
                    "Lavadora",
                    "",
                    "LDY",
                )
                self.assertEqual(detail["ldy_capacity"], raw)

    def test_requested_loading_targets_extract_exact_values(self):
        cases = (
            ("Acesso ao cesto", "Superior", "Top load"),
            ("Abertura da Tampa", "Superior", "Top load"),
            ("Tipo", "Front Loading automatica", "Front load"),
        )
        for label, raw, expected in cases:
            with self.subTest(label=label, raw=raw):
                detail = extract_casas_fields(
                    {label: [raw]},
                    "Lavadora",
                    "",
                    "LDY",
                )
                self.assertEqual(detail["ldy_loading_type"], expected)

    def test_loading_direction_exact_off_label_fallback(self):
        off_label_cases = (
            ("Consumo de água", "Superior", "Top load"),
            ("Capacidade (kg de roupas)", "Superior", "Top load"),
            ("Consumo (kWh)", "Superior", "Top load"),
            ("Velocidade de centrifugação (rpm)", "Frontal", "Front load"),
            ("Tipo", "Front Loading", "Front load"),
            ("Abertura da Tampa", "Carga Superior", "Top load"),
        )
        for label, raw, expected in off_label_cases:
            with self.subTest(label=label, raw=raw):
                magalu = extract_magalu_fields(
                    {
                        "title": "Lavadora 13kg",
                        "factsheet": [{"keyName": label, "value": raw}],
                    },
                    "LDY",
                )
                casas = extract_casas_fields(
                    {label: [raw]},
                    "Lavadora 13kg",
                    "",
                    "LDY",
                )
                self.assertEqual(magalu["ldy_loading_type"], expected)
                self.assertEqual(casas["ldy_loading_type"], expected)

        installation = "Visão Frontal: Superior Direito, Superior Esquerdo"
        self.assertEqual(
            extract_magalu_fields(
                {
                    "title": "Lavadora 13kg",
                    "factsheet": [{"keyName": "Instalação", "value": installation}],
                },
                "LDY",
            )["ldy_loading_type"],
            "",
        )
        self.assertEqual(
            extract_casas_fields(
                {"Instalação": [installation]},
                "Lavadora 13kg",
                "",
                "LDY",
            )["ldy_loading_type"],
            "",
        )

    def test_ref_compartment_storage_labels_with_l_suffix(self):
        magalu = extract_magalu_fields(
            {
                "title": "Geladeira",
                "factsheet": [
                    {
                        "keyName": "Capacidade de armazenagem do Freezer (L)",
                        "value": "84",
                    },
                    {
                        "keyName": "Capacidade de armazenagem do Refrigerador (L)",
                        "value": "305",
                    },
                ],
            },
            "REF",
        )
        casas = extract_casas_fields(
            {
                "Capacidade de armazenagem do Freezer (L)": ["84"],
                "Capacidade de armazenagem do Refrigerador (L)": ["305"],
            },
            "Geladeira",
            "",
            "REF",
        )
        self.assertEqual(magalu["ref_capacity"], "305")
        self.assertEqual(casas["ref_capacity"], "305")

        magalu = extract_magalu_fields(
            {
                "title": "Freezer vertical",
                "factsheet": [
                    {
                        "keyName": "Capacidade de armazenagem do Congelador (L)",
                        "value": "84",
                    }
                ],
            },
            "REF",
        )
        casas = extract_casas_fields(
            {"Capacidade de armazenagem do Congelador (L)": ["84"]},
            "Freezer vertical",
            "",
            "REF",
        )
        self.assertEqual(magalu["ref_capacity"], "84")
        self.assertEqual(casas["ref_capacity"], "84")

    def test_ref_capacity_standalone_ml_survives_title_and_description(self):
        self.assertEqual(
            extract_ref_capacity_from_title("Mini Geladeira compacta 1040 ml"),
            "1040 ml",
        )
        magalu = extract_magalu_fields(
            {"title": "Mini Geladeira", "description": "Capacidade: 1040 ml"},
            "REF",
        )
        casas = extract_casas_fields(
            {},
            "Mini Geladeira",
            "Capacidade: 1040 ml",
            "REF",
        )
        self.assertEqual(magalu["ref_capacity"], "1040 ml")
        self.assertEqual(casas["ref_capacity"], "1040 ml")
        self.assertEqual(
            extract_magalu_fields(
                {"title": "Lavadora automatica", "description": "Capacidade: 1040 ml"},
                "LDY",
            )["ldy_capacity"],
            "",
        )

    def test_capacity_qualifiers_survive_ref_title_and_description(self):
        raw_values = (
            "acima de 400 litros",
            "abaixo de 400 litros",
            "até 400 litros",
            "aprox. 400 litros",
            "aproximadamente 400 litros",
            "cerca de 400 litros",
            "menos de 400 litros",
            "mais de 400 litros",
            "<400 litros",
            ">400 litros",
            "~400 litros",
        )
        for raw in raw_values:
            with self.subTest(raw=raw):
                self.assertEqual(
                    extract_ref_capacity_from_title(f"Mini Geladeira {raw}"),
                    raw,
                )
                self.assertEqual(
                    extract_magalu_fields(
                        {"title": "Geladeira", "description": f"Capacidade: {raw}"},
                        "REF",
                    )["ref_capacity"],
                    raw,
                )
                self.assertEqual(
                    extract_casas_fields(
                        {},
                        "Geladeira",
                        f"Capacidade: {raw}",
                        "REF",
                    )["ref_capacity"],
                    raw,
                )

    def test_capacity_qualifiers_survive_ldy_title_and_description(self):
        for raw in ("aprox. 14kg", "cerca de 14kg", "acima de 14kg", "<14kg", "~14kg"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    extract_ldy_capacity_from_title(f"Lavadora automatica {raw}"),
                    raw,
                )
                self.assertEqual(
                    extract_magalu_fields(
                        {
                            "title": "Lavadora automatica",
                            "description": f"Capacidade de lavagem: {raw}",
                        },
                        "LDY",
                    )["ldy_capacity"],
                    raw,
                )
                self.assertEqual(
                    extract_casas_fields(
                        {},
                        "Lavadora automatica",
                        f"Capacidade de lavagem: {raw}",
                        "LDY",
                    )["ldy_capacity"],
                    raw,
                )

    def test_magalu_nested_capacity_children_do_not_become_parent_total(self):
        def capacity(children):
            item = {
                "title": "Geladeira",
                "factsheet": [{"keyName": "Capacidades", "elements": children}],
            }
            return extract_magalu_fields(item, "REF")["ref_capacity"]

        self.assertEqual(
            capacity(
                [
                    {"keyName": "Capacidade do Freezer", "value": "84"},
                    {"keyName": "Capacidade do Refrigerador", "value": "305"},
                    {"keyName": "Capacidade total", "value": "389"},
                ]
            ),
            "389",
        )
        self.assertEqual(
            capacity(
                [
                    {"keyName": "Capacidade do Freezer", "value": "84"},
                    {"keyName": "Capacidade do Refrigerador", "value": "305"},
                ]
            ),
            "305",
        )
        self.assertEqual(
            capacity([{"keyName": "Capacidade do Freezer", "value": "84"}]),
            "84",
        )

    def test_magalu_pdp_needs_specs_is_product_line_specific(self):
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            self.assertEqual(
                _relevant_audited_fields({}),
                ("screen_size", "estimated_annual_electricity_use"),
            )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
            self.assertEqual(
                _relevant_audited_fields({}),
                ("estimated_annual_electricity_use", "ref_capacity"),
            )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            self.assertEqual(
                _relevant_audited_fields({}),
                ("estimated_annual_electricity_use", "ldy_capacity", "ldy_loading_type"),
            )

        row = {
            "retailer": "Magalu",
            "product_line": "REF",
            "product_url": "https://example/p/ref",
            "screen_size": "",
            "estimated_annual_electricity_use": "130W",
            "ref_capacity": "305L",
            "summarized_review_content": "summary",
            "retailer_sku_name_similar": "similar",
            "star_rating": "0",
            "count_of_star_ratings": "0",
            "count_of_reviews": "0",
        }
        with patch("seda.step08_detail_enrichment._fetch_magalu_next_html") as fetch:
            _merge_magalu_pdp_html(row, row["product_url"])
            fetch.assert_not_called()

        row["ref_capacity"] = ""
        with patch(
            "seda.step08_detail_enrichment._fetch_magalu_next_html",
            return_value={"status_code": 500, "text": "", "method": "test", "error": "failed"},
        ) as fetch:
            _merge_magalu_pdp_html(row, row["product_url"])
            fetch.assert_called_once()

    def test_parallel_part_contract_checks_count_and_identity(self):
        expected = [
            {
                **dict.fromkeys(OUTPUT_COLUMNS, ""),
                "retailer": "Magalu",
                "product_url": "https://example/p/a",
            },
            {
                **dict.fromkeys(OUTPUT_COLUMNS, ""),
                "retailer": "Magalu",
                "product_url": "https://example/p/b",
            },
        ]
        self.assertEqual(_parallel_part_error(expected, list(expected)), "")
        self.assertIn("row_count", _parallel_part_error(expected, expected[:1]))
        self.assertIn("identity_at:0", _parallel_part_error(expected, list(reversed(expected))))
        truncated = [
            {"retailer": row["retailer"], "product_url": row["product_url"]}
            for row in expected
        ]
        self.assertIn("missing_columns", _parallel_part_error(expected, truncated))
        short = [dict(row) for row in expected]
        short[0]["screen_size"] = None
        self.assertIn("short_row:row=1", _parallel_part_error(expected, short))
        wide = [dict(row) for row in expected]
        wide[0][None] = ["extra"]
        self.assertIn("extra_values_without_header:row=1", _parallel_part_error(expected, wide))

    def test_parallel_success_preserves_order_and_removes_parts(self):
        rows = [
            {"retailer": "Magalu", "item": str(index), "product_url": f"https://example/p/{index}"}
            for index in range(5)
        ]

        class SuccessfulProcess:
            def wait(self):
                return 0

        def fake_popen(_command, env):
            start = int(env["SEDA_DETAIL_SKIP"])
            end = int(env["SEDA_DETAIL_LIMIT"])
            write_csv(env["SEDA_DETAIL_OUTPUT_CSV"], rows[start:end])
            return SuccessfulProcess()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "final_output_enriched.csv"
            with patch.dict(os.environ, {"SEDA_MAGALU_DETAIL_WORKER_STAGGER_SECONDS": "0"}), patch(
                "seda.step08_detail_enrichment.subprocess.Popen",
                side_effect=fake_popen,
            ):
                _run_parallel(2, rows, str(output))
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                saved = list(csv.DictReader(handle))
            self.assertEqual([row["product_url"] for row in saved], [row["product_url"] for row in rows])
            self.assertEqual(list(Path(directory).glob("_detail_part_*.csv")), [])

    def test_parallel_failure_does_not_replace_existing_output(self):
        rows = [{"retailer": "Magalu", "item": "new", "product_url": "https://example/p/new"}]

        class FailedProcess:
            def wait(self):
                return 7

        def fake_popen(_command, env):
            write_csv(env["SEDA_DETAIL_OUTPUT_CSV"], rows)
            return FailedProcess()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "final_output_enriched.csv"
            write_csv(output, [{"retailer": "Magalu", "item": "old", "product_url": "https://example/p/old"}])
            with patch.dict(os.environ, {"SEDA_MAGALU_DETAIL_WORKER_STAGGER_SECONDS": "0"}), patch(
                "seda.step08_detail_enrichment.subprocess.Popen",
                side_effect=fake_popen,
            ):
                with self.assertRaisesRegex(RuntimeError, "detail_parallel_worker_failed"):
                    _run_parallel(1, rows, str(output))
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                saved = list(csv.DictReader(handle))
            self.assertEqual(saved[0]["item"], "old")

    def test_parallel_partial_success_is_rejected_without_replacing_output(self):
        rows = [
            {"retailer": "Magalu", "item": "1", "product_url": "https://example/p/1"},
            {"retailer": "Magalu", "item": "2", "product_url": "https://example/p/2"},
        ]

        class SuccessfulProcess:
            def wait(self):
                return 0

        def fake_popen(_command, env):
            write_csv(env["SEDA_DETAIL_OUTPUT_CSV"], rows[:1])
            return SuccessfulProcess()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "final_output_enriched.csv"
            write_csv(output, [{"retailer": "Magalu", "item": "old", "product_url": "https://example/p/old"}])
            with patch.dict(os.environ, {"SEDA_MAGALU_DETAIL_WORKER_STAGGER_SECONDS": "0"}), patch(
                "seda.step08_detail_enrichment.subprocess.Popen",
                side_effect=fake_popen,
            ):
                with self.assertRaisesRegex(RuntimeError, "detail_parallel_invalid_output"):
                    _run_parallel(1, rows, str(output))
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                saved = list(csv.DictReader(handle))
            self.assertEqual(saved[0]["item"], "old")

    def test_worker_resume_does_not_reuse_stale_part_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "part.csv"
            expected = [{"retailer": "Magalu", "product_url": "https://example/p/expected"}]
            write_csv(output, [{"retailer": "Magalu", "product_url": "https://example/p/stale"}])
            self.assertEqual(_resume_prefix(str(output), 1, is_worker=True, expected_rows=expected), [])
            with self.assertRaisesRegex(RuntimeError, "identity_at:0"):
                _resume_prefix(str(output), 1, is_worker=False, expected_rows=expected)
            write_csv(output, expected)
            self.assertEqual(
                len(_resume_prefix(str(output), 1, is_worker=False, expected_rows=expected)),
                1,
            )

    def test_serial_resume_requires_existing_complete_matching_prefix(self):
        expected = [
            {"retailer": "Magalu", "product_url": "https://example/p/a"},
            {"retailer": "Magalu", "product_url": "https://example/p/b"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "resume.csv"
            with self.assertRaisesRegex(RuntimeError, "missing_output"):
                _resume_prefix(str(output), 2, is_worker=False, expected_rows=expected)

            write_csv(output, expected[:1])
            with self.assertRaisesRegex(RuntimeError, "row_count:1!=2"):
                _resume_prefix(str(output), 2, is_worker=False, expected_rows=expected)

            write_csv(output, list(reversed(expected)))
            with self.assertRaisesRegex(RuntimeError, "identity_at:0"):
                _resume_prefix(str(output), 2, is_worker=False, expected_rows=expected)

            write_csv(output, expected, columns=["retailer", "product_url"])
            with self.assertRaisesRegex(RuntimeError, "missing_columns"):
                _resume_prefix(str(output), 2, is_worker=False, expected_rows=expected)

            write_csv(output, expected)
            resumed = _resume_prefix(str(output), 2, is_worker=False, expected_rows=expected)
            self.assertEqual(
                [row["product_url"] for row in resumed],
                [row["product_url"] for row in expected],
            )

    def test_serial_resume_rejects_duplicate_header(self):
        expected = [
            {"retailer": "Magalu", "product_url": "https://example/p/a"}
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "resume.csv"
            fieldnames = OUTPUT_COLUMNS + ["screen_size"]
            values = [expected[0].get(column, "") for column in OUTPUT_COLUMNS] + [""]
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(fieldnames)
                writer.writerow(values)
            with self.assertRaisesRegex(RuntimeError, "duplicate_columns:screen_size"):
                _resume_prefix(
                    str(output),
                    1,
                    is_worker=False,
                    expected_rows=expected,
                )

    def test_serial_resume_rejects_checkpoint_older_than_targets(self):
        expected = [
            {
                "retailer": "Magalu",
                "product_url": "https://example/p/a",
                "final_sku_price": "NEW",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "targets.csv"
            output = Path(directory) / "resume.csv"
            write_csv(target, expected)
            write_csv(output, [{**expected[0], "final_sku_price": "OLD"}])
            os.utime(output, ns=(1_000_000_000, 1_000_000_000))
            os.utime(target, ns=(2_000_000_000, 2_000_000_000))
            with self.assertRaisesRegex(RuntimeError, "older_than_target"):
                _resume_prefix(
                    str(output),
                    1,
                    is_worker=False,
                    expected_rows=expected,
                    target_path=str(target),
                )

    def test_parallel_worker_raw_filename_has_worker_prefix(self):
        row = {"sku": "ABC/123"}
        with patch.dict(os.environ, {"SEDA_DETAIL_WORKER_ID": "0"}, clear=True):
            self.assertEqual(_detail_raw_filename(row, 1), "w0_0001_ABC_123.html")
        with patch.dict(
            os.environ,
            {"SEDA_DETAIL_WORKER_ID": "0", "SEDA_DETAIL_RUN_TOKEN": "123_456"},
            clear=True,
        ):
            self.assertEqual(_detail_raw_filename(row, 1), "r123_456_w0_0001_ABC_123.html")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_detail_raw_filename(row, 1), "0001_ABC_123.html")

    def test_final_source_uses_newest_normal_artifact_and_honors_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            badged = output_dir / "final_output_badged.csv"
            enriched = output_dir / "final_output_enriched.csv"
            override = output_dir / "manual.csv"
            targets = output_dir / "seda_final_targets.csv"
            identity_row = {"retailer": "Magalu", "product_url": "https://example/p/same"}
            write_csv(targets, [identity_row])
            write_csv(badged, [{**identity_row, "parse_status": "old"}])
            write_csv(enriched, [{**identity_row, "parse_status": "new"}])
            write_csv(override, [{"retailer": "Magalu", "product_url": "https://example/p/manual"}])
            os.utime(targets, ns=(100_000_000, 100_000_000))
            os.utime(badged, ns=(1_000_000_000, 1_000_000_000))
            os.utime(enriched, ns=(2_000_000_000, 2_000_000_000))
            os.utime(override, ns=(500_000_000, 500_000_000))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SEDA_FINAL_SOURCE_CSV", None)
                self.assertEqual(_source_path(root), enriched)
            with patch.dict(os.environ, {"SEDA_FINAL_SOURCE_CSV": str(override)}):
                self.assertEqual(_source_path(root), override)

    def test_final_source_rejects_newer_partial_candidate_and_fails_if_all_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            targets = output_dir / "seda_final_targets.csv"
            badged = output_dir / "final_output_badged.csv"
            enriched = output_dir / "final_output_enriched.csv"
            rows = [
                {"retailer": "Magalu", "product_url": "https://example/p/1"},
                {"retailer": "Magalu", "product_url": "https://example/p/2"},
            ]
            write_csv(targets, rows)
            write_csv(badged, rows)
            write_csv(enriched, rows[:1])
            os.utime(targets, ns=(500_000_000, 500_000_000))
            os.utime(badged, ns=(1_000_000_000, 1_000_000_000))
            os.utime(enriched, ns=(2_000_000_000, 2_000_000_000))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SEDA_FINAL_SOURCE_CSV", None)
                self.assertEqual(_source_path(root), badged)

            write_csv(enriched, rows, columns=["retailer", "product_url"])
            os.utime(enriched, ns=(3_000_000_000, 3_000_000_000))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SEDA_FINAL_SOURCE_CSV", None)
                self.assertEqual(_source_path(root), badged)

            write_csv(badged, list(reversed(rows)))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SEDA_FINAL_SOURCE_CSV", None)
                with self.assertRaisesRegex(RuntimeError, "final_source_no_complete_candidate"):
                    _source_path(root)

        self.assertIn("row_count", _source_completeness_error(rows, rows[:1]))
        self.assertIn("identity_at:0", _source_completeness_error(rows, list(reversed(rows))))
        self.assertIn("missing_columns", _source_completeness_error(rows, rows))

    def test_final_source_skips_newer_duplicate_header_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            targets = output_dir / "seda_final_targets.csv"
            badged = output_dir / "final_output_badged.csv"
            enriched = output_dir / "final_output_enriched.csv"
            rows = [{"retailer": "Magalu", "product_url": "https://example/p/1"}]
            write_csv(targets, rows)
            write_csv(badged, rows)
            output_dir.mkdir(parents=True, exist_ok=True)
            duplicate_columns = OUTPUT_COLUMNS + ["screen_size"]
            duplicate_values = [rows[0].get(column, "") for column in OUTPUT_COLUMNS] + [""]
            with enriched.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(duplicate_columns)
                writer.writerow(duplicate_values)
            os.utime(targets, ns=(500_000_000, 500_000_000))
            os.utime(badged, ns=(1_000_000_000, 1_000_000_000))
            os.utime(enriched, ns=(2_000_000_000, 2_000_000_000))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SEDA_FINAL_SOURCE_CSV", None)
                self.assertEqual(_source_path(root), badged)

    def test_final_source_rejects_candidate_older_than_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            targets = output_dir / "seda_final_targets.csv"
            enriched = output_dir / "final_output_enriched.csv"
            rows = [
                {
                    "retailer": "Magalu",
                    "product_url": "https://example/p/1",
                    "final_sku_price": "NEW",
                }
            ]
            write_csv(targets, rows)
            write_csv(enriched, [{**rows[0], "final_sku_price": "OLD"}])
            os.utime(enriched, ns=(1_000_000_000, 1_000_000_000))
            os.utime(targets, ns=(2_000_000_000, 2_000_000_000))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SEDA_FINAL_SOURCE_CSV", None)
                with self.assertRaisesRegex(RuntimeError, "older_than_targets"):
                    _source_path(root)

    def test_final_source_schema_rejects_truncated_and_malformed_rows(self):
        complete = dict.fromkeys(OUTPUT_COLUMNS, "")
        complete.update(
            {
                "retailer": "Magalu",
                "product_line": "TV",
                "product_url": "https://example/p/1",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "missing_columns"):
            _validate_internal_source_schema(
                [{"retailer": "Magalu", "product_line": "TV"}],
                "override.csv",
            )
        short = dict(complete)
        short["screen_size"] = None
        with self.assertRaisesRegex(RuntimeError, "short_row:row=1"):
            _validate_internal_source_schema([short], "override.csv")
        wide = dict(complete)
        wide[None] = ["extra"]
        with self.assertRaisesRegex(RuntimeError, "extra_values_without_header:row=1"):
            _validate_internal_source_schema([wide], "override.csv")

    def test_final_source_context_mismatch_fails_before_formatting(self):
        rows = [
            {
                "product_line": "REF",
                "retailer": "Casas Bahia",
                "product_url": "https://www.casasbahia.com.br/p/1",
            }
        ]
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"}):
            with self.assertRaisesRegex(RuntimeError, "product_line_mismatch"):
                _validate_source_context(rows, "source.csv")

        rows[0]["product_line"] = "TV"
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"}):
            with self.assertRaisesRegex(RuntimeError, "retailer_mismatch"):
                _validate_source_context(rows, "source.csv")

    def test_final_output_retailer_aliases_use_canonical_formatting_rules(self):
        cases = (
            (
                "magazineluiza",
                "https://www.magazineluiza.com.br/p/abc",
                "Magalu",
            ),
            (
                "casas_bahia",
                "https://www.casasbahia.com.br/p/123",
                "CasasBahia",
            ),
            (
                "casasbahiacombr",
                "https://www.casasbahia.com.br/p/456",
                "CasasBahia",
            ),
        )
        for retailer, product_url, expected_account in cases:
            with self.subTest(retailer=retailer):
                row = {
                    "product_line": "TV",
                    "retailer": retailer,
                    "product_url": product_url,
                    "sku": "SHOULD_BE_BLANK",
                }
                with patch.dict(
                    os.environ,
                    {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": retailer},
                ):
                    _validate_source_context([row], "source.csv")
                    formatted = _format_row(row, datetime(2026, 7, 18, 12, 0, 0))
                self.assertEqual(formatted["account_name"], expected_account)
                if expected_account == "Magalu":
                    self.assertTrue(formatted["batch_id"].startswith("m_"))
                else:
                    self.assertEqual(formatted["sku"], "")

    def test_final_source_context_rejects_empty_missing_and_unknown_values(self):
        env = {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"}
        with patch.dict(os.environ, env):
            with self.assertRaisesRegex(RuntimeError, "final_source_empty"):
                _validate_source_context([], "source.csv")
            with self.assertRaisesRegex(RuntimeError, "product_line_missing"):
                _validate_source_context([{"retailer": "Magalu"}], "source.csv")
            with self.assertRaisesRegex(RuntimeError, "product_line_unknown"):
                _validate_source_context([{"product_line": "OTHER", "retailer": "Magalu"}], "source.csv")
            with self.assertRaisesRegex(RuntimeError, "retailer_missing"):
                _validate_source_context([{"product_line": "TV"}], "source.csv")
            with self.assertRaisesRegex(RuntimeError, "retailer_unknown"):
                _validate_source_context([{"product_line": "TV", "retailer": "Unknown"}], "source.csv")

        with patch.dict(os.environ, {"SEDA_ACTIVE_RETAILER": "magalu"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "env_product_line"):
                _validate_source_context([{"product_line": "TV", "retailer": "Magalu"}], "source.csv")
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "env_retailer"):
                _validate_source_context([{"product_line": "TV", "retailer": "Magalu"}], "source.csv")

    def test_step14_direct_load_validates_source_before_database_import(self):
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"}), patch(
            "seda.step14_db_load.read_csv",
            return_value=[],
        ):
            with self.assertRaisesRegex(RuntimeError, "final_source_empty"):
                step14_db_load.main()

    def test_step14_rejects_extra_or_missing_columns_before_database_import(self):
        expected_columns = final_output_columns("TV")
        base = {column: "" for column in expected_columns}
        base.update({"product": "TV", "account_name": "Magalu"})
        cases = []
        extra = dict(base)
        extra["product_line"] = "TV"
        extra["retailer"] = "Magalu"
        cases.append((extra, "extra=.*product_line.*retailer"))
        missing = dict(base)
        missing.pop("batch_id")
        cases.append((missing, "missing=batch_id"))

        env = {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"}
        for row, error in cases:
            with self.subTest(error=error), patch.dict(os.environ, env), patch(
                "seda.step14_db_load.read_csv",
                return_value=[row],
            ), patch.dict(
                "sys.modules",
                {"psycopg2": None, "psycopg2.extras": None},
            ):
                with self.assertRaisesRegex(RuntimeError, error):
                    step14_db_load.main()


    def test_casas_verified_dom_excludes_recommendation_specs(self):
        def parsed(line, title, main_dom=""):
            product = {
                "id": "product-id",
                "name": title,
                "sku": {"id": "123"},
                "specGroups": [],
            }
            payload = {"props": {"pageProps": {"product": product}}}
            recommendation = (
                '<section><h2>Produtos recomendados</h2>'
                '<article>'
                '<div>Capacidade total</div><div>500 L</div>'
                '<div>Tamanho da Tela</div><div>99 polegadas</div>'
                '</article></section>'
            )
            html = (
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload)
                + "</script>"
                + main_dom
                + recommendation
            )
            with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": line}):
                return parse_detail(
                    html,
                    "Casas Bahia",
                    "https://www.casasbahia.com.br",
                    "https://www.casasbahia.com.br/produto/p/123",
                )

        recommendation_only = parsed("REF", "Geladeira Principal")
        self.assertIs(recommendation_only["_detail_identity_verified"], True)
        self.assertEqual(recommendation_only["ref_capacity"], "")

        with_main = parsed(
            "REF",
            "Geladeira Principal",
            '<section data-testid="product-specifications">'
            '<div>Capacidade total</div><div>305 L</div>'
            '</section>',
        )
        self.assertEqual(with_main["ref_capacity"], "305 L")

        tv = parsed(
            "TV",
            "Smart TV Principal",
            '<section data-testid="product-specifications">'
            '<div>Tamanho da Tela</div><div>55 polegadas</div>'
            '</section>',
        )
        self.assertEqual(tv["screen_size"], '55"')

    def test_magalu_verified_dom_excludes_recommendation_specs(self):
        recommendation = (
            '<section><h2>Produtos recomendados</h2>'
            '<article>'
            '<div>Capacidade total</div><div>500 L</div>'
            '</article></section>'
        )
        detail = {
            "retailer_sku_name": "Geladeira Principal",
            "_detail_identity_verified": True,
        }
        row = {
            "retailer": "Magalu",
            "retailer_sku_name": "Geladeira Principal",
            "product_line": "REF",
        }
        self.assertFalse(_merge_magalu_exact_html_specs(row, recommendation, detail))
        self.assertNotIn("ref_capacity", row)

        main = (
            '<section data-testid="product-specifications">'
            '<div>Capacidade total</div><div>305 L</div>'
            '</section>'
        )
        self.assertTrue(_merge_magalu_exact_html_specs(row, main + recommendation, detail))
        self.assertEqual(row["ref_capacity"], "305 L")

    def test_casas_meta_description_requires_matching_main_title(self):
        product = {
            "id": "product-id",
            "name": "Geladeira Principal",
            "sku": {"id": "123"},
            "specGroups": [],
        }
        payload = {"props": {"pageProps": {"product": product}}}

        def parsed(meta_title):
            html = (
                f'<meta property="og:title" content="{meta_title}">'
                '<meta property="og:description" content="Capacidade total: 500 L">'
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(payload)
                + "</script>"
            )
            with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
                return parse_detail(
                    html,
                    "Casas Bahia",
                    "https://www.casasbahia.com.br",
                    "https://www.casasbahia.com.br/produto/p/123",
                )

        self.assertEqual(parsed("Geladeira Recomendada")["ref_capacity"], "")
        self.assertEqual(parsed("Geladeira Principal")["ref_capacity"], "500 L")


if __name__ == "__main__":
    unittest.main()
