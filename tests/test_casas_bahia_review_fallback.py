import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from seda.casas_bahia.review_api import _fetch_review_page_zenrows, fetch_reviews
from seda.step08_detail_enrichment import _merge_casas_bahia_apis


class _Response:
    def __init__(self, status_code, payload=None, content_type="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": content_type}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _payload(rating=4.5, rating_qty=2, recommendation=80, reviews=None):
    return {
        "review": {
            "rating": rating,
            "ratingQty": rating_qty,
            "recommendationPercentage": recommendation,
            "aiSummary": {"aiSummaryText": "Resumo"},
            "userReviews": reviews if reviews is not None else [{"text": "Bom produto"}],
        }
    }


def _zenrows_result(*, success, payload=None, error="", status_code=200):
    return SimpleNamespace(
        success=success,
        text=json.dumps(payload) if payload is not None else "",
        error=error,
        status_code=status_code,
        estimated_multiplier="10x",
        headers={"X-Request-Cost": "test-cost"},
    )


class CasasBahiaReviewFallbackTests(unittest.TestCase):
    def test_legacy_checkpoint_synthetic_zero_is_cleared_without_touching_real_zero(self):
        env = {"SEDA_CASAS_BAHIA_API_ENRICH": "0"}
        synthetic = {
            "retailer": "Casas Bahia",
            "recommendation_intent": "0% dos clientes recomendam esse produto",
            "parse_status": "listing+recommendation_default_0",
        }
        real = {
            "retailer": "Casas Bahia",
            "recommendation_intent": "0% dos clientes recomendam esse produto",
            "parse_status": "reviews_0",
        }
        with patch.dict(os.environ, env, clear=False):
            _merge_casas_bahia_apis(synthetic)
            _merge_casas_bahia_apis(real)
        self.assertEqual(synthetic["recommendation_intent"], "")
        self.assertIn(
            "recommendation_default_0_cleared",
            synthetic["parse_status"].split("+"),
        )
        self.assertEqual(
            real["recommendation_intent"],
            "0% dos clientes recomendam esse produto",
        )

    def test_direct_success_does_not_call_zenrows(self):
        direct = _Response(200, _payload())
        with patch("seda.casas_bahia.review_api.request_with_retry", return_value=direct), patch(
            "seda.magalu.zenrows_client.request_url"
        ) as zenrows:
            result = fetch_reviews("product-1", limit=20, timeout=5)

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "casas_bahia_reviews_api")
        self.assertEqual(result["general"]["recommendationPercentage"], 80)
        self.assertFalse(result["zenrows_requested"])
        zenrows.assert_not_called()

    def test_direct_failure_uses_one_10x_br_non_js_fallback(self):
        direct = _Response(403, None, "text/html")
        paid = _zenrows_result(success=True, payload=_payload())
        with patch("seda.casas_bahia.review_api.request_with_retry", return_value=direct), patch(
            "seda.magalu.zenrows_client.request_url", return_value=paid
        ) as zenrows:
            result = fetch_reviews(
                "product-2",
                limit=20,
                timeout=7,
                referer_url="https://www.casasbahia.com.br/example/p/2",
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["zenrows_requested"])
        zenrows.assert_called_once()
        target_url = zenrows.call_args.args[0]
        kwargs = zenrows.call_args.kwargs
        self.assertIn("/product/product-2/source/CB?", target_url)
        self.assertEqual(kwargs["profile"], "premium_html")
        self.assertEqual(kwargs["extra"]["premium_proxy"], "true")
        self.assertEqual(kwargs["extra"]["proxy_country"], "br")
        self.assertEqual(kwargs["extra"]["custom_headers"], "true")
        self.assertNotIn("js_render", kwargs["extra"])
        self.assertNotIn("x-safe", kwargs["extra_headers"])
        self.assertNotIn("cookie", kwargs["extra_headers"])
        self.assertEqual(kwargs["extra_headers"]["referer"], "https://www.casasbahia.com.br/example/p/2")

    def test_10x_failure_returns_failure_without_second_paid_call(self):
        direct = _Response(403, None, "text/html")
        paid = _zenrows_result(success=False, error="http_403", status_code=403)
        with patch("seda.casas_bahia.review_api.request_with_retry", return_value=direct), patch(
            "seda.magalu.zenrows_client.request_url", return_value=paid
        ) as zenrows:
            result = fetch_reviews("product-3", limit=20, timeout=5)

        self.assertFalse(result["success"])
        self.assertEqual(result["general"], {})
        self.assertEqual(result["reviews"], [])
        self.assertIn("direct_status_403", result["error"])
        self.assertIn("zenrows_http_403", result["error"])
        self.assertIn("casas_bahia_reviews_api", result["method"])
        self.assertIn("casas_bahia_reviews_api_zenrows:10x", result["method"])
        self.assertEqual(result["headers"]["X-Request-Cost"], "test-cost")
        self.assertTrue(result["zenrows_requested"])
        zenrows.assert_called_once()

    def test_one_paid_fallback_is_retained_across_later_direct_page(self):
        blocked = _Response(403, None, "text/html")
        final_direct = _Response(200, _payload(reviews=[]))
        first_page = _payload(reviews=[{"text": f"review-{index}"} for index in range(20)])
        paid = _zenrows_result(success=True, payload=first_page)
        with patch(
            "seda.casas_bahia.review_api.request_with_retry",
            side_effect=[blocked, final_direct],
        ), patch("seda.magalu.zenrows_client.request_url", return_value=paid) as zenrows:
            result = fetch_reviews("product-pages", limit=21, timeout=5)

        self.assertTrue(result["success"])
        self.assertEqual(len(result["reviews"]), 20)
        self.assertIn("casas_bahia_reviews_api_zenrows:10x", result["method"])
        zenrows.assert_called_once()

    def test_numeric_zero_is_preserved_from_zenrows_payload(self):
        direct = _Response(403, None, "text/html")
        paid = _zenrows_result(success=True, payload=_payload(0, 0, 0, []))
        with patch("seda.casas_bahia.review_api.request_with_retry", return_value=direct), patch(
            "seda.magalu.zenrows_client.request_url", return_value=paid
        ):
            result = fetch_reviews("product-4", limit=20, timeout=5)

        self.assertTrue(result["success"])
        self.assertEqual(result["general"]["rating"], 0)
        self.assertEqual(result["general"]["ratingQty"], 0)
        self.assertEqual(result["general"]["recommendationPercentage"], 0)

    def test_direct_zero_review_allows_missing_or_null_user_reviews(self):
        for user_reviews in ("missing", None):
            with self.subTest(user_reviews=user_reviews):
                payload = {
                    "review": {
                        "rating": 0,
                        "ratingQty": 0,
                        "recommendationPercentage": 0,
                    }
                }
                if user_reviews != "missing":
                    payload["review"]["userReviews"] = user_reviews
                direct = _Response(200, payload)
                with patch(
                    "seda.casas_bahia.review_api.request_with_retry",
                    return_value=direct,
                ), patch("seda.magalu.zenrows_client.request_url") as zenrows:
                    result = fetch_reviews("zero-review", limit=20, timeout=5)

                self.assertTrue(result["success"])
                self.assertEqual(result["reviews"], [])
                self.assertEqual(result["general"]["rating"], 0)
                self.assertEqual(result["general"]["ratingQty"], 0)
                self.assertEqual(result["general"]["recommendationPercentage"], 0)
                zenrows.assert_not_called()

    def test_global_kill_switch_and_dry_run_are_never_overridden(self):
        cases = (
            ({"SEDA_ALLOW_ZENROWS": "0", "SEDA_ZENROWS_DRY_RUN": "0"}, "zenrows_disabled"),
            ({"SEDA_ALLOW_ZENROWS": "1", "SEDA_ZENROWS_DRY_RUN": "1"}, "zenrows_dry_run"),
        )
        for env, expected in cases:
            with self.subTest(expected=expected), patch.dict(os.environ, env, clear=False):
                result = _fetch_review_page_zenrows(
                    "product-kill-switch",
                    page=1,
                    page_size=20,
                    timeout=5,
                )
                self.assertEqual(os.environ["SEDA_ALLOW_ZENROWS"], env["SEDA_ALLOW_ZENROWS"])
                self.assertEqual(os.environ["SEDA_ZENROWS_DRY_RUN"], env["SEDA_ZENROWS_DRY_RUN"])

            self.assertFalse(result["success"])
            self.assertIn(expected, result["error"])
            self.assertFalse(result["zenrows_requested"])

    def test_key_missing_is_preflight_but_request_error_is_an_attempt(self):
        cases = (
            (_zenrows_result(success=False, error="key_missing", status_code=0), False),
            (_zenrows_result(success=False, error="request_error:Timeout", status_code=0), True),
        )
        for central_result, expected in cases:
            with self.subTest(error=central_result.error), patch(
                "seda.magalu.zenrows_client.request_url",
                return_value=central_result,
            ):
                result = _fetch_review_page_zenrows(
                    "product-request-state",
                    page=1,
                    page_size=20,
                    timeout=5,
                )

            self.assertFalse(result["success"])
            self.assertEqual(result["zenrows_requested"], expected)

    def test_missing_product_id_never_calls_direct_or_paid_path(self):
        with patch("seda.casas_bahia.review_api.request_with_retry") as direct, patch(
            "seda.magalu.zenrows_client.request_url"
        ) as zenrows:
            result = fetch_reviews("")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "missing_product_id")
        self.assertFalse(result["zenrows_requested"])
        direct.assert_not_called()
        zenrows.assert_not_called()

    def test_failed_review_collection_leaves_recommendation_blank(self):
        row = {
            "retailer": "Casas Bahia",
            "retailer_product_id": "product-5",
            "product_url": "https://www.casasbahia.com.br/example/p/123",
            "sku": "123",
            "detailed_review_content": "",
            "recommendation_intent": "",
            "parse_status": "",
        }
        env = {
            "SEDA_CASAS_BAHIA_API_ENRICH": "1",
            "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API": "0",
            "SEDA_CASAS_BAHIA_FREIGHT_API": "0",
            "SEDA_CASAS_BAHIA_PICKUP_API": "0",
            "SEDA_CASAS_BAHIA_RECS_API": "0",
            "SEDA_CASAS_BAHIA_REVIEW_API": "1",
        }
        failed = {"success": False, "error": "zenrows_http_403", "reviews": [], "general": {}}
        with patch.dict(os.environ, env, clear=False), patch(
            "seda.casas_bahia.review_api.fetch_reviews", return_value=failed
        ):
            _merge_casas_bahia_apis(row)

        self.assertEqual(row["recommendation_intent"], "")
        self.assertIn("reviews_api_failed:zenrows_http_403", row["parse_status"])
        self.assertNotIn("recommendation_default_0", row["parse_status"])

    def test_explicit_zero_recommendation_is_still_written(self):
        row = {
            "retailer": "Casas Bahia",
            "retailer_product_id": "product-6",
            "product_url": "https://www.casasbahia.com.br/example/p/124",
            "sku": "124",
            "detailed_review_content": "",
            "recommendation_intent": "",
            "parse_status": "",
        }
        env = {
            "SEDA_CASAS_BAHIA_API_ENRICH": "1",
            "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API": "0",
            "SEDA_CASAS_BAHIA_FREIGHT_API": "0",
            "SEDA_CASAS_BAHIA_PICKUP_API": "0",
            "SEDA_CASAS_BAHIA_RECS_API": "0",
            "SEDA_CASAS_BAHIA_REVIEW_API": "1",
        }
        success = {
            "success": True,
            "reviews": [],
            "general": {"rating": 0, "ratingQty": 0, "recommendationPercentage": 0},
            "method": "casas_bahia_reviews_api_zenrows:10x",
            "headers": {"X-Request-Cost": "test-cost"},
            "zenrows_requested": True,
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "seda.casas_bahia.review_api.fetch_reviews", return_value=success
        ):
            _merge_casas_bahia_apis(row)

        self.assertEqual(row["recommendation_intent"], "0% dos clientes recomendam esse produto")
        self.assertEqual(row["star_rating"], "0")
        self.assertEqual(row["count_of_reviews"], "0")
        self.assertIn("casas_bahia_reviews_api_zenrows:10x", row["fetch_method"])
        self.assertIn("reviews_zenrows_requested:10x", row["parse_status"])
        self.assertIn("reviews_cost:test-cost", row["parse_status"])

    def test_failed_paid_attempt_records_attempt_and_cost_but_preflight_does_not(self):
        base_row = {
            "retailer": "Casas Bahia",
            "retailer_product_id": "product-trace",
            "product_url": "https://www.casasbahia.com.br/example/p/125",
            "sku": "125",
            "detailed_review_content": "",
            "recommendation_intent": "",
            "parse_status": "",
        }
        env = {
            "SEDA_CASAS_BAHIA_API_ENRICH": "1",
            "SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API": "0",
            "SEDA_CASAS_BAHIA_FREIGHT_API": "0",
            "SEDA_CASAS_BAHIA_PICKUP_API": "0",
            "SEDA_CASAS_BAHIA_RECS_API": "0",
            "SEDA_CASAS_BAHIA_REVIEW_API": "1",
        }
        cases = (
            (
                {
                    "success": False,
                    "error": "direct_status_403|zenrows_http_403",
                    "reviews": [],
                    "general": {},
                    "method": "casas_bahia_reviews_api+casas_bahia_reviews_api_zenrows:10x",
                    "headers": {"X-Request-Cost": "paid-cost"},
                    "zenrows_requested": True,
                },
                True,
            ),
            (
                {
                    "success": False,
                    "error": "direct_status_403|zenrows_disabled",
                    "reviews": [],
                    "general": {},
                    "method": "casas_bahia_reviews_api+casas_bahia_reviews_api_zenrows:10x",
                    "headers": {},
                    "zenrows_requested": False,
                },
                False,
            ),
        )
        for result, requested in cases:
            with self.subTest(requested=requested):
                row = dict(base_row)
                with patch.dict(os.environ, env, clear=False), patch(
                    "seda.casas_bahia.review_api.fetch_reviews",
                    return_value=result,
                ):
                    _merge_casas_bahia_apis(row)
                tokens = row["parse_status"].split("+")
                self.assertEqual(
                    "reviews_zenrows_requested:10x" in tokens,
                    requested,
                )
                self.assertEqual("reviews_cost:paid-cost" in tokens, requested)
                self.assertNotIn("casas_bahia_reviews_api_zenrows:10x", row.get("fetch_method", ""))


if __name__ == "__main__":
    unittest.main()
