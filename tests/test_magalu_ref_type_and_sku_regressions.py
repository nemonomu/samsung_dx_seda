import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from seda.common.translations import translate_row
from seda.magalu.detail_api import _detail_from_item
from seda.magalu.field_extraction import ref_refrigerator_type_from_title
from seda.parsers import (
    _parse_magalu_next_detail,
    appliance_model_number_from_text,
    is_appliance_spec_token,
    preferred_magalu_sku,
)
from seda.step00_config import write_csv
from seda.step08_detail_enrichment import _merge_authoritative_detail
from seda.step15_final_output import _sku_for_output


def _fact(label, value):
    return {"keyName": label, "value": value}


def _item(title, factsheet=(), item_id="sample"):
    return {
        "id": item_id,
        "title": title,
        "factsheet": list(factsheet),
        "attributes": [],
        "offers": [],
        "rating": {},
    }


def _producer_details(line, item, product_url=None):
    product_url = product_url or (
        f"https://www.magazineluiza.com.br/produto/p/{item['id']}/ed/refr/"
    )
    payload = {"props": {"pageProps": {"data": {"item": item}}}}
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )
    with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": line}, clear=False):
        graphql = _detail_from_item(item)
        next_data = _parse_magalu_next_detail(
            html,
            "https://www.magazineluiza.com.br",
            product_url,
        )
    return graphql, next_data


