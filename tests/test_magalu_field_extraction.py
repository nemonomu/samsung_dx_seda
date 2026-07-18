import json
import os
import unittest
from unittest.mock import patch

from seda.magalu.detail_api import _detail_from_item
from seda.magalu.field_extraction import extract_fields, sanitize_labeled_energy_value
from seda.parsers import _parse_magalu_next_detail


def fact(label, value):
    return {"keyName": label, "value": value}


class MagaluFieldExtractionTests(unittest.TestCase):
    def test_attribute_uses_current_sku_not_all_variation_options(self):
        item = {
            "title": "Smart TV 55 4K",
            "attributes": [
                {
                    "label": "Polegada",
                    "current": "55",
                    "values": [{"value": "50"}, {"value": "55"}, {"value": "65"}],
                }
            ],
        }
        self.assertEqual(extract_fields(item, "TV")["screen_size"], "55")

        item["attributes"] = [
            {"label": "Polegada", "value": '40 pol.', "current": '40"'}
        ]
        self.assertEqual(extract_fields(item, "TV")["screen_size"], '40"')

    def test_screen_alias_and_bundle_mount_range(self):
        item = {
            "title": 'Smart TV 40" 4K + Suporte',
            "factsheet": [],
            "bundles": [{"factsheet": [fact("Tamanho da Tela", '10" a 85"')]}],
        }
        self.assertEqual(extract_fields(item, "TV")["screen_size"], '40"')
        item = {"title": "Smart TV Toshiba", "factsheet": [fact("Tamanho da Tela", "55 polegadas")]}
        self.assertEqual(extract_fields(item, "TV")["screen_size"], "55 polegadas")
        item = {"title": "Smart TV32 HD LED", "factsheet": [fact("Tamanho da Tela", "32 polegadas")]}
        self.assertEqual(extract_fields(item, "TV")["screen_size"], "32 polegadas")
        accessory = {"title": 'Suporte articulado para TV 10" a 40"', "factsheet": []}
        self.assertEqual(extract_fields(accessory, "TV")["screen_size"], "")
        accessory = {"title": "Placa Principal TV Samsung 32 polegadas", "factsheet": [fact("Tamanho da Tela", "32 polegadas")]}
        self.assertEqual(extract_fields(accessory, "TV")["screen_size"], "")
        coded_accessory = {
            "title": 'STPA 45 Suporte Articulado para TV (10" a 40")',
            "factsheet": [],
        }
        self.assertEqual(extract_fields(coded_accessory, "TV")["screen_size"], "")
        main_kit = {"title": "Kit Smart TV Samsung 55 polegadas + Suporte", "factsheet": []}
        self.assertEqual(extract_fields(main_kit, "TV")["screen_size"], "55 polegadas")

    def test_energy_aliases_and_invalid_values(self):
        item = {"title": "Smart TV", "factsheet": [fact("Consumo (máximo)", "130 W")]}
        self.assertEqual(extract_fields(item, "TV")["estimated_annual_electricity_use"], "130 W")
        item = {"title": "Smart TV", "factsheet": [fact("Consumo de energia em funcionamento", "<165W")]}
        self.assertEqual(extract_fields(item, "TV")["estimated_annual_electricity_use"], "<165W")
        item = {
            "title": "Smart TV",
            "factsheet": [fact("Eficiência energética", "Classe A")],
            "description": "<p>Consumo: Baixo consumo de energia</p>",
        }
        self.assertEqual(
            extract_fields(item, "TV")["estimated_annual_electricity_use"],
            "Baixo consumo de energia",
        )
        item = {
            "title": "Smart TV 55 polegadas",
            "factsheet": [
                fact(
                    "Características Gerais",
                    "Capacidades: 55 Consumo: 29.3 kWh/ano Peso: 12kg Voltagem: Bivolt",
                )
            ],
        }
        self.assertEqual(extract_fields(item, "TV")["estimated_annual_electricity_use"], "29.3 kWh/ano")
        item = {
            "title": "Smart TV LG 43 polegadas",
            "factsheet": [],
            "description": "Consumo em Standby: Conectividade Wi-Fi, HDMI (2), USB (1)",
        }
        self.assertEqual(extract_fields(item, "TV")["estimated_annual_electricity_use"], "")
        item = {
            "title": "Smart TV LG 43 polegadas",
            "factsheet": [],
            "description": "Consumo: Abaixo de 0,5W (Stand by)",
        }
        self.assertEqual(
            extract_fields(item, "TV")["estimated_annual_electricity_use"],
            "Abaixo de 0,5W (Stand by)",
        )
        water = {
            "title": "Smart TV",
            "factsheet": [fact("Consumo", "Baixo consumo de água")],
        }
        self.assertEqual(extract_fields(water, "TV")["estimated_annual_electricity_use"], "")
        mixed_voltage = {
            "title": "Smart TV",
            "factsheet": [fact("Consumo", "127V / 130W")],
        }
        self.assertEqual(
            extract_fields(mixed_voltage, "TV")["estimated_annual_electricity_use"],
            "130W",
        )
        equivalent = {
            "title": "Smart TV",
            "factsheet": [fact("Consumo", "130W"), fact("Consumo", "130 W")],
        }
        self.assertEqual(
            extract_fields(equivalent, "TV")["estimated_annual_electricity_use"],
            "130W",
        )
        voltage_keyed = {
            "title": "Smart TV",
            "description": "Consumo (kWh) 110V: 0,39 | 220V: 0,39",
            "factsheet": [],
        }
        self.assertEqual(
            extract_fields(voltage_keyed, "TV")["estimated_annual_electricity_use"],
            "0,39",
        )

    def test_html_tags_between_description_label_and_value(self):
        item = {
            "title": "Geladeira",
            "description": "<p><strong>Capacidade:</strong><br>394L</p>",
            "factsheet": [],
        }
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "394L")
        item = {
            "title": "Lavadora automática",
            "description": "<div><b>Capacidade de lavagem:</b><br/>13kg</div>",
            "factsheet": [],
        }
        self.assertEqual(extract_fields(item, "LDY")["ldy_capacity"], "13kg")
        item = {
            "title": "Smart TV",
            "description": "<p><strong>Consumo:</strong><br>130W</p>",
            "factsheet": [],
        }
        self.assertEqual(
            extract_fields(item, "TV")["estimated_annual_electricity_use"],
            "130W",
        )

    def test_hyphen_mount_range_and_parenthesized_energy_raw(self):
        item = {
            'title': 'Smart TV 55 polegadas + Suporte',
            'factsheet': [],
            'bundles': [{'factsheet': [fact('Tamanho da Tela', '10-40 polegadas')]}],
        }
        self.assertEqual(extract_fields(item, 'TV')['screen_size'], '55 polegadas')
        item = {
            'title': 'Smart TV',
            'factsheet': [fact('Consumo de energia', '9,77 (kwh/mês)')],
        }
        self.assertEqual(
            extract_fields(item, 'TV')['estimated_annual_electricity_use'],
            '9,77 (kwh/mês)',
        )

    def test_ref_capacity_priority_and_raw_title_fallback(self):
        title_components = {
            'title': (
                'Geladeira Capacidade do Freezer 84L '
                'Capacidade do Refrigerador 305L'
            ),
            'factsheet': [],
        }
        self.assertEqual(extract_fields(title_components, 'REF')['ref_capacity'], '305L')
        mixed = {
            'title': 'Geladeira LG 395L',
            'factsheet': [
                fact('Capacidade', '84'),
                fact(
                    'Capacidades',
                    'Capacidade total: 389 L; Freezer: 84 L; Refrigerador: 305 L',
                ),
            ],
        }
        self.assertEqual(extract_fields(mixed, 'REF')['ref_capacity'], '389 L')
        mixed['factsheet'] = [fact('Capacidade', '84')]
        self.assertEqual(extract_fields(mixed, 'REF')['ref_capacity'], '84')
        item = {
            "title": "Geladeira 395L",
            "factsheet": [
                fact("Capacidade do Freezer (L)", "84"),
                fact("Capacidade do Refrigerador (L)", "305"),
            ],
        }
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "305")
        item["factsheet"] = [fact("Capacidade do Freezer (L)", "84")]
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "84")
        item["factsheet"] = [fact("Capacidade", "De 301 a 400 litros")]
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "De 301 a 400 litros")
        item = {"title": "Mini geladeira 0,95 pés cúbicos (aprox. 26,9 litros)", "factsheet": []}
        self.assertEqual(
            extract_fields(item, "REF")["ref_capacity"],
            "0,95 pés cúbicos (aprox. 26,9 litros)",
        )
        item = {"title": "Geladeira modelo ABC", "description": "Capacidade: 394 litros", "factsheet": []}
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "394 litros")
        item = {
            "title": "Geladeira",
            "factsheet": [
                fact("Capacidades", "Freezer: 84 L; Refrigerador: 305 L")
            ],
        }
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "305 L")
        item["factsheet"] = [
            fact("Capacidades", "Total: 389 L; Freezer: 84 L; Refrigerador: 305 L")
        ]
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "389 L")
        item = {"title": "Kit Cervejeira Frost Free 82L + ímã", "factsheet": []}
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "82L")
        accessory = {
            "title": "Kit prateleira para geladeira 82L",
            "factsheet": [fact("Capacidade", "82L")],
        }
        self.assertEqual(extract_fields(accessory, "REF")["ref_capacity"], "")
        accessory = {
            "title": "Kit 2 Organizadores de Geladeira 3,2L Cestos Multiuso",
            "factsheet": [fact("Capacidade", "3,2L")],
        }
        self.assertEqual(extract_fields(accessory, "REF")["ref_capacity"], "")

    def test_ldy_invalid_meaning_falls_back_and_loading_is_normalized(self):
        item = {
            "title": "Lavadora automática 13kg",
            "factsheet": [
                fact("Capacidade de lavagem", "26 L"),
                fact("Capacidade", "630"),
                fact("Abertura da Tampa", "Superior"),
            ],
        }
        fields = extract_fields(item, "LDY")
        self.assertEqual(fields["ldy_capacity"], "13kg")
        self.assertEqual(fields["ldy_loading_type"], "Top load")
        item["factsheet"] = [fact("Tipo", "Front Loading automática")]
        self.assertEqual(extract_fields(item, "LDY")["ldy_loading_type"], "Front load")
        pressure = {"title": "Lavadora Alta Pressão 15kg", "factsheet": [fact("Capacidade", "15kg")]}
        self.assertEqual(extract_fields(pressure, "LDY")["ldy_capacity"], "")

    def test_ldy_repeated_target_uses_shared_conflict_policy(self):
        item = {
            'title': 'Lavadora automatica 16kg',
            'factsheet': [
                fact('Capacidade de lavagem', '0,49'),
                fact('Capacidade de lavagem', '16kg'),
            ],
        }
        self.assertEqual(extract_fields(item, 'LDY')['ldy_capacity'], '16kg')
        item['factsheet'] = [fact('Capacidade de lavagem', '0,49')]
        item['title'] = 'Lavadora compacta'
        self.assertEqual(extract_fields(item, 'LDY')['ldy_capacity'], '0,49')
        item['factsheet'] = [
            fact('Capacidade de lavagem', '0,49'),
            fact('Capacidade de lavagem', '0,49 kg'),
        ]
        item['title'] = 'Lavadora automatica 16kg'
        self.assertEqual(extract_fields(item, 'LDY')['ldy_capacity'], '0,49')

    def test_adjacent_dom_targets_and_repeated_consumo_segments(self):
        item = {
            'title': 'Geladeira',
            'description': '<div>Capacidade</div><div>394L</div>',
            'factsheet': [],
        }
        self.assertEqual(extract_fields(item, 'REF')['ref_capacity'], '394L')
        item = {
            'title': 'Lavadora automatica 13kg',
            'description': '<table><tr><td>Capacidade de lavagem</td><td>13kg</td></tr></table>',
            'factsheet': [],
        }
        self.assertEqual(extract_fields(item, 'LDY')['ldy_capacity'], '13kg')
        item['description'] = 'Consumo de agua: 100L - Consumo de energia:130W'
        self.assertEqual(
            extract_fields(item, 'LDY')['estimated_annual_electricity_use'],
            '130W',
        )
        self.assertEqual(sanitize_labeled_energy_value('130'), '130')
        self.assertEqual(sanitize_labeled_energy_value('<165'), '<165')
        self.assertEqual(sanitize_labeled_energy_value('Bivolt'), '')

    def test_graphql_and_next_data_paths_match(self):
        item = {
            "id": "sample",
            "title": "Geladeira 395L",
            "factsheet": [
                fact("Capacidade do Refrigerador (L)", "305"),
                fact("Capacidade do Freezer (L)", "84"),
            ],
            "attributes": [],
            "offers": [],
            "rating": {},
        }
        payload = {"props": {"pageProps": {"data": {"item": item}}}}
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload, ensure_ascii=False) + "</script>"
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
            graphql = _detail_from_item(item)
            next_data = _parse_magalu_next_detail(html, "https://www.magazineluiza.com.br", "https://example/p/sample")
        self.assertEqual(graphql["ref_capacity"], "305")
        for field_name in ("ref_capacity", "screen_size", "estimated_annual_electricity_use"):
            self.assertEqual(graphql.get(field_name), next_data.get(field_name), field_name)


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
                    fields = extract_fields(
                        {"title": title, "factsheet": [fact("Consumo", "15W")]},
                        line,
                    )
                    self.assertEqual(fields["estimated_annual_electricity_use"], "")
                    for field_name in ("screen_size", "ref_capacity", "ldy_capacity", "ldy_loading_type"):
                        self.assertFalse(fields.get(field_name, ""), field_name)

        preserved = (
            (
                "TV",
                "Smart TV LG 55 polegadas Controle AI Magic",
                "screen_size",
                "55 polegadas",
            ),
            (
                "REF",
                "Geladeira Electrolux 480L com Painel Digital",
                "ref_capacity",
                "480L",
            ),
            (
                "LDY",
                "Maquina de Lavar Brastemp 14kg com Smart Sensor",
                "ldy_capacity",
                "14kg",
            ),
        )
        for line, title, field_name, expected in preserved:
            with self.subTest(preserved=title):
                fields = extract_fields(
                    {"title": title, "factsheet": [fact("Consumo", "15W")]},
                    line,
                )
                self.assertEqual(fields[field_name], expected)
                self.assertEqual(fields["estimated_annual_electricity_use"], "15W")


if __name__ == "__main__":
    unittest.main()
