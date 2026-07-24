import time
import unittest

from seda.common.field_rules import (
    combine_distinct,
    combine_capacity_distinct,
    combine_measurement_distinct,
    extract_ldy_capacity_from_title,
    extract_ref_capacity_components,
    extract_ref_capacity_from_title,
    extract_ref_capacity_scalar_values,
    extract_ref_title_capacity_components,
    extract_screen_size_from_title,
    filter_ref_capacity_exact_over_qualified_levels,
    is_energy_value,
    is_auxiliary_water_volume_context,
    is_ldy_capacity_value,
    is_ref_auxiliary_volume_context,
    is_ref_capacity_category_band,
    is_ref_capacity_value,
    is_screen_size_value,
    normalize_loading_type,
    select_ref_capacity_exact_over_qualified,
    select_ldy_capacity_level,
    select_ldy_capacity_from_levels,
    select_priority_ldy_capacity,
)
from seda.common.translations import translate_value


class FieldRuleTests(unittest.TestCase):
    def test_measurement_dedupe_keeps_a_second_explicit_unit(self):
        self.assertEqual(
            combine_measurement_distinct(['53', '53L', '53 Quartos']),
            '53,53 Quartos',
        )
        self.assertEqual(combine_measurement_distinct(['165W', '<165W']), '165W,<165W')
        self.assertEqual(combine_measurement_distinct(['<165W', '165W']), '<165W,165W')
        self.assertEqual(combine_measurement_distinct(['14kg', '~14kg']), '14kg,~14kg')
        self.assertEqual(combine_measurement_distinct(['~14kg', '14kg']), '~14kg,14kg')
        self.assertEqual(combine_measurement_distinct(['300 watts', '300 W']), '300 watts')
        self.assertEqual(combine_measurement_distinct(['300 W', '300 watts']), '300 W')

    def test_screen_size_rejects_other_measurement_meanings(self):
        self.assertFalse(is_screen_size_value('127V'))
        self.assertFalse(is_screen_size_value('130W'))
        self.assertFalse(is_screen_size_value('55 cm'))
        for value in ('2024', '1000', '55%', '50/55'):
            with self.subTest(value=value):
                self.assertFalse(is_screen_size_value(value))

    def test_combines_distinct_values_in_source_order(self):
        self.assertEqual(combine_distinct(["14", "14", "15"]), "14,15")
        self.assertEqual(combine_distinct(["14,5", "14,5"]), "14,5")
        self.assertEqual(combine_distinct(["14-14"]), "14")
        self.assertEqual(
            combine_distinct(["480 Litros-480 Litros-480 Litros-480 Litros"]),
            "480 Litros",
        )
        self.assertEqual(
            combine_distinct(["De 401 a 500 litros", "De 401 a 500 litros"]),
            "De 401 a 500 litros",
        )
        self.assertEqual(combine_capacity_distinct(["de 14 kg", "14 kg"]), "de 14 kg")
        self.assertEqual(combine_capacity_distinct(["14", "14 kg"]), "14")
        self.assertEqual(combine_capacity_distinct(["14 kg", "14"]), "14 kg")
        self.assertEqual(combine_capacity_distinct(["53 Quartos", "53L"]), "53 Quartos,53L")
        self.assertEqual(combine_measurement_distinct(['40 pol.', '40"']), "40 pol.")
        self.assertEqual(combine_measurement_distinct(["55", "55 polegadas"]), "55")

    def test_ref_capacity_accepts_nonstandard_but_meaningful_values(self):
        accepted = [
            "0,95 pés cúbicos (aprox. 26,9 litros)",
            "53 Quartos",
            "De 301 a 400 litros",
            "305",
        ]
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(is_ref_capacity_value(value))

    def test_ref_capacity_category_band_is_conservative(self):
        for value in (
            "De 301 a 400 litros",
            "Acima de 501 litros",
            "Abaixo de 200 litros",
            "301-400L",
            "301/400L",
            "301L-400L",
            "301L a 400L",
        ):
            with self.subTest(category_band=value):
                self.assertTrue(is_ref_capacity_category_band(value))

        for value in (
            "ate 260 litros",
            "0,95 pes cubicos (aprox. 26,9 litros)",
            "526L",
            "Capacidade total 426L",
        ):
            with self.subTest(practical_capacity=value):
                self.assertFalse(is_ref_capacity_category_band(value))

    def test_ref_capacity_rejects_container_counts_and_nested_volume(self):
        for value in (
            "44 latas",
            "22 latas de 473 ml",
            "96latas(350ml)",
            "12 garrafas de 750 ml",
            "12garrafas de 750ml",
            "6 unidades",
            "8 recipientes de 500 ml",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_ref_capacity_value(value))

        for title in (
            "Geladeira portátil para 44 latas",
            "Geladeira portátil para 22 latas de 473 ml",
            "Mini geladeira capacidade 24 latas de 350 ml",
            "Adega para 12 garrafas de 750 ml",
            "Adega para garrafas de 750 ml",
            "Geladeira para 96 latas (350ml)",
            "Geladeira para 96latas(350ml)",
            "Geladeira portátil para 6 garrafas 500 ml",
            "Adega para 12garrafas de 750ml",
            "Geladeira para 8 recipientes de 500 ml",
        ):
            with self.subTest(title=title):
                self.assertEqual(extract_ref_capacity_from_title(title), "")

        for title, expected in (
            ("Mini Geladeira 1040 ml", "1040 ml"),
            ("Mini Geladeira 1040ml", "1040ml"),
        ):
            with self.subTest(standalone_volume=title):
                self.assertEqual(extract_ref_capacity_from_title(title), expected)
        self.assertEqual(
            extract_ref_capacity_from_title(
                "Geladeira 22 latas de 473 ml com capacidade total de 18 litros"
            ),
            "18 litros",
        )
        self.assertEqual(
            extract_ref_capacity_from_title(
                "Adega 12 garrafas de 750 ml com capacidade total de 18 litros"
            ),
            "18 litros",
        )

    def test_ref_capacity_separates_container_size_from_physical_volume(self):
        for connector in (
            "de", "com", "x", "/", "-", "\u2013", "\u2014", ":", ";", "+", ",", "&"
        ):
            with self.subTest(connector=connector):
                self.assertEqual(
                    extract_ref_capacity_from_title(
                        f"Geladeira 22 latas {connector} 473 ml - 18 litros"
                    ),
                    "18 litros",
                )
                self.assertEqual(
                    extract_ref_capacity_from_title(
                        f"Geladeira 22 latas {connector} 473 ml"
                    ),
                    "",
                )

        for target in ("18 litros / 22 latas", "22 latas, 18 litros"):
            with self.subTest(target=target):
                self.assertEqual(
                    extract_ref_capacity_scalar_values(target),
                    ["18 litros"],
                )

        self.assertEqual(
            extract_ref_capacity_from_title("Geladeira 22 latas - 18 litros"),
            "18 litros",
        )
        self.assertEqual(
            extract_ref_capacity_from_title("Geladeira 22 latas - 0,473L"),
            "",
        )
        self.assertEqual(extract_ref_capacity_scalar_values("22 latas x 473 ml"), [])
        self.assertEqual(extract_ref_capacity_scalar_values("1040 ml"), ["1040 ml"])
        self.assertEqual(
            extract_ref_capacity_scalar_values(
                "0,95 p\u00e9s c\u00fabicos (aprox. 26,9 litros)"
            ),
            ["0,95 p\u00e9s c\u00fabicos (aprox. 26,9 litros)"],
        )

    def test_ref_capacity_keeps_appliance_volume_after_container_count(self):
        for title, expected in (
            ("Adega Climatizada 12 Garrafas 33L", "33L"),
            ("Cervejeira 96 Latas 82L", "82L"),
            ("Mini Geladeira 6 Latas 4L", "4L"),
        ):
            with self.subTest(appliance_volume=title):
                self.assertEqual(extract_ref_capacity_from_title(title), expected)

        for title in (
            "Adega para 12 Garrafas 1,5L",
            "Mini Geladeira para 6 Latas 350ml",
            "Mini Geladeira para 6 Garrafas / 1,5L",
            "Adega para garrafas de 3L",
            "Adega para garrafas com 5L",
            "Adega para garrafas x 5L",
            "Adega para 12 Garrafas / 5L",
            "Adega para 12 Garrafas, 5L",
            "Adega para 12 Garrafas - 7,5L",
            "Adega para 12 Garrafas 5L",
        ):
            with self.subTest(container_unit_volume=title):
                self.assertEqual(extract_ref_capacity_from_title(title), "")

    def test_ref_title_capacity_ranks_main_then_refrigerator_then_freezer(self):
        compartment_titles = (
            "Geladeira 84L do Freezer 305L do Refrigerador",
            "Geladeira 84L Freezer 305L Refrigerador",
            "Geladeira Freezer 84L Refrigerador 305L",
        )
        for title in compartment_titles:
            with self.subTest(compartment_orientation=title):
                components = extract_ref_title_capacity_components(title)
                self.assertEqual(components["refrigerator"], ["305L"])
                self.assertEqual(components["freezer"], ["84L"])
                self.assertEqual(extract_ref_capacity_from_title(title), "305L")

        self.assertEqual(
            extract_ref_capacity_from_title(
                "Geladeira 426L Refrigerador 296L Freezer 130L"
            ),
            "426L",
        )
        self.assertEqual(
            extract_ref_capacity_from_title("Geladeira Capacidade do Freezer 84L"),
            "84L",
        )

    def test_ref_title_components_are_owned_by_each_local_label(self):
        cases = (
            "Geladeira 84L no Freezer e 305L no Refrigerador",
            "Geladeira 84L no Freezer e 305L na Refrigeradora",
            "Geladeira 84L do Freezer e 305L do Refrigerador",
            "Geladeira 84L do Freezer e 305L da Refrigeradora",
            "Geladeira 84L Freezer e Refrigerador: 305L",
            "Geladeira Freezer com 84L e Refrigerador com 305L",
        )
        for title in cases:
            with self.subTest(local_component_orientation=title):
                components = extract_ref_title_capacity_components(title)
                self.assertEqual(components["refrigerator"], ["305L"])
                self.assertEqual(components["freezer"], ["84L"])
                self.assertEqual(extract_ref_capacity_from_title(title), "305L")

        self.assertEqual(
            extract_ref_capacity_from_title(
                "Geladeira 426L, 84L no Freezer e 305L no Refrigerador"
            ),
            "426L",
        )
        self.assertEqual(
            extract_ref_capacity_from_title(
                "Geladeira 2 Portas, 84L no Freezer e 305L no Refrigerador"
            ),
            "305L",
        )

    def test_ref_title_component_matching_stays_fast_for_repeated_pairs(self):
        title = "Geladeira " + " e ".join(
            f"{80 + index}L no Freezer e {280 + index}L no Refrigerador"
            for index in range(24)
        )
        started = time.perf_counter()
        first = extract_ref_title_capacity_components(title)
        elapsed = time.perf_counter() - started
        second = extract_ref_title_capacity_components(title)

        self.assertEqual(first, second)
        self.assertEqual(
            first["freezer"],
            [f"{80 + index}L" for index in range(24)],
        )
        self.assertEqual(
            first["refrigerator"],
            [f"{280 + index}L" for index in range(24)],
        )
        self.assertLess(elapsed, 0.5)

    def test_ref_capacity_rejects_auxiliary_water_volumes(self):
        for title in (
            "Geladeira com Dispenser de Agua 2,5L",
            "Geladeira com Reservatorio de Agua 3L",
            "Geladeira com Dispenser 2,5L de Agua",
            "Geladeira com Reservatorio 3L para Agua",
            "Geladeira com Dispenser para Agua de 2,5L",
        ):
            with self.subTest(auxiliary_water_title=title):
                self.assertEqual(extract_ref_capacity_from_title(title), "")
        for title in (
            "Geladeira 400L com Dispenser de Agua 2,5L",
            "Geladeira 400L com Dispenser 2,5L de Agua",
            "Geladeira 400L com Reservatorio 3L para Agua",
        ):
            with self.subTest(main_volume_with_auxiliary_water=title):
                self.assertEqual(extract_ref_capacity_from_title(title), "400L")

        plain_tank = "Mini Lavadora Tanque 12L"
        start = plain_tank.index("12L")
        self.assertFalse(
            is_auxiliary_water_volume_context(plain_tank, start, start + 3)
        )
        self.assertTrue(
            is_ref_auxiliary_volume_context(plain_tank, start, start + 3)
        )
        self.assertEqual(
            extract_ref_capacity_from_title("Geladeira com tanque 5L"),
            "",
        )

    def test_ref_scalar_rejects_auxiliary_storage_volumes(self):
        for value in (
            "Reservatorio de agua 2L",
            "2L reservatorio de agua",
            "Dispenser de agua:2L",
            "Tanque para agua5L",
            "Consumo de agua12L",
            "Geladeira com tanque 5L",
        ):
            with self.subTest(auxiliary_scalar=value):
                self.assertEqual(extract_ref_capacity_scalar_values(value), [])

        self.assertEqual(
            extract_ref_capacity_scalar_values(
                "400L + reservatorio de agua 2L"
            ),
            ["400L"],
        )
        self.assertEqual(extract_ref_capacity_scalar_values("305"), ["305"])
        self.assertEqual(
            extract_ref_capacity_scalar_values(
                "0,95 pes cubicos (aprox. 26,9 litros)"
            ),
            ["0,95 pes cubicos (aprox. 26,9 litros)"],
        )

    def test_ref_components_reject_auxiliary_storage_labels(self):
        for value in (
            "Reservatorio de agua do refrigerador:2L",
            "Dispenser do refrigerador:2L",
            "Tanque de agua da geladeira:5L",
        ):
            with self.subTest(auxiliary_component=value):
                self.assertEqual(
                    extract_ref_capacity_components(value),
                    {"total": [], "refrigerator": [], "freezer": []},
                )

        components = extract_ref_capacity_components(
            "Capacidade do Refrigerador:305L; Freezer:84L"
        )
        self.assertEqual(components["refrigerator"], ["305L"])
        self.assertEqual(components["freezer"], ["84L"])

    def test_ldy_capacity_keeps_mass_and_rejects_other_meanings(self):
        accepted = ["15", "De 11 a 15kg", "8,8 libras", "145kg"]
        rejected = ["26 l", "620", "630", "1400 rpm", "15W", "800W", "Superior", "Não", "0,08 kWh/ciclo", "4K", "HDMI2"]
        for value in accepted:
            with self.subTest(accepted=value):
                self.assertTrue(is_ldy_capacity_value(value))
        for value in rejected:
            with self.subTest(rejected=value):
                self.assertFalse(is_ldy_capacity_value(value))

    def test_ref_capacity_rejects_power(self):
        self.assertFalse(is_ref_capacity_value("80W"))
        self.assertFalse(is_ref_capacity_value("127V"))
        self.assertFalse(is_ldy_capacity_value("12V"))

    def test_energy_accepts_consumption_text_but_not_efficiency_or_voltage(self):
        accepted = [
            "Abaixo de 0,5W (Stand by)",
            "Baixo consumo de energia",
            "130W (Pico) / <0.5W (Standby)",
            "47KWH/ano",
            "300 watts",
        ]
        rejected = [
            "Classe A (Eficiência Energética)",
            "Eficiência energética A",
            "Bivolt",
            "Elétrica",
            "Baixo consumo de água",
            "Voltagem: 220 V",
            "127V / 220V",
        ]
        for value in accepted:
            with self.subTest(accepted=value):
                self.assertTrue(is_energy_value(value))
        for value in rejected:
            with self.subTest(rejected=value):
                self.assertFalse(is_energy_value(value))

    def test_screen_size_rejects_mount_ranges(self):
        self.assertFalse(is_screen_size_value('10-40 polegadas'))
        self.assertFalse(is_screen_size_value('10–40 polegadas'))
        self.assertTrue(is_screen_size_value('55 polegadas'))
        self.assertEqual(
            extract_screen_size_from_title('Smart TV 55 4K + Suporte 10-40 polegadas'),
            '55"',
        )
        self.assertEqual(
            extract_screen_size_from_title('STPA 45 Suporte Articulado (10" a 40") para TV'),
            "",
        )

        self.assertEqual(
            extract_screen_size_from_title('Smart TV 4K 55 QLED'),
            '55' + chr(34),
        )
        self.assertEqual(
            extract_screen_size_from_title('Smart TV QN120ABC 4K 55 QLED'),
            '55' + chr(34),
        )
        for title in (
            'Smart TV 120 Hz 55 Samsung',
            'Smart TV 130 W 55 Samsung',
            'Smart TV 127 V 55 Samsung',
            'Smart TV 100 nits 55 Samsung',
        ):
            with self.subTest(title=title):
                self.assertEqual(extract_screen_size_from_title(title), '55' + chr(34))
        for title in (
            'Smart TV 130 W Samsung',
            'Smart TV 127 V Samsung',
            'Smart TV 100 nit Samsung',
            'Smart TV Samsung QN90 4K',
            'Smart TV Samsung QN999 4K',
            'TV 50/55 polegadas',
        ):
            with self.subTest(title=title):
                self.assertEqual(extract_screen_size_from_title(title), '')

    def test_shared_ldy_conflict_policy(self):
        self.assertEqual(select_ldy_capacity_level(['0,49', '16kg']), '16kg')
        self.assertEqual(select_ldy_capacity_level(['0,49']), '0,49')
        self.assertEqual(select_ldy_capacity_level(['0,49', '0,49 kg']), '0,49')
        self.assertEqual(select_priority_ldy_capacity(['0,49', '16kg']), '16kg')
        self.assertEqual(select_priority_ldy_capacity(['0,49', '0,49 kg', '16kg']), '0,49')
        self.assertEqual(select_ldy_capacity_from_levels([['0,49'], ['16kg']]), '16kg')
        self.assertEqual(
            select_ldy_capacity_from_levels([['0,49', '0,49 kg'], ['16kg']]),
            '0,49',
        )
        self.assertEqual(
            select_ldy_capacity_level(['14kg', 'De 11 a 15kg']),
            '14kg',
        )
        self.assertEqual(
            select_ldy_capacity_level(['De 11 a 15kg', '14kg']),
            '14kg',
        )
        self.assertEqual(
            select_ldy_capacity_from_levels([['14kg', 'De 11 a 15kg']]),
            '14kg',
        )
        self.assertEqual(
            select_ldy_capacity_from_levels([['De 11 a 15kg'], ['14kg']]),
            '14kg',
        )
        self.assertEqual(
            select_ldy_capacity_from_levels([['De 11 a 15kg'], []]),
            'De 11 a 15kg',
        )
        self.assertEqual(
            select_ldy_capacity_level(['De 11 a 15kg']),
            'De 11 a 15kg',
        )

    def test_ref_exact_capacity_replaces_same_qualified_value_only(self):
        self.assertEqual(
            select_ref_capacity_exact_over_qualified(
                ['ate 260 litros', '260 litros']
            ),
            '260 litros',
        )
        self.assertEqual(
            select_ref_capacity_exact_over_qualified(
                ['260 litros', 'aprox. 260 litros']
            ),
            '260 litros',
        )
        self.assertEqual(
            select_ref_capacity_exact_over_qualified(
                ['ate 260 litros', '260']
            ),
            '260',
        )
        self.assertEqual(
            select_ref_capacity_exact_over_qualified(
                ['ate 260 litros', '260 Quartos']
            ),
            'ate 260 litros,260 Quartos',
        )
        self.assertEqual(
            select_ref_capacity_exact_over_qualified(['ate 260 litros']),
            'ate 260 litros',
        )
        self.assertEqual(
            select_ref_capacity_exact_over_qualified(
                ['De 301 a 400 litros', '400 litros']
            ),
            'De 301 a 400 litros,400 litros',
        )
        self.assertEqual(
            filter_ref_capacity_exact_over_qualified_levels(
                [
                    ['ate 260 litros'],
                    ['260L'],
                    ['De 301 a 400 litros'],
                ]
            ),
            [[], ['260L'], ['De 301 a 400 litros']],
        )

    def test_ref_capacity_components_keep_raw_compartment_values(self):
        components = extract_ref_capacity_components(
            'Geladeira 84L de capacidade total 305L do refrigerador'
        )
        self.assertEqual(components['total'], ['84L'])
        self.assertEqual(components['refrigerator'], ['305L'])
        self.assertEqual(components['freezer'], [])
        self.assertEqual(
            extract_ref_capacity_from_title(
                'Geladeira 84L de capacidade total 305L do refrigerador'
            ),
            '84L',
        )

        components = extract_ref_capacity_components('Freezer 84L Refrigerador 305L')
        self.assertEqual(components['total'], [])
        self.assertEqual(components['refrigerator'], ['305L'])
        self.assertEqual(components['freezer'], ['84L'])

        components = extract_ref_capacity_components(
            'Total 389L Freezer 84L Refrigerador 305L'
        )
        self.assertEqual(components['total'], ['389L'])
        self.assertEqual(components['refrigerator'], ['305L'])
        self.assertEqual(components['freezer'], ['84L'])

        components = extract_ref_capacity_components(
            "Capacidade do Freezer=84, Capacidade do Refrigerador=305"
        )
        self.assertEqual(components["total"], [])
        self.assertEqual(components["refrigerator"], ["305"])
        self.assertEqual(components["freezer"], ["84"])

        components = extract_ref_capacity_components(
            "Total: 389 L; Freezer: 84 L; Refrigerador: 305 L"
        )
        self.assertEqual(components["total"], ["389 L"])
        self.assertEqual(components["refrigerator"], ["305 L"])
        self.assertEqual(components["freezer"], ["84 L"])

    def test_loading_type_is_normalized(self):
        self.assertEqual(normalize_loading_type("Superior"), "Top load")
        self.assertEqual(normalize_loading_type("Tipo: Top Load automática"), "Top load")
        self.assertEqual(normalize_loading_type("Frontal"), "Front load")
        self.assertEqual(normalize_loading_type("Front Loading automática"), "Front load")
        self.assertEqual(normalize_loading_type("Elétrica"), "")

    def test_loading_hyphen_loader_and_negative_context_boundaries(self):
        self.assertEqual(normalize_loading_type("Front-Load"), "Front load")
        self.assertEqual(normalize_loading_type("Front Loader"), "Front load")
        for value in (
            "sem abertura frontal",
            "nao possui abertura frontal",
            "nao tem abertura frontal",
            "nao e Front Load",
            "sem ser Front Load",
            "nao e do tipo Front Load",
            "nao possui sistema de carga frontal",
            "nao e uma maquina de lavar frontal",
            "nao e Top Load",
            "nao frontal",
            "nao top load",
            "nao carga superior",
            "painel frontal",
            "porta frontal",
        ):
            with self.subTest(non_loading_context=value):
                self.assertEqual(normalize_loading_type(value), "")
                self.assertEqual(translate_value("ldy_loading_type", value), "")

        self.assertEqual(
            normalize_loading_type("nao e Front Load, mas Front Load"),
            "Front load",
        )
        self.assertEqual(
            normalize_loading_type("nao e Top Load, mas Front Load"),
            "Front load",
        )
        for value in (
            "Nao e frontal e possui abertura superior",
            "Nao possui abertura frontal e tem carga superior",
            "Nao possui abertura frontal e com carga superior",
        ):
            with self.subTest(positive_clause_after_negation=value):
                self.assertEqual(normalize_loading_type(value), "Top load")
        self.assertEqual(
            normalize_loading_type("sem abertura frontal e superior"),
            "",
        )
        self.assertEqual(
            normalize_loading_type("nao frontal e superior"),
            "",
        )
        self.assertEqual(
            normalize_loading_type("nao frontal e sim superior"),
            "Top load",
        )
        self.assertEqual(
            normalize_loading_type("abertura da tampa frontal"),
            "Front load",
        )

    def test_loading_translation_rejects_unknown_values_but_keeps_directions(self):
        for value in ("Automática", "Roupa", "Elétrica"):
            with self.subTest(rejected=value):
                self.assertEqual(translate_value("ldy_loading_type", value), "")

        expected = (
            ("Superior", "Top load"),
            ("Front Loading automática", "Front load"),
            ("Superior,Front Loading automática", "Top load,Front load"),
            ("Automática; Superior; Roupa", "Top load"),
        )
        for value, translated in expected:
            with self.subTest(accepted=value):
                self.assertEqual(
                    translate_value("ldy_loading_type", value),
                    translated,
                )

    def test_title_capacity_extractors_preserve_source_text(self):
        self.assertEqual(
            extract_ref_capacity_from_title(
                'Geladeira Capacidade do Freezer 84L Capacidade do Refrigerador 305L'
            ),
            '305L',
        )
        self.assertEqual(
            extract_ref_capacity_from_title(
                'Geladeira Capacidade do Freezer 84L Capacidade total 389L '
                'Capacidade do Refrigerador 305L'
            ),
            '389L',
        )
        self.assertEqual(
            extract_ref_capacity_from_title("Mini geladeira 0,95 pés cúbicos (aprox. 26,9 litros)"),
            "0,95 pés cúbicos (aprox. 26,9 litros)",
        )
        self.assertEqual(extract_ref_capacity_from_title("Geladeira portátil para 44 latas"), "")
        self.assertEqual(extract_ref_capacity_from_title("Geladeira Multidoor 541L"), "541L")
        self.assertEqual(extract_ldy_capacity_from_title("Lavadora portátil 8,8 libras"), "8,8 libras")
        self.assertEqual(extract_ldy_capacity_from_title("Máquina de lavar 14,5kg"), "14,5kg")
        self.assertEqual(extract_ldy_capacity_from_title("Lavadora de 8kg a 9kg"), "de 8kg a 9kg")
        self.assertEqual(extract_screen_size_from_title("Smart TV DLED 55 4K"), '55"')


    def test_title_capacity_and_display_regressions(self):
        for title, expected in (
            ("Geladeira 2 Portas Frost Free 332L", "332L"),
            ("Geladeira 220V 300 Litros", "300 Litros"),
            ("Refrigerador 2 Portas 490L", "490L"),
            ("Freezer 110V 300L", "300L"),
            ("Geladeira 3 Portas Multidoor 554 Litros", "554 Litros"),
            ("Freezer Philco Horizontal PFZ330B 295L Refrigerador 110V", "295L"),
            ("Geladeira 389L Freezer 84L Refrigerador 305L", "389L"),
            ("Freezer 84L Refrigerador 305L", "305L"),
            (
                "Mini geladeira atualizada de 90,6 L com freezer de 3,2 pes cubicos",
                "90,6 L",
            ),
            ("Geladeira portatil para 22 latas de 473 ml", ""),
            ("Mini geladeira capacidade 24 latas de 350 ml", ""),
        ):
            with self.subTest(ref_title=title):
                self.assertEqual(extract_ref_capacity_from_title(title), expected)
        self.assertEqual(extract_ref_capacity_from_title("Geladeira 2024"), "")
        self.assertEqual(
            extract_ref_capacity_from_title("Electrolux DFN41 DFX41 Geladeira 127V 220V"),
            "",
        )
        self.assertEqual(extract_ldy_capacity_from_title("Lavadora 8kg - 12kg"), "8kg - 12kg")
        self.assertEqual(
            extract_ldy_capacity_from_title("Lavadora 8 kg\u201312 kg"),
            "8 kg\u201312 kg",
        )
        for title, expected in (
            ("Lavadora 11-15kg", "11-15kg"),
            ("Lavadora 11/15kg", "11/15kg"),
            ("Lavadora 11kg-15kg", "11kg-15kg"),
            ("Lavadora de 11 a 15kg", "de 11 a 15kg"),
        ):
            with self.subTest(ldy_compact_range=title):
                self.assertEqual(extract_ldy_capacity_from_title(title), expected)
        for title, expected in (
            ("Geladeira 301-400L", "301-400L"),
            ("Geladeira 301/400L", "301/400L"),
            ("Geladeira 301L-400L", "301L-400L"),
            ("Geladeira 301L a 400L", "301L a 400L"),
            ("Geladeira de 301 a 400 litros", "de 301 a 400 litros"),
        ):
            with self.subTest(ref_compact_range=title):
                self.assertEqual(extract_ref_capacity_from_title(title), expected)
        self.assertEqual(extract_screen_size_from_title("Televisor Samsung 55 4K"), '55"')
        self.assertEqual(extract_screen_size_from_title("Samsung Crystal UHD 55 4K"), '55"')


if __name__ == "__main__":
    unittest.main()
