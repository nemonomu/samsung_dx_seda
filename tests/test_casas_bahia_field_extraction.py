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

    def test_tv_title_screen_priority_is_shared_by_product_source_and_next(self):
        cases = (
            (
                'wall_mount_measurement',
                'Smart TV Samsung 4K + Suporte de Parede 32 polegadas',
                '50 polegadas',
                f'50{chr(34)}',
            ),
            (
                'panel_measurement',
                'Smart TV Samsung 4K + Painel para Sala 55',
                '50 polegadas',
                f'50{chr(34)}',
            ),
            (
                'rack_measurement',
                'Smart TV Samsung 4K + Rack compatível 65',
                '50 polegadas',
                f'50{chr(34)}',
            ),
            (
                'base_measurement',
                'Smart TV Samsung 4K + Base Universal 43',
                '50 polegadas',
                f'50{chr(34)}',
            ),
            (
                'compact_main_size',
                'Smart TV65',
                '50 polegadas',
                f'65{chr(34)}',
            ),
            (
                'processor_bits',
                'Smart TV LG processador 64 bits 4K',
                '55 polegadas',
                f'55{chr(34)}',
            ),
            (
                'refresh_fps',
                'Smart TV LG atualização 120 FPS 4K',
                '55 polegadas',
                f'55{chr(34)}',
            ),
            (
                'android_storage',
                'Smart TV Android 4K 32 GB',
                '55 polegadas',
                f'55{chr(34)}',
            ),
            (
                'accessory_only_size',
                'Smart TV Samsung 4K + Suporte para TV de 32 polegadas',
                '65 polegadas',
                f'65{chr(34)}',
            ),
            (
                'main_size_with_accessory_range',
                'Smart TV Samsung 32 4K + Suporte para TV de 10 a 40 polegadas',
                '65 polegadas',
                f'32{chr(34)}',
            ),
            (
                "explicit_unit",
                "Smart TV LG 55 Polegadas 4K UHD",
                "65 polegadas",
                "55 Polegadas",
            ),
            (
                "safe_tv_context_number",
                "Smart TV LG 65 4K UHD ThinQ AI",
                "60 polegadas",
                '65"',
            ),
            (
                "google_tv_screen",
                "Smart TV TCL Google TV 55 4K",
                "50 polegadas",
                '55"',
            ),
            (
                "roku_tv_screen",
                "Smart TV Roku TV 43 Full HD",
                "50 polegadas",
                '43"',
            ),
            (
                "google_tv_version_with_target",
                "Smart TV TCL Google TV 12",
                "50 polegadas",
                '50"',
            ),
            (
                "roku_tv_version_with_target",
                "Smart TV Roku TV 12",
                "50 polegadas",
                '50"',
            ),
            (
                "multiple_title_sizes",
                "Smart TV LG 55 e 65 Polegadas 4K",
                "60 polegadas",
                '60"',
            ),
            (
                "non_screen_measurements",
                "Smart TV LG 4K 120Hz 130W 127V",
                "60 polegadas",
                '60"',
            ),
            (
                "warranty_duration",
                "Smart TV Samsung QN65ABC 4K com 10 anos de garantia",
                "65 polegadas",
                '65"',
            ),
            (
                "model_number",
                "Smart TV Samsung QN65Q60D 4K UHD",
                "60 polegadas",
                '60"',
            ),
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            for index, (label, title, target, expected) in enumerate(cases, start=1):
                with self.subTest(case=label, producer="product_source"):
                    data = source(title, [spec("Tamanho da Tela", target)])
                    self.assertEqual(_product_source_detail(data)["screen_size"], expected)

                with self.subTest(case=label, producer="next_data"):
                    product = {
                        "id": index,
                        "name": title,
                        "sku": {"id": str(index)},
                        "specGroups": [
                            {
                                "name": "Especificações Técnicas",
                                "specs": [{"name": "Tamanho da Tela", "value": target}],
                            }
                        ],
                    }
                    payload = {"props": {"pageProps": {"product": product}}}
                    html = (
                        '<script id="__NEXT_DATA__" type="application/json">'
                        + json.dumps(payload, ensure_ascii=False)
                        + "</script>"
                    )
                    detail = _parse_casas_bahia_html_detail(
                        html,
                        "https://www.casasbahia.com.br",
                        f"https://www.casasbahia.com.br/produto/p/{index}",
                    )
                    self.assertEqual(detail["screen_size"], expected)

    def test_tv_title_only_screen_does_not_restore_os_versions(self):
        cases = (
            ("webos_version", "Smart TV LG WebOS 23", ""),
            ("android_version", "Smart TV LG Android 11", ""),
            ("android_tv_version", "Smart TV LG Android TV 11", ""),
            ("google_tv_version", "Smart TV TCL Google TV 12", ""),
            ("roku_tv_version", "Smart TV Roku TV 12", ""),
            ("titan_os_version", "Smart TV Philips Titan OS 10", ""),
            ("vidaa_version", "Smart TV Toshiba Vidaa 23", ""),
            ("size_before_os", "Smart TV 32 Google TV", '32"'),
            (
                "warranty_duration",
                "Smart TV Samsung QN65ABC 4K com 10 anos de garantia",
                "",
            ),
            ("explicit_screen", 'SMART TV LG 43" ThinQ AI', '43"'),
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            for index, (label, title, expected) in enumerate(cases, start=101):
                with self.subTest(case=label, producer="product_source"):
                    self.assertEqual(
                        _product_source_detail(source(title, []))["screen_size"],
                        expected,
                    )

                with self.subTest(case=label, producer="next_data"):
                    product = {
                        "id": index,
                        "name": title,
                        "sku": {"id": str(index)},
                        "specGroups": [],
                    }
                    payload = {"props": {"pageProps": {"product": product}}}
                    html = (
                        '<script id="__NEXT_DATA__" type="application/json">'
                        + json.dumps(payload, ensure_ascii=False)
                        + "</script>"
                    )
                    detail = _parse_casas_bahia_html_detail(
                        html,
                        "https://www.casasbahia.com.br",
                        f"https://www.casasbahia.com.br/produto/p/{index}",
                    )
                    self.assertEqual(detail["screen_size"], expected)

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

    def details_from_product_source_and_next(
        self,
        line,
        title,
        specs,
        description="",
        product_id="edge-fixture",
    ):
        product_source = self.detail(
            line,
            source(title, specs, description),
        )
        product = {
            "id": product_id,
            "name": title,
            "description": description,
            "sku": {"id": product_id},
            "specGroups": [
                {"name": "Especificações Técnicas", "specs": specs}
            ],
        }
        payload = {"props": {"pageProps": {"product": product}}}
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": line}):
            next_detail = _parse_casas_bahia_html_detail(
                html,
                "https://www.casasbahia.com.br",
                f"https://www.casasbahia.com.br/produto/p/{product_id}",
            )
        return product_source, next_detail

    def test_edge_contracts_are_shared_by_product_source_and_next(self):
        loading_cases = (
            (
                "description_negation",
                "Lavadora Samsung 11kg",
                [],
                "Esta lavadora nao possui abertura frontal; abertura da tampa superior.",
                "Top load",
            ),
            (
                "official_porta_frontal",
                "Lavadora Samsung 11kg",
                [spec("Acesso ao cesto", "Porta frontal")],
                "",
                "Front load",
            ),
            (
                "official_abertura_pela_porta_frontal",
                "Lavadora Samsung 11kg",
                [spec("Acesso ao cesto", "Abertura pela porta frontal")],
                "",
                "Front load",
            ),
            (
                "title_porta_frontal_is_not_loading",
                "Lavadora Samsung com porta frontal 11kg",
                [spec("Acesso ao cesto", "Superior")],
                "",
                "Top load",
            ),
            (
                "title_painel_frontal_is_not_loading",
                "Lavadora Samsung com painel frontal 11kg",
                [spec("Acesso ao cesto", "Superior")],
                "",
                "Top load",
            ),
        )
        for index, (label, title, specs, description, expected) in enumerate(
            loading_cases,
            start=2101,
        ):
            with self.subTest(case=label):
                for detail in self.details_from_product_source_and_next(
                    "LDY",
                    title,
                    specs,
                    description,
                    str(index),
                ):
                    self.assertEqual(detail["ldy_loading_type"], expected)

        drying_cases = (
            (
                "suffix_de_secagem",
                "Lava e Seca 7kg de secagem",
                "11kg",
                "11kg",
            ),
            (
                "prefix_capacidade_de_secagem",
                "Lava e Seca capacidade de secagem 7kg",
                "11kg",
                "11kg",
            ),
            (
                "suffix_para_secar",
                "Lava e Seca 7kg para secar",
                "11kg",
                "11kg",
            ),
            (
                "prefix_capacidade_para_secar",
                "Lava e Seca capacidade para secar 7kg",
                "11kg",
                "11kg",
            ),
            (
                "repeated_seca",
                "Lava e Seca seca 7kg",
                "11kg",
                "11kg",
            ),
            (
                "brand_between_product_and_drying",
                "Lava e Seca Samsung seca 7kg",
                "11kg",
                "11kg",
            ),
            (
                "model_between_product_and_drying",
                "Lava e Seca Samsung WD11 seca 7kg",
                "11kg",
                "11kg",
            ),
            (
                "maximum_drying_capacity",
                "Lava e Seca capacidade maxima de secagem 7kg",
                "11kg",
                "11kg",
            ),
            (
                "maximum_capacity_to_dry",
                "Lava e Seca capacidade maxima para secar 7kg",
                "11kg",
                "11kg",
            ),
            (
                "single_washing_capacity",
                "Lava e Seca 11kg",
                "10kg",
                "11kg",
            ),
            (
                "dry_clothes_washing_capacity",
                "Lava e Seca capacidade de roupa seca 11kg",
                "10kg",
                "11kg",
            ),
            (
                "labeled_washing_and_drying",
                "Lava e Seca 11kg lavagem / 7kg secagem",
                "10kg",
                "11kg",
            ),
            (
                "washing_then_drying_prefix_binds_next_measurement",
                "Lava e Seca lavagem 11kg secagem 7kg",
                "10kg",
                "11kg",
            ),
            (
                "unlabeled_dual_capacity",
                "Lava e Seca 11kg/7kg",
                "10kg",
                "10kg",
            ),
        )
        for index, (label, title, target, expected) in enumerate(
            drying_cases,
            start=2201,
        ):
            with self.subTest(drying_case=label):
                details = self.details_from_product_source_and_next(
                    "LDY",
                    title,
                    [spec("Capacidade de lavagem", target)],
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["ldy_capacity"], expected)

        ref_cases = (
            (
                "bottle_unit_volume",
                "Adega para 6 garrafas / 1,5L",
                "De 31 a 40 litros",
                "De 31 a 40 litros",
            ),
            (
                "bottle_volume_with_de",
                "Adega para garrafas de 3L",
                "33L",
                "33L",
            ),
            (
                "bottle_volume_with_com",
                "Adega para garrafas com 5L",
                "33L",
                "33L",
            ),
            (
                "bottle_volume_with_x",
                "Adega para garrafas x 4L",
                "33L",
                "33L",
            ),
            (
                "bottle_volume_with_slash",
                "Adega para 6 garrafas / 3L",
                "33L",
                "33L",
            ),
            (
                "headline_total_after_bottle_count",
                "Adega 12 Garrafas 33L",
                "De 31 a 40 litros",
                "33L",
            ),
        )
        for index, (label, title, target, expected) in enumerate(
            ref_cases,
            start=2301,
        ):
            with self.subTest(case=label):
                details = self.details_from_product_source_and_next(
                    "REF",
                    title,
                    [spec("Capacidade total", target)],
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["ref_capacity"], expected)

        ref_band_priority_cases = (
            (
                "generic_band_before_refrigerator",
                [
                    spec("Capacidade", "De 301 a 400 litros"),
                    spec("Capacidade do Refrigerador", "305L"),
                ],
                "305L",
            ),
            (
                "total_alias_band_before_refrigerator",
                [
                    spec("Capacidade total", "De 301 a 400 litros"),
                    spec("Capacidade do Refrigerador", "305L"),
                ],
                "305L",
            ),
            (
                "above_band_before_freezer",
                [
                    spec("Capacidade", "Acima de 501 litros"),
                    spec("Capacidade do Freezer", "210L"),
                ],
                "210L",
            ),
            (
                "below_band_before_refrigerator",
                [
                    spec("Capacidade total", "Abaixo de 400 litros"),
                    spec("Capacidade do Refrigerador", "305L"),
                ],
                "305L",
            ),
            (
                "exact_total_before_refrigerator",
                [
                    spec("Capacidade total", "400L"),
                    spec("Capacidade do Refrigerador", "305L"),
                ],
                "400L",
            ),
            (
                "exact_generic_main_before_refrigerator",
                [
                    spec("Capacidade", "400L"),
                    spec("Capacidade do Refrigerador", "305L"),
                ],
                "400L",
            ),
            (
                "band_only_is_preserved",
                [spec("Capacidade", "De 301 a 400 litros")],
                "De 301 a 400 litros",
            ),
            (
                "same_level_upper_band_yields_to_exact",
                [
                    spec("Capacidade", "Até 260 litros"),
                    spec("Capacidade", "260 litros"),
                ],
                "260 litros",
            ),
            (
                "higher_level_upper_band_yields_to_next_exact",
                [
                    spec("Capacidade total", "Até 260 litros"),
                    spec("Capacidade", "260 litros"),
                ],
                "260 litros",
            ),
            (
                "upper_band_only_is_preserved",
                [spec("Capacidade", "Até 260 litros")],
                "Até 260 litros",
            ),
            (
                "practical_limit_keeps_total_priority_over_freezer",
                [
                    spec("Capacidade total", "Até 260 litros"),
                    spec("Capacidade do Freezer", "84L"),
                ],
                "Até 260 litros",
            ),
        )
        for index, (label, specs, expected) in enumerate(
            ref_band_priority_cases,
            start=2351,
        ):
            with self.subTest(ref_band_priority=label):
                details = self.details_from_product_source_and_next(
                    "REF",
                    "Geladeira Electrolux modelo ABC",
                    specs,
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["ref_capacity"], expected)

        display_panels = ("OLED", "QLED", "LED", "Mini LED", "LCD", "VA", "IPS")
        for index, panel_type in enumerate(display_panels, start=2401):
            with self.subTest(display_panel=panel_type):
                details = self.details_from_product_source_and_next(
                    "TV",
                    f"Smart TV Samsung com Painel {panel_type} 55 polegadas",
                    [spec("Tamanho da Tela", "60 polegadas")],
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["screen_size"], "55 polegadas")

        for index, panel_type in enumerate(display_panels, start=2451):
            with self.subTest(leading_display_panel=panel_type):
                details = self.details_from_product_source_and_next(
                    "TV",
                    f"Painel {panel_type} 55 polegadas Smart TV LG",
                    [spec("Tamanho da Tela", "60 polegadas")],
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["screen_size"], "55 polegadas")

        for detail in self.details_from_product_source_and_next(
            "TV",
            "Painel OLED Evo 55 polegadas Smart TV LG",
            [spec("Tamanho da Tela", "60 polegadas")],
            product_id="2499",
        ):
            self.assertEqual(detail["screen_size"], "55 polegadas")

        accessory_panels = (
            "Painel para Sala 55 polegadas",
            "Painel Rack 55 polegadas",
            "Painel Suporte 55 polegadas",
            "Painel de Parede 55 polegadas",
            "Painel QLED de Parede 55 polegadas",
            "Painel OLED para Sala 55 polegadas",
            "Painel Mini LED Rack 55 polegadas",
            "Painel LCD Suporte 55 polegadas",
        )
        for index, panel_text in enumerate(accessory_panels, start=2501):
            with self.subTest(accessory_panel=panel_text):
                details = self.details_from_product_source_and_next(
                    "TV",
                    f"Smart TV Samsung 4K + {panel_text}",
                    [spec("Tamanho da Tela", "60 polegadas")],
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["screen_size"], '60"')

        suffix_accessory_panels = (
            "Smart TV LG + Painel QLED 55 polegadas para Sala",
            "Smart TV LG + Painel OLED 55 polegadas de Parede",
        )
        for index, title in enumerate(suffix_accessory_panels, start=2551):
            with self.subTest(suffix_accessory_panel=title):
                details = self.details_from_product_source_and_next(
                    "TV",
                    title,
                    [spec("Tamanho da Tela", "65 polegadas")],
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["screen_size"], '65"')

        leading_accessory_panels = (
            "Painel OLED 55 para Sala Smart TV LG",
            "Painel QLED 55 para Parede Smart TV LG",
            "Painel Mini LED 55 Rack Smart TV LG",
            "Painel LCD 55 Suporte Smart TV LG",
            "Painel OLED Evo para Sala 55 polegadas Smart TV LG",
            "Painel OLED Evo 55 polegadas para Sala Smart TV LG",
        )
        for index, title in enumerate(leading_accessory_panels, start=2601):
            with self.subTest(leading_accessory_panel=title):
                details = self.details_from_product_source_and_next(
                    "TV",
                    title,
                    [spec("Tamanho da Tela", "60 polegadas")],
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["screen_size"], "")

        standalone_dryers = (
            (
                "dryer_title_capacity",
                "Secadora de Roupas Samsung 11kg",
                "Capacidade de lavagem",
                "11kg",
            ),
            (
                "dryer_structured_capacity",
                "Secadora de Roupas Samsung",
                "Capacidade",
                "7kg",
            ),
        )
        for index, (label, title, target_label, target) in enumerate(
            standalone_dryers,
            start=2701,
        ):
            with self.subTest(standalone_dryer=label):
                details = self.details_from_product_source_and_next(
                    "LDY",
                    title,
                    [spec(target_label, target)],
                    product_id=str(index),
                )
                for detail in details:
                    self.assertEqual(detail["ldy_capacity"], "")

        for detail in self.details_from_product_source_and_next(
            "REF",
            "Geladeira com tanque 5L",
            [],
            product_id="2751",
        ):
            self.assertEqual(detail["ref_capacity"], "")

        for detail in self.details_from_product_source_and_next(
            "LDY",
            "Mini Lavadora Tanque 12L",
            [],
            product_id="2752",
        ):
            self.assertEqual(detail["ldy_capacity"], "12L")

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
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "395L")
        data = source("Geladeira 470L", [spec("Capacidade total", "De 401 a 500 litros")])
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "470L")
        data = source(
            "Geladeira 490L",
            [spec("Capacidade total", "500 L"), spec("Capacidade total líquida", "490 litros")],
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "490L")
        data = source(
            "Geladeira sem capacidade no título",
            [spec("Capacidade total", "500 L")],
            "Capacidade líquida total: 490 L",
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "490 L")

    def test_ref_type_uses_title_only_after_description_and_specs_are_empty(self):
        title_only = source("Geladeira Electrolux Multidoor 541L", [])
        self.assertEqual(
            self.detail("REF", title_only)["ref_refrigerator_type"],
            "Multidoor",
        )

        description_first = source(
            "Geladeira Electrolux Multidoor 541L",
            [spec("Quantidade de portas", "1 portas")],
            "Geladeira Side by Side com amplo espaco interno",
        )
        self.assertEqual(
            self.detail("REF", description_first)["ref_refrigerator_type"],
            "Side by Side",
        )

        spec_before_title = source(
            "Geladeira Electrolux Multidoor 541L",
            [spec("Quantidade de portas", "1 portas")],
        )
        self.assertEqual(
            self.detail("REF", spec_before_title)["ref_refrigerator_type"],
            "1 portas",
        )

    def test_ref_title_door_count_does_not_override_structured_capacity(self):
        cases = (
            ("Refrigerador 1 Porta Consul CRA30", "300 L"),
            ("Freezer 2 Portas Horizontal Electrolux", "250 L"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                data = source(title, [spec("Capacidade total", expected)])
                self.assertEqual(
                    self.detail("REF", data)["ref_capacity"],
                    expected,
                )

        explicit_unitless = source(
            "Geladeira Capacidade do Refrigerador: 305",
            [spec("Capacidade total", "400 L")],
        )
        self.assertEqual(
            self.detail("REF", explicit_unitless)["ref_capacity"],
            "305",
        )

    def test_ref_single_exact_title_volume_priority_for_reported_cases(self):
        cases = (
            (
                "1567241501",
                "Geladeira Refrigerador DC35A 2 Portas 260 Litros Electrolux - 220V",
                [
                    spec("Capacidade", "260 litros"),
                    spec("Capacidade do Refrigerador", "207"),
                ],
                "capacidade de até 260 litros",
                "260 Litros",
            ),
            (
                "1572356905",
                "Geladeira Electrolux Frost Free Inverter 526L Efficient AutoSense Side by Side Vidro Preto (ES5GB) - 110V",
                [
                    spec("Capacidade total", "Acima de 501 litros"),
                    spec("Capacidade Líquida do Refrigerador", "316L"),
                    spec("Capacidade Líquida do Freezer", "210L"),
                ],
                "",
                "526L",
            ),
            (
                "1572789844",
                "Geladeira Refrigerador HQ Frost Free Inverter Multidoor 426 Litros Cinza HQ-426MDFF - 110V",
                [
                    spec("Capacidade total", "De 401 a 500 litros"),
                    spec("Capacidade Líquida do Refrigerador", "296L"),
                    spec("Capacidade Líquida do Freezer", "130L"),
                ],
                "",
                "426 Litros",
            ),
            (
                "1576385899",
                "Geladeira Electrolux Frost Free 320L Duplex Branca TF38",
                [spec("Capacidade total", "De 301 a 400 litros")],
                "",
                "320L",
            ),
            (
                "1576513849",
                "Geladeira Consul 377L Frost Free Duplex CRM44",
                [spec("Capacidade total", "De 301 a 400 litros")],
                "",
                "377L",
            ),
            (
                "1579437096",
                "Geladeira Consul 455L Frost Free Duplex Inverter CRM53MB",
                [spec("Capacidade total", "De 401 a 500 litros")],
                "",
                "455L",
            ),
            (
                "1580757123",
                "Geladeira 333L Frost Free Duplex CRM40MB Consul",
                [spec("Capacidade total", "De 301 a 400 litros")],
                "",
                "333L",
            ),
        )
        for sku, title, specs, description, expected in cases:
            with self.subTest(sku=sku):
                self.assertEqual(
                    self.detail("REF", source(title, specs, description))["ref_capacity"],
                    expected,
                )

    def test_ref_title_priority_requires_one_unqualified_volume(self):
        cases = (
            (
                "multiple_compartments",
                "Geladeira 305L com Freezer 84L",
                [spec("Capacidade total", "389L")],
                "305L",
            ),
            (
                "qualified",
                "Geladeira com capacidade de até 260 litros",
                [spec("Capacidade total", "250L")],
                "250L",
            ),
            (
                "range",
                "Geladeira de 301 a 400 litros",
                [spec("Capacidade total", "333L")],
                "333L",
            ),
            (
                "approximate_conversion",
                "Mini Geladeira 0,95 pés cúbicos (aprox. 26,9 litros)",
                [spec("Capacidade total", "27L")],
                "27L",
            ),
            (
                "same_volume_repeated",
                "Geladeira Consul 320L Frost Free - 320 Litros",
                [spec("Capacidade total", "De 301 a 400 litros")],
                "320L",
            ),
            (
                "can_size_plus_real_volume",
                "Geladeira Portátil para 22 latas de 473 ml - 18 litros",
                [spec("Capacidade", "22 latas de 473 ml")],
                "18 litros",
            ),
            (
                "can_size_only",
                "Geladeira Portátil para 22 latas de 473 ml",
                [spec("Capacidade total", "18 litros")],
                "18 litros",
            ),
            (
                "can_parenthesized_unit_volume",
                "Cervejeira 96 latas (350ml)",
                [spec("Capacidade total", "82L")],
                "82L",
            ),
            (
                "bottle_unit_volume_without_preposition",
                "Adega 12 garrafas 750 ml",
                [spec("Capacidade total", "33L")],
                "33L",
            ),
            (
                "standalone_milliliters",
                "Mini Geladeira 1040 ml",
                [spec("Capacidade total", "1L")],
                "1040 ml",
            ),
            (
                "refrigerator_freezer_component",
                "Geladeira com freezer de 84L",
                [spec("Capacidade total", "300L")],
                "84L",
            ),
            (
                "freezer_product",
                "Freezer Vertical 84L",
                [spec("Capacidade total", "De 51 a 100 litros")],
                "84L",
            ),
        )
        for label, title, specs, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    self.detail("REF", source(title, specs))["ref_capacity"],
                    expected,
                )

        for title in (
            "Geladeira 84L do Freezer 305L do Refrigerador",
            "Geladeira 84L Freezer 305L Refrigerador",
            "Geladeira Freezer 84L Refrigerador 305L",
        ):
            with self.subTest(compartment_orientation=title):
                data = source(title, [spec("Capacidade total", "De 301 a 400 litros")])
                self.assertEqual(self.detail("REF", data)["ref_capacity"], "305L")

        main_with_components = source(
            "Geladeira 426L Refrigerador 296L Freezer 130L",
            [spec("Capacidade total", "De 401 a 500 litros")],
        )
        self.assertEqual(
            self.detail("REF", main_with_components)["ref_capacity"],
            "426L",
        )

        freezer_only = source(
            "Geladeira Capacidade do Freezer 84L",
            [spec("Capacidade total", "De 301 a 400 litros")],
        )
        self.assertEqual(self.detail("REF", freezer_only)["ref_capacity"], "84L")
        approximate_fallback = source(
            "Mini Geladeira 0,95 pés cúbicos (aprox. 26,9 litros)",
            [],
        )
        self.assertEqual(
            self.detail("REF", approximate_fallback)["ref_capacity"],
            "0,95 pés cúbicos (aprox. 26,9 litros)",
        )
        for title in (
            "Cervejeira 96 latas (350ml)",
            "Adega 12 garrafas 750 ml",
            "Adega para garrafas de 750 ml",
        ):
            with self.subTest(container_only_title=title):
                self.assertEqual(
                    self.detail("REF", source(title, []))["ref_capacity"],
                    "",
                )
        self.assertEqual(
            self.detail(
                "REF",
                source(
                    "Adega para garrafas de 750 ml",
                    [spec("Capacidade total", "300L")],
                ),
            )["ref_capacity"],
            "300L",
        )

    def test_ref_title_priority_excludes_water_components(self):
        for title in (
            "Geladeira com Dispenser de Agua 2,5L",
            "Geladeira com Reservatorio de Agua 3L",
        ):
            with self.subTest(auxiliary_water_title=title):
                data = source(title, [spec("Capacidade total", "400L")])
                self.assertEqual(self.detail("REF", data)["ref_capacity"], "400L")

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
        self.assertEqual(self.detail('REF', data)['ref_capacity'], '395L')

        data = source('Geladeira LG 395L', [spec('Capacidade', '84')])
        self.assertEqual(self.detail('REF', data)['ref_capacity'], '395L')
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

    def test_ref_capacity_ignores_can_size_and_keeps_actual_volume(self):
        for connector in (
            "de", "com", "x", "/", "-", "\u2013", "\u2014", ":", ";", "+", ",", "&"
        ):
            with self.subTest(title_connector=connector):
                data = source(
                    f"Geladeira 22 latas {connector} 473 ml - 18 litros",
                    [spec("Capacidade total", "De 1 a 20 litros")],
                )
                self.assertEqual(self.detail("REF", data)["ref_capacity"], "18 litros")
                self.assertEqual(
                    self.detail("REF", source(data["product"]["name"], []))["ref_capacity"],
                    "18 litros",
                )

            with self.subTest(target_fallback_connector=connector):
                data = source(
                    f"Geladeira 22 latas {connector} 473 ml",
                    [spec("Capacidade total", "18 litros")],
                )
                self.assertEqual(self.detail("REF", data)["ref_capacity"], "18 litros")

        for target in ("18 litros / 22 latas", "22 latas, 18 litros"):
            with self.subTest(composite_target=target):
                data = source(
                    "Geladeira Portatil sem volume no titulo",
                    [spec("Capacidade", target)],
                )
                self.assertEqual(self.detail("REF", data)["ref_capacity"], "18 litros")

        total_after_count = source(
            "Geladeira 22 latas - 18 litros",
            [spec("Capacidade total", "De 1 a 20 litros")],
        )
        self.assertEqual(
            self.detail("REF", total_after_count)["ref_capacity"],
            "18 litros",
        )
        sub_liter_can_size = source(
            "Geladeira 22 latas - 0,473L",
            [spec("Capacidade total", "18 litros")],
        )
        self.assertEqual(
            self.detail("REF", sub_liter_can_size)["ref_capacity"],
            "18 litros",
        )

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
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "294L")
        data = source(
            "Refrigerador 264L",
            [],
            "Capacidade Liquida: Freezer: 53; Refrigerador: 207; Total: 260; "
            "Capacidade Bruta: Freezer: 54; Refrigerador: 210; Capacidade total: 264",
        )
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "264L")
        data = source("Geladeira Multidoor 541L", [])
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "541L")
        data = source("Geladeira modelo ABC", [], "Capacidade: 394 litros")
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "394 litros")

    def test_duplicate_values_are_collapsed(self):
        data = source("Geladeira 480L", [spec("Capacidade total", "480 Litros-480 Litros-480 Litros-480 Litros")])
        self.assertEqual(self.detail("REF", data)["ref_capacity"], "480L")

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
            "9kg",
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

    def test_ldy_title_priority_rejects_weight_and_water_storage_context(self):
        cases = (
            (
                "product_weight",
                "Mini Lavadora Portátil Peso 2kg",
                [spec("Capacidade de lavagem", "1kg")],
                "1kg",
            ),
            (
                "water_reservoir",
                "Mini Lavadora Reservatório de Água Capacidade 12 litros",
                [spec("Capacidade de lavagem", "2kg")],
                "2kg",
            ),
            (
                "washer_tank",
                "Mini Lavadora Tanque 12 litros",
                [spec("Capacidade de lavagem", "2kg")],
                "12L",
            ),
        )
        for label, title, specs, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    self.detail("LDY", source(title, specs))["ldy_capacity"],
                    expected,
                )
        self.assertEqual(
            self.detail("LDY", source("Mini Lavadora Portátil Peso 2kg", []))[
                "ldy_capacity"
            ],
            "",
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
                data = source(
                    f"Lavadora Portatil {wording}",
                    [spec("Capacidade de lavagem", target)],
                )
                self.assertEqual(self.detail("LDY", data)["ldy_capacity"], target)

        exact = source(
            "Lavadora Colormaq 6kg",
            [spec("Capacidade de lavagem", "9kg")],
        )
        self.assertEqual(self.detail("LDY", exact)["ldy_capacity"], "6kg")

        for title in (
            "Lavadora 14kg Peso do Produto 50kg",
            "Peso do Produto 50kg Lavadora 14kg",
        ):
            with self.subTest(capacity_and_weight_order=title):
                data = source(
                    title,
                    [spec("Capacidade de lavagem", "De 11 a 15kg")],
                )
                self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "14kg")

        weight_suffix = source(
            "Mini Lavadora Portatil 2kg de peso",
            [spec("Capacidade de lavagem", "9kg")],
        )
        self.assertEqual(self.detail("LDY", weight_suffix)["ldy_capacity"], "9kg")

        compact_tank = source(
            "Mini Lavadora Peso 2kg Tanque 12L",
            [spec("Capacidade de lavagem", "2kg")],
        )
        self.assertEqual(self.detail("LDY", compact_tank)["ldy_capacity"], "12L")

        for title in (
            "Mini Lavadora 12L Reservatorio de Agua",
            "Mini Lavadora 12L Consumo de Agua",
        ):
            with self.subTest(trailing_water_context=title):
                data = source(title, [spec("Capacidade de lavagem", "2kg")])
                self.assertEqual(self.detail("LDY", data)["ldy_capacity"], "2kg")

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

    def test_ldy_explicit_title_loading_type_has_priority(self):
        cases = (
            (
                "front_load",
                "Lavadora Electrolux Front Load 11kg",
                [spec("Acesso ao cesto", "Superior")],
                "Front load",
            ),
            (
                "top_loading",
                "Lavadora Electrolux Top Loading 17kg",
                [spec("Acesso ao cesto", "Frontal")],
                "Top load",
            ),
            (
                "carga_superior",
                "Máquina de Lavar 14kg Carga Superior",
                [spec("Tipo", "Front Loading automática")],
                "Top load",
            ),
            (
                "carga_frontal",
                "Máquina de Lavar 14kg Carga Frontal",
                [spec("Acesso ao cesto", "Superior")],
                "Front load",
            ),
            (
                "abertura_frontal",
                "Máquina de Lavar 13kg Abertura Frontal",
                [spec("Abertura da Tampa", "Superior")],
                "Front load",
            ),
            (
                "ambiguous_title",
                "Lavadora Front Load e Top Load 12kg",
                [spec("Acesso ao cesto", "Superior")],
                "Top load",
            ),
        )
        for label, title, specs, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    self.detail("LDY", source(title, specs))["ldy_loading_type"],
                    expected,
                )

        combined = source(
            "Máquina de Lavar 14kg Front Load",
            [
                spec("Capacidade", "De 11 a 15kg"),
                spec("Acesso ao cesto", "Superior"),
            ],
        )
        combined_detail = self.detail("LDY", combined)
        self.assertEqual(combined_detail["ldy_capacity"], "14kg")
        self.assertEqual(combined_detail["ldy_loading_type"], "Front load")

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
                data = source(title, [spec("Acesso ao cesto", "Superior")])
                self.assertEqual(
                    self.detail("LDY", data)["ldy_loading_type"],
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
                data = source(title, [spec("Acesso ao cesto", "Superior")])
                self.assertEqual(
                    self.detail("LDY", data)["ldy_loading_type"],
                    "Top load",
                )

        for title in (
            "Lavadora n\u00e3o \u00e9 Front Load, mas Front Load 11kg",
            "Lavadora n\u00e3o \u00e9 Top Load, mas Front Load 11kg",
            "Lavadora abertura da tampa frontal 11kg",
        ):
            with self.subTest(positive_clause_or_lid=title):
                data = source(title, [spec("Acesso ao cesto", "Superior")])
                self.assertEqual(
                    self.detail("LDY", data)["ldy_loading_type"],
                    "Front load",
                )

        top_negated = source(
            "Lavadora n\u00e3o \u00e9 Top Load 11kg",
            [spec("Acesso ao cesto", "Frontal")],
        )
        self.assertEqual(
            self.detail("LDY", top_negated)["ldy_loading_type"],
            "Front load",
        )

    def test_ldy_title_loading_priority_is_preserved_in_next_data_merge(self):
        product = {
            "id": 991,
            "name": "Máquina de Lavar 14kg Front Load",
            "sku": {"id": "991"},
            "specGroups": [
                {
                    "name": "Especificações Técnicas",
                    "specs": [
                        {"name": "Capacidade", "value": "De 11 a 15kg"},
                        {"name": "Acesso ao cesto", "value": "Superior"},
                    ],
                }
            ],
        }
        payload = {"props": {"pageProps": {"product": product}}}
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "LDY"}):
            detail = _parse_casas_bahia_html_detail(
                html,
                "https://www.casasbahia.com.br",
                "https://www.casasbahia.com.br/produto/p/991",
            )
        self.assertEqual(detail["ldy_capacity"], "14kg")
        self.assertEqual(detail["ldy_loading_type"], "Front load")

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
            ('Capacidade do Refrigerador: 305L', '395L'),
            ('Capacidade do Refrigerador: 305L; Capacidade total: 389L', '395L'),
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
