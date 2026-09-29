"""Postal-code request contracts using synthetic responses only."""
from contextlib import ExitStack, nullcontext
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

_DISABLED_ENV = Path(__file__).with_name("POSTAL_TEST_ENV_DOES_NOT_EXIST")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
with patch.dict(os.environ, {"SEDA_ENV_PATH": str(_DISABLED_ENV)}):
    from seda import step00_config
    from seda.casas_bahia import detail_api as casas
    from seda.common.translations import _translate_delivery
    from seda.magalu import detail_api as magalu
    from seda.magalu import search_api


class PostalCodeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SEDA_ENV_PATH": str(_DISABLED_ENV)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_common_and_magalu_listing_default_destination(self):
        self.assertEqual(step00_config.DEFAULT_POSTAL_CODE, "01010-010")
        payload = search_api._payload("https://www.magazineluiza.com.br/busca/tv/", 20)
        self.assertEqual(payload["variables"]["zipCode"], "01010-010")

    def test_magalu_item_and_shipping_use_same_digit_only_destination(self):
        with patch.object(magalu, "_post", return_value={}) as post:
            magalu._request_item("fixture-item", 1, [])
        self.assertEqual(post.call_args.args[0]["variables"]["zipcode"], "01010010")
        self.assertEqual(magalu._shipping_request({})["zipcode"], "01010010")

    def test_existing_magalu_explicit_destination_precedence_is_preserved(self):
        os.environ["SEDA_POSTAL_CODE"] = "01311-000"
        self.assertEqual(magalu._shipping_request({})["zipcode"], "01311000")
        payload = search_api._payload("https://www.magazineluiza.com.br/busca/tv/", 20)
        self.assertEqual(payload["variables"]["zipCode"], "01311-000")
        os.environ["SEDA_MAGALU_SHIPPING_ZIP_CODE"] = "20040-020"
        self.assertEqual(magalu._shipping_request({})["zipcode"], "20040020")

    def test_magalu_non_digit_destination_uses_new_default(self):
        os.environ["SEDA_MAGALU_SHIPPING_ZIP_CODE"] = "---"
        self.assertEqual(magalu._shipping_request({})["zipcode"], "01010010")

    def test_casas_freight_request_default_and_explicit_destinations(self):
        response = Mock(status_code=200, headers={"content-type": "application/json"})
        response.json.return_value = {"options": []}
        with ExitStack() as stack:
            stack.enter_context(patch.object(casas, "_headers", return_value={}))
            stack.enter_context(patch.object(casas, "throttle"))
            stack.enter_context(patch.object(casas, "_freight_transports", return_value=["requests"]))
            request = stack.enter_context(patch.object(casas, "_freight_get", return_value=response))
            for env, explicit, expected in (({}, None, "01010-010"),
                                             ({"SEDA_POSTAL_CODE": "01311-000"}, None, "01311-000"),
                                             ({"SEDA_POSTAL_CODE": "01311-000"}, "20040-020", "20040-020")):
                with self.subTest(expected=expected), patch.dict(os.environ, env):
                    result = casas.fetch_freight("fixture-sku", "fixture-seller", zipcode=explicit)
                    self.assertTrue(result["success"])
                    self.assertIn("/zipcode/" + expected + "/source/CB", request.call_args.args[1])

    def test_casas_pickup_request_uses_same_default(self):
        response = Mock(status_code=200, headers={"content-type": "application/json"})
        response.json.return_value = {}
        with patch.object(casas, "_pickup_headers", return_value={}), \
             patch.object(casas, "_pickup_text", return_value="fixture pickup"), \
             patch.object(casas, "request_with_retry", side_effect=lambda call, **kwargs: call()), \
             patch.object(casas.requests, "get", return_value=response) as request:
            result = casas.fetch_pickup("fixture-sku", "fixture-seller")
        self.assertTrue(result["success"])
        self.assertEqual(request.call_args.kwargs["params"]["cep"], "01010010")

    def test_delivery_text_contract_is_unchanged(self):
        cases = (
            ({"options": [{"name": "Normal", "description": "fixture deadline"}]}, "Normal fixture deadline"),
            ({"options": [{"name": "Retira", "description": "fixture store"}]}, "Delivery unavailable for this ZIP code"),
            ({}, ""),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                detail = casas._freight_detail(payload)
                self.assertEqual(_translate_delivery(detail["delivery_availability"]), expected)

    def test_casas_browser_backfill_default_matches_api(self):
        from seda.casas_bahia import freight_cdp_backfill as backfill
        captured = {}

        def fake_run(args):
            captured["zipcode"] = args.zipcode
            return {"stats": {}, "output": "fixture.csv"}

        with patch("sys.argv", ["freight_cdp_backfill"]), \
             patch.object(backfill, "run", new=Mock(side_effect=fake_run)), \
             patch.object(backfill.asyncio, "run", side_effect=lambda result: result), \
             patch.object(backfill, "detail_consumer_guard", return_value=nullcontext()), \
             patch("builtins.print"):
            backfill.main()
        self.assertEqual(captured["zipcode"], "01010-010")


if __name__ == "__main__":
    unittest.main()
