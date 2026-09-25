"""Offline Mode 1-1 REST transport, identity and optional-price contracts."""

from contextlib import ExitStack
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

_DISABLED_ENV = Path(__file__).with_name("MODE11_TEST_ENV_DOES_NOT_EXIST")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ["SEDA_ENV_PATH"] = str(_DISABLED_ENV)

from seda import parsers
from seda.casas_bahia import listing_modes
from seda.casas_bahia import rest_url_first as rest


BASE = "https://www.casasbahia.com.br"
URL = BASE + "/tv/b?page=1"


def product(sku=201, product_id=1001, seller=None):
    item = {"id": product_id, "sku": sku, "href": f"/smart-tv-samsung/p/{sku}",
            "title": "Smart TV Samsung 43 polegadas DU7700", "rating": 4.5, "ratingCount": 12}
    if seller is not None:
        item["lojista"] = seller
    return item


def offer(sku=201, product_id=1001, seller=7, price=1500):
    return {"skuId": sku, "productId": product_id, "sellerId": seller,
            "currentPrice": price, "oldPrice": 1800, "standardPrice": 1600,
            "availability": {"IdSku": sku, "IdLojista": seller}}


def response(products=None, queries=None, *, status=200, content_type="application/json"):
    data = {"products": [product()] if products is None else products,
            "queries": {} if queries is None else queries}
    value = Mock(status_code=status, text=json.dumps(data), headers={"content-type": content_type})
    value.json.return_value = data
    return value


class RestURLFirstTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.environment = {"SEDA_ENV_PATH": str(_DISABLED_ENV), "SEDA_PRODUCT_LINE": "TV",
                            "SEDA_TRANSLATE_OUTPUT": "0", "SEDA_CASAS_BAHIA_LISTING_MODE": "1-1",
                            "SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "0"}
        self.stack.enter_context(patch.dict(os.environ, self.environment, clear=True))
        self.session = Mock()
        self.session.get.return_value = response()
        self.stack.enter_context(patch.object(rest.requests, "Session", return_value=self.session))
        self.prices = self.stack.enter_context(patch.object(rest.price_api, "fetch_listing_prices"))
        self.prices.return_value = {"success": True, "status_code": 200, "offers": [offer()]}
        self.sleep = self.stack.enter_context(patch.object(rest.time, "sleep"))
        self.logs = io.StringIO()
        self.stack.enter_context(patch("sys.stdout", self.logs))
        rest.clear_state()
        self.addCleanup(rest.clear_state)

    def products(self, result):
        self.assertTrue(result["success"], result)
        payload = parsers.extract_next_data(result["text"])
        return payload["props"]["pageProps"]["initialState"]["search"]["results"]["products"]

    def rows(self, result, url=URL):
        return parsers.parse_listing(result["text"], "Casas Bahia", BASE, url)

    def test_direct_rest_returns_url_and_complete_quote_without_browser(self):
        with patch("subprocess.Popen", side_effect=AssertionError("browser_must_not_start")):
            result = rest.fetch_listing(URL)
        self.assertEqual(result["method"], "api_partner_url_first")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(self.products(result)[0]["price"]["currentPrice"], 1500)
        self.assertEqual(self.rows(result)[0]["product_url"], BASE + "/smart-tv-samsung/p/201")
        self.session.get.assert_called_once()
        self.session.close.assert_called_once()
        self.assertTrue(self.prices.call_args.kwargs["include_offers"])
        self.assertTrue(all(item["method"] == rest.METHOD for item in result["trace"]))

    def test_complete_valid_source_rows_equal_actual_mode_one_output(self):
        items = [product(201, 1001, 7), product(202, 1002, 8)]
        offers = [offer(201, 1001, 7), offer(202, 1002, 8, price=1700)]
        self.prices.return_value = {"success": True, "status_code": 200, "offers": offers,
                                   "prices": {"1001": offers[0], "201": offers[0],
                                              "1002": offers[1], "202": offers[1]}, "count": 4}
        for url in (URL, URL + "&ordenacao=maisvendidos"):
            with self.subTest(url=url):
                self.session.get.return_value = response(deepcopy(items), {"page": "1"})
                legacy = listing_modes.fetch_listing(url, mode="1")
                self.assertTrue(legacy["success"], legacy)
                legacy_rows = self.rows(legacy, url)
                self.session.get.return_value = response(deepcopy(items), {"page": "1"})
                new_result = rest.fetch_listing(url)
                self.assertTrue(new_result["success"], new_result)
                self.assertEqual(self.rows(new_result, url), legacy_rows)

