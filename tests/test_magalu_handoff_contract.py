import json
import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from seda.magalu import browser_session
from seda.magalu.detail_api import (
    _detail_from_item,
    _post,
    fetch_shipping,
    fetch_similar_names,
)
from seda.magalu.graphql_contract import graphql_envelope_error
from seda.magalu.review_api import (
    _post_summary_browser,
    _request_product_rating,
    fetch_product_rating,
)
from seda.magalu.search_api import (
    _fetch_search_listing_browser,
    fetch_search_listing,
)
from seda.parsers import (
    _parse_magalu_next_detail,
    magalu_exact_factsheet_reference,
    preferred_magalu_sku,
)
from seda.step08_detail_enrichment import (
    _merge_authoritative_detail,
    _merge_generic_product_detail,
)
from seda.step15_final_output import _format_row, _screen_size_for_output


class _FakePage:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def run_js(self, *_args, **_kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def _browser_response(payload):
    return json.dumps({"status": 200, "text": json.dumps(payload)})


def _tv_row(sku=""):
    return {
        "retailer": "Magalu",
        "product_line": "TV",
        "item": "kh6643e17a",
        "product_url": "https://www.magazineluiza.com.br/tv/p/kh6643e17a/et/tves/",
        "retailer_sku_name": "Samsung Smart TV 55 Crystal U8600F",
        "sku": sku,
    }


def _reference_detail(reference="UN55U8600FGXZD", item="kh6643e17a"):
    return {
        "sku": reference,
        "_magalu_factsheet_reference": reference,
        "_detail_item_id": item,
        "_detail_identity_verified": True,
        "screen_size": "55 polegadas",
    }


class MagaluHandoffContractTests(unittest.TestCase):
    def test_graphql_envelope_matrix(self):
        cases = [
            ([], False, "invalid_json"),
            ({"data": None, "errors": [{"message": "x"}]}, False, "graphql_errors"),
            ({}, False, "graphql_data_missing"),
            ({"data": None}, False, "graphql_data_invalid_type"),
            ({"data": []}, False, "graphql_data_invalid_type"),
            ({"data": "invalid"}, False, "graphql_data_invalid_type"),
            ({"data": 1}, False, "graphql_data_invalid_type"),
            ({"data": {}}, True, "graphql_item_missing"),
            ({"data": {}}, False, ""),
            ({"data": {"item": {"id": "ok"}}}, True, ""),
        ]
        for payload, require_item, expected in cases:
            with self.subTest(payload=payload, require_item=require_item):
                self.assertEqual(
                    graphql_envelope_error(payload, require_item=require_item),
                    expected,
                )

    def test_browser_graphql_retries_nested_data_without_crashing(self):
        page = _FakePage(
            [
                _browser_response({"data": [{"unexpected": True}]}),
                _browser_response({}),
                _browser_response({"data": {"item": {"id": "ok"}}}),
            ]
        )
        payload = {"operationName": "itemQuery", "variables": {"itemId": "ok"}}
        with patch.dict(os.environ, {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"}), patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ):
            result = browser_session.graphql_post(payload)
        self.assertEqual(page.calls, 3)
        self.assertEqual(result["data"]["data"]["item"]["id"], "ok")
        self.assertEqual(
            [entry["error"] for entry in result["trace"]],
            ["graphql_data_invalid_type", "graphql_data_missing", ""],
        )
        self.assertEqual(
            [entry["item_present"] for entry in result["trace"]],
            [0, 0, 1],
        )

    def test_browser_graphql_exhausts_malformed_envelopes_with_full_trace(self):
        page = _FakePage(
            [
                _browser_response({"data": "invalid"}),
                _browser_response({"data": 1}),
                _browser_response({"data": []}),
            ]
        )
        payload = {"operationName": "itemQuery", "variables": {"itemId": "ok"}}
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"},
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ):
            result = browser_session.graphql_post(payload)

        self.assertEqual(page.calls, 3)
        self.assertEqual(result["data"], {})
        self.assertEqual(result["error"], "graphql_data_invalid_type")
        self.assertEqual(
            [entry["attempt"] for entry in result["trace"]],
            [1, 2, 3],
        )
        self.assertEqual(
            [entry["error"] for entry in result["trace"]],
            ["graphql_data_invalid_type"] * 3,
        )

    def test_direct_graphql_uses_same_contract_and_retry_count(self):
        payloads = [
            {"data": []},
            {},
            {"data": {"item": {"id": "ok"}}},
        ]
        responses = []
        for payload in payloads:
            response = Mock(
                status_code=200,
                text=json.dumps(payload),
                headers={"content-type": "application/json"},
            )
            response.json.return_value = payload
            responses.append(response)
        trace = []
        with patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_BROWSER_GRAPHQL": "0",
                "SEDA_MAGALU_DETAIL_RETRIES": "2",
                "SEDA_MAGALU_DETAIL_RETRY_SLEEP_SECONDS": "0",
            },
        ), patch("seda.magalu.detail_api.requests.post", side_effect=responses) as post:
            result = _post(
                {"operationName": "itemQuery"},
                10,
                trace,
                "item",
            )
        self.assertEqual(post.call_count, 3)
        self.assertEqual(result["data"]["item"]["id"], "ok")
        self.assertEqual(
            [entry.get("error", "") for entry in trace],
            ["graphql_data_invalid_type", "graphql_data_missing", ""],
        )

    def test_raw_graphql_batch_contract_remains_list_compatible(self):
        payload = [
            {"operationName": "one"},
            {"operationName": "two"},
        ]
        response_payload = [{"data": {"one": 1}}, {"data": {"two": 2}}]
        page = _FakePage([_browser_response(response_payload)])
        with patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ):
            result = browser_session.graphql_post_raw(payload)
        self.assertEqual(result["data"], response_payload)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["payload_count"], 2)

    def test_search_direct_transport_rejects_malformed_envelope_without_crash(self):
        response = Mock(
            status_code=200,
            text="[]",
            headers={"content-type": "application/json"},
        )
        response.json.return_value = []
        session = Mock()
        session.post.return_value = response
        with patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_SEARCH_RETRIES": "0",
                "SEDA_MAGALU_SEARCH_BROWSER_GRAPHQL": "0",
            },
        ), patch("seda.magalu.search_api.requests.Session", return_value=session), patch(
            "seda.magalu.search_api._page_sizes",
            return_value=[20],
        ):
            result = fetch_search_listing(
                "https://www.magazineluiza.com.br/busca/smart-tv/"
            )
        self.assertIs(result["success"], False)
        self.assertEqual(result["trace"][0]["error"], "invalid_json")

    def test_search_rejects_non_list_products_without_crash(self):
        payload = {"data": {"search": {"products": {"id": "wrong"}}}}
        response = Mock(
            status_code=200,
            text=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        response.json.return_value = payload
        session = Mock()
        session.post.return_value = response
        with patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_SEARCH_RETRIES": "0",
                "SEDA_MAGALU_SEARCH_BROWSER_GRAPHQL": "0",
            },
        ), patch("seda.magalu.search_api.requests.Session", return_value=session), patch(
            "seda.magalu.search_api._page_sizes",
            return_value=[20],
        ):
            result = fetch_search_listing(
                "https://www.magazineluiza.com.br/busca/smart-tv/"
            )
        self.assertIs(result["success"], False)
        self.assertEqual(
            result["trace"][0]["error"],
            "invalid_search_products",
        )

    def test_review_direct_transport_rejects_malformed_envelope_without_crash(self):
        response = Mock(
            status_code=200,
            text='{"data":[]}',
            headers={"content-type": "application/json"},
        )
        response.json.return_value = {"data": []}
        session = Mock()
        session.post.return_value = response
        trace = []
        with patch.dict(os.environ, {"SEDA_MAGALU_BROWSER_GRAPHQL": "0"}):
            result = _request_product_rating(
                session,
                "variation",
                1,
                20,
                10,
                0,
                0,
                trace,
            )
        self.assertEqual(result, {})
        self.assertEqual(trace[0]["error"], "graphql_data_invalid_type")

    def test_browser_callers_preserve_every_inner_graphql_attempt(self):
        inner_trace = [
            {
                "attempt": 1,
                "method": "browser_graphql",
                "status_code": 200,
                "error": "graphql_data_invalid_type",
            },
            {
                "attempt": 2,
                "method": "browser_graphql",
                "status_code": 200,
                "error": "",
            },
        ]
        rating_result = {
            "status_code": 200,
            "data": {
                "data": {"productRating": {"productId": "variation"}}
            },
            "trace": inner_trace,
            "error": "",
        }
        trace = []
        with patch.dict(os.environ, {"SEDA_MAGALU_BROWSER_GRAPHQL": "1"}), patch(
            "seda.magalu.browser_session.graphql_post",
            return_value=rating_result,
        ):
            rating = _request_product_rating(
                Mock(),
                "variation",
                1,
                20,
                10,
                0,
                0,
                trace,
            )
        self.assertEqual(rating["productId"], "variation")
        self.assertEqual([item["attempt"] for item in trace], [1, 2])
        self.assertEqual(
            [item.get("error", "") for item in trace],
            ["graphql_data_invalid_type", ""],
        )

        summary_trace = []
        summary_result = {
            "status_code": 200,
            "data": {"data": {"reviewSummary": {"summary": "ok"}}},
            "trace": inner_trace,
            "error": "",
        }
        with patch(
            "seda.magalu.browser_session.graphql_post",
            return_value=summary_result,
        ):
            summary = _post_summary_browser(
                {"operationName": "reviewSummary"},
                10,
                summary_trace,
            )
        self.assertEqual(summary["data"]["reviewSummary"]["summary"], "ok")
        self.assertEqual([item["attempt"] for item in summary_trace], [1, 2])

        search_trace = []
        search_result = {
            "status_code": 200,
            "data": {
                "data": {
                    "search": {
                        "products": [{"id": "one"}],
                    }
                }
            },
            "trace": inner_trace,
            "error": "",
        }
        with patch(
            "seda.magalu.browser_session.ensure_magalu_session",
            return_value={"success": True, "trace": []},
        ), patch(
            "seda.magalu.browser_session.graphql_post",
            return_value=search_result,
        ):
            result = _fetch_search_listing_browser(
                "https://www.magazineluiza.com.br/busca/tv/",
                [20],
                10,
                search_trace,
            )
        self.assertIs(result["success"], True)
        self.assertEqual([item["attempt"] for item in search_trace], [1, 2])
        self.assertEqual([item["products"] for item in search_trace], [0, 1])

    def test_operation_nodes_with_wrong_types_do_not_crash(self):
        with patch(
            "seda.magalu.detail_api._post",
            return_value={"data": {"shipping": []}},
        ):
            shipping = fetch_shipping({})
        self.assertIs(shipping["success"], False)

        with patch(
            "seda.magalu.detail_api._similar_place_ids",
            return_value=["place"],
        ), patch(
            "seda.magalu.detail_api._post",
            return_value={"data": {"recommendation": []}},
        ):
            similar = fetch_similar_names("item")
        self.assertIs(similar["success"], False)

        malformed_rating = {
            "productId": "variation",
            "general": [],
            "dimensions": {},
            "reviewsByRating": {},
            "userReviews": [],
        }
        with patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_REVIEW_MAX_PAGES": "1",
                "SEDA_MAGALU_REVIEW_INITIAL_SLEEP_SECONDS": "0",
                "SEDA_MAGALU_REVIEW_SLEEP_SECONDS": "0",
            },
        ), patch(
            "seda.magalu.review_api._request_product_rating",
            return_value=malformed_rating,
        ):
            rating = fetch_product_rating("variation", limit=1)
        self.assertIs(rating["success"], False)

    def test_tv_sku_source_is_reference_only(self):
        self.assertEqual(
            preferred_magalu_sku(
                "TV",
                "  UN55U8600FGXZD  ",
                "MODEL-FALLBACK",
                "Samsung Smart TV 55 U8600F",
            ),
            "UN55U8600FGXZD",
        )
        self.assertEqual(
            preferred_magalu_sku("TV", "", "MODEL-FALLBACK", "Smart TV 55 U8600F"),
            "",
        )
        self.assertEqual(
            preferred_magalu_sku("REF", "REF-FULL", "MODEL", "Geladeira"),
            "REF-FULL",
        )

    def test_reference_producers_preserve_internal_text_without_reassembly(self):
        item = {
            "id": "kh6643e17a",
            "title": "Samsung Smart TV 55 Crystal U8600F",
            "factsheet": [
                {
                    "keyName": "Referência",
                    "elements": [{"value": "  UN55  U8600FGXZD  "}],
                }
            ],
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            graphql_detail = _detail_from_item(item)
            next_payload = {
                "props": {"pageProps": {"data": {"item": item}}}
            }
            html = (
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(next_payload)
                + "</script>"
            )
            next_detail = _parse_magalu_next_detail(
                html,
                "https://www.magazineluiza.com.br",
                _tv_row()["product_url"],
            )
        for detail in (graphql_detail, next_detail):
            self.assertEqual(
                detail["_magalu_factsheet_reference"],
                "UN55  U8600FGXZD",
            )
            self.assertEqual(detail["sku"], "UN55  U8600FGXZD")

        item["factsheet"][0]["elements"].append({"value": "GXZD"})
        self.assertEqual(magalu_exact_factsheet_reference(item), "")

    def test_reference_prefers_top_level_and_rejects_ambiguous_bundles(self):
        fact = lambda value: {
            "keyName": "Referência",
            "elements": [{"value": value}],
        }
        item = {
            "factsheet": [fact(" TOP-REF ")],
            "bundles": [
                {"factsheet": [fact("SOUNDBAR-REF")]},
                {"factsheet": [fact("TV-REF")]},
            ],
        }
        self.assertEqual(magalu_exact_factsheet_reference(item), "TOP-REF")

        item["factsheet"] = []
        self.assertEqual(magalu_exact_factsheet_reference(item), "")

        item["bundles"][0]["factsheet"] = [fact(" TV-REF ")]
        self.assertEqual(magalu_exact_factsheet_reference(item), "TV-REF")

    def test_verified_reference_only_recovers_blank_or_item_sentinel(self):
        for current in ("", "kh6643e17a"):
            row = _tv_row(current)
            detail = _reference_detail("  UN55  U8600FGXZD  ")
            _merge_authoritative_detail(row, detail, identity_verified=True)
            self.assertEqual(row["sku"], "UN55  U8600FGXZD")
            self.assertEqual(row["screen_size"], "55 polegadas")

        for current in ("55U8600F", "EXISTING-SHORT"):
            row = _tv_row(current)
            _merge_authoritative_detail(
                row,
                _reference_detail("UN55U8600FGXZD"),
                identity_verified=True,
            )
            self.assertEqual(row["sku"], current)
            self.assertEqual(row["screen_size"], "55 polegadas")

    def test_sku_gate_rejects_wrong_identity_provenance_and_same_name(self):
        row = _tv_row("")
        _merge_authoritative_detail(
            row,
            _reference_detail(item="different"),
            identity_verified=True,
        )
        self.assertEqual(row["sku"], "")

        row = _tv_row("")
        _merge_authoritative_detail(
            row,
            {"sku": "UN55U8600FGXZD", "_detail_item_id": "kh6643e17a"},
            identity_verified=True,
        )
        self.assertEqual(row["sku"], "")

        row = _tv_row("")
        detail = _reference_detail()
        detail["retailer_sku_name"] = row["retailer_sku_name"]
        detail.pop("_detail_identity_verified")
        self.assertTrue(_merge_generic_product_detail(row, detail))
        self.assertEqual(row["sku"], "")
        self.assertEqual(row["screen_size"], "55 polegadas")

    def test_existing_sku_conflict_is_preserved_and_diagnosed_once(self):
        row = _tv_row("EXISTING-SHORT")
        detail = _reference_detail("UN55U8600FGXZD")
        _merge_authoritative_detail(row, detail, identity_verified=True)
        _merge_authoritative_detail(row, detail, identity_verified=True)
        self.assertEqual(row["sku"], "EXISTING-SHORT")
        self.assertEqual(
            row["parse_status"].split("+").count(
                "sku_reference_conflict_preserved"
            ),
            1,
        )

    def test_verified_full_reference_survives_existing_final_synthetic_filter(self):
        reference = "REFERENCE-" + ("X" * 45)
        row = _tv_row("")
        _merge_authoritative_detail(
            row,
            _reference_detail(reference),
            identity_verified=True,
        )
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"},
        ):
            formatted = _format_row(row, datetime(2026, 7, 26, 12, 0, 0))
            unverified = _format_row(
                {**_tv_row(reference), "parse_status": ""},
                datetime(2026, 7, 26, 12, 0, 0),
            )
        self.assertEqual(formatted["sku"], reference)
        self.assertEqual(unverified["sku"], "")

    def test_verified_reference_equal_to_item_survives_final_item_filter(self):
        row = _tv_row("")
        _merge_authoritative_detail(
            row,
            _reference_detail(row["item"]),
            identity_verified=True,
        )
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"},
        ):
            formatted = _format_row(row, datetime(2026, 7, 26, 12, 0, 0))
            unverified = _format_row(
                {**_tv_row(row["item"]), "parse_status": ""},
                datetime(2026, 7, 26, 12, 0, 0),
            )
        self.assertIn(
            "sku_factsheet_reference_recovered",
            row["parse_status"].split("+"),
        )
        self.assertEqual(formatted["sku"], row["item"])
        self.assertEqual(unverified["sku"], "")

    def test_tv_screen_size_changes_only_at_final_boundary(self):
        accepted = {
            "24": "24 inches",
            "55": "55 inches",
            '55"': "55 inches",
            "55''": "55 inches",
            "55 Polegada": "55 inches",
            "55 polegadas": "55 inches",
            "55 pol.": "55 inches",
            "55 Pol": "55 inches",
            "100": "100 inches",
            "115 polegadas": "115 inches",
        }
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
            for value, expected in accepted.items():
                with self.subTest(value=value):
                    self.assertEqual(
                        _screen_size_for_output({"screen_size": value}),
                        expected,
                    )
            for value in (
                "9",
                "1000",
                "55 cm",
                "1270 mm",
                "10-40 polegadas",
                "50/55",
                "55.5 polegadas",
            ):
                with self.subTest(value=value):
                    self.assertEqual(_screen_size_for_output({"screen_size": value}), value)
            self.assertEqual(_screen_size_for_output({"screen_size": "   "}), "")

        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "REF"}):
            self.assertEqual(_screen_size_for_output({"screen_size": '55"'}), '55"')

    def test_format_row_serializes_tv_screen_without_touching_sku(self):
        row = _tv_row("EXISTING-SHORT")
        row["screen_size"] = "55 polegadas"
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "magalu"},
        ):
            formatted = _format_row(row, datetime(2026, 7, 26, 12, 0, 0))
        self.assertEqual(formatted["screen_size"], "55 inches")
        self.assertEqual(formatted["sku"], "EXISTING-SHORT")


if __name__ == "__main__":
    unittest.main()
