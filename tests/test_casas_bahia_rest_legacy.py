"""Mode 1 restored REST behavior. Synthetic HTTP only, no real environment."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

_DISABLED_ENV = Path(__file__).with_name("REST_LEGACY_TEST_ENV_DOES_NOT_EXIST")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ["SEDA_ENV_PATH"] = str(_DISABLED_ENV)

from seda import parsers
from seda.casas_bahia import price_api, rest_legacy, search_api


URL = "https://www.casasbahia.com.br/tv/b?page=6&ordenacao=mais-vendidos"


def product(sku=101):
    return {"id": 10, "sku": sku, "href": f"/smart-tv/p/{sku}", "lojista": 7,
            "title": "Smart TV Samsung 43 polegadas"}


def quote(sku=101):
    return {"productId": 10, "skuId": sku, "sellerId": 7, "currentPrice": 850}


def response(data=None, status=200, content_type="application/json"):
    data = {"products": [product()]} if data is None else data
    value = Mock(status_code=status, text=json.dumps(data), headers={"content-type": content_type})
    value.json.return_value = data
    return value


class RestLegacyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "SEDA_ENV_PATH": str(_DISABLED_ENV), "SEDA_PRODUCT_LINE": "TV",
            "SEDA_TRANSLATE_OUTPUT": "0", "SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "0",
        }, clear=True))
        self.session = Mock()
        self.session.get.return_value = response()
        self.stack.enter_context(patch.object(rest_legacy.requests, "Session", return_value=self.session))
        self.price = self.stack.enter_context(patch.object(price_api, "fetch_listing_prices"))
        self.price.return_value = {"success": True, "prices": {"10": quote(), "101": quote()}, "count": 2}
        self.sleep = self.stack.enter_context(patch.object(rest_legacy.time, "sleep"))

    def products(self, result):
        self.assertTrue(result["success"], result)
        return parsers.extract_next_data(result["text"])["props"]["pageProps"]["initialState"]["search"]["results"]["products"]

    def test_unchanged_helpers_are_reused(self):
        for name in ("_params", "_headers", "_as_next_data_html", "_supported_listing_path"):
            self.assertIs(getattr(rest_legacy, name), getattr(search_api, name))

    def test_complete_response_attaches_same_legacy_price(self):
        result = rest_legacy.fetch_search_listing(URL)
        self.assertEqual(self.products(result)[0]["price"], quote())
        self.assertEqual(result["trace"][0]["price_count"], 2)
        self.session.get.assert_called_once()
        self.assertNotIn("include_offers", self.price.call_args.kwargs)

    def test_prices_optional_missing_price_still_success_without_repeat(self):
        self.price.return_value = {"success": False, "prices": {}, "error": "price_http_403:sensitive body"}
        result = rest_legacy.fetch_search_listing(URL)
        self.assertNotIn("price", self.products(result)[0])
        self.assertEqual(result["trace"][0]["price_error"], "price_http_403")
        self.assertNotIn("sensitive body", json.dumps(result))
        self.session.get.assert_called_once()

    def test_historical_product_id_first_join_is_preserved(self):
        self.price.return_value = {"success": True, "prices": {"10": quote(999), "101": quote(101)}, "count": 2}
        result = rest_legacy.fetch_search_listing(URL)
        self.assertEqual(self.products(result)[0]["price"]["skuId"], 999)
        self.session.get.assert_called_once()

    def test_no_new_page_sort_price_identity_or_url_validation(self):
        original = {"queries": {"page": 1, "sortby": "relevancia"},
                    "products": [dict(product(), href="/not-a-product-url", lojista=99)]}
        self.session.get.return_value = response(original)
        self.assertTrue(rest_legacy.fetch_search_listing(URL)["success"])
        self.session.get.assert_called_once()

    def test_default_retries_two_means_three_gets(self):
        self.session.get.return_value = response(status=403)
        result = rest_legacy.fetch_search_listing(URL)
        self.assertFalse(result["success"])
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual([item["attempt"] for item in result["trace"]], [1, 2, 3])

    def test_environment_retry_four_means_five_gets_without_new_ceiling(self):
        os.environ["SEDA_CASAS_BAHIA_SEARCH_RETRIES"] = "4"
        os.environ["SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS"] = "2"
        self.session.get.side_effect = [response(status=403)] * 4 + [response()]
        self.assertTrue(rest_legacy.fetch_search_listing(URL)["success"])
        self.assertEqual(self.session.get.call_count, 5)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 4, 6, 8])

    def test_zero_retries_and_negative_retry_behavior_are_historical(self):
        os.environ["SEDA_CASAS_BAHIA_SEARCH_RETRIES"] = "0"
        self.session.get.return_value = response(status=403)
        self.assertFalse(rest_legacy.fetch_search_listing(URL)["success"])
        self.session.get.assert_called_once()
        self.session.get.reset_mock()
        os.environ["SEDA_CASAS_BAHIA_SEARCH_RETRIES"] = "-1"
        result = rest_legacy.fetch_search_listing(URL)
        self.assertFalse(result["success"])
        self.assertEqual(result["trace"], [])
        self.session.get.assert_not_called()

    def test_invalid_retry_environment_still_raises_value_error(self):
        os.environ["SEDA_CASAS_BAHIA_SEARCH_RETRIES"] = "bad"
        with self.assertRaises(ValueError):
            rest_legacy.fetch_search_listing(URL)
        self.session.get.assert_not_called()

    def test_empty_products_non_json_invalid_json_retry_then_success(self):
        invalid_json = response()
        invalid_json.json.side_effect = ValueError("private value")
        os.environ["SEDA_CASAS_BAHIA_SEARCH_RETRIES"] = "3"
        self.session.get.side_effect = [response({"products": []}), response(content_type="text/html"), invalid_json, response()]
        result = rest_legacy.fetch_search_listing(URL)
        self.assertTrue(result["success"])
        self.assertEqual([item.get("error") for item in result["trace"]],
                         ["empty_products", "non_json_or_blocked", "invalid_json", None])
        self.assertNotIn("private value", json.dumps(result))

    def test_json_list_retains_legacy_exception_not_added_retry_gate(self):
        self.session.get.return_value = response([])
        with self.assertRaises(AttributeError):
            rest_legacy.fetch_search_listing(URL)
        self.session.get.assert_called_once()

    def test_search_exception_trace_uses_type_only(self):
        self.session.get.side_effect = [RuntimeError("private value"), response()]
        result = rest_legacy.fetch_search_listing(URL)
        self.assertTrue(result["success"])
        self.assertEqual(result["trace"][0]["error"], "RuntimeError")
        self.assertNotIn("private value", json.dumps(result))

    def test_price_exception_keeps_success_and_only_safe_type(self):
        self.price.side_effect = RuntimeError("private value")
        result = rest_legacy.fetch_search_listing(URL)
        self.assertTrue(result["success"])
        self.assertEqual(result["trace"][0]["price_error"], "RuntimeError")
        self.assertNotIn("private value", json.dumps(result))

    def test_disabled_price_attachment_preserves_legacy_success(self):
        os.environ["SEDA_CASAS_BAHIA_ATTACH_PRICES"] = "0"
        result = rest_legacy.fetch_search_listing(URL)
        self.assertTrue(result["success"])
        self.assertEqual(result["trace"][0]["price_error"], "price_attach_disabled")
        self.price.assert_not_called()

    def test_query_and_timeout_settings_pass_through_unchanged(self):
        os.environ.update(SEDA_TIMEOUT="17", SEDA_CASAS_BAHIA_RESULTS_PER_PAGE="40",
                          SEDA_CASAS_BAHIA_REGION_ID="88", SEDA_CASAS_BAHIA_VARIANT_CONFIGURATION="fixture")
        self.assertTrue(rest_legacy.fetch_search_listing(URL)["success"])
        args = self.session.get.call_args.kwargs
        self.assertEqual(args["timeout"], 17)
        self.assertEqual(args["params"]["resultsperpage"], "40")
        self.assertEqual(args["params"]["page"], "6")
        self.assertEqual(args["params"]["sortby"], "maisvendidos")
        self.assertEqual(args["params"]["regionid"], "88")
        self.assertEqual(args["params"]["variantconfiguration"], "fixture")

    def test_foreign_or_unsupported_listing_rejected_without_network(self):
        for url in ("https://example.invalid/tv/b", "https://www.casasbahia.com.br/unknown/b"):
            with self.subTest(url=url):
                result = rest_legacy.fetch_search_listing(url)
                self.assertFalse(result["success"])
                self.assertEqual(result["trace"], [])
        self.session.get.assert_not_called()
        self.price.assert_not_called()

    def test_historical_mutation_and_html_conversion_preserved(self):
        raw = {"queries": {"page": "6"}, "products": [product()]}
        self.session.get.return_value = response(raw)
        result = rest_legacy.fetch_search_listing(URL)
        self.assertEqual(raw["products"][0]["price"], quote())
        self.assertEqual(result["text"], search_api._as_next_data_html(raw, URL))


if __name__ == "__main__":
    unittest.main()