    def test_same_product_multi_sku_joins_exact_url_not_last_product_offer(self):
        self.session.get.return_value = response([product(201), product(202)])
        self.prices.return_value = {"success": True, "status_code": 200,
                                   "offers": [offer(202, price=2200), offer(201, price=1500)],
                                   "prices": {"1001": offer(202, price=2200)}}
        values = self.products(rest.fetch_listing(URL))
        self.assertEqual([item["price"]["currentPrice"] for item in values], [1500, 2200])
        self.assertEqual([item["sku"] for item in values], ["201", "202"])

    def test_known_seller_selects_matching_offer(self):
        self.session.get.return_value = response([product(seller=7)])
        self.prices.return_value["offers"] = [offer(seller=8, price=1200), offer(seller=7, price=1500)]
        value = self.products(rest.fetch_listing(URL))[0]
        self.assertEqual(value["price"]["currentPrice"], 1500)
        self.assertEqual(value["lojista"], "7")

    def test_unknown_seller_ambiguous_quotes_keep_url_and_pending(self):
        self.prices.return_value["offers"] = [offer(seller=8), offer(seller=7)]
        result = rest.fetch_listing(URL)
        self.assertTrue(self.products(result)[0]["_casas_listing_price_pending"])
        self.assertEqual(len(self.rows(result)), 1)
        self.session.get.assert_called_once()

    def test_same_identity_conflicting_quotes_are_not_last_wins(self):
        self.prices.return_value["offers"] = [offer(price=1000), offer(price=1500)]
        self.assertTrue(self.products(rest.fetch_listing(URL))[0]["_casas_listing_price_pending"])

    def test_wrong_product_sku_seller_or_missing_identity_keeps_url(self):
        cases = [offer(product_id=9999), offer(sku=999), offer(seller=8),
                 dict(offer(), productId=""), dict(offer(), sellerId="")]
        for bad in cases:
            with self.subTest(bad=bad):
                rest.clear_state()
                self.session.get.return_value = response([product(seller=7)])
                self.prices.return_value["offers"] = [bad]
                value = self.products(rest.fetch_listing(URL))[0]
                self.assertTrue(value["_casas_listing_price_pending"])
                self.assertEqual(value["price"], {})

    def test_availability_identity_conflict_is_quarantined(self):
        bad = offer()
        bad["availability"]["IdSku"] = 999
        self.prices.return_value["offers"] = [bad]
        self.assertTrue(self.products(rest.fetch_listing(URL))[0]["_casas_listing_price_pending"])

    def test_availability_product_conflict_is_quarantined(self):
        bad = offer()
        bad["availability"]["IdProduto"] = 999
        self.prices.return_value["offers"] = [bad]
        self.assertTrue(self.products(rest.fetch_listing(URL))[0]["_casas_listing_price_pending"])

    def test_inline_availability_product_conflict_quarantined_when_price_api_fails(self):
        for availability_product in (99, "invalid", True):
            with self.subTest(availability_product=availability_product):
                source = product(100, 10, 7)
                source["price"] = offer(100, 10, 7, price=850)
                source["price"]["availability"]["IdProduto"] = availability_product
                baseline = deepcopy(source)
                self.session.get.return_value = response([source])
                self.prices.return_value = {"success": False, "status_code": 403,
                                           "error": "price_http_403", "offers": []}
                result = rest.fetch_listing(URL)
                value = self.products(result)[0]
                self.assertEqual(value["price"], {})
                self.assertTrue(value["_casas_listing_price_pending"])
                self.assertEqual(value["id"], 10)
                self.assertEqual(value["sku"], "100")
                self.assertEqual(self.rows(result)[0]["final_sku_price"], "")
                self.assertEqual(source, baseline)

    def test_inline_availability_product_conflict_can_receive_valid_fresh_quote(self):
        source = product(100, 10, 7)
        source["price"] = offer(100, 10, 7, price=850)
        source["price"]["availability"]["IdProduto"] = 99
        self.session.get.return_value = response([source])
        self.prices.return_value["offers"] = [offer(100, 10, 7, price=950)]
        value = self.products(rest.fetch_listing(URL))[0]
        self.assertEqual(value["price"]["currentPrice"], 950)
        self.assertNotIn("_casas_listing_price_pending", value)

