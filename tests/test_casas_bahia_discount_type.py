import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from seda.casas_bahia.destaque_api import discount_types_from_payload, fetch_discount_type
from seda.casas_bahia.listing_discount_backfill import (
    _needs_discount_type_backfill,
    run,
)
from seda.common.translations import translate_value


def payload(*destaques):
    return {"value": {"destaques": list(destaques)}, "success": True}


class CasasBahiaDiscountTypeTests(unittest.TestCase):
    def test_real_coupon_payload_becomes_canonical_discount_type(self):
        data = payload(
            {
                "dscFlag": "AC Cupom Desconto 3P - 10%",
                "titulo": "Cupom de desconto - 10%",
            }
        )

        self.assertEqual(
            discount_types_from_payload(data),
            ["USE O CUPOM DESCONTO 10%"],
        )

    def test_regular_percent_discount_keeps_existing_format(self):
        data = payload({"dscFlag": "15% de desconto"})

        self.assertEqual(discount_types_from_payload(data), ["15% de desconto"])

    def test_coupon_without_percent_does_not_guess_a_number(self):
        data = payload({"titulo": "Cupom de desconto"})

        self.assertEqual(discount_types_from_payload(data), [])

    def test_multiple_coupon_destaques_keep_only_first_storefront_coupon(self):
        for first, second in (("5", "7"), ("3", "5")):
            with self.subTest(first=first, second=second):
                data = payload(
                    {
                        "dscFlag": f"AC Cupom Desconto 3P - {first}%",
                        "titulo": f"Cupom de desconto - {first}%",
                    },
                    {
                        "dscFlag": f"AC Cupom Desconto 3P - {second}%",
                        "titulo": f"Cupom de desconto - {second}%",
                    },
                )

                self.assertEqual(
                    discount_types_from_payload(data),
                    [f"USE O CUPOM DESCONTO {first}%"],
                )

    def test_coupon_without_percent_skips_to_next_explicit_coupon(self):
        data = payload(
            {"titulo": "Cupom de desconto"},
            {"dscFlag": "AC Cupom Desconto 3P - 7%", "titulo": "Cupom de desconto - 7%"},
        )

        self.assertEqual(
            discount_types_from_payload(data),
            ["USE O CUPOM DESCONTO 7%"],
        )

    def test_fetch_discount_type_returns_one_value_with_one_request(self):
        data = payload(
            {"titulo": "Cupom de desconto - 5%"},
            {"titulo": "Cupom de desconto - 7%"},
        )
        response = SimpleNamespace(status_code=200, text="payload", json=lambda: data)

        with patch("seda.casas_bahia.destaque_api.requests.Session") as session_factory:
            session_factory.return_value.get.return_value = response
            result = fetch_discount_type("1580546835", "seller", timeout=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["discount_type"], "USE O CUPOM DESCONTO 5%")
        self.assertEqual(result["count"], 1)
        session_factory.return_value.get.assert_called_once()

    def test_percent_from_another_destaque_is_not_borrowed_by_coupon(self):
        data = payload(
            {"titulo": "Cupom de desconto"},
            {"dscFlag": "20% de desconto"},
        )

        self.assertEqual(discount_types_from_payload(data), ["20% de desconto"])

    def test_translation_preserves_canonical_portuguese_coupon(self):
        self.assertEqual(
            translate_value("discount_type", "USE O CUPOM DESCONTO 10%"),
            "USE O CUPOM DESCONTO 10%",
        )

    def test_backfill_rechecks_ambiguous_percent_but_skips_canonical_coupon(self):
        self.assertTrue(_needs_discount_type_backfill(""))
        self.assertTrue(_needs_discount_type_backfill("10% de desconto"))
        self.assertTrue(_needs_discount_type_backfill("10% discount off"))
        self.assertTrue(
            _needs_discount_type_backfill(
                "USE O CUPOM DESCONTO 5%; USE O CUPOM DESCONTO 7%"
            )
        )
        self.assertFalse(_needs_discount_type_backfill("USE O CUPOM DESCONTO 10%"))
        self.assertFalse(_needs_discount_type_backfill("No Pix"))

    def test_backfill_replaces_ambiguous_percent_and_csv_keeps_canonical_coupon(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.csv"
            output_path = Path(directory) / "output.csv"
            columns = [
                "retailer_sku_id",
                "seller_id",
                "discount_type",
                "fetch_method",
                "parse_status",
            ]
            with input_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerow(
                    {
                        "retailer_sku_id": "1582613546",
                        "seller_id": "235772",
                        "discount_type": "10% discount off",
                        "fetch_method": "listing",
                        "parse_status": "listing_ok",
                    }
                )
            args = SimpleNamespace(
                input=str(input_path),
                output=str(output_path),
                force=False,
                limit=0,
                timeout=0,
            )
            with patch(
                "seda.casas_bahia.listing_discount_backfill.fetch_discount_type",
                return_value={
                    "success": True,
                    "discount_type": "USE O CUPOM DESCONTO 10%",
                },
            ):
                result = run(args)

            with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
                saved = next(csv.DictReader(handle))

        self.assertEqual(result["stats"]["updated"], 1)
        self.assertEqual(saved["discount_type"], "USE O CUPOM DESCONTO 10%")
        self.assertIn("casas_bahia_destaque_api", saved["fetch_method"])


if __name__ == "__main__":
    unittest.main()
