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
            "title": "Smart TV 4K",
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

    def test_screen_size_prefers_only_one_safe_title_measurement(self):
        exact_title = {
            "title": "Smart TV Samsung 55 polegadas 4K",
            "factsheet": [fact("Tamanho da Tela", "65 polegadas")],
        }
        self.assertEqual(
            extract_fields(exact_title, "TV")["screen_size"],
            "55 polegadas",
        )

        implicit_title = {
            "title": "Smart TV Samsung 65 4K",
            "factsheet": [fact("Tamanho da Tela", "60 polegadas")],
        }
        self.assertEqual(
            extract_fields(implicit_title, "TV")["screen_size"],
            '65"',
        )

        for title, expected in (
            ("Smart TV TCL Google TV 55 4K", '55"'),
            ("Smart TV Roku TV 43 Full HD", '43"'),
        ):
            with self.subTest(os_labeled_screen=title):
                item = {
                    "title": title,
                    "factsheet": [fact("Tamanho da Tela", "50 polegadas")],
                }
                self.assertEqual(extract_fields(item, "TV")["screen_size"], expected)

        non_size_numbers = {
            "title": "Smart TV Samsung QN65ABC 4K 120Hz 130W 127V",
            "factsheet": [fact("Tamanho da Tela", "60 polegadas")],
        }
        self.assertEqual(
            extract_fields(non_size_numbers, "TV")["screen_size"],
            "60 polegadas",
        )

        for title in (
            'Smart TV LG processador 64 bits 4K',
            'Smart TV LG atualização 120 FPS 4K',
            'Smart TV Android 4K 32 GB',
        ):
            with self.subTest(metadata_title=title):
                metadata_item = {
                    'title': title,
                    'factsheet': [fact('Tamanho da Tela', '55 polegadas')],
                }
                self.assertEqual(
                    extract_fields(metadata_item, 'TV')['screen_size'],
                    '55 polegadas',
                )

        for title in (
            'Smart TV Samsung 4K + Suporte de Parede 32 polegadas',
            'Smart TV Samsung 4K + Painel para Sala 55',
            'Smart TV Samsung 4K + Rack compatível 65',
            'Smart TV Samsung 4K + Base Universal 43',
        ):
            with self.subTest(accessory_measurement=title):
                accessory_measurement = {
                    'title': title,
                    'factsheet': [fact('Tamanho da Tela', '50 polegadas')],
                }
                self.assertEqual(
                    extract_fields(accessory_measurement, 'TV')['screen_size'],
                    '50 polegadas',
                )

        compact_main_size = {
            'title': 'Smart TV65',
            'factsheet': [fact('Tamanho da Tela', '50 polegadas')],
        }
        self.assertEqual(
            extract_fields(compact_main_size, 'TV')['screen_size'],
            f'65{chr(34)}',
        )

        for title in ('Smart TV até 65 polegadas', 'Smart TV cerca de 65 polegadas'):
            with self.subTest(qualified_title=title):
                qualified_item = {
                    'title': title,
                    'factsheet': [fact('Tamanho da Tela', '55 polegadas')],
                }
                self.assertEqual(
                    extract_fields(qualified_item, 'TV')['screen_size'],
                    '55 polegadas',
                )

        accessory_only_size = {
            'title': 'Smart TV Samsung 4K + Suporte para TV de 32 polegadas',
            'factsheet': [fact('Tamanho da Tela', '65 polegadas')],
        }
        self.assertEqual(
            extract_fields(accessory_only_size, 'TV')['screen_size'],
            '65 polegadas',
        )

        main_size_with_accessory_range = {
            'title': 'Smart TV Samsung 32 4K + Suporte para TV de 10 a 40 polegadas',
            'factsheet': [fact('Tamanho da Tela', '65 polegadas')],
        }
        self.assertEqual(
            extract_fields(main_size_with_accessory_range, 'TV')['screen_size'],
            f'32{chr(34)}',
        )

        explicit_with_hdr_version = {
            "title": 'SMART TV LG 43" ThinQ AI HDR 10',
            "factsheet": [fact("Tamanho da Tela", "50 polegadas")],
        }
        self.assertEqual(
            extract_fields(explicit_with_hdr_version, "TV")["screen_size"],
            '43"',
        )

        os_version_only = {
            "title": "Smart TV LG WebOS 23",
            "factsheet": [],
        }
        self.assertEqual(extract_fields(os_version_only, "TV")["screen_size"], "")

        for os_title in (
            "Smart TV Google TV 12",
            "Smart TV Roku TV 12",
            "Smart TV Titan OS 12",
            "Smart TV VIDAA 12",
        ):
            with self.subTest(os_title=os_title):
                self.assertEqual(
                    extract_fields({"title": os_title, "factsheet": []}, "TV")["screen_size"],
                    "",
                )
                self.assertEqual(
                    extract_fields(
                        {
                            "title": os_title,
                            "factsheet": [fact("Tamanho da Tela", "50 polegadas")],
                        },
                        "TV",
                    )["screen_size"],
                    "50 polegadas",
                )

        screen_before_os = {
            "title": "Smart TV 32 Google TV",
            "factsheet": [fact("Tamanho da Tela", "50 polegadas")],
        }
        self.assertEqual(
            extract_fields(screen_before_os, "TV")["screen_size"],
            '32"',
        )

        warranty_years = {
            "title": "Smart TV Samsung QN65ABC 4K com 10 anos de garantia",
            "factsheet": [fact("Tamanho da Tela", "65 polegadas")],
        }
        self.assertEqual(
            extract_fields(warranty_years, "TV")["screen_size"],
            "65 polegadas",
        )

        ambiguous_title = {
            "title": "Kit Smart TV 55 polegadas + Smart TV 65 polegadas",
            "factsheet": [fact("Tamanho da Tela", "50 polegadas")],
        }
        self.assertEqual(
            extract_fields(ambiguous_title, "TV")["screen_size"],
            "50 polegadas",
        )

        compact_accessory_after_main = {
            "title": "Smart TV Samsung 65 polegadas 4K + Suporte para TV55",
            "factsheet": [fact("Tamanho da Tela", "55 polegadas")],
        }
        self.assertEqual(
            extract_fields(compact_accessory_after_main, "TV")["screen_size"],
            "65 polegadas",
        )

        for technology in ("OLED", "QLED", "LED", "Mini LED", "LCD", "VA", "IPS"):
            with self.subTest(display_panel_technology=technology):
                display_panel = {
                    "title": f"Smart TV Samsung com Painel {technology} 55 polegadas 4K",
                    "factsheet": [fact("Tamanho da Tela", "60 polegadas")],
                }
                self.assertEqual(
                    extract_fields(display_panel, "TV")["screen_size"],
                    "55 polegadas",
                )

        for title, expected in (
            ("Smart TV LG com Painel OLED Evo 55 polegadas", "55 polegadas"),
            ("Smart TV Samsung com Painel QLED 4K 55 polegadas", "55 polegadas"),
            ("Smart TV TCL com Painel Mini LED 4K 65 polegadas", "65 polegadas"),
        ):
            with self.subTest(display_panel_descriptor=title):
                display_panel = {
                    "title": title,
                    "factsheet": [fact("Tamanho da Tela", "60 polegadas")],
                }
                self.assertEqual(
                    extract_fields(display_panel, "TV")["screen_size"],
                    expected,
                )

        for title in (
            "Smart TV LG + Painel OLED de Parede 55 polegadas",
            "Smart TV Samsung + Painel QLED para Parede 55 polegadas",
            "Smart TV Samsung + Painel QLED para Sala 55 polegadas",
            "Smart TV Samsung + Painel QLED com Rack 55 polegadas",
            "Smart TV Samsung + Painel QLED com Suporte 55 polegadas",
        ):
            with self.subTest(display_panel_furniture=title):
                display_panel_furniture = {
                    "title": title,
                    "factsheet": [fact("Tamanho da Tela", "60 polegadas")],
                }
                self.assertEqual(
                    extract_fields(display_panel_furniture, "TV")["screen_size"],
                    "60 polegadas",
                )

        for title, target, expected in (
            (
                "Painel OLED 55 polegadas Smart TV LG",
                "65 polegadas",
                "55 polegadas",
            ),
            (
                "Painel QLED 4K 55 polegadas Smart TV Samsung",
                "65 polegadas",
                "55 polegadas",
            ),
            (
                "Painel Mini LED 4K 65 polegadas Smart TV TCL",
                "60 polegadas",
                "65 polegadas",
            ),
        ):
            with self.subTest(panel_before_tv=title):
                panel_before_tv = {
                    "title": title,
                    "factsheet": [fact("Tamanho da Tela", target)],
                }
                self.assertEqual(
                    extract_fields(panel_before_tv, "TV")["screen_size"],
                    expected,
                )

        for title in (
            "Painel OLED para Sala 55 polegadas Smart TV LG",
            "Painel QLED 55 polegadas de Parede Smart TV Samsung",
            "Rack com Painel Mini LED 65 polegadas Smart TV TCL",
            "Suporte com Painel OLED 55 polegadas Smart TV LG",
        ):
            with self.subTest(panel_first_furniture=title):
                panel_first_furniture = {
                    "title": title,
                    "factsheet": [fact("Tamanho da Tela", "65 polegadas")],
                }
                self.assertEqual(
                    extract_fields(panel_first_furniture, "TV")["screen_size"],
                    "",
                )

        for title in (
            "Smart TV LG + Painel QLED 55 polegadas para Sala",
            "Smart TV LG + Painel OLED 55 polegadas de Parede",
            "Smart TV LG + Painel OLED 55 polegadas com Rack",
            "Smart TV LG + Painel QLED 55 polegadas com Suporte",
        ):
            with self.subTest(furniture_marker_after_size=title):
                furniture_after_size = {
                    "title": title,
                    "factsheet": [fact("Tamanho da Tela", "65 polegadas")],
                }
                self.assertEqual(
                    extract_fields(furniture_after_size, "TV")["screen_size"],
                    "65 polegadas",
                )

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

    def test_confirmed_raw_energy_values_are_not_over_cleaned(self):
        cases = (
            (
                [fact("Consumo Aproximado de Energia", "1")],
                "1",
            ),
            (
                [
                    fact("Consumo Aproximado de Energia", "Máximo: 130W"),
                    fact("Consumo Aproximado de Energia", "0,5W"),
                ],
                "Máximo: 130W,0,5W",
            ),
            (
                [
                    fact("Consumo Aproximado de Energia", "<1 W"),
                    fact("Consumo Aproximado de Energia", "8"),
                ],
                "<1 W,8",
            ),
        )
        for factsheet, expected in cases:
            with self.subTest(expected=expected):
                item = {
                    "title": "Smart TV 55 polegadas",
                    "factsheet": factsheet,
                }
                self.assertEqual(
                    extract_fields(item, "TV")["estimated_annual_electricity_use"],
                    expected,
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
        self.assertEqual(extract_fields(mixed, 'REF')['ref_capacity'], '395L')
        mixed['factsheet'] = [fact('Capacidade', '84')]
        self.assertEqual(extract_fields(mixed, 'REF')['ref_capacity'], '395L')
        item = {
            "title": "Geladeira 395L",
            "factsheet": [
                fact("Capacidade do Freezer (L)", "84"),
                fact("Capacidade do Refrigerador (L)", "305"),
            ],
        }
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "395L")
        item["factsheet"] = [fact("Capacidade do Freezer (L)", "84")]
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "395L")
        item["factsheet"] = [fact("Capacidade", "De 301 a 400 litros")]
        self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "395L")
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

    def test_ref_title_door_count_does_not_override_structured_capacity(self):
        cases = (
            ("Refrigerador 1 Porta Consul CRA30", "300 L"),
            ("Freezer 2 Portas Horizontal Electrolux", "250 L"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                item = {
                    "title": title,
                    "factsheet": [fact("Capacidade total", expected)],
                }
                self.assertEqual(
                    extract_fields(item, "REF")["ref_capacity"],
                    expected,
                )

        explicit_unitless = {
            "title": "Geladeira Capacidade do Refrigerador: 305",
            "factsheet": [fact("Capacidade total", "400 L")],
        }
        self.assertEqual(
            extract_fields(explicit_unitless, "REF")["ref_capacity"],
            "305",
        )

    def test_ref_capacity_prefers_only_one_exact_title_volume(self):
        exact_title = {
            "title": "Geladeira Electrolux Side by Side 526L",
            "factsheet": [fact("Capacidade total", "Acima de 501 litros")],
        }
        self.assertEqual(extract_fields(exact_title, "REF")["ref_capacity"], "526L")

        can_count_target = {
            "title": "Geladeira Portatil para Van 31 Litros",
            "factsheet": [fact("Capacidade", "44 latas")],
        }
        self.assertEqual(
            extract_fields(can_count_target, "REF")["ref_capacity"],
            "31 Litros",
        )

        can_only_title = {
            "title": "Geladeira para 22 latas de 473 ml",
            "factsheet": [fact("Capacidade total", "31 litros")],
        }
        self.assertEqual(
            extract_fields(can_only_title, "REF")["ref_capacity"],
            "31 litros",
        )

        only_cans = {
            "title": "Geladeira para 22 latas de 473 ml",
            "factsheet": [],
        }
        self.assertEqual(extract_fields(only_cans, "REF")["ref_capacity"], "")

        can_and_volume_title = {
            "title": "Geladeira para 22 latas de 473 ml com 18 litros",
            "factsheet": [fact("Capacidade total", "De 1 a 20 litros")],
        }
        self.assertEqual(
            extract_fields(can_and_volume_title, "REF")["ref_capacity"],
            "18 litros",
        )

        for title in (
            "Adega Climatizada 12 Garrafas de 750ml",
            "Adega para garrafas de 750 ml",
            "Cervejeira 96 latas (350ml)",
            "Frigobar para 6 garrafas 500 ml",
        ):
            with self.subTest(container_title=title):
                container_size = {
                    "title": title,
                    "factsheet": [fact("Capacidade total", "33 Litros")],
                }
                self.assertEqual(
                    extract_fields(container_size, "REF")["ref_capacity"],
                    "33 Litros",
                )

        uncounted_bottle_size = {
            "title": "Adega para garrafas de 750 ml",
            "factsheet": [fact("Capacidade total", "300L")],
        }
        self.assertEqual(
            extract_fields(uncounted_bottle_size, "REF")["ref_capacity"],
            "300L",
        )

        standalone_ml = {
            "title": "Mini Geladeira compacta 1040 ml",
            "factsheet": [fact("Capacidade total", "De 1 a 2 litros")],
        }
        self.assertEqual(
            extract_fields(standalone_ml, "REF")["ref_capacity"],
            "1040 ml",
        )

        ambiguous_title = {
            "title": "Geladeira Freezer 84L Refrigerador 305L",
            "factsheet": [fact("Capacidade total", "389 L")],
        }
        self.assertEqual(
            extract_fields(ambiguous_title, "REF")["ref_capacity"],
            "305L",
        )

        freezer_component_title = {
            "title": "Geladeira com freezer de 84L",
            "factsheet": [fact("Capacidade total", "300L")],
        }
        self.assertEqual(
            extract_fields(freezer_component_title, "REF")["ref_capacity"],
            "84L",
        )

        for title in (
            "Geladeira 84L do Freezer 305L do Refrigerador",
            "Geladeira 84L Freezer 305L Refrigerador",
            "Geladeira Freezer 84L Refrigerador 305L",
        ):
            with self.subTest(compartment_orientation=title):
                item = {
                    "title": title,
                    "factsheet": [fact("Capacidade total", "De 301 a 400 litros")],
                }
                self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "305L")

        main_with_components = {
            "title": "Geladeira 426L Refrigerador 296L Freezer 130L",
            "factsheet": [fact("Capacidade total", "De 401 a 500 litros")],
        }
        self.assertEqual(
            extract_fields(main_with_components, "REF")["ref_capacity"],
            "426L",
        )

        explicit_freezer_only = {
            "title": "Geladeira Capacidade do Freezer 84L",
            "factsheet": [fact("Capacidade total", "De 301 a 400 litros")],
        }
        self.assertEqual(
            extract_fields(explicit_freezer_only, "REF")["ref_capacity"],
            "84L",
        )

        freezer_product_title = {
            "title": "Freezer Vertical 84L",
            "factsheet": [fact("Capacidade", "De 1 a 100 litros")],
        }
        self.assertEqual(
            extract_fields(freezer_product_title, "REF")["ref_capacity"],
            "84L",
        )

        ranged_title = {
            "title": "Geladeira de 401 a 500 litros",
            "factsheet": [fact("Capacidade total", "426L")],
        }
        self.assertEqual(extract_fields(ranged_title, "REF")["ref_capacity"], "426L")

    def test_ref_capacity_ignores_can_size_and_keeps_actual_volume(self):
        for connector in (
            "de", "com", "x", "/", "-", "\u2013", "\u2014", ":", ";", "+", ",", "&"
        ):
            with self.subTest(title_connector=connector):
                item = {
                    "title": f"Geladeira 22 latas {connector} 473 ml - 18 litros",
                    "factsheet": [fact("Capacidade total", "De 1 a 20 litros")],
                }
                self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "18 litros")
                item["factsheet"] = []
                self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "18 litros")

            with self.subTest(target_fallback_connector=connector):
                item = {
                    "title": f"Geladeira 22 latas {connector} 473 ml",
                    "factsheet": [fact("Capacidade total", "18 litros")],
                }
                self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "18 litros")

        for target in ("18 litros / 22 latas", "22 latas, 18 litros"):
            with self.subTest(composite_target=target):
                item = {
                    "title": "Geladeira Portatil sem volume no titulo",
                    "factsheet": [fact("Capacidade", target)],
                }
                self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "18 litros")

        total_after_count = {
            "title": "Geladeira 22 latas - 18 litros",
            "factsheet": [fact("Capacidade total", "De 1 a 20 litros")],
        }
        self.assertEqual(
            extract_fields(total_after_count, "REF")["ref_capacity"],
            "18 litros",
        )
        sub_liter_can_size = {
            "title": "Geladeira 22 latas - 0,473L",
            "factsheet": [fact("Capacidade total", "18 litros")],
        }
        self.assertEqual(
            extract_fields(sub_liter_can_size, "REF")["ref_capacity"],
            "18 litros",
        )

        bottle_unit_size = {
            "title": "Adega para 6 garrafas / 1,5L",
            "factsheet": [fact("Capacidade total", "33L")],
        }
        self.assertEqual(
            extract_fields(bottle_unit_size, "REF")["ref_capacity"],
            "33L",
        )
        bottle_unit_size["factsheet"] = []
        self.assertEqual(
            extract_fields(bottle_unit_size, "REF")["ref_capacity"],
            "",
        )

        main_capacity_after_bottle_count = {
            "title": "Adega 12 Garrafas 33L",
            "factsheet": [fact("Capacidade total", "De 31 a 40 litros")],
        }
        self.assertEqual(
            extract_fields(main_capacity_after_bottle_count, "REF")["ref_capacity"],
            "33L",
        )

        for title in (
            "Adega para garrafas de 3L",
            "Adega para garrafas com 5L",
            "Adega para garrafas x 4L",
            "Adega para 6 garrafas / 3L",
            "Adega para 6 garrafas / 9L",
        ):
            with self.subTest(extended_container_unit=title):
                container_unit = {
                    "title": title,
                    "factsheet": [fact("Capacidade total", "33L")],
                }
                self.assertEqual(
                    extract_fields(container_unit, "REF")["ref_capacity"],
                    "33L",
                )
                container_unit["factsheet"] = []
                self.assertEqual(
                    extract_fields(container_unit, "REF")["ref_capacity"],
                    "",
                )

        no_connector_main_capacity = {
            "title": "Cervejeira 6 latas 4L",
            "factsheet": [fact("Capacidade total", "De 1 a 10 litros")],
        }
        self.assertEqual(
            extract_fields(no_connector_main_capacity, "REF")["ref_capacity"],
            "4L",
        )

        punctuation_main_capacity = {
            "title": "Adega 6 garrafas / 10L",
            "factsheet": [fact("Capacidade total", "33L")],
        }
        self.assertEqual(
            extract_fields(punctuation_main_capacity, "REF")["ref_capacity"],
            "10L",
        )

    def test_ref_title_priority_excludes_approximate_and_water_components(self):
        approximate = {
            "title": "Mini Geladeira 0,95 p\u00e9s c\u00fabicos (aprox. 26,9 litros)",
            "factsheet": [fact("Capacidade total", "27L")],
        }
        self.assertEqual(extract_fields(approximate, "REF")["ref_capacity"], "27L")

        for title in (
            "Geladeira com Dispenser de Agua 2,5L",
            "Geladeira com Reservatorio de Agua 3L",
        ):
            with self.subTest(auxiliary_water_title=title):
                item = {
                    "title": title,
                    "factsheet": [fact("Capacidade total", "400L")],
                }
                self.assertEqual(extract_fields(item, "REF")["ref_capacity"], "400L")

    def test_ref_exact_targets_precede_category_bands(self):
        refrigerator_exact = {
            "title": "Geladeira Electrolux",
            "factsheet": [
                fact("Capacidade total", "De 401 a 500 litros"),
                fact("Capacidade do Refrigerador", "305L"),
                fact("Capacidade do Freezer", "84L"),
            ],
        }
        self.assertEqual(
            extract_fields(refrigerator_exact, "REF")["ref_capacity"],
            "305L",
        )

        freezer_exact = {
            "title": "Geladeira Electrolux",
            "factsheet": [
                fact("Capacidade total", "Acima de 501 litros"),
                fact("Capacidade do Freezer", "84L"),
            ],
        }
        self.assertEqual(
            extract_fields(freezer_exact, "REF")["ref_capacity"],
            "84L",
        )

        exact_and_limit = {
            "title": "Geladeira Electrolux",
            "factsheet": [
                fact("Capacidade", "At\u00e9 260 litros"),
                fact("Capacidade", "260 litros"),
            ],
        }
        self.assertEqual(
            extract_fields(exact_and_limit, "REF")["ref_capacity"],
            "260 litros",
        )

        qualified_total_over_freezer = {
            "title": "Geladeira Electrolux",
            "factsheet": [
                fact("Capacidade total", "At\u00e9 260 litros"),
                fact("Capacidade do Freezer", "84L"),
            ],
        }
        self.assertEqual(
            extract_fields(qualified_total_over_freezer, "REF")["ref_capacity"],
            "At\u00e9 260 litros",
        )

        cross_level_exact = {
            "title": "Geladeira Electrolux",
            "factsheet": [
                fact("Capacidade total", "At\u00e9 260 litros"),
                fact("Capacidade", "260 litros"),
            ],
        }
        self.assertEqual(
            extract_fields(cross_level_exact, "REF")["ref_capacity"],
            "260 litros",
        )

        for band in (
            "De 401 a 500 litros",
            "Acima de 501 litros",
        ):
            with self.subTest(band_only=band):
                band_only = {
                    "title": "Geladeira Electrolux",
                    "factsheet": [fact("Capacidade", band)],
                }
                self.assertEqual(
                    extract_fields(band_only, "REF")["ref_capacity"],
                    band,
                )

        qualified_only = {
            "title": "Geladeira Electrolux",
            "factsheet": [fact("Capacidade total", "At\u00e9 260 litros")],
        }
        self.assertEqual(
            extract_fields(qualified_only, "REF")["ref_capacity"],
            "At\u00e9 260 litros",
        )

        tank_volume = {
            "title": "Geladeira tanque 5L",
            "factsheet": [fact("Capacidade total", "400L")],
        }
        self.assertEqual(
            extract_fields(tank_volume, "REF")["ref_capacity"],
            "400L",
        )
        tank_volume["factsheet"] = []
        self.assertEqual(
            extract_fields(tank_volume, "REF")["ref_capacity"],
            "",
        )

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

    def test_ldy_title_priority_requires_one_exact_capacity(self):
        exact_mass = {
            "title": "Maquina de Lavar Philco 14kg",
            "factsheet": [fact("Capacidade", "Acima de 16 kg")],
        }
        self.assertEqual(extract_fields(exact_mass, "LDY")["ldy_capacity"], "14kg")

        exact_compact_volume = {
            "title": "Mini Maquina de Lavar Portatil 12 Litros",
            "path": "/mini-maquina/p/sample/ed/mmlp/",
            "factsheet": [fact("Capacidade", "De 11 a 15kg")],
        }
        self.assertEqual(
            extract_fields(exact_compact_volume, "LDY")["ldy_capacity"],
            "12 Litros",
        )

        ambiguous_mass = {
            "title": "Lava e Seca 11kg Lava e 7kg Seca",
            "factsheet": [fact("Capacidade de lavagem", "10kg")],
        }
        self.assertEqual(extract_fields(ambiguous_mass, "LDY")["ldy_capacity"], "10kg")

        ranged_mass = {
            "title": "Lavadora 11-15kg",
            "factsheet": [fact("Capacidade de lavagem", "10kg")],
        }
        self.assertEqual(extract_fields(ranged_mass, "LDY")["ldy_capacity"], "10kg")

        qualified_mass = {
            "title": "Lavadora acima de 16kg",
            "factsheet": [fact("Capacidade de lavagem", "14kg")],
        }
        self.assertEqual(extract_fields(qualified_mass, "LDY")["ldy_capacity"], "14kg")

        water_volume = {
            "title": "Mini Lavadora com economia de 20 litros de agua",
            "factsheet": [fact("Capacidade de lavagem", "2kg")],
        }
        self.assertEqual(extract_fields(water_volume, "LDY")["ldy_capacity"], "2kg")

        product_weight = {
            "title": "Mini Lavadora Portatil Peso 2kg",
            "factsheet": [fact("Capacidade de lavagem", "1kg")],
        }
        self.assertEqual(extract_fields(product_weight, "LDY")["ldy_capacity"], "1kg")

        product_weight_and_tub = {
            "title": "Mini Lavadora Portatil Peso 2kg Tanque 12 litros",
            "factsheet": [fact("Capacidade de lavagem", "1kg")],
        }
        self.assertEqual(
            extract_fields(product_weight_and_tub, "LDY")["ldy_capacity"],
            "12 litros",
        )

        water_reservoir = {
            "title": "Mini Lavadora com reservatorio de agua capacidade 12 litros",
            "factsheet": [fact("Capacidade de lavagem", "2kg")],
        }
        self.assertEqual(extract_fields(water_reservoir, "LDY")["ldy_capacity"], "2kg")

        for title in (
            "Lava e Seca Samsung 7kg de secagem",
            "Lava e Seca Samsung capacidade de secagem 7kg",
            "Lava e Seca Samsung 7kg para secar",
            "Lava e Seca Samsung capacidade para secar 7kg",
            "Lava e Seca Samsung seca 7kg",
        ):
            with self.subTest(drying_capacity_title=title):
                drying_capacity = {
                    "title": title,
                    "factsheet": [fact("Capacidade de lavagem", "11kg")],
                }
                self.assertEqual(
                    extract_fields(drying_capacity, "LDY")["ldy_capacity"],
                    "11kg",
                )
                drying_capacity["factsheet"] = []
                self.assertEqual(
                    extract_fields(drying_capacity, "LDY")["ldy_capacity"],
                    "",
                )

        plain_washing_capacity = {
            "title": "Lava e Seca Samsung 11kg",
            "factsheet": [fact("Capacidade de lavagem", "10kg")],
        }
        self.assertEqual(
            extract_fields(plain_washing_capacity, "LDY")["ldy_capacity"],
            "11kg",
        )

        dry_clothes_washing_capacity = {
            "title": "Lava e Seca Samsung capacidade de roupa seca 11kg",
            "factsheet": [fact("Capacidade de lavagem", "10kg")],
        }
        self.assertEqual(
            extract_fields(dry_clothes_washing_capacity, "LDY")["ldy_capacity"],
            "11kg",
        )
        dry_clothes_washing_capacity["factsheet"] = []
        self.assertEqual(
            extract_fields(dry_clothes_washing_capacity, "LDY")["ldy_capacity"],
            "11kg",
        )

        labeled_washing_and_drying = {
            "title": "Lava e Seca Samsung 11kg lavagem / 7kg secagem",
            "factsheet": [fact("Capacidade de lavagem", "10kg")],
        }
        self.assertEqual(
            extract_fields(labeled_washing_and_drying, "LDY")["ldy_capacity"],
            "11kg",
        )

        for title in (
            "Lava e Seca Samsung lavagem 11kg secagem 7kg",
            "Lava e Seca Samsung 11kg secagem 7kg",
        ):
            with self.subTest(next_drying_measurement=title):
                washing_before_drying = {
                    "title": title,
                    "factsheet": [fact("Capacidade de lavagem", "10kg")],
                }
                self.assertEqual(
                    extract_fields(washing_before_drying, "LDY")["ldy_capacity"],
                    "11kg",
                )
                washing_before_drying["factsheet"] = []
                self.assertEqual(
                    extract_fields(washing_before_drying, "LDY")["ldy_capacity"],
                    "11kg",
                )

        unlabeled_dual_capacity = {
            "title": "Lava e Seca Samsung 11kg/7kg",
            "factsheet": [fact("Capacidade de lavagem", "10kg")],
        }
        self.assertEqual(
            extract_fields(unlabeled_dual_capacity, "LDY")["ldy_capacity"],
            "10kg",
        )

    def test_ldy_standalone_dryer_does_not_use_washing_capacity(self):
        dryer = {
            "title": "Secadora de Roupas Electrolux 11kg",
            "factsheet": [fact("Capacidade de lavagem", "11kg")],
            "description": "Capacidade de lavagem: 11kg",
        }
        self.assertEqual(
            extract_fields(dryer, "LDY")["ldy_capacity"],
            "",
        )

        washer_dryer = {
            "title": "Lava e Seca Electrolux 11kg",
            "factsheet": [fact("Capacidade de lavagem", "10kg")],
        }
        self.assertEqual(
            extract_fields(washer_dryer, "LDY")["ldy_capacity"],
            "11kg",
        )

    def test_ldy_loading_type_prefers_one_explicit_title_direction(self):
        front_title = {
            "title": "Lavadora Electrolux Front Load 11kg",
            "factsheet": [fact("Abertura da Tampa", "Superior")],
        }
        self.assertEqual(
            extract_fields(front_title, "LDY")["ldy_loading_type"],
            "Front load",
        )

        top_title = {
            "title": "Lavadora Electrolux carga superior 11kg",
            "factsheet": [fact("Tipo", "Front Loading automatica")],
        }
        self.assertEqual(
            extract_fields(top_title, "LDY")["ldy_loading_type"],
            "Top load",
        )

        frontal_title = {
            "title": "Lavadora Electrolux carga frontal 11kg",
            "factsheet": [fact("Abertura da Tampa", "Superior")],
        }
        self.assertEqual(
            extract_fields(frontal_title, "LDY")["ldy_loading_type"],
            "Front load",
        )

        ambiguous_title = {
            "title": "Lavadora Top Load ou Front Load 11kg",
            "factsheet": [fact("Tipo", "Front Loading automatica")],
        }
        self.assertEqual(
            extract_fields(ambiguous_title, "LDY")["ldy_loading_type"],
            "Front load",
        )

        ambiguous_title["factsheet"] = [fact("Abertura da Tampa", "Superior")]
        self.assertEqual(
            extract_fields(ambiguous_title, "LDY")["ldy_loading_type"],
            "Top load",
        )

    def test_ldy_title_rejects_expanded_weight_contexts(self):
        cases = (
            ("Peso do Produto 2kg", "1kg"),
            ("Peso da M\u00e1quina 2kg", "9kg"),
            ("Peso Total 2kg", "1kg"),
            ("pesa 2kg", "9kg"),
        )
        for wording, target in cases:
            with self.subTest(weight_wording=wording):
                item = {
                    "title": f"Lavadora Portatil {wording}",
                    "factsheet": [fact("Capacidade de lavagem", target)],
                }
                self.assertEqual(extract_fields(item, "LDY")["ldy_capacity"], target)

        exact = {
            "title": "Lavadora Colormaq 6kg",
            "factsheet": [fact("Capacidade de lavagem", "9kg")],
        }
        self.assertEqual(extract_fields(exact, "LDY")["ldy_capacity"], "6kg")

        for title in (
            "Lavadora 14kg Peso do Produto 50kg",
            "Peso do Produto 50kg Lavadora 14kg",
        ):
            with self.subTest(capacity_and_weight_order=title):
                item = {
                    "title": title,
                    "factsheet": [fact("Capacidade de lavagem", "De 11 a 15kg")],
                }
                self.assertEqual(extract_fields(item, "LDY")["ldy_capacity"], "14kg")

        weight_suffix = {
            "title": "Mini Lavadora Portatil 2kg de peso",
            "factsheet": [fact("Capacidade de lavagem", "9kg")],
        }
        self.assertEqual(
            extract_fields(weight_suffix, "LDY")["ldy_capacity"],
            "9kg",
        )

        compact_tank = {
            "title": "Mini Lavadora Peso 2kg Tanque 12L",
            "path": "/mini-lavadora/p/sample/ed/mmlp/",
            "factsheet": [fact("Capacidade de lavagem", "2kg")],
        }
        self.assertEqual(
            extract_fields(compact_tank, "LDY")["ldy_capacity"],
            "12L",
        )

        for title in (
            "Mini Lavadora 12L Reservatorio de Agua",
            "Mini Lavadora 12L Consumo de Agua",
        ):
            with self.subTest(trailing_water_context=title):
                item = {
                    "title": title,
                    "path": "/mini-lavadora/p/sample/ed/mmlp/",
                    "factsheet": [fact("Capacidade de lavagem", "2kg")],
                }
                self.assertEqual(extract_fields(item, "LDY")["ldy_capacity"], "2kg")

    def test_ldy_loading_title_variants_and_negative_contexts(self):
        front_titles = (
            "Lavadora Electrolux Front-Load 11kg",
            "Lavadora Electrolux Front Loader 11kg",
            "M\u00e1quina de Lavar Frontal Electrolux 11kg",
            "Lavadora Frontal Electrolux 11kg",
            "Lava e Seca Frontal Electrolux 11kg",
        )
        for title in front_titles:
            with self.subTest(front_title=title):
                item = {
                    "title": title,
                    "factsheet": [fact("Abertura da Tampa", "Superior")],
                }
                self.assertEqual(
                    extract_fields(item, "LDY")["ldy_loading_type"],
                    "Front load",
                )

        non_front_titles = (
            "Lavadora sem abertura frontal 11kg",
            "Lavadora n\u00e3o possui abertura frontal 11kg",
            "Lavadora n\u00e3o tem abertura frontal 11kg",
            "Lavadora n\u00e3o \u00e9 Front Load 11kg",
            "Lavadora sem ser Front Load 11kg",
            "Lavadora n\u00e3o \u00e9 do tipo Front Load 11kg",
            "Lavadora n\u00e3o possui sistema de carga frontal 11kg",
            "Lavadora n\u00e3o \u00e9 uma m\u00e1quina de lavar frontal 11kg",
            "Lavadora com painel frontal 11kg",
            "Lavadora com porta frontal 11kg",
        )
        for title in non_front_titles:
            with self.subTest(non_front_title=title):
                item = {
                    "title": title,
                    "factsheet": [fact("Abertura da Tampa", "Superior")],
                }
                self.assertEqual(
                    extract_fields(item, "LDY")["ldy_loading_type"],
                    "Top load",
                )

        for title in (
            "Lavadora n\u00e3o \u00e9 Front Load, mas Front Load 11kg",
            "Lavadora n\u00e3o \u00e9 Top Load, mas Front Load 11kg",
            "Lavadora abertura da tampa frontal 11kg",
        ):
            with self.subTest(positive_clause_or_lid=title):
                item = {
                    "title": title,
                    "factsheet": [fact("Abertura da Tampa", "Superior")],
                }
                self.assertEqual(
                    extract_fields(item, "LDY")["ldy_loading_type"],
                    "Front load",
                )

        top_negated = {
            "title": "Lavadora n\u00e3o \u00e9 Top Load 11kg",
            "factsheet": [fact("Abertura da Tampa", "Frontal")],
        }
        self.assertEqual(
            extract_fields(top_negated, "LDY")["ldy_loading_type"],
            "Front load",
        )

    def test_ldy_loading_description_negation_and_official_porta_values(self):
        negated_description = {
            "title": "Lavadora Samsung 11kg",
            "factsheet": [],
            "description": (
                "Nao possui abertura frontal; abertura da tampa superior"
            ),
        }
        self.assertEqual(
            extract_fields(negated_description, "LDY")["ldy_loading_type"],
            "Top load",
        )

        for value in ("Porta frontal", "Abertura pela porta frontal"):
            with self.subTest(official_loading_value=value):
                official_value = {
                    "title": "Lavadora Samsung 11kg",
                    "factsheet": [fact("Acesso ao cesto", value)],
                }
                self.assertEqual(
                    extract_fields(official_value, "LDY")["ldy_loading_type"],
                    "Front load",
                )

    def test_ldy_mini_liter_capacity_uses_exact_measurement_not_item_count(self):
        for raw in ("6,5 L", "6,5L"):
            with self.subTest(raw=raw):
                item = {
                    "title": "Mini Maquina de Lavar Portatil",
                    "path": "/mini-maquina/p/sample/ed/mmlp/",
                    "factsheet": [fact("Capacidade de Lavagem", raw)],
                }
                self.assertEqual(extract_fields(item, "LDY")["ldy_capacity"], raw)

        approximate = {
            "title": "Mini Maquina de Lavar Portatil",
            "path": "/mini-maquina/p/sample/ed/mmlp/",
            "factsheet": [fact("Capacidade de Lavagem", "Ate 6,5 L")],
        }
        self.assertEqual(
            extract_fields(approximate, "LDY")["ldy_capacity"],
            "Ate 6,5 L",
        )

        alias = {
            "title": "Lavadora Mini Portatil Dobravel",
            "factsheet": [fact("Capacidade", "6,5L")],
        }
        self.assertEqual(extract_fields(alias, "LDY")["ldy_capacity"], "6,5L")

        mmlp_without_mini_title = {
            "title": "Maquina de Lavar Silenciosa Verde",
            "path": "/maquina-de-lavar/p/sample/ed/mmlp/",
            "factsheet": [fact("Capacidade de Lavagem", "6,5L")],
            "description": "Capacidade de Lavagem: 1 toalha de banho de bebe",
        }
        self.assertEqual(
            extract_fields(mmlp_without_mini_title, "LDY")["ldy_capacity"],
            "6,5L",
        )

        description = {
            "title": "Mini Maquina de Lavar Portatil",
            "path": "/mini-maquina/p/sample/ed/mmlp/",
            "factsheet": [],
            "description": (
                "Capacidade de Lavagem: 6,5L; "
                "Capacidade de Lavagem: 1 toalha de banho de bebe "
                "8 roupas de bebe 4 babadores 12 pares de meias"
            ),
        }
        self.assertEqual(
            extract_fields(description, "LDY")["ldy_capacity"],
            "6,5L",
        )
        for count_text in (
            "1 toalha de banho de bebe",
            "1x Mini Maquina de Lavar",
        ):
            with self.subTest(count_text=count_text):
                count_only = {
                    "title": "Mini Maquina de Lavar Portatil",
                    "path": "/mini-maquina/p/sample/ed/mmlp/",
                    "factsheet": [],
                    "description": f"Capacidade de Lavagem: {count_text}",
                }
                self.assertEqual(
                    extract_fields(count_only, "LDY")["ldy_capacity"],
                    "",
                )

        title_only = {
            "title": "Mini Maquina de Lavar 6,5L Portatil e Dobravel",
            "path": "/mini-maquina/p/sample/ed/mmlp/",
            "factsheet": [],
        }
        self.assertEqual(extract_fields(title_only, "LDY")["ldy_capacity"], "6,5L")
        water_title = {
            "title": "Mini Lavadora com economia de 20 litros de agua",
            "factsheet": [],
        }
        self.assertEqual(extract_fields(water_title, "LDY")["ldy_capacity"], "")

        bare_one = {
            "title": "Mini Maquina de Lavar Portatil",
            "path": "/mini-maquina/p/sample/ed/mmlp/",
            "factsheet": [fact("Capacidade de Lavagem", "1")],
        }
        self.assertEqual(extract_fields(bare_one, "LDY")["ldy_capacity"], "1")

        mass_title_boundaries = (
            ("Lavadora Portatil 1.2Kg", "26 L", "1.2Kg"),
            ("Tanquinho Maquina de Lavar 10kg", "96 litros", "10kg"),
            ("Lavadora Ultrassonica 3kg", "20 litros", "3kg"),
        )
        for title, target, expected in mass_title_boundaries:
            with self.subTest(title=title, target=target):
                item = {
                    "title": title,
                    "path": "/maquina-de-lavar/p/sample/ed/mmlp/",
                    "factsheet": [fact("Capacidade de Lavagem", target)],
                }
                self.assertEqual(
                    extract_fields(item, "LDY")["ldy_capacity"],
                    expected,
                )

        weak_portable_context = {
            "title": "Lavadora Portatil",
            "factsheet": [fact("Capacidade de Lavagem", "26 L")],
        }
        self.assertEqual(
            extract_fields(weak_portable_context, "LDY")["ldy_capacity"],
            "",
        )

    def test_ldy_mini_liter_graphql_and_next_data_paths_match(self):
        item = {
            "id": "sample",
            "title": "Maquina de Lavar Silenciosa Verde",
            "path": "/mini-maquina/p/sample/ed/mmlp/",
            "description": (
                "Capacidade de Lavagem: 6,5L; "
                "Capacidade de Lavagem: 1 toalha de banho de bebe"
            ),
            "factsheet": [fact("Capacidade de Lavagem", "6,5L")],
            "attributes": [],
            "offers": [],
            "rating": {},
        }
        next_item = dict(item)
        next_item.pop("path")
        payload = {"props": {"pageProps": {"data": {"item": next_item}}}}
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            graphql = _detail_from_item(item)
            next_data = _parse_magalu_next_detail(
                html,
                "https://www.magazineluiza.com.br",
                "https://www.magazineluiza.com.br/p/sample/ed/mmlp/",
            )
        self.assertEqual(graphql["ldy_capacity"], "6,5L")
        self.assertEqual(next_data["ldy_capacity"], "6,5L")

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
        item['title'] = 'Lavadora automatica'
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
        self.assertEqual(graphql["ref_capacity"], "395L")
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