    def test_matching_inline_availability_product_survives_price_api_failure(self):
        source = product(100, 10, 7)
        source["price"] = offer(100, 10, 7, price=850)
        source["price"]["availability"]["IdProduto"] = 10
        self.session.get.return_value = response([source])
        self.prices.return_value = {"success": False, "status_code": 403,
                                   "error": "price_http_403", "offers": []}
        value = self.products(rest.fetch_listing(URL))[0]
        self.assertEqual(value["price"]["currentPrice"], 850)
        self.assertNotIn("_casas_listing_price_pending", value)

    def test_initialization_exception_returns_safe_failure_contract(self):
        with patch.object(rest.requests, "Session", side_effect=RuntimeError("do_not_log_this")):
            result = rest.fetch_listing(URL)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "rest_listing_request_failed")
        self.assertEqual(result["trace"][0]["method"], rest.METHOD)
        self.assertNotIn("do_not_log_this", json.dumps(result) + self.logs.getvalue())

    def test_price_http_403_does_not_retry_search_or_drop_url(self):
        self.prices.return_value = {"success": False, "status_code": 403,
                                   "error": "price_http_403", "offers": []}
        result = rest.fetch_listing(URL)
        self.assertTrue(self.products(result)[0]["_casas_listing_price_pending"])
        self.assertEqual(result["trace"][0]["price_error"], "price_http_403")
        self.session.get.assert_called_once()
        self.prices.assert_called_once()

    def test_success_flag_without_http_200_never_attaches_offer(self):
        for status in (403, 0, None, "200"):
            with self.subTest(status=status):
                self.prices.return_value = {"success": True, "status_code": status, "offers": [offer()]}
                result = rest.fetch_listing(URL)
                self.assertTrue(self.products(result)[0]["_casas_listing_price_pending"])
                self.assertEqual(result["trace"][0]["price_error"], "invalid_price_status")

    def test_price_exception_does_not_leak_error_or_retry_search(self):
        self.prices.side_effect = RuntimeError("must_not_be_logged")
        result = rest.fetch_listing(URL)
        self.assertTrue(self.products(result)[0]["_casas_listing_price_pending"])
        self.assertNotIn("must_not_be_logged", self.logs.getvalue() + json.dumps(result))
        self.session.get.assert_called_once()

    def test_empty_partial_offers_preserve_all_products_and_order(self):
        self.session.get.return_value = response([product(201), product(202), product(203)])
        result = rest.fetch_listing(URL)
        values = self.products(result)
        self.assertEqual([value["sku"] for value in values], ["201", "202", "203"])
        self.assertEqual(result["trace"][0]["price_pending_products"], 2)
        self.assertEqual(len(self.rows(result)), 3)

    def test_no_price_offers_preserve_product(self):
        self.prices.return_value["offers"] = []
        self.assertTrue(self.products(rest.fetch_listing(URL))[0]["_casas_listing_price_pending"])

    def test_invalid_page_sort_url_and_duplicate_sku_rejected_before_price(self):
        cases = [(response(queries={"page": 2}), URL),
                 (response(queries={"sortby": "relevancia"}), URL + "&ordenacao=mais-vendidos"),
                 (response([dict(product(), href="https://foreign.invalid/p/201")]), URL),
                 (response([product(), product()]), URL)]
        for value, url in cases:
            with self.subTest(url=url, response=value.json.return_value):
                rest.clear_state()
                self.prices.reset_mock()
                self.session.get.return_value = value
                result = rest.fetch_listing(url)
                self.assertFalse(result["success"])
                self.assertEqual(len(result["trace"]), 3)
                self.prices.assert_not_called()

    def test_unsafe_requested_url_rejected_without_any_http(self):
        for url in ("http://www.casasbahia.com.br/tv/b", "https://foreign.invalid/tv/b",
                    "https://account@www.casasbahia.com.br/tv/b", BASE + ":444/tv/b"):
            with self.subTest(url=url):
                self.assertFalse(rest.fetch_listing(url)["success"])
        self.session.get.assert_not_called()
        self.prices.assert_not_called()

    def test_search_retries_three_and_logs_exact_search_status(self):
        self.session.get.side_effect = [response(status=403), response(status=403), response()]
        result = rest.fetch_listing(URL)
        self.assertTrue(result["success"])
        self.assertEqual([item["status_code"] for item in result["trace"]], [403, 403, 200])
        self.prices.assert_called_once()
        self.assertIn("attempt=1/3 FAILED status=403", self.logs.getvalue())

    def test_search_retries_hard_ceiling_three_and_honors_lower_config(self):
        self.session.get.return_value = response(status=403)
        for retries, count in (("99", 3), ("0", 1), ("invalid", 3)):
            with self.subTest(retries=retries):
                self.session.get.reset_mock()
                os.environ["SEDA_CASAS_BAHIA_SEARCH_RETRIES"] = retries
                self.assertFalse(rest.fetch_listing(URL)["success"])
                self.assertEqual(self.session.get.call_count, count)
        self.prices.assert_not_called()

    def test_timeout_results_page_region_variant_and_retry_delay_config_preserved(self):
        os.environ.update(SEDA_TIMEOUT="17", SEDA_CASAS_BAHIA_RESULTS_PER_PAGE="40",
                          SEDA_CASAS_BAHIA_REGION_ID="88", SEDA_CASAS_BAHIA_VARIANT_CONFIGURATION="custom",
                          SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS="2")
        self.session.get.side_effect = [response(status=403), response(status=403), response()]
        result = rest.fetch_listing(URL + "&ordenacao=mais-vendidos")
        self.assertTrue(result["success"])
        args = self.session.get.call_args.kwargs
        self.assertEqual(args["timeout"], 17)
        self.assertEqual(args["params"]["resultsperpage"], "40")
        self.assertEqual(args["params"]["regionid"], "88")
        self.assertEqual(args["params"]["variantconfiguration"], "custom")
        self.assertEqual(args["params"]["sortby"], "maisvendidos")
        self.assertEqual([value.args[0] for value in self.sleep.call_args_list], [2, 4])

    def test_disabled_price_config_honored_without_losing_url(self):
        os.environ["SEDA_CASAS_BAHIA_ATTACH_PRICES"] = "0"
        result = rest.fetch_listing(URL)
        self.assertTrue(self.products(result)[0]["_casas_listing_price_pending"])
        self.assertEqual(result["trace"][0]["price_error"], "price_attach_disabled")
        self.prices.assert_not_called()

    def test_filtered_accessories_do_not_change_remaining_product_order(self):
        items = [product(201), dict(product(202), title="Suporte para TV"), product(203)]
        self.session.get.return_value = response(items)
        result = rest.fetch_listing(URL)
        self.assertTrue(result["success"])
        self.assertEqual([row["product_url"].rsplit("/", 1)[1] for row in self.rows(result)], ["201", "203"])

    def test_response_order_or_filtered_identity_change_rejected(self):
        self.session.get.return_value = response([product(201), product(202)])
        original = parsers.parse_listing
        with patch.object(parsers, "parse_listing", side_effect=lambda *args, **kwargs: list(reversed(original(*args, **kwargs)))):
            result = rest.fetch_listing(URL)
        self.assertFalse(result["success"])
        self.prices.assert_not_called()

    def test_previous_page_repeated_is_rejected_but_main_bsr_are_separate(self):
        self.assertTrue(rest.fetch_listing(URL)["success"])
        self.prices.reset_mock()
        second = rest.fetch_listing(BASE + "/tv/b?page=2")
        self.assertFalse(second["success"])
        self.assertEqual(second["error"], "earlier_page_repeated_for_other_page")
        self.prices.assert_not_called()
        self.assertTrue(rest.fetch_listing(URL + "&ordenacao=mais-vendidos")["success"])

    def test_new_run_can_clear_accepted_page_evidence(self):
        self.assertTrue(rest.fetch_listing(URL)["success"])
        rest.clear_state()
        self.assertTrue(rest.fetch_listing(BASE + "/tv/b?page=2")["success"])

    def test_source_data_is_not_mutated(self):
        value = response([product()])
        baseline = deepcopy(value.json.return_value)
        self.session.get.return_value = value
        self.assertTrue(rest.fetch_listing(URL)["success"])
        self.assertEqual(value.json.return_value, baseline)

    def test_non_json_invalid_json_and_non_object_search_fail_safely(self):
        invalid = response()
        invalid.json.side_effect = ValueError("must_not_be_logged")
        non_object = response()
        non_object.json.return_value = []
        for value in (response(content_type="text/html"), invalid, non_object):
            with self.subTest(response=value):
                self.session.get.return_value = value
                result = rest.fetch_listing(URL)
                self.assertFalse(result["success"])
                self.assertNotIn("must_not_be_logged", self.logs.getvalue() + json.dumps(result))
        self.prices.assert_not_called()


if __name__ == "__main__":
    unittest.main()
