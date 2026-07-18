import unittest

from seda.common.field_rules import (
    combine_distinct,
    combine_capacity_distinct,
    combine_measurement_distinct,
    extract_ldy_capacity_from_title,
    extract_ref_capacity_components,
    extract_ref_capacity_from_title,
    extract_screen_size_from_title,
    is_energy_value,
    is_ldy_capacity_value,
    is_ref_capacity_value,
    is_screen_size_value,
    normalize_loading_type,
    select_ldy_capacity_level,
    select_ldy_capacity_from_levels,
    select_priority_ldy_capacity,
)


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
            "22 latas de 473 ml",
            "44 latas",
            "De 301 a 400 litros",
            "305",
        ]
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(is_ref_capacity_value(value))

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
        self.assertEqual(extract_ref_capacity_from_title("Geladeira portátil para 44 latas"), "44 latas")
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
            ("Geladeira portatil para 22 latas de 473 ml", "22 latas de 473 ml"),
            ("Mini geladeira capacidade 24 latas de 350 ml", "24 latas de 350 ml"),
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
        self.assertEqual(extract_screen_size_from_title("Televisor Samsung 55 4K"), '55"')
        self.assertEqual(extract_screen_size_from_title("Samsung Crystal UHD 55 4K"), '55"')


if __name__ == "__main__":
    unittest.main()