class MagaluRefTitleTypeRegressionTests(unittest.TestCase):
    def test_compound_specs_are_canonical_through_both_producers_and_output(self):
        cases = (
            ("Geladeira Frost Free 490L", "Duplex Inverse", "Inverse", "Freezer-on-Bottom"),
            ("Geladeira Frost Free 490L", "2 portas Inverse", "Inverse", "Freezer-on-Bottom"),
            ("Geladeira Frost Free 490L", "3 portas French Door", "French Door", "French Door"),
            ("Geladeira Frost Free 490L", "4 portas Multidoor", "Multidoor", "Multidoor"),
            ("Geladeira Duplex 490L", "Freezer em cima", "Top Freezer", "Freezer-on-Top"),
            ("Geladeira Duplex 490L", "Freezer superior", "Top Freezer", "Freezer-on-Top"),
        )
        output_rows = []
        for index, (title, raw, expected, translated) in enumerate(cases, start=1):
            with self.subTest(raw=raw):
                item = _item(
                    title,
                    (_fact("Tipo", raw),),
                    item_id=f"ref-compound-{index}",
                )
                graphql, next_data = _producer_details("REF", item)
                self.assertEqual(graphql["ref_refrigerator_type"], expected)
                self.assertEqual(next_data["ref_refrigerator_type"], expected)
                self.assertEqual(
                    translate_row({"ref_refrigerator_type": raw})[
                        "ref_refrigerator_type"
                    ],
                    translated,
                )
                self.assertEqual(
                    translate_row(graphql)["ref_refrigerator_type"],
                    translated,
                )
                output_rows.append({"ref_refrigerator_type": expected})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ref_type.csv"
            write_csv(path, output_rows, columns=["ref_refrigerator_type"])
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                saved = list(csv.DictReader(handle))
        self.assertEqual(
            [row["ref_refrigerator_type"] for row in saved],
            [case[3] for case in cases],
        )

    def test_title_and_all_specs_use_specificity_then_title_tiebreak(self):
        cases = (
            (
                "Geladeira Electrolux Duplex 490L",
                (_fact("Tipo", "French Door"),),
                "French Door",
            ),
            (
                "Geladeira Electrolux Side by Side 490L",
                (_fact("Tipo", "Duplex"),),
                "Side by Side",
            ),
            (
                "Geladeira Electrolux Multidoor 490L",
                (_fact("Tipo", "French Door"),),
                "Multidoor",
            ),
            (
                "Geladeira Electrolux 3 Portas 490L",
                (_fact("Tipo", "1 porta"),),
                "3 portas",
            ),
            (
                "Geladeira Electrolux Duplex 490L",
                (_fact("Tipo", "2 portas"),),
                "Duplex",
            ),
            (
                "Geladeira Electrolux Frost Free 490L",
                (_fact("Tipo", "Duplex"), _fact("Porta", "French Door")),
                "French Door",
            ),
            (
                "Geladeira Electrolux Frost Free 490L",
                (_fact("Tipo", "Freezer Embaixo"),),
                "Inverse",
            ),
            (
                "Geladeira Electrolux Top Freezer 490L",
                (_fact("Tipo", "Freezer Embaixo"),),
                "Inverse",
            ),
            (
                "Geladeira Electrolux Inverse 490L",
                (_fact("Tipo", "Top Freezer"),),
                "Inverse",
            ),
            (
                "Geladeira Electrolux 3 Portas 490L",
                (_fact("Tipo", "Top Freezer"),),
                "Top Freezer",
            ),
            (
                "Geladeira Electrolux Top Freezer 490L",
                (_fact("Tipo", "3 portas"),),
                "Top Freezer",
            ),
        )
        for index, (title, facts, expected) in enumerate(cases, start=1):
            with self.subTest(title=title, expected=expected):
                item = _item(title, facts, item_id=f"ref-priority-{index}")
                graphql, next_data = _producer_details("REF", item)
                self.assertEqual(graphql["ref_refrigerator_type"], expected)
                self.assertEqual(next_data["ref_refrigerator_type"], expected)

    def test_explicit_title_architecture_has_same_priority_in_both_producers(self):
        cases = (
            ("Geladeira Electrolux Side by Side 526L", "Side by Side"),
            ("Refrigerador Brastemp French Door 554L", "French Door"),
            ("Geladeira HQ Multidoor 426L", "Multidoor"),
            ("Geladeira Consul Duplex com Freezer Embaixo 399L", "Inverse"),
            ("Geladeira Electrolux Inverse Inverter 490L", "Inverse"),
            ("Geladeira Top Freezer Duplex 400L", "Top Freezer"),
            ("Geladeira Esmaltec 1 Porta 293L", "1 porta"),
            ("Geladeira 3 Portas Multidoor 541L", "Multidoor"),
            ("Geladeira Frost Free Inverter Duplex 395L", "Duplex"),
            ("Geladeira Frost Free 2 Portas 377L", "2 portas"),
        )
        for index, (title, expected) in enumerate(cases, start=1):
            with self.subTest(title=title):
                item = _item(
                    title,
                    (_fact("Tipo", "Duplex"),),
                    item_id=f"ref-title-{index}",
                )
                graphql, next_data = _producer_details("REF", item)
                self.assertEqual(graphql["ref_refrigerator_type"], expected)
                self.assertEqual(next_data["ref_refrigerator_type"], expected)

    def test_inverter_is_not_inverse_and_existing_spec_fallback_is_unchanged(self):
        spec_item = _item(
            "Geladeira Electrolux Frost Free Inverter 490L",
            (_fact("Tipo", "Side by Side"),),
        )
        graphql, next_data = _producer_details("REF", spec_item)
        self.assertEqual(graphql["ref_refrigerator_type"], "Side by Side")
        self.assertEqual(next_data["ref_refrigerator_type"], "Side by Side")

        door_item = _item(
            "Geladeira Electrolux Frost Free Inverter 490L",
            (
                _fact("Porta", "Inverter"),
                _fact("Quantidade de Portas", "2"),
            ),
        )
        graphql, next_data = _producer_details("REF", door_item)
        self.assertEqual(graphql["ref_refrigerator_type"], "Duplex")
        self.assertEqual(next_data["ref_refrigerator_type"], "Duplex")

    def test_title_helper_keeps_existing_accessory_guard(self):
        for title in (
            "Placa potencia para refrigerador French Door",
            "Prateleira para geladeira Multidoor",
            "Sensor para freezer Top Freezer",
        ):
            with self.subTest(title=title):
                self.assertEqual(ref_refrigerator_type_from_title(title), "")


