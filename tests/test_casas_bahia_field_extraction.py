import os
import json
import unittest
from unittest.mock import patch

from seda.casas_bahia.detail_api import _product_source_detail, _screen_size_from_tamanho_tela
from seda.casas_bahia.field_extraction import _same_ldy_capacity_numbers
from seda.common.translations import translate_value
from seda.parsers import _parse_casas_bahia_html_detail, screen_size_from_text


def source(name, specs, description=""):
    return {
        "product": {
            "id": "fixture",
            "name": name,
            "description": description,
            "specGroups": [{"name": "Especificações Técnicas", "specs": specs}],
        },
        "sku": {"name": ""},
    }


def spec(name, value):
    return {"name": name, "value": value}


class CasasBahiaFieldExtractionTests(unittest.TestCase):
    def test_product_source_screen_uses_common_validator(self):
        self.assertEqual(_screen_size_from_tamanho_tela('55 polegadas'), '55' + chr(34))
        for value in ("55\u2033", "55 inch", "55 inches"):
            with self.subTest(explicit_screen=value):
                self.assertEqual(_screen_size_from_tamanho_tela(value), '55"')
                self.assertEqual(screen_size_from_text(value), '55"')
        for value in ('50-55 polegadas', '50/55 polegadas', '2024', '130 W', '127 V'):
            with self.subTest(value=value):
                self.assertEqual(_screen_size_from_tamanho_tela(value), '')

    def test_same_ldy_target_drops_conflicting_unitless_sub_one_value(self):
        data = source(
            'Lavadora Electrolux 16kg',
            [
                spec('Capacidade (kg de roupas)', '0,49'),
                spec('Capacidade (kg de roupas)', '16kg'),
            ],
        )
        self.assertEqual(self.detail('LDY', data)['ldy_capacity'], '16kg')

    def detail(self, line, data):
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": line}):
            return _product_source_detail(data)

    def test_ldy_description_prefers_later_exact_same_capacity(self):
        description = (
            "Possuindo capacidade de lavagem de 1,2 k e 5 programas. "
            "Capacidade de lavagem : 1,2 Kg. "
            "Capacidade de lavagem: 1,2kg."
        )
        data = source(
            "Maquina de Lavar Petit 1,2 Kg 5 Programas de Lavagem Praxis",
            [],
            description,
        )
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "1,2 Kg")

        title_only_exact = source(
            "Maquina de Lavar Petit 1,2 Kg",
            [],
            "Possuindo capacidade de lavagem de 1,2 k e 5 programas.",
        )
        self.assertEqual(self.detail("LDY", title_only_exact)["ldy_capacity"], "1,2 Kg")

        structured_then_description = source(
            "Lavadora compacta",
            [spec("Capacidade de lavagem", "de 1,2")],
            "Capacidade de lavagem: 1,2 Kg.",
        )
        self.assertEqual(
            self.detail("LDY", structured_then_description)["ldy_capacity"],
            "1,2 Kg",
        )

        nonstandard_only = source(
            "Lavadora compacta",
            [],
            "Possuindo capacidade de lavagem de 1,2 k e 5 programas.",
        )
        self.assertEqual(self.detail("LDY", nonstandard_only)["ldy_capacity"], "de 1,2 k")

        numeric_only = source("Lavadora compacta", [], "Capacidade total: 14")
        self.assertEqual(self.detail("LDY", numeric_only)["ldy_capacity"], "14")

        self.assertFalse(_same_ldy_capacity_numbers("1", "10 kg"))
        self.assertTrue(_same_ldy_capacity_numbers("10", "10 kg"))
        self.assertTrue(_same_ldy_capacity_numbers("01,20", "1.2 kg"))

    def test_ref_capacity_priority(self):
        data = source(
            "Geladeira LG 395L",
            [spec("Capacidade do Freezer (L)", "84"), spec("Capacidade do Refrigerador (L)", "305")],
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "305")
        data = source("Geladeira 470L", [spec("Capacidade total", "De 401 a 500 litros")])
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "De 401 a 500 litros")
        data = source(
            "Geladeira 490L",
            [spec("Capacidade total", "500 L"), spec("Capacidade total líquida", "490 litros")],
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "490 litros")
        data = source(
            "Geladeira sem capacidade no título",
            [spec("Capacidade total", "500 L")],
            "Capacidade líquida total: 490 L",
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "490 L")

    def test_ref_capacity_inside_composite_spec_value(self):
        data = source(
            'Geladeira LG 395L',
            [
                spec('Capacidade', '84'),
                spec(
                    'Capacidades',
                    'Capacidade total: 389 L; Freezer: 84 L; Refrigerador: 305 L',
                ),
            ],
        )
        self.assertEqual(self.detail('REF', data)['ref_capacity'], '389 L')
        data = source('Geladeira LG 395L', [spec('Capacidade', '84')])
        self.assertEqual(self.detail('REF', data)['ref_capacity'], '84')
        data = source(
            "Geladeira LG",
            [spec("Capacidades", "Freezer: 84; Refrigerador: 305")],
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "305")
        data = source(
            "Geladeira LG",
            [spec("Capacidades", "Freezer: 84; Refrigerador: 305; Total: 389")],
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "389")
        data = source("Freezer vertical", [spec("Capacidades", "Freezer: 84")])
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "84")
        data = source(
            "Geladeira LG",
            [spec("Capacidade total líquida", "Freezer: 84; Refrigerador: 305; Total: 389")],
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "389")

    def test_ref_description_components_and_title_fallback(self):
        data = source(
            'Geladeira Capacidade do Freezer 84L Capacidade do Refrigerador 305L',
            [],
        )
        self.assertEqual(self.detail('REF', data)['ref_capacity'], '305L')
        data = source(
            "Geladeira 294L",
            [],
            "<p>Capacidade do freezer: 69L; Capacidade do refrigerador: 225L</p>",
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "225L")
        data = source(
            "Refrigerador 264L",
            [],
            "Capacidade Liquida: Freezer: 53; Refrigerador: 207; Total: 260; "
            "Capacidade Bruta: Freezer: 54; Refrigerador: 210; Capacidade total: 264",
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "260")
        data = source("Geladeira Multidoor 541L", [])
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "541L")
        data = source("Geladeira modelo ABC", [], "Capacidade: 394 litros")
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "394 litros")

    def test_duplicate_values_are_collapsed(self):
        data = source("Geladeira 480L", [spec("Capacidade total", "480 Litros-480 Litros-480 Litros-480 Litros")])
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "480 Litros")

    def test_ldy_capacity_rejects_wrong_meaning_then_falls_back(self):
        data = source(
            "Lavadora Consul 9kg",
            [spec("Capacidade (kg de roupas)", "96 litros"), spec("Capacidade de lavagem", "De 7 a 10kg")],
        )
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "9kg")
        data = source("Lavadora portátil 1.2Kg", [spec("Capacidade", "26 l")])
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "1.2Kg")
        dishwasher = source("Lava-louças 14 serviços", [spec("Capacidade", "14")])
        self.assertEqual(self.detail("LDY", dishwasher)["ldy_capacity"], "")
        dishwasher = source("Máquina de Lavar Louças Ecomax", [spec("Capacidade", "24")])
        self.assertEqual(self.detail("LDY", dishwasher)["ldy_capacity"], "")
        range_data = source("Lavadora Consul 9kg", [], "Capacidade 9kg; Faixa Capacidade 8kg a 9kg")
        self.assertEqual(self.detail("LDY", range_data)["ldy_capacity"], "9kg")
        range_data = source("Lavadora Brastemp 14kg", [], "Capacidade 12kg - 14kg")
        self.assertEqual(self.detail("LDY", range_data)["ldy_capacity"], "14kg")
        ordered = source("Lavadora 15kg", [], "Capacidade 15kg; Capacidade total 14kg")
        self.assertEqual(self.detail("LDY", ordered)["ldy_capacity"], "15kg")
        target_only = source(
            "Lavadora Consul",
            [],
            "Capacidade 9kg; Faixa Capacidade 8kg a 9kg",
        )
        self.assertEqual(
            self.detail("LDY", target_only)["ldy_capacity"],
            "9kg,8kg a 9kg",
        )

    def test_ldy_suspicious_unitless_decimal_yields_to_conflicting_capacity(self):
        data = source(
            "Lavadora automática 16kg",
            [spec("Capacidade (kg de roupas)", "0,49")],
        )
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "16kg")
        data = source(
            "Lavadora automática 16kg",
            [
                spec("Capacidade (kg de roupas)", "0,49"),
                spec("Capacidade de lavagem", "15kg"),
            ],
        )
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "16kg")

    def test_ldy_small_capacity_is_not_globally_rejected(self):
        data = source(
            "Mini lavadora portátil 1kg",
            [spec("Capacidade (kg de roupas)", "1")],
        )
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "1kg")
        data = source("Lavadora compacta", [spec("Capacidade (kg de roupas)", "0,49")])
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "0,49")
        data = source(
            "Lavadora automática 16kg",
            [
                spec("Capacidade (kg de roupas)", "0,49"),
                spec("Capacidade de lavagem", "0,49 kg"),
            ],
        )
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "16kg")

    def test_ldy_exact_title_capacity_has_priority_for_reported_cases(self):
        cases = (
            (
                "1570578247",
                "Máquina de Lavar Consul 15kg com Modo Eco - 110V",
                [
                    spec("Capacidade (kg de roupas)", "620"),
                    spec("Capacidade", "De 11 a 15kg"),
                ],
                "Capacidade Total: 15 kg",
                "15kg",
            ),
            (
                "1570474147",
                "Máquina De Lavar 14 Kg Philco Preto PLR14B",
                [spec("Capacidade", "Acima de 16 kg")],
                "Capacidade de lavagem: 14kg",
                "14 Kg",
            ),
            (
                "1514271762",
                "Lavadora de Roupas Electrolux Top Load LED17 17Kg Automática",
                [spec("Capacidade", "De 11 a 15kg")],
                "Capacidade (kg): 17",
                "17Kg",
            ),
            (
                "1582258509",
                "Mini Máquina De Lavar Roupas Dobrável Portátil 12 Litros Lilás",
                [
                    spec("Capacidade (kg de roupas)", "12L"),
                    spec("Capacidade", "De 11 a 15kg"),
                ],
                "Capacidade: 12L",
                "12L",
            ),
            (
                "1582708993",
                "Máquina de Lavar 14 kg Philco 12 Programas Titânio PLR14B",
                [
                    spec("Capacidade (kg de roupas)", "14"),
                    spec("Capacidade (kg de roupas)", "De 11 a 15kg"),
                ],
                "",
                "14 kg",
            ),
            (
                "1545346578",
                "Lavadora de Roupas Consul CWB09BB 9kg",
                [],
                "Capacidade 9kg; Faixa Capacidade 8kg a 9kg",
                "9kg",
            ),
            (
                "1582406374",
                "Máquina de Lavar Mueller Energy Automática 8kg",
                [
                    spec("Capacidade (kg de roupas)", "8Kg"),
                    spec("Capacidade (kg de roupas)", "De 7 a 10kg"),
                ],
                "",
                "8kg",
            ),
            (
                "1577381447",
                "Máquina De Lavar Midea Healthguard MF200 11kg",
                [
                    spec("Capacidade (kg de roupas)", "11 kg"),
                    spec("Capacidade (kg de roupas)", "De 11 a 15kg"),
                ],
                "",
                "11kg",
            ),
            (
                "1582410067",
                "Máquina de Lavar Midea Wave Agitator Branca 13Kg",
                [
                    spec("Capacidade (kg de roupas)", "13"),
                    spec("Capacidade (kg de roupas)", "De 11 a 15kg"),
                ],
                "",
                "13Kg",
            ),
        )
        for sku, title, specs, description, expected in cases:
            with self.subTest(sku=sku):
                data = source(title, specs, description)
                self.assertEqual(self.detail("LDY", data)["ldy_capacity"], expected)

    def test_ldy_title_priority_requires_one_unambiguous_exact_capacity(self):
        qualified = source(
            "Lavadora acima de 16kg",
            [spec("Capacidade de lavagem", "14kg")],
        )
        self.assertEqual(self.detail("LDY", qualified)["ldy_capacity"], "14kg")

        multiple = source(
            "Lava e Seca 11kg Lava / 7kg Seca",
            [spec("Capacidade de lavagem", "10kg")],
        )
        self.assertEqual(self.detail("LDY", multiple)["ldy_capacity"], "10kg")

        water_usage = source(
            "Mini lavadora com economia de 20 litros de água",
            [spec("Capacidade de lavagem", "2kg")],
        )
        self.assertEqual(self.detail("LDY", water_usage)["ldy_capacity"], "2kg")

        approximate = source(
            "Lavadora com capacidade aproximada de 16kg",
            [spec("Capacidade de lavagem", "14kg")],
        )
        self.assertEqual(self.detail("LDY", approximate)["ldy_capacity"], "14kg")
        approximate_suffix = source(
            "Lavadora com capacidade de 16kg aprox.",
            [spec("Capacidade de lavagem", "14kg")],
        )
        self.assertEqual(
            self.detail("LDY", approximate_suffix)["ldy_capacity"],
            "14kg",
        )

        compact_tub = source("Mini lavadora tanque de 12 litros", [])
        self.assertEqual(self.detail("LDY", compact_tub)["ldy_capacity"], "12L")

        for title in (
            "Lavadora 11-15kg",
            "Lavadora 11/15kg",
            "Lavadora 11~15kg",
            "Lavadora 11 ou 15kg",
            "Lavadora 11 ate 15kg",
            "Lavadora entre 11 e 15kg",
        ):
            with self.subTest(title=title):
                ranged = source(title, [spec("Capacidade de lavagem", "10kg")])
                self.assertEqual(self.detail("LDY", ranged)["ldy_capacity"], "10kg")

        compact_range = source(
            "Mini lavadora portatil 11-15L",
            [spec("Capacidade de lavagem", "2kg")],
        )
        self.assertEqual(self.detail("LDY", compact_range)["ldy_capacity"], "2kg")

    def test_product_source_html_keeps_label_attached_to_value(self):
        data = source(
            "Geladeira modelo ABC",
            [],
            "<p><strong>Capacidade:</strong><br>394 litros</p>",
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "394 litros")
        data = source(
            "Lavadora automática 16kg",
            [],
            "<p><strong>Capacidade de lavagem:</strong><br>16kg</p>",
        )
        self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "16kg")
        data = source(
            "Smart TV 55 polegadas",
            [],
            "<strong>Consumo de energia:</strong><br>&lt;165W",
        )
        self.assertEqual(self.detail("TV", data)["estimated_annual_electricity_use"], "<165W")

    def test_ldy_color_repeated_value_contract(self):
        data = source("Lavadora automática 16kg", [spec("Cor", "Branco-Branco")])
        self.assertEqual(self.detail("LDY", data)["ldy_color"], "Branco,Branco")

    def test_ldy_loading_type_aliases_and_invalid_type(self):
        data = source("Lavadora 11kg", [spec("Tipo", "Front Loading automática")])
        self.assertEqual(self.detail("LDY", data)["ldy_loading_type"], "Front load")
        data = source("Lavadora 11kg", [spec("Tipo", "Elétrica"), spec("Abertura da Tampa", "Superior")])
        self.assertEqual(self.detail("LDY", data)["ldy_loading_type"], "Top load")
        data = source("Máquina de Lavar Brastemp 15kg", [], "A Lavadora Front Load Brastemp 15 kg")
        self.assertEqual(self.detail("LDY", data)["ldy_loading_type"], "Front load")

    def test_ldy_loading_ignores_unrelated_exact_direction_values(self):
        data = source(
            "Máquina de Lavar Consul 15kg com Modo Eco - 110V",
            [spec("Consumo de água", "Superior")],
            "Abertura da Tampa: Frontal",
        )
        self.assertEqual(self.detail("LDY", data)["ldy_loading_type"], "Front load")

        unrelated_only = source(
            "Máquina de Lavar Consul 15kg",
            [spec("Consumo de água", "Superior")],
        )
        self.assertEqual(self.detail("LDY", unrelated_only)["ldy_loading_type"], "")

    def test_energy_keeps_consumption_and_rejects_efficiency(self):
        data = source(
            "Smart TV 55",
            [spec("Eficiência energética", "Procel Nível A")],
            "Consumo com TV ligada: <80W; Consumo em modo standby: <0,5W",
        )
        self.assertEqual(
            self.detail("TV", data)["estimated_annual_electricity_use"],
            "<80W,<0,5W",
        )
        data = source(
            "Lavadora 13kg",
            [],
            "Consumo de energia (água fria): 0,31 kWh/ciclo<br>Potência: 1250W-127V",
        )
        self.assertEqual(self.detail("LDY", data)["estimated_annual_electricity_use"], "0,31 kWh/ciclo")
        data = source("Smart TV 55 polegadas", [spec("Consumo de energia", "180")])
        self.assertEqual(self.detail("TV", data)["estimated_annual_electricity_use"], "180")
        data = source("Smart TV 55 polegadas", [spec("Consumo médio (W)", "26,5")])
        self.assertEqual(self.detail("TV", data)["estimated_annual_electricity_use"], "26,5")
        data = source(
            "Smart TV 55 polegadas",
            [spec("Características Gerais", "Capacidades: 55 Consumo: 29.3 kWh/ano Peso: 12kg Voltagem: Bivolt")],
        )
        self.assertEqual(self.detail("TV", data)["estimated_annual_electricity_use"], "29.3 kWh/ano")

    def test_energy_deduplicates_formatting_and_rejects_voltage_number(self):
        mixed_voltage = source(
            'Smart TV 55 polegadas',
            [spec('Consumo de energia', '127V / 130W')],
        )
        self.assertEqual(
            self.detail('TV', mixed_voltage)['estimated_annual_electricity_use'],
            '130W',
        )
        data = source(
            "Smart TV 55 polegadas",
            [
                spec("Consumo de energia", "130W"),
                spec("Consumo de energia", "130 W"),
            ],
        )
        self.assertEqual(self.detail("TV", data)["estimated_annual_electricity_use"], "130W")
        data = source(
            "Lavadora automática 16kg - 110V",
            [],
            "Consumo de energia (kWh) 110V: 0.39",
        )
        self.assertEqual(self.detail("LDY", data)["estimated_annual_electricity_use"], "0.39")

    def test_energy_does_not_treat_low_water_consumption_as_energy(self):
        data = source("Lavadora automática 16kg", [], "Baixo consumo de água")
        self.assertEqual(self.detail("LDY", data)["estimated_annual_electricity_use"], "")

    def test_nonstandard_labeled_and_inline_energy_raw(self):
        data = source('Smart TV 55 polegadas', [spec('Consumo de energia', '<1')])
        self.assertEqual(self.detail('TV', data)['estimated_annual_electricity_use'], '<1')
        data = source('Smart TV 55 polegadas', [], 'Consumo médio (W) 49,4')
        self.assertEqual(self.detail('TV', data)['estimated_annual_electricity_use'], '49,4')

    def test_html_energy_does_not_restore_invalid_value(self):
        payload = {"props": {"pageProps": {"product": {"specGroups": [{"specs": [{"name": "Consumo de energia", "value": "Elétrica"}]}]}}}}
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload, ensure_ascii=False) + "</script>"
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            detail = _parse_casas_bahia_html_detail(html, "https://www.casasbahia.com.br", "https://example/p/1")
        self.assertEqual(detail["estimated_annual_electricity_use"], "")

    def test_html_fallback_uses_same_capacity_rules(self):
        payload = {
            "props": {
                "pageProps": {
                    "product": {
                        "specGroups": [
                            {
                                "name": "Especificações Técnicas",
                                "specs": [{"name": "Capacidade", "value": "26 l"}],
                            }
                        ]
                    }
                }
            }
        }
        html = (
            '<script type="application/ld+json">'
            + json.dumps({"@type": "Product", "name": "Lavadora automática 13kg"})
            + '</script><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            detail = _parse_casas_bahia_html_detail(html, "https://www.casasbahia.com.br", "https://example/p/1")
        self.assertEqual(detail["ldy_capacity"], "13kg")

    def test_html_fallback_keeps_distinct_same_label_values(self):
        payload = {
            "props": {"pageProps": {"product": {"specGroups": [{"specs": [
                {"name": "Capacidade", "value": "14kg"},
                {"name": "Capacidade", "value": "15kg"},
            ]}]}}}
        }
        html = (
            '<script type="application/ld+json">'
            + json.dumps({"@type": "Product", "name": "Lavadora automática"})
            + '</script><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            detail = _parse_casas_bahia_html_detail(html, "https://www.casasbahia.com.br", "https://example/p/2")
        self.assertEqual(detail["ldy_capacity"], "14kg,15kg")

    def test_html_fallback_uses_next_product_description(self):
        payload = {
            "props": {"pageProps": {"product": {
                "name": "Lavadora automática 16kg",
                "description": "<strong>Capacidade de lavagem:</strong><br>16kg",
                "specGroups": [],
            }}}
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            detail = _parse_casas_bahia_html_detail(
                html,
                "https://www.casasbahia.com.br",
                "https://example/p/3",
            )
        self.assertEqual(detail["ldy_capacity"], "16kg")

    def test_html_structured_spec_has_priority_over_meta_description(self):
        payload = {
            "props": {"pageProps": {"product": {
                "name": "Lavadora automática",
                "description": "",
                "specGroups": [{"specs": [{"name": "Capacidade", "value": "14kg"}]}],
            }}}
        }
        html = (
            '<meta property="og:description" content="Capacidade: 15kg">'
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            detail = _parse_casas_bahia_html_detail(
                html,
                "https://www.casasbahia.com.br",
                "https://example/p/4",
            )
        self.assertEqual(detail["ldy_capacity"], "14kg")

    def test_title_guards_allow_main_kits_and_reject_parts(self):
        data = source('Kit Cervejeira Frost Free 82L + ima', [])
        self.assertEqual(self.detail('REF', data)['ref_capacity'], '82L')
        data = source(
            'Kit prateleira para geladeira 82L',
            [spec('Capacidade', '82L')],
        )
        self.assertEqual(self.detail('REF', data)['ref_capacity'], '')

        data = source('Kit Smart TV 55 polegadas + suporte', [spec('Consumo de energia', '130W')])
        self.assertEqual(self.detail('TV', data)['estimated_annual_electricity_use'], '130W')
        data = source('Kit suporte para TV 55 polegadas', [spec('Consumo de energia', '130W')])
        self.assertEqual(self.detail('TV', data)['estimated_annual_electricity_use'], '')

        data = source('Kit Lavadora automatica 16kg + suporte', [])
        self.assertEqual(self.detail('LDY', data)['ldy_capacity'], '16kg')
        data = source('Trava tampa para lavadora 15kg', [spec('Capacidade', '15kg')])
        self.assertEqual(self.detail('LDY', data)['ldy_capacity'], '')

    def test_html_ref_priority_is_global_across_structured_and_description(self):
        quote = chr(34)
        for description, expected in (
            ('Capacidade do Refrigerador: 305L', '305L'),
            ('Capacidade do Refrigerador: 305L; Capacidade total: 389L', '389L'),
        ):
            with self.subTest(description=description):
                payload = {
                    'props': {'pageProps': {'product': {
                        'name': 'Geladeira LG 395L',
                        'description': description,
                        'specGroups': [{'specs': [
                            {'name': 'Capacidade do Freezer (L)', 'value': '84L'},
                        ]}],
                    }}},
                }
                html = (
                    f'<script id={quote}__NEXT_DATA__{quote} '
                    f'type={quote}application/json{quote}>'
                    + json.dumps(payload)
                    + '</script>'
                )
                with patch.dict(os.environ, {'SEDA_PRODUCT_LINE': 'REF'}):
                    detail = _parse_casas_bahia_html_detail(
                        html,
                        'https://www.casasbahia.com.br',
                        'https://example/p/ref-priority',
                    )
                self.assertEqual(detail['ref_capacity'], expected)

    def test_html_uses_only_main_next_product_specs_and_title(self):
        quote = chr(34)
        main = {
            'name': 'Geladeira Principal 490L',
            'specGroups': [{'specs': [
                {'name': 'Capacidade total', 'value': '490L'},
                {'name': 'Tamanho da tela', 'value': '2024'},
            ]}],
        }
        recommendation = {
            'name': 'Geladeira Recomendada 300L',
            'specGroups': [{'specs': [
                {'name': 'Capacidade total', 'value': '300L'},
            ]}],
        }
        payload = {
            'props': {'pageProps': {
                'product': main,
                'recommendations': [{'product': recommendation}],
            }},
        }
        html = (
            f'<script id={quote}__NEXT_DATA__{quote} type={quote}application/json{quote}>'
            + json.dumps(payload)
            + '</script>'
        )
        with patch.dict(os.environ, {'SEDA_PRODUCT_LINE': 'REF'}):
            detail = _parse_casas_bahia_html_detail(
                html,
                'https://www.casasbahia.com.br',
                'https://example/p/main-only',
            )
        self.assertEqual(detail['ref_capacity'], '490L')
        self.assertEqual(detail['retailer_sku_name'], 'Geladeira Principal 490L')
        self.assertEqual(detail['screen_size'], '')

    def test_loading_translation_preserves_distinct_directions(self):
        self.assertEqual(
            translate_value("ldy_loading_type", "Superior,Front Loading automática"),
            "Top load,Front load",
        )


    def test_real_accessory_titles_do_not_fill_audited_fields(self):
        cases = {
            "TV": (
                "Tv Samsung Smart Hub Controle R.tv Lcd Led",
                "Controlo remoto de substituicao para Samsung Smart TV",
                "Cr para smart tv semp tcl 49c2us",
            ),
            "REF": (
                "Lampada Led E14 Geladeira Electrolux DFN41 Original",
                "Moto Ventilador Geladeira 809069211",
                "Resistencia de Degelo 110V para Geladeira Brastemp",
                "Termistor Refrigerador LFXC24726D Original",
                "Forma bandeja de gelo geladeira Electrolux original",
            ),
            "LDY": (
                "Limpador de maquina de lavar roupa 10 comprimidos",
                "Refil Mop Lava e Seca de Microfibra",
                "Sensor Termistor Lava e Seca LG",
                "Lavadora de Loucas Ecomax 503",
            ),
        }
        for line, titles in cases.items():
            for title in titles:
                with self.subTest(line=line, title=title):
                    detail = self.detail(
                        line,
                        source(title, [spec("Consumo de energia", "15W")]),
                    )
                    self.assertEqual(detail["estimated_annual_electricity_use"], "")
                    for field_name in ("screen_size", "ref_capacity", "ldy_capacity", "ldy_loading_type"):
                        self.assertFalse(detail.get(field_name, ""), field_name)

        preserved = (
            ("REF", "Geladeira Electrolux 480L com Painel Digital", "ref_capacity", "480L"),
            (
                "LDY",
                "Maquina de Lavar Brastemp 14kg com Smart Sensor",
                "ldy_capacity",
                "14kg",
            ),
        )
        for line, title, field_name, expected in preserved:
            with self.subTest(preserved=title):
                detail = self.detail(
                    line,
                    source(title, [spec("Consumo de energia", "15W")]),
                )
                self.assertEqual(detail[field_name], expected)
                self.assertEqual(detail["estimated_annual_electricity_use"], "15W")


if __name__ == "__main__":
    unittest.main()
