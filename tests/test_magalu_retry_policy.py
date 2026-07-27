import json
import os
import unittest
from unittest.mock import patch

from seda.magalu import browser_session
from seda.magalu.detail_api import (
    fetch_detail,
    fetch_shipping,
    fetch_similar_names,
)
from seda.magalu.graphql_contract import graphql_terminal_business_error
from seda.step08_detail_enrichment import (
    _merge_magalu_similar,
    _record_result_trace,
)
from seda.step14_db_load import _db_value


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


def _failed_fetch_response():
    return json.dumps(
        {"status": 0, "error": "TypeError: Failed to fetch", "text": ""}
    )


def _browser_http_response(status, text=""):
    return json.dumps({"status": status, "text": text})


def _shipping_business_error(message):
    return {
        "data": {"shipping": None},
        "errors": [
            {
                "message": message,
                "path": ["shipping"],
                "extensions": {
                    "code": "not_available",
                    "dualMode": True,
                    "status": "RESOURCE_NOT_FOUND",
                    "service": "freight",
                },
            }
        ],
    }


def _showcase_payload(names=None):
    dynamic = []
    if names:
        dynamic = [
            {
                "title": "Quem viu este produto também viu",
                "products": [{"title": name} for name in names],
            }
        ]
    return {"data": {"recommendation": {"dynamic": dynamic}}}