class MagaluApplianceSkuRegressionTests(unittest.TestCase):
    def test_spec_tokens_are_skipped_until_a_real_model_candidate(self):
        cases = (
            ("Lavadora 14kg 130W Modelo MLA14", "MLA14"),
            ("Lavadora 14kg 500W 60Hz MLA15", "MLA15"),
            ("Geladeira 60cm 400L CRM44", "CRM44"),
            ("Lavadora 1200RPM MLA15", "MLA15"),
            (
                "Geladeira 12V/24V/110V/220V MD-RT611EVD013",
                "MD-RT611EVD013",
            ),
            ("Lavadora Modelo 123W456A", "123W456A"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(appliance_model_number_from_text(text), expected)
                self.assertEqual(
                    preferred_magalu_sku("REF", text, "MODEL-FALLBACK", ""),
                    expected,
                )

        self.assertEqual(
            preferred_magalu_sku(
                "LDY",
                "Lavadora 14kg 220V-60HZ",
                "MA512W130A/WK-05",
                "",
            ),
            "MA512W130A/WK-05",
        )

    def test_spec_only_tokens_are_rejected_at_final_sku_boundary(self):
        rejected = (
            "110V",
            "110V/220V",
            "12V/24V/110V/220V",
            "220V-60HZ",
            "220V60HZ",
            "220V-50/60HZ",
            "127V/220V-50/60HZ",
            "110VAC",
            "12VDC",
            "130W",
            "500W",
            "60CM",
            "60X60CM",
            "1200RPM",
            "14KG",
            "400L",
        )
        valid = (
            "MD-RT611EVD013",
            "MLA11",
            "MLA15",
            "MA512W130A/WK-05",
            "123W456A",
            "55P7K",
            "75U8600F",
            "TB059E",
        )
        for line in ("REF", "LDY"):
            with patch.dict(
                os.environ,
                {"SEDA_PRODUCT_LINE": line, "SEDA_ACTIVE_RETAILER": "magalu"},
                clear=False,
            ):
                for value in rejected:
                    with self.subTest(line=line, rejected=value):
                        self.assertTrue(is_appliance_spec_token(value))
                        self.assertEqual(
                            _sku_for_output(
                                {"retailer": "Magalu", "sku": value},
                                "sample",
                            ),
                            "",
                        )
                for value in valid:
                    with self.subTest(line=line, valid=value):
                        self.assertFalse(is_appliance_spec_token(value))
                        self.assertEqual(
                            _sku_for_output(
                                {"retailer": "Magalu", "sku": value},
                                "sample",
                            ),
                            value,
                        )

        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"},
            clear=False,
        ):
            for value in ("55A", "55P7K", "75U8600F", "TB059E"):
                with self.subTest(line="TV", valid=value):
                    self.assertEqual(
                        _sku_for_output(
                            {"retailer": "Magalu", "sku": value},
                            "sample",
                        ),
                        value,
                    )

    def test_reference_then_model_priority_is_preserved_with_narrow_salvage(self):
        cases = (
            (
                "REF",
                "Refrigerador Midea 473L Frost Free MD-RT611EVD013",
                "MODEL-SHOULD-NOT-WIN",
                "Geladeira Midea 473L",
                "MD-RT611EVD013",
            ),
            (
                "REF",
                "Geladeira Frost Free 400L",
                "VALID-MODEL",
                "Geladeira 400L",
                "VALID-MODEL",
            ),
            (
                "LDY",
                "Lavadora 11Kg MLA11",
                "MODEL-SHOULD-NOT-WIN",
                "Lavadora Mueller 11kg",
                "MLA11",
            ),
            (
                "LDY",
                "",
                "Lavadora 15Kg MLA15",
                "Lavadora Mueller 15kg",
                "MLA15",
            ),
            (
                "REF",
                "REF-FULL",
                "MODEL",
                "Geladeira 400L TITLE-MODEL",
                "REF-FULL",
            ),
            (
                "LDY",
                "",
                "MA512W130A/WK-05",
                "Lavadora Midea 13kg",
                "MA512W130A/WK-05",
            ),
            (
                "REF",
                "130W",
                "MD-RT611EVD013",
                "Geladeira Midea 473L",
                "MD-RT611EVD013",
            ),
            (
                "LDY",
                "400L",
                "MLA15",
                "Lavadora Mueller 15kg",
                "MLA15",
            ),
        )
        for line, reference, model, title, expected in cases:
            with self.subTest(line=line, reference=reference, model=model):
                self.assertEqual(
                    preferred_magalu_sku(line, reference, model, title),
                    expected,
                )

    def test_both_producers_use_the_same_ref_and_ldy_sku_salvage(self):
        cases = (
            (
                "REF",
                _item(
                    "Refrigerador Midea 473L Frost Free",
                    (
                        _fact(
                            "Referencia",
                            "Refrigerador Midea 473L Frost Free MD-RT611EVD013",
                        ),
                        _fact("Modelo", "MODEL-SHOULD-NOT-WIN"),
                    ),
                    item_id="ref-sku",
                ),
                "MD-RT611EVD013",
            ),
            (
                "LDY",
                _item(
                    "Máquina de Lavar Mueller 11kg",
                    (
                        _fact("Referencia", "Lavadora 11Kg MLA11"),
                        _fact("Modelo", "MODEL-SHOULD-NOT-WIN"),
                    ),
                    item_id="ldy-sku-11",
                ),
                "MLA11",
            ),
            (
                "LDY",
                _item(
                    "Máquina de Lavar Mueller 15kg",
                    (_fact("Modelo", "Lavadora 15Kg MLA15"),),
                    item_id="ldy-sku-15",
                ),
                "MLA15",
            ),
            (
                "REF",
                _item(
                    "Refrigerador Midea 473L Frost Free",
                    (
                        _fact(
                            "Referencia",
                            "Refrigerador 473L 127V/220V MD-RT611EVD013",
                        ),
                    ),
                    item_id="ref-sku-after-voltage",
                ),
                "MD-RT611EVD013",
            ),
            (
                "LDY",
                _item(
                    "Maquina de Lavar Mueller 15kg",
                    (
                        _fact(
                            "Referencia",
                            "Lavadora 15kg 220V-60HZ MLA15",
                        ),
                    ),
                    item_id="ldy-sku-after-electrical",
                ),
                "MLA15",
            ),
            (
                "LDY",
                _item(
                    "Maquina de Lavar Midea 13kg",
                    (
                        _fact("Referencia", "Lavadora 13kg 220V-60HZ"),
                        _fact("Modelo", "MA512W130A/WK-05"),
                    ),
                    item_id="ldy-model-after-spec-reference",
                ),
                "MA512W130A/WK-05",
            ),
            (
                "REF",
                _item(
                    "Refrigerador Midea 473L Frost Free",
                    (
                        _fact("Referencia", "130W"),
                        _fact("Modelo", "MD-RT611EVD013"),
                    ),
                    item_id="ref-model-after-pure-spec",
                ),
                "MD-RT611EVD013",
            ),
        )
        for line, item, expected in cases:
            with self.subTest(line=line, item=item["id"]):
                graphql, next_data = _producer_details(line, item)
                self.assertEqual(graphql["sku"], expected)
                self.assertEqual(next_data["sku"], expected)

    def test_final_safety_filter_still_rejects_descriptive_values(self):
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "REF", "SEDA_ACTIVE_RETAILER": "magalu"},
            clear=False,
        ):
            self.assertEqual(
                _sku_for_output(
                    {"retailer": "Magalu", "sku": "Refrigerador Midea 473L"},
                    "sample",
                ),
                "",
            )
            self.assertEqual(
                _sku_for_output(
                    {"retailer": "Magalu", "sku": "MD-RT611EVD013"},
                    "sample",
                ),
                "MD-RT611EVD013",
            )


