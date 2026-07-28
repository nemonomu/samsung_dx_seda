import os
import unittest
from datetime import datetime
from unittest.mock import patch

from seda.step08_detail_enrichment import (
    _magalu_review_graphql_failure_reason,
    _merge_magalu_reviews,
    _record_result_trace,
)
from seda.step14_db_load import _db_value
from seda.step15_final_output import _format_row


def _row(product_line="TV", count_of_reviews=""):
    return {
        "retailer": "Magalu",
        "product_line": product_line,
        "item": "reviewcase1",
        "product_url": (
            "https://www.magazineluiza.com.br/produto/p/reviewcase1/et/tves/"
        ),
        "retailer_sku_name": "Produto de teste",
        "sku": "MODEL-1",
        "star_rating": "4.8",
        "count_of_star_ratings": "10",
        "count_of_reviews": count_of_reviews,
        "detailed_review_content": "",
        "parse_status": "",
    }


def _failure(error="blocked_response"):
    return {
        "success": False,
        "error": "",
        "reviews": [],
        "product_id": "",
        "general": {},
        "dimensions": [],
        "reviews_by_rating": [],
        "page": {},
        "trace": [{"status_code": 403, "error": error}],
        "method": "graphql_product_rating",
    }


class MagaluReviewFailureContractTests(unittest.TestCase):
    def _merge(self, row, result):
        env = {
            "SEDA_MAGALU_REVIEW_GRAPHQL": "1",
            "SEDA_MAGALU_SKIP_REVIEW_WITHOUT_RATING": "1",
            "SEDA_MAGALU_REVIEW_LIMIT": "20",
            "SEDA_MAGALU_REVIEW_SUMMARY_GRAPHQL": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "seda.magalu.review_api.fetch_product_rating",
            return_value=result,
        ):
            return _merge_magalu_reviews(row, row["product_url"])

    def test_proven_failure_keeps_blank_count_blank_at_final_output(self):
        for product_line in ("TV", "REF"):
            with self.subTest(product_line=product_line):
                row = _row(product_line)
                self._merge(row, _failure())
                self.assertIn(
                    "reviews_graphql_failed:count_missing:blocked_response",
                    row["parse_status"].split("+"),
                )
                formatted = _format_row(row, datetime(2026, 7, 29, 12, 0, 0))
                self.assertEqual(formatted["count_of_reviews"], "")
                self.assertIsNone(
                    _db_value("count_of_reviews", formatted["count_of_reviews"])
                )

    def test_proven_failure_never_erases_existing_count(self):
        row = _row("TV", "27")
        self._merge(row, _failure("failed_to_fetch"))
        formatted = _format_row(row, datetime(2026, 7, 29, 12, 0, 0))
        self.assertEqual(row["count_of_reviews"], "27")
        self.assertEqual(formatted["count_of_reviews"], "27")

    def test_proven_failure_does_not_turn_invalid_count_text_into_zero(self):
        for value in ("N/A", "-", "-1"):
            with self.subTest(value=value):
                row = _row("TV", value)
                self._merge(row, _failure("count_unavailable"))
                formatted = _format_row(row, datetime(2026, 7, 29, 12, 0, 0))
                self.assertEqual(formatted["count_of_reviews"], "")
                self.assertIsNone(
                    _db_value("count_of_reviews", formatted["count_of_reviews"])
                )

    def test_explicit_zero_review_response_is_successful_evidence(self):
        result = {
            "success": False,
            "reviews": [],
            "product_id": "product-1",
            "general": {"rating": 0, "reviewCount": 0, "commentCount": 0},
            "dimensions": [],
            "reviews_by_rating": [],
            "page": {"current": 1, "totalItems": 0, "totalPages": 0},
            "trace": [{"status_code": 200, "error": ""}],
            "method": "graphql_product_rating",
        }
        row = _row()
        self._merge(row, result)
        self.assertNotIn("reviews_graphql_failed:", row["parse_status"])
        self.assertEqual(row["count_of_reviews"], 0)
        formatted = _format_row(row, datetime(2026, 7, 29, 12, 0, 0))
        self.assertEqual(formatted["count_of_reviews"], "0")
        self.assertEqual(
            _db_value("count_of_reviews", formatted["count_of_reviews"]),
            "0",
        )

    def test_page_total_items_is_merged_even_without_general(self):
        result = {
            "success": False,
            "reviews": [],
            "product_id": "product-1",
            "general": {},
            "dimensions": [],
            "reviews_by_rating": [],
            "page": {"current": 1, "totalItems": 27, "totalPages": 2},
            "trace": [{"status_code": 200, "error": ""}],
            "method": "graphql_product_rating",
        }
        row = _row()
        self._merge(row, result)
        self.assertNotIn("reviews_graphql_failed:", row["parse_status"])
        self.assertEqual(row["count_of_reviews"], 27)
        formatted = _format_row(row, datetime(2026, 7, 29, 12, 0, 0))
        self.assertEqual(formatted["count_of_reviews"], "27")

    def test_normal_blank_without_failure_token_keeps_existing_output_contract(self):
        row = _row()
        formatted = _format_row(row, datetime(2026, 7, 29, 12, 0, 0))
        self.assertEqual(formatted["count_of_reviews"], "0")

    def test_failure_reason_requires_explicit_count_not_generic_evidence(self):
        self.assertEqual(
            _magalu_review_graphql_failure_reason(_failure("HTTP 403 / Akamai")),
            "count_missing:http_403_akamai",
        )
        for key, value in (
            ("reviews", ["texto"]),
            ("product_id", "product-1"),
            ("general", {"rating": 4.8, "reviewCount": 10}),
            ("dimensions", [{"id": "quality"}]),
            ("reviews_by_rating", [{"rating": 5, "total": 0}]),
            ("page", {"current": 1}),
        ):
            with self.subTest(key=key):
                result = _failure()
                result[key] = value
                self.assertTrue(
                    _magalu_review_graphql_failure_reason(result).startswith(
                        "count_missing"
                    )
                )

        for key, value in (
            ("general", {"commentCount": 0}),
            ("general", {"commentCount": "27"}),
            ("page", {"totalItems": 0}),
            ("page", {"totalItems": "27"}),
        ):
            with self.subTest(explicit_count=key, value=value):
                result = _failure()
                result[key] = value
                self.assertEqual(_magalu_review_graphql_failure_reason(result), "")

    def test_partial_success_without_count_stays_null_but_keeps_existing_count(self):
        partial = {
            "success": True,
            "reviews": ["texto"],
            "product_id": "product-1",
            "general": {"rating": 4.8, "reviewCount": 10},
            "dimensions": [{"id": "quality"}],
            "reviews_by_rating": [{"rating": 5, "total": 10}],
            "page": {"current": 1, "totalPages": 1},
            "trace": [{"status_code": 200, "error": ""}],
            "method": "graphql_product_rating",
        }
        blank = _row("TV")
        self._merge(blank, partial)
        self.assertIn(
            "reviews_graphql_failed:count_missing",
            blank["parse_status"].split("+"),
        )
        formatted = _format_row(blank, datetime(2026, 7, 29, 12, 0, 0))
        self.assertEqual(formatted["count_of_reviews"], "")

        existing = _row("TV", "27")
        self._merge(existing, partial)
        formatted = _format_row(existing, datetime(2026, 7, 29, 12, 0, 0))
        self.assertEqual(existing["count_of_reviews"], "27")
        self.assertEqual(formatted["count_of_reviews"], "27")

    def test_malformed_summary_shapes_do_not_break_count_failure_flow(self):
        result = _failure("invalid_product_rating")
        result["general"] = ["invalid"]
        result["page"] = ["invalid"]
        row = _row()
        self._merge(row, result)
        self.assertIn(
            "reviews_graphql_failed:count_missing:invalid_product_rating",
            row["parse_status"].split("+"),
        )
        formatted = _format_row(row, datetime(2026, 7, 29, 12, 0, 0))
        self.assertEqual(formatted["count_of_reviews"], "")

    def test_content_type_is_saved_in_existing_trace_detail_column(self):
        trace_rows = []
        result = _failure("blocked_response")
        result["trace"][0]["content_type"] = "application/json; charset=utf-8"
        _record_result_trace(
            trace_rows,
            _row(),
            1,
            _row()["product_url"],
            "review_graphql",
            result,
        )
        self.assertEqual(len(trace_rows), 1)
        self.assertIn(
            "content_type:application/json; charset=utf-8",
            trace_rows[0]["detail"],
        )
        self.assertNotIn("content_type", trace_rows[0])


if __name__ == "__main__":
    unittest.main()
