import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from seda.magalu import browser_session
from seda.magalu.detail_api import _detail_from_item, _post, _post_browser_graphql
from seda.parsers import (
    _parse_magalu_next_detail,
    high_confidence_tv_model_number_from_text,
)
from seda.step00_config import read_csv
from seda.step08_detail_enrichment import (
    _backfill_magalu_detail_blanks,
    _backfill_magalu_tv_sku_from_title,
    _has_blocked_graphql_trace,
    _is_expected_magalu_item_query_block,
    _magalu_tv_sku_is_recovery_target,
    _merge_parallel_detail_traces,
    _needs_magalu_detail_retry,
    _retry_magalu_detail_blanks,
    _write_detail_traces,
    _write_trace_csv,
)
from seda.step14_db_load import _db_value
from seda.step15_final_output import _format_row, _sku_for_output


class _FakePage:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def run_js(self, _script, _payload, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


def _browser_response(payload, status=200):
    return json.dumps(
        {
            "status": status,
            "text": payload if isinstance(payload, str) else json.dumps(payload),
        }
    )


def _failed_row(item="240144500", title='Smart TV 75" TCL 4K UHD QLED 75P7K'):
    return {
        "retailer": "Magalu",
        "product_line": "TV",
        "item": item,
        "sku": item,
        "product_url": f"https://www.magazineluiza.com.br/produto/p/{item}/et/tv4k/",
        "retailer_sku_name": title,
        "parse_status": "listing_next_data+detail_graphql_failed:item_query_failed",
    }


class MagaluSkuRecoveryTests(unittest.TestCase):
    def test_retry_selects_item_query_failure_but_skips_successful_no_model(self):
        failed = _failed_row()
        successful_no_model = {
            "retailer": "Magalu",
            "product_line": "TV",
            "item": "cc2215dbaj",
            "sku": "cc2215dbaj",
            "product_url": "https://www.magazineluiza.com.br/produto/p/cc2215dbaj/et/tv4k/",
            "retailer_sku_name": "Smart TV 43 Full HD AOC Roku TV HDMI 1 USB WiFi Conversor Digital",
            "parse_status": "listing_next_data+detail_item_graphql",
        }
        self.assertTrue(_needs_magalu_detail_retry(failed))
        self.assertFalse(_needs_magalu_detail_retry(successful_no_model))
        identity_failure = dict(failed, parse_status="detail_graphql_failed:item_identity_mismatch")
        self.assertFalse(_needs_magalu_detail_retry(identity_failure))

        def recover(row, *_args, **_kwargs):
            row["sku"] = "75P7K"
            row["parse_status"] += "+detail_item_graphql"
            return {"success": True, "detail": {"sku": "75P7K"}}

        with patch("seda.step08_detail_enrichment._magalu_graphql_detail", side_effect=recover) as call:
            rows = _backfill_magalu_detail_blanks(
                [failed, successful_no_model],
                "unused.csv",
                checkpoint_every=0,
            )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(rows[0]["sku"], "75P7K")
        self.assertEqual(rows[1]["sku"], "cc2215dbaj")
        self.assertEqual([row["item"] for row in rows], ["240144500", "cc2215dbaj"])

    def test_retry_failure_does_not_treat_listing_sentinel_as_success(self):
        row = _failed_row()
        with patch.dict(os.environ, {"SEDA_MAGALU_DETAIL_BLANK_RETRY_ATTEMPTS": "2"}), patch(
            "seda.step08_detail_enrichment._magalu_graphql_detail",
            return_value={"success": False, "error": "item_query_failed"},
        ) as call:
            self.assertFalse(_retry_magalu_detail_blanks(row, row["product_url"]))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(row["sku"], row["item"])
        self.assertIn("detail_blank_retry_failed", row["parse_status"])
        self.assertNotIn("detail_blank_retry+", row["parse_status"])

    def test_retry_success_uses_result_success_even_without_detail_sku(self):
        row = _failed_row()
        with patch.dict(os.environ, {"SEDA_MAGALU_DETAIL_BLANK_RETRY_ATTEMPTS": "2"}), patch(
            "seda.step08_detail_enrichment._magalu_graphql_detail",
            return_value={"success": True, "detail": {}},
        ) as call:
            self.assertTrue(_retry_magalu_detail_blanks(row, row["product_url"]))
        self.assertEqual(call.call_count, 1)
        self.assertIn("detail_blank_retry", row["parse_status"])

    def test_failed_retries_use_guarded_title_fallback_and_forward_trace_context(self):
        row = _failed_row()
        trace_rows = []
        with patch.dict(os.environ, {"SEDA_MAGALU_DETAIL_BLANK_RETRY_ATTEMPTS": "1"}), patch(
            "seda.step08_detail_enrichment._magalu_graphql_detail",
            return_value={"success": False, "error": "item_query_failed"},
        ) as call:
            rows = _backfill_magalu_detail_blanks(
                [row],
                "unused.csv",
                checkpoint_every=0,
                trace_rows=trace_rows,
                row_index_offset=38,
            )
        self.assertEqual(rows[0]["sku"], "75P7K")
        self.assertIn("sku_title_fallback_after_detail_retry", rows[0]["parse_status"])
        self.assertEqual(call.call_args.kwargs["trace_rows"], trace_rows)
        self.assertEqual(call.call_args.kwargs["row_index"], 39)
        self.assertEqual(call.call_args.kwargs["trace_subcall"], "detail_graphql_retry")

    def test_high_confidence_title_fallback_real_cases_and_guards(self):
        cases = {
            'Smart TV 75" TCL 4K UHD QLED 75P7K Google TV': "75P7K",
            'Smart TV 55" TCL 4K UHD QLED 55P7K Google TV': "55P7K",
            'Smart TV 65" TCL 4K UHD MiniLED 65C6K 120Hz Google TV': "65C6K",
            'Smart TV 98" TCL 4K UHD QD-Mini LED 98A400M Google TV 2026': "98A400M",
            'Smart TV AIWA 32" Android HD Borda Ultrafina HDR10 AWS-TV-32-BL-02-A': "AWS-TV-32-BL-02-A",
            'Smart TV Samsung 32" HD Wi-Fi Tizen LS32H5000FGXZD': "LS32H5000FGXZD",
            'Smart TV 4K LG Mini RGB 65" 65MRGB85BSC HDR10': "65MRGB85BSC",
            "Smart TV Philco 32'' PTV32K34RKGB Roku TV LED": "PTV32K34RKGB",
            'Smart TV Samsung 55" 55U8600F + Soundbar HW55Q600B': "55U8600F",
            'Smart TV Samsung 55" 55U8600F com Home Theater HT55X900': "55U8600F",
            'Smart TV TCL 65" 65P7K + Câmera CAM65X20': "65P7K",
            'Smart TV Philco 32" P32CRB Roku TV': "P32CRB",
            'Smart TV Philco 40" P40CRA Roku TV': "P40CRA",
            'Smart TV Britânia 43" B43KRA': "B43KRA",
            'Smart TV Semp 32" 32S42': "32S42",
            'Smart TV 32" Samsung HD 32H5000F Tizen': "32H5000F",
            'Smart TV 40" TCL Full HD QLED 40S5K Google TV': "40S5K",
            'Smart TV LG 43 LED FULL HD Smart Pro 43LR671CB Bivolt': "43LR671CB",
            'SEMP Smart TV 43" 4K UHD LED Google TV HDR10+ Wi-Fi Bluetooth Alexa S4362': "S4362",
            'Smart TV TCL 50" 50S62': "50S62",
            'Smart TV LG OLED 65" OLED65CX': "OLED65CX",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(high_confidence_tv_model_number_from_text(title), expected)

        guards = (
            "Smart TV 43 Full HD AOC Roku TV HDMI 1 USB WiFi Conversor Digital",
            "Smart TV 32 HD Multi Roku 3HDMI 2USB Wi-Fi HDR10 RJ45",
            "Smart TV 43 polegadas FreeSync120 HDR10 HDMI21",
            "Controle Compatível Samsung 4K 50LS03A para TV 50''",
            "Cr Tv Philco Rc3000m01 099463001ATA TV PH46 LED",
            "Smart TV 55 polegadas 55FPS 55GHZ 55BIT TV55SMART",
            "Smart TV 55 polegadas com Suporte STPA55V2",
            "Smart TV 55 polegadas com Base BASE55V2",
            "Smart TV 55 polegadas com Painel PNL55V2",
            'Soundbar HW55Q600B para Smart TV Samsung 55"',
            'Smart TV 55" modelos 55ABC123 e 55XYZ789',
            'Smart TV 55" SMART55 Android 55',
            'Smart TV 55" QLED55 HDR55',
            'Capa protetora para Smart TV Samsung 55" UN55U8600FGXZD',
            'Película protetora para Smart TV Samsung 55" UN55U8600FGXZD',
            'Estante para TV 65" modelo 65ABC123',
        )
        for title in guards:
            with self.subTest(guard=title):
                self.assertEqual(high_confidence_tv_model_number_from_text(title), "")

    def test_tv_producers_preserve_reference_until_verified_recovery(self):
        cases = (
            (
                'Smart TV Samsung 32" HD Wi-Fi Tizen LS32H5000FGXZD',
                "2729",
                "",
                "2729",
            ),
            (
                'Smart TV AIWA 32" Android HD Borda Ultrafina HDR10 AWS-TV-32-BL-02-A',
                "",
                "",
                "",
            ),
            (
                'Smart TV Philco 32" P32CRB Roku TV',
                "",
                "",
                "",
            ),
            (
                "Smart TV 43 Full HD AOC Roku TV HDMI 1 USB WiFi",
                "VALID-REFERENCE",
                "",
                "VALID-REFERENCE",
            ),
            (
                "Smart TV 43 Full HD AOC Roku TV HDMI 1 USB WiFi",
                "",
                "",
                "",
            ),
            (
                'Smart TV Samsung 55" 55U8600F + Soundbar HW55Q600B',
                "UN55U8600FGXZD",
                "",
                "UN55U8600FGXZD",
            ),
        )
        for title, reference, model, expected in cases:
            with self.subTest(title=title):
                factsheet = []
                if reference:
                    factsheet.append({"keyName": "Referencia", "value": reference})
                if model:
                    factsheet.append({"keyName": "Modelo", "value": model})
                item = {
                    "id": "sample",
                    "title": title,
                    "factsheet": factsheet,
                    "attributes": [],
                    "offers": [],
                    "rating": {},
                }
                payload = {"props": {"pageProps": {"data": {"item": item}}}}
                html = (
                    '<script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps(payload)
                    + "</script>"
                )
                with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}):
                    graphql = _detail_from_item(item)
                    next_data = _parse_magalu_next_detail(
                        html,
                        "https://www.magazineluiza.com.br",
                        "https://www.magazineluiza.com.br/p/sample/et/tv4k/",
                    )
                self.assertEqual(graphql["sku"], expected)
                self.assertEqual(next_data["sku"], expected)

    def test_non_tv_sku_priority_is_unchanged(self):
        item = {
            "id": "sample",
            'title': 'Geladeira portátil com display TV 32" LS32H5000FGXZD',
            "factsheet": [{"keyName": "Referencia", "value": "REF-ORIGINAL"}],
            "attributes": [],
            "offers": [],
            "rating": {},
        }
        payload = {"props": {"pageProps": {"data": {"item": item}}}}
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )
        for line, path in (("REF", "ed/refr"), ("LDY", "ed/lava")):
            with self.subTest(line=line), patch.dict(os.environ, {"SEDA_PRODUCT_LINE": line}):
                graphql = _detail_from_item(item)
                next_data = _parse_magalu_next_detail(
                    html,
                    "https://www.magazineluiza.com.br",
                    f"https://www.magazineluiza.com.br/p/sample/{path}/",
                )
            self.assertEqual(graphql["sku"], "REF-ORIGINAL")
            self.assertEqual(next_data["sku"], "REF-ORIGINAL")

    def test_title_fallback_hook_is_identity_and_accessory_guarded(self):
        row = _failed_row()
        self.assertTrue(_backfill_magalu_tv_sku_from_title(row))
        self.assertEqual(row["sku"], "75P7K")
        self.assertIn("sku_title_fallback_after_detail_retry", row["parse_status"])

        authoritative = _failed_row()
        authoritative["sku"] = "UN75U8600FGXZD"
        self.assertFalse(_backfill_magalu_tv_sku_from_title(authoritative))
        self.assertEqual(authoritative["sku"], "UN75U8600FGXZD")

        identity_mismatch = _failed_row()
        identity_mismatch["product_url"] = (
            "https://www.magazineluiza.com.br/product/p/different/et/tv4k/"
        )
        self.assertFalse(_backfill_magalu_tv_sku_from_title(identity_mismatch))
        self.assertEqual(identity_mismatch["sku"], identity_mismatch["item"])

        accessory = _failed_row(
            title='Capa protetora para Smart TV Samsung 75" UN75U8600FGXZD'
        )
        self.assertFalse(_backfill_magalu_tv_sku_from_title(accessory))
        self.assertEqual(accessory["sku"], accessory["item"])

    def test_descriptive_sku_stays_a_recovery_target_even_if_reference_tagged(self):
        row = _failed_row()
        row["sku"] = 'Smart TV Samsung 75" 75U8600F'
        self.assertTrue(_magalu_tv_sku_is_recovery_target(row))
        with patch.dict(
            os.environ,
            {"SEDA_ACTIVE_RETAILER": "magalu", "SEDA_PRODUCT_LINE": "TV"},
        ):
            self.assertEqual(_sku_for_output(row, row["item"]), "")

        row["parse_status"] += "+sku_factsheet_reference_recovered"
        self.assertTrue(_magalu_tv_sku_is_recovery_target(row))
        with patch.dict(
            os.environ,
            {"SEDA_ACTIVE_RETAILER": "magalu", "SEDA_PRODUCT_LINE": "TV"},
        ):
            self.assertEqual(_sku_for_output(row, row["item"]), "")

    def test_magalu_descriptive_filter_does_not_change_casas_ldy_sku(self):
        row = {
            "retailer": "Casas Bahia",
            "sku": "Lavadora XPTO",
        }
        with patch.dict(
            os.environ,
            {"SEDA_ACTIVE_RETAILER": "casas_bahia", "SEDA_PRODUCT_LINE": "LDY"},
        ):
            self.assertEqual(_sku_for_output(row, "item-1"), "Lavadora XPTO")

    def test_recovered_and_unresolved_final_db_contract(self):
        with patch.dict(
            os.environ,
            {"SEDA_ACTIVE_RETAILER": "magalu", "SEDA_PRODUCT_LINE": "TV"},
        ):
            self.assertEqual(_sku_for_output({"sku": "75P7K"}, "240144500"), "75P7K")
            self.assertEqual(_sku_for_output({"sku": "240144500"}, "240144500"), "")
        self.assertEqual(_db_value("sku", "75P7K"), "75P7K")
        self.assertIsNone(_db_value("sku", ""))

        recovered = _failed_row()
        recovered["sku"] = "75P7K"
        unresolved = _failed_row()
        with patch.dict(
            os.environ,
            {"SEDA_ACTIVE_RETAILER": "magalu", "SEDA_PRODUCT_LINE": "TV"},
        ):
            recovered_final = _format_row(recovered, datetime(2026, 7, 24, 20, 17, 50))
            unresolved_final = _format_row(unresolved, datetime(2026, 7, 24, 20, 17, 50))
        self.assertEqual(recovered_final["sku"], "75P7K")
        self.assertEqual(unresolved_final["sku"], "")
        self.assertEqual(_db_value("sku", recovered_final["sku"]), "75P7K")
        self.assertIsNone(_db_value("sku", unresolved_final["sku"]))

    def test_browser_item_query_semantic_failures_are_retried_and_traced(self):
        item = {"id": "240144500", "title": "Smart TV TCL 75P7K"}
        page = _FakePage(
            [
                _browser_response({"data": {"item": None}}),
                _browser_response({"errors": [{"message": "temporary"}]}),
                _browser_response({"data": {"item": item}}),
            ]
        )
        payload = {"operationName": "itemQuery", "variables": {"itemId": "240144500"}}
        with patch.dict(os.environ, {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"}), patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ):
            result = browser_session.graphql_post(payload, context_url="https://example/p/240144500")
        self.assertEqual(page.calls, 3)
        self.assertEqual(result["data"]["data"]["item"]["id"], "240144500")
        self.assertEqual([entry["error"] for entry in result["trace"]], [
            "graphql_item_missing",
            "graphql_errors",
            "",
        ])
        self.assertEqual([entry["item_present"] for entry in result["trace"]], [0, 0, 1])
        self.assertIn("temporary", json.dumps(result["trace"][1]["graphql_errors"]))

    def test_browser_item_query_normal_first_response_stays_single_call(self):
        page = _FakePage([_browser_response({"data": {"item": {"id": "ok"}}})])
        payload = {"operationName": "itemQuery", "variables": {"itemId": "ok"}}
        with patch.dict(os.environ, {"SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS": "3"}), patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._prepare_js_page",
            side_effect=lambda active_page, *_args, **_kwargs: active_page,
        ):
            result = browser_session.graphql_post(payload)
        self.assertEqual(page.calls, 1)
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(result["trace"][0]["error"], "")

    def test_detail_api_preserves_each_browser_attempt(self):
        payload = {"operationName": "itemQuery", "variables": {"itemId": "ok"}}
        browser_result = {
            "status_code": 200,
            "text": json.dumps({"data": {"item": {"id": "ok"}}}),
            "data": {"data": {"item": {"id": "ok"}}},
            "error": "",
            "trace": [
                {
                    "operation": "itemQuery",
                    "attempt": 1,
                    "method": "browser_graphql",
                    "status_code": 200,
                    "length": 23,
                    "error": "graphql_item_missing",
                    "item_present": 0,
                    "response_preview": '{"data":{"item":null}}',
                },
                {
                    "operation": "itemQuery",
                    "attempt": 2,
                    "method": "browser_graphql",
                    "status_code": 200,
                    "length": 31,
                    "error": "",
                    "item_present": 1,
                },
            ],
        }
        trace = []
        with patch("seda.magalu.browser_session.graphql_post", return_value=browser_result):
            data = _post_browser_graphql(payload, 10, trace, "item", context_url="https://example/p/ok")
        self.assertEqual(data["data"]["item"]["id"], "ok")
        self.assertEqual([item["attempt"] for item in trace], [1, 2])
        self.assertEqual([item["item_present"] for item in trace], [0, 1])
        self.assertEqual(trace[0]["error"], "graphql_item_missing")

    def test_non_dict_graphql_json_is_diagnostic_failure_not_worker_crash(self):
        response = Mock(
            status_code=200,
            text="[]",
            headers={"content-type": "application/json"},
        )
        response.json.return_value = []
        trace = []
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_BROWSER_GRAPHQL": "0", "SEDA_MAGALU_DETAIL_RETRIES": "0"},
        ), patch("seda.magalu.detail_api.requests.post", return_value=response):
            self.assertEqual(_post({"operationName": "itemQuery"}, 10, trace, "item"), {})
        self.assertEqual(trace[-1]["error"], "invalid_json")

        browser_trace = []
        browser_result = {
            "status_code": 200,
            "text": "[]",
            "data": [],
            "error": "invalid_json",
            "trace": [
                {
                    "operation": "itemQuery",
                    "attempt": 1,
                    "status_code": 200,
                    "length": 2,
                    "error": "invalid_json",
                    "item_present": 0,
                    "response_preview": "[]",
                }
            ],
        }
        with patch("seda.magalu.browser_session.graphql_post", return_value=browser_result):
            self.assertIsNone(
                _post_browser_graphql(
                    {"operationName": "itemQuery"},
                    10,
                    browser_trace,
                    "item",
                )
            )
        self.assertEqual(browser_trace[-1]["error"], "invalid_json")

    def test_browser_mode_skips_known_blocked_plain_requests_fallback_by_default(self):
        with patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_BROWSER_GRAPHQL": "1",
                "SEDA_MAGALU_BROWSER_GRAPHQL_REQUESTS_FALLBACK": "0",
            },
        ), patch(
            "seda.magalu.detail_api._post_browser_graphql",
            return_value=None,
        ), patch("seda.magalu.detail_api.requests.post") as requests_post:
            self.assertEqual(_post({"operationName": "itemQuery"}, 10, [], "item"), {})
        requests_post.assert_not_called()

    def test_item_missing_then_requests_403_is_not_misclassified_as_global_block(self):
        result = {
            "success": False,
            "error": "item_query_failed",
            "trace": [
                {
                    "label": "item",
                    "method": "browser_graphql",
                    "status_code": 200,
                    "error": "graphql_item_missing",
                },
                {
                    "label": "item",
                    "method": "requests",
                    "status_code": 403,
                    "error": "non_json_or_blocked",
                },
            ],
        }
        self.assertTrue(_has_blocked_graphql_trace(result))
        self.assertTrue(_is_expected_magalu_item_query_block(result))

        blocked_browser = {
            **result,
            "trace": [
                {
                    "label": "item",
                    "method": "browser_graphql",
                    "status_code": 403,
                    "error": "blocked_response",
                }
            ],
        }
        self.assertFalse(_is_expected_magalu_item_query_block(blocked_browser))

        mixed_browser = {
            **result,
            "trace": [
                result["trace"][0],
                {
                    "label": "item",
                    "method": "browser_graphql",
                    "status_code": 403,
                    "error": "blocked_response",
                },
                result["trace"][1],
            ],
        }
        self.assertFalse(_is_expected_magalu_item_query_block(mixed_browser))

    def test_worker_trace_parts_are_unique_and_parent_merge_preserves_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tags = ["run_w0", "run_w1"]
            with patch.dict(os.environ, {"SEDA_DETAIL_TRACE": "1"}):
                for worker, tag in enumerate(tags):
                    with patch.dict(
                        os.environ,
                        {
                            "SEDA_DETAIL_TRACE_TAG": tag,
                            "SEDA_DETAIL_RUN_TOKEN": "run",
                            "SEDA_DETAIL_WORKER_ID": str(worker),
                        },
                    ):
                        _write_detail_traces(
                            root,
                            [
                                {
                                    "row_index": worker + 1,
                                    "run_token": "run",
                                    "worker_id": str(worker),
                                    "item": f"item-{worker}",
                                    "subcall": "detail_graphql",
                                    "operation": "itemQuery",
                                    "attempt": 1,
                                    "status_code": 200,
                                    "item_present": worker,
                                }
                            ],
                            [],
                        )
                expected_rows = [
                    {"item": "item-0", "product_url": ""},
                    {"item": "item-1", "product_url": ""},
                ]
                parts = [
                    (0, 0, 1, "unused-0.csv", tags[0]),
                    (1, 1, 2, "unused-1.csv", tags[1]),
                ]
                merged, reviews = _merge_parallel_detail_traces(
                    root,
                    parts,
                    expected_rows,
                    "run",
                )

            trace_dir = root / "detail" / "trace"
            self.assertTrue((trace_dir / "subcall_trace_run_w0.csv").exists())
            self.assertTrue((trace_dir / "subcall_trace_run_w1.csv").exists())
            self.assertEqual([row["item"] for row in merged], ["item-0", "item-1"])
            self.assertEqual([row["worker_id"] for row in merged], ["0", "1"])
            self.assertEqual(reviews, [])
            self.assertFalse((trace_dir / "subcall_trace.csv").exists())

    def test_trace_write_is_atomic_and_missing_parts_preserve_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace_path = root / "detail" / "trace" / "subcall_trace.csv"
            _write_trace_csv(trace_path, [{"item": "old-complete"}], ["item"])
            with patch(
                "seda.step08_detail_enrichment.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    _write_trace_csv(trace_path, [{"item": "new-partial"}], ["item"])
            self.assertEqual(read_csv(str(trace_path)), [{"item": "old-complete"}])
            self.assertEqual(list(trace_path.parent.glob(".*.tmp")), [])

            with patch.dict(os.environ, {"SEDA_DETAIL_TRACE": "1"}), self.assertRaisesRegex(
                RuntimeError,
                "subcall_trace:missing",
            ):
                _merge_parallel_detail_traces(
                    root,
                    [(0, 0, 1, "unused.csv", "missing-worker")],
                    [{"item": "item", "product_url": ""}],
                    "run",
                )
            self.assertEqual(read_csv(str(trace_path)), [{"item": "old-complete"}])


if __name__ == "__main__":
    unittest.main()