class MagaluTvReferenceTimelineRegressionTests(unittest.TestCase):
    def test_tb059e_passes_producer_verified_merge_and_final_output(self):
        item_id = "kf98389g73"
        title = "Smart TV QLED 50 4K Toshiba Google TV 3HDMI 2USB Wi-Fi"
        product_url = (
            "https://www.magazineluiza.com.br/"
            "smart-tv-qled-50-4k-toshiba-google-tv-3hdmi-2usb-wi-fi/"
            f"p/{item_id}/et/elit/"
        )
        item = _item(
            title,
            (_fact("Referência", "TB059E"),),
            item_id=item_id,
        )
        row = {
            "retailer": "Magalu",
            "product_line": "TV",
            "item": item_id,
            "product_url": product_url,
            "retailer_sku_name": title,
            "sku": item_id,
            "parse_status": "listing_next_data",
        }
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"},
            clear=False,
        ):
            detail = _detail_from_item(item)
            self.assertEqual(detail["sku"], "TB059E")
            detail["_detail_identity_verified"] = True
            detail["_detail_item_id"] = item_id
            _merge_authoritative_detail(row, detail, identity_verified=True)
            self.assertEqual(row["sku"], "TB059E")
            self.assertIn(
                "sku_factsheet_reference_recovered",
                row["parse_status"].split("+"),
            )
            self.assertEqual(_sku_for_output(row, item_id), "TB059E")


if __name__ == "__main__":
    unittest.main()