class MagaluRetryPolicyTests(unittest.TestCase):
    def test_shipping_terminal_classifier_is_strict(self):
        for message in (
            "Frete indisponível para sua região.",
            "Produto esgotado!",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    graphql_terminal_business_error(
                        "shippingQuery",
                        _shipping_business_error(message),
                    ),
                    "shipping_not_available",
                )

        unknown = _shipping_business_error("Falha temporária")
        self.assertEqual(
            graphql_terminal_business_error("shippingQuery", unknown),
            "",
        )
        mixed = _shipping_business_error("Produto esgotado!")
        mixed["errors"].append(
            {
                "message": "temporary",
                "path": ["shipping"],
                "extensions": {"code": "INTERNAL_SERVER_ERROR"},
            }
        )
        self.assertEqual(
            graphql_terminal_business_error("shippingQuery", mixed),
            "",
        )
        self.assertEqual(
            graphql_terminal_business_error(
                "itemQuery",
                _shipping_business_error("Produto esgotado!"),
            ),
            "",
        )

    def test_shipping_business_results_stop_after_one_browser_attempt(self):
        for message in (
            "Frete indisponível para sua região.",
            "Produto esgotado!",
        ):
            page = _FakePage([_browser_response(_shipping_business_error(message))])
            with self.subTest(message=message), patch.dict(
                os.environ,
                {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"},
            ), patch(
                "seda.magalu.browser_session._page_for_use",
                return_value=page,
            ), patch(
                "seda.magalu.browser_session._prepare_js_page",
                side_effect=lambda active_page, *_args, **_kwargs: active_page,
            ), patch(
                "seda.magalu.browser_session._restart_page",
            ) as restart:
                result = fetch_shipping(
                    {},
                    timeout=10,
                    context_url="https://www.magazineluiza.com.br/p/item/",
                )
            self.assertEqual(page.calls, 1)
            restart.assert_not_called()
            self.assertIs(result["success"], False)
            self.assertEqual(result["delivery"], "")
            self.assertEqual(result["pickup"], "")
            self.assertIsNone(_db_value("delivery_availability", result["delivery"]))
            self.assertIsNone(_db_value("pick_up_availability", result["pickup"]))
            self.assertEqual(len(result["trace"]), 1)
            self.assertEqual(result["trace"][0]["error"], "graphql_errors")
            self.assertEqual(
                result["trace"][0]["terminal_business_error"],
                "shipping_not_available",
            )

    def test_unknown_shipping_error_keeps_existing_retry_and_recovers(self):
        unknown = _shipping_business_error("Falha temporária")
        success = {
            "data": {
                "shipping": {
                    "deliveries": [
                        {
                            "modalities": [
                                {
                                    "type": "delivery",
                                    "shippingTime": {"description": "3 dias"},
                                }
                            ]
                        }
                    ]
                }
            }
        }
        page = _FakePage(
            [_browser_response(unknown), _browser_response(success)]
        )
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"},
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ), patch(
            "seda.magalu.browser_session._restart_page",
        ) as restart:
            result = fetch_shipping(
                {},
                timeout=10,
                context_url="https://www.magazineluiza.com.br/p/item/",
            )
        self.assertEqual(page.calls, 2)
        restart.assert_not_called()
        self.assertIs(result["success"], True)
        self.assertEqual(result["delivery"], "3 dias")
        self.assertEqual(
            [item.get("error", "") for item in result["trace"]],
            ["graphql_errors", ""],
        )

    def test_showcase_failed_fetch_restarts_once_then_opens_circuit(self):
        old_page = _FakePage([_failed_fetch_response() for _ in range(2)])
        new_page = _FakePage([_failed_fetch_response()])
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"},
        ), patch(
            "seda.magalu.detail_api._similar_place_ids",
            return_value=["one", "two", "three", "four", "five"],
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            side_effect=[old_page, new_page],
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ), patch(
            "seda.magalu.browser_session._restart_page",
        ) as restart:
            result = fetch_similar_names(
                "item",
                timeout=10,
                context_url="https://www.magazineluiza.com.br/p/item/",
            )
        self.assertEqual(old_page.calls, 2)
        self.assertEqual(new_page.calls, 1)
        restart.assert_called_once_with("graphql_post_showcase_failed_fetch")
        self.assertIs(result["success"], False)
        self.assertEqual(result["error"], "showcase_failed_fetch_circuit_open")
        self.assertEqual(len(result["trace"]), 3)
        self.assertEqual(
            {item["label"] for item in result["trace"]},
            {"showcase:one"},
        )
        self.assertEqual(
            result["trace"][1]["recovery"],
            "browser_restart_after_failed_fetch",
        )
        self.assertIs(
            result["trace"][2]["showcase_failed_fetch_circuit_open"],
            True,
        )

    def test_showcase_post_restart_success_is_preserved(self):
        old_page = _FakePage(
            [
                _failed_fetch_response(),
                _failed_fetch_response(),
            ]
        )
        new_page = _FakePage(
            [_browser_response(_showcase_payload(["Recovered TV"]))]
        )
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"},
        ), patch(
            "seda.magalu.detail_api._similar_place_ids",
            return_value=["one", "two"],
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            side_effect=[old_page, new_page],
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ), patch(
            "seda.magalu.browser_session._restart_page",
        ) as restart:
            result = fetch_similar_names(
                "item",
                timeout=10,
                context_url="https://www.magazineluiza.com.br/p/item/",
            )
        self.assertEqual(old_page.calls, 2)
        self.assertEqual(new_page.calls, 1)
        restart.assert_called_once_with("graphql_post_showcase_failed_fetch")
        self.assertIs(result["success"], True)
        self.assertEqual(result["names"], ["Recovered TV"])
        self.assertNotIn("error", result)

    def test_showcase_attempts_two_gets_one_bounded_restart_probe(self):
        old_page = _FakePage([_failed_fetch_response() for _ in range(2)])
        new_page = _FakePage([_failed_fetch_response()])
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "2"},
        ), patch(
            "seda.magalu.detail_api._similar_place_ids",
            return_value=["one", "two", "three", "four", "five"],
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            side_effect=[old_page, new_page],
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ), patch(
            "seda.magalu.browser_session._restart_page",
        ) as restart:
            result = fetch_similar_names("item", timeout=10)
        self.assertEqual(old_page.calls, 2)
        self.assertEqual(new_page.calls, 1)
        restart.assert_called_once_with("graphql_post_showcase_failed_fetch")
        self.assertEqual(result["error"], "showcase_failed_fetch_circuit_open")
        self.assertEqual(len(result["trace"]), 3)

    def test_showcase_late_consecutive_failures_get_restart_probe(self):
        old_page = _FakePage(
            [
                _browser_http_response(403, "Access denied"),
                _failed_fetch_response(),
                _failed_fetch_response(),
            ]
        )
        new_page = _FakePage(
            [_browser_response(_showcase_payload(["Late recovery TV"]))]
        )
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"},
        ), patch(
            "seda.magalu.detail_api._similar_place_ids",
            return_value=["one", "two"],
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            side_effect=[old_page, new_page],
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ), patch(
            "seda.magalu.browser_session._restart_page",
        ) as restart:
            result = fetch_similar_names("item", timeout=10)
        self.assertEqual(old_page.calls, 3)
        self.assertEqual(new_page.calls, 1)
        restart.assert_called_once_with("graphql_post_showcase_failed_fetch")
        self.assertIs(result["success"], True)
        self.assertEqual(result["names"], ["Late recovery TV"])

    def test_showcase_restart_prepare_error_opens_circuit_without_reuse(self):
        old_page = _FakePage([_failed_fetch_response() for _ in range(2)])
        new_page = _FakePage([])

        def prepare(active_page, *_args, **_kwargs):
            if active_page is new_page:
                raise RuntimeError("new page warmup failed")
            return active_page

        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "2"},
        ), patch(
            "seda.magalu.detail_api._similar_place_ids",
            return_value=["one", "two", "three"],
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            side_effect=[old_page, new_page],
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=prepare,
        ), patch(
            "seda.magalu.browser_session._restart_page",
        ) as restart:
            result = fetch_similar_names("item", timeout=10)
        self.assertEqual(old_page.calls, 2)
        self.assertEqual(new_page.calls, 0)
        restart.assert_called_once_with("graphql_post_showcase_failed_fetch")
        self.assertEqual(result["error"], "showcase_failed_fetch_circuit_open")
        self.assertIn("new page warmup failed", result["trace"][-1]["recovery_error"])

    def test_showcase_normal_empty_still_checks_next_place(self):
        page = _FakePage(
            [
                _browser_response(_showcase_payload()),
                _browser_response(_showcase_payload(["Second Place TV"])),
            ]
        )
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"},
        ), patch(
            "seda.magalu.detail_api._similar_place_ids",
            return_value=["one", "two"],
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ), patch(
            "seda.magalu.browser_session._restart_page",
        ) as restart:
            result = fetch_similar_names("item", timeout=10)
        self.assertEqual(page.calls, 2)
        restart.assert_not_called()
        self.assertIs(result["success"], True)
        self.assertEqual(result["names"], ["Second Place TV"])

    def test_showcase_403_keeps_existing_retry_and_next_place_fallback(self):
        page = _FakePage(
            [
                _browser_http_response(403, "Access denied"),
                _browser_http_response(403, "Access denied"),
                _browser_http_response(403, "Access denied"),
                _browser_response(_showcase_payload(["Recovered on next place"])),
            ]
        )
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"},
        ), patch(
            "seda.magalu.detail_api._similar_place_ids",
            return_value=["one", "two"],
        ), patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ), patch(
            "seda.magalu.browser_session._restart_page",
        ) as restart:
            result = fetch_similar_names("item", timeout=10)
        self.assertEqual(page.calls, 4)
        restart.assert_not_called()
        self.assertIs(result["success"], True)
        self.assertEqual(result["names"], ["Recovered on next place"])
        self.assertNotIn("error", result)

    def test_detail_propagates_only_showcase_circuit_state(self):
        item = {"id": "item", "title": "TV"}
        with patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_SHIPPING_GRAPHQL": "0",
                "SEDA_MAGALU_SIMILAR_GRAPHQL": "1",
            },
        ), patch(
            "seda.magalu.detail_api._request_item",
            return_value=item,
        ), patch(
            "seda.magalu.detail_api._detail_from_item",
            return_value={"retailer_sku_name": "TV"},
        ), patch(
            "seda.magalu.detail_api.fetch_similar_names",
            return_value={
                "success": False,
                "error": "showcase_failed_fetch_circuit_open",
                "names": [],
                "trace": [],
            },
        ):
            result = fetch_detail("item")
        self.assertIs(result["success"], True)
        self.assertEqual(
            result["similar_error"],
            "showcase_failed_fetch_circuit_open",
        )
        self.assertEqual(result["detail"]["retailer_sku_name"], "TV")

    def test_step08_skips_only_duplicate_call_after_showcase_circuit(self):
        row = {
            "retailer": "Magalu",
            "item": "item",
            "sku": "sku",
            "product_url": "https://www.magazineluiza.com.br/p/item/",
            "retailer_sku_name_similar": "",
        }
        trace = []
        with patch(
            "seda.magalu.detail_api.fetch_similar_names",
        ) as fetch:
            changed = _merge_magalu_similar(
                row,
                row["product_url"],
                trace_rows=trace,
                row_index=1,
                prior_error="showcase_failed_fetch_circuit_open",
            )
        self.assertIs(changed, False)
        fetch.assert_not_called()
        self.assertEqual(trace[-1]["error"], "showcase_failed_fetch_circuit_open")
        self.assertEqual(
            trace[-1]["detail"],
            "skipped_after_detail_graphql_circuit",
        )

    def test_retry_diagnostics_survive_result_trace_mapping(self):
        trace_rows = []
        _record_result_trace(
            trace_rows,
            {"retailer": "Magalu", "item": "item", "sku": "sku"},
            1,
            "https://www.magazineluiza.com.br/p/item/",
            "detail_graphql",
            {
                "success": False,
                "trace": [
                    {
                        "operation": "showcaseQuery",
                        "attempt": 2,
                        "method": "browser_graphql",
                        "status_code": 0,
                        "error": "TypeError: Failed to fetch",
                        "terminal_business_error": "shipping_not_available",
                        "recovery": "browser_restart_after_failed_fetch",
                        "recovery_error": "RuntimeError: warmup failed",
                        "showcase_failed_fetch_circuit_open": True,
                    }
                ],
            },
        )
        saved_detail = trace_rows[0]["detail"]
        self.assertIn("terminal_business_error:shipping_not_available", saved_detail)
        self.assertIn("recovery:browser_restart_after_failed_fetch", saved_detail)
        self.assertIn("recovery_error:RuntimeError: warmup failed", saved_detail)
        self.assertIn("showcase_failed_fetch_circuit_open:1", saved_detail)


if __name__ == "__main__":
    unittest.main()
