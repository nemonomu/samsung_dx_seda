import json
import os
import unittest
from unittest.mock import Mock, call, patch, sentinel

from seda import transport
from seda.common.orchestrator import _magalu_listing_env, steps_for
from seda.magalu import browser_session, zenrows_client


SEARCH_URL = (
    "https://www.magazineluiza.com.br/busca/tv/"
    "?page=1&sortType=score&sortOrientation=desc"
)
SEARCH_URL_PAGE_2 = SEARCH_URL.replace("page=1", "page=2")


def _listing_html(
    page=1,
    sort_type="score",
    orientation="desc",
    term="tv",
    page_size=60,
    path="/smart-tv-teste/p/240144500/et/tv4k/",
):
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "search": {
                        "products": [
                            {
                                "id": "240144500",
                                "title": "Smart TV Teste 55 4K",
                                "path": path,
                            }
                        ],
                        "pagination": {"page": page, "size": page_size},
                        "sorts": [
                            {
                                "selected": True,
                                "type": sort_type,
                                "orientation": orientation,
                            }
                        ],
                        "term": {"raw": term, "refined": term},
                    }
                }
            }
        }
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
        + (" " * 600)
    )


class MagaluListingTransportFallbackTests(unittest.TestCase):
    def setUp(self):
        transport._reset_magalu_listing_recovery_state()
        browser_session._reset_search_browser_circuit()

    def tearDown(self):
        transport._reset_magalu_listing_recovery_state()
        browser_session._reset_search_browser_circuit()

    def test_listing_subprocess_forces_br_country_and_maps_scoped_settings(self):
        with patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_LISTING_FETCH_MODE": "magalu_listing_graphql_zenrows",
                "SEDA_MAGALU_LISTING_ALLOW_ZENROWS": "1",
                "SEDA_MAGALU_LISTING_ZENROWS_DRY_RUN": "0",
                "SEDA_MAGALU_LISTING_ZENROWS_PROFILE": "premium_html",
                "SEDA_MAGALU_LISTING_ZENROWS_TIMEOUT": "45",
                "SEDA_ZENROWS_PROXY_COUNTRY": "us",
            },
            clear=True,
        ):
            env = _magalu_listing_env("seda.magalu")

        self.assertEqual(
            env["SEDA_FETCH_MODE"],
            "magalu_listing_graphql_zenrows",
        )
        self.assertEqual(env["SEDA_ALLOW_ZENROWS"], "1")
        self.assertEqual(env["SEDA_ZENROWS_LISTING_PROFILE"], "premium_html")
        self.assertEqual(env["SEDA_ZENROWS_TIMEOUT"], "45")
        self.assertEqual(env["SEDA_ZENROWS_PROXY_COUNTRY"], "br")

    def test_listing_mode_is_browser_then_zenrows_and_honors_allow_switch(self):
        with patch.dict(os.environ, {"SEDA_ALLOW_ZENROWS": "1"}):
            self.assertEqual(
                transport.fetch_attempts("magalu_browser_zenrows"),
                ["browser", "zenrows"],
            )
        with patch.dict(os.environ, {"SEDA_ALLOW_ZENROWS": "0"}):
            self.assertEqual(
                transport.fetch_attempts("magalu_browser_zenrows"),
                ["browser"],
            )

    def test_listing_graphql_mode_is_graphql_then_zenrows(self):
        with patch.dict(os.environ, {"SEDA_ALLOW_ZENROWS": "1"}):
            self.assertEqual(
                transport.fetch_attempts(
                    "magalu_listing_graphql_zenrows"
                ),
                ["graphql", "zenrows"],
            )
        with patch.dict(os.environ, {"SEDA_ALLOW_ZENROWS": "0"}):
            self.assertEqual(
                transport.fetch_attempts(
                    "magalu_listing_graphql_zenrows"
                ),
                ["graphql"],
            )

    def test_listing_graphql_success_skips_zenrows(self):
        graphql_result = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="direct_graphql_search",
        )
        with patch.object(
            transport,
            "_fetch_graphql",
            return_value=graphql_result,
        ) as graphql, patch.object(
            transport,
            "_fetch_zenrows",
        ) as zenrows:
            result = transport.fetch_url(
                SEARCH_URL,
                mode="magalu_listing_graphql_zenrows",
                timeout=1,
            )

        self.assertIs(result, graphql_result)
        graphql.assert_called_once_with(SEARCH_URL, 1)
        zenrows.assert_not_called()

    def test_listing_graphql_failure_falls_through_to_zenrows(self):
        graphql_result = transport.FetchResult(
            url=SEARCH_URL,
            text="",
            method="browser_graphql_search",
            error="browser_graphql_failed",
            attempts=[{"method": "direct_graphql", "status_code": 403}],
        )
        zenrows_result = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows_graphql_search",
        )
        with patch.dict(
            os.environ,
            {"SEDA_ALLOW_ZENROWS": "1", "SEDA_RETRY_SLEEP_SECONDS": "0"},
        ), patch.object(
            transport,
            "_fetch_graphql",
            return_value=graphql_result,
        ) as graphql, patch.object(
            transport,
            "_fetch_zenrows",
            return_value=zenrows_result,
        ) as zenrows:
            result = transport.fetch_url(
                SEARCH_URL,
                mode="magalu_listing_graphql_zenrows",
                timeout=1,
            )

        self.assertIs(result, zenrows_result)
        graphql.assert_called_once_with(SEARCH_URL, 1)
        zenrows.assert_called_once_with(SEARCH_URL, 1)
        self.assertEqual(
            [item["method"] for item in result.attempts],
            ["browser_graphql_search", "zenrows_graphql_search"],
        )
        self.assertEqual(
            result.attempts[0]["inner_attempts"][0]["method"],
            "direct_graphql",
        )

    def test_listing_graphql_adapter_preserves_search_trace(self):
        search_trace = [
            {"method": "direct_graphql", "status_code": 403},
            {"method": "browser_graphql", "status_code": 0},
        ]
        with patch(
            "seda.magalu.search_api.fetch_search_listing",
            return_value={
                "success": False,
                "text": "",
                "method": "browser_graphql_search",
                "error": "browser_graphql_failed",
                "trace": search_trace,
            },
        ):
            result = transport._fetch_graphql(SEARCH_URL, 1)

        self.assertEqual(result.attempts, search_trace)
        self.assertIn("browser_graphql_failed", result.error)

    def test_listing_mode_is_scoped_to_main_and_bsr_only(self):
        with patch.dict(os.environ, {}, clear=True):
            magalu_steps = steps_for("seda.magalu")
            casas_steps = steps_for("seda.casas_bahia")

        by_name = {step.name: step for step in magalu_steps}
        self.assertEqual(
            by_name["main_list"].env["SEDA_FETCH_MODE"],
            "magalu_listing_graphql_zenrows",
        )
        self.assertEqual(
            by_name["bsr_list"].env["SEDA_FETCH_MODE"],
            "magalu_listing_graphql_zenrows",
        )
        self.assertIsNone(by_name["detail_enrichment"].env)
        casas_by_name = {step.name: step for step in casas_steps}
        self.assertIsNone(casas_by_name["main_list"].env)
        self.assertIsNone(casas_by_name["bsr_list"].env)

    def test_normal_browser_success_does_not_call_zenrows(self):
        browser_result = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="browser",
        )
        with patch.object(
            transport, "fetch_attempts", return_value=["browser", "zenrows"]
        ), patch.object(
            transport, "_fetch_browser", return_value=browser_result
        ), patch.object(transport, "_fetch_zenrows") as zenrows:
            result = transport.fetch_url(
                SEARCH_URL, mode="magalu_browser_zenrows", timeout=1
            )

        self.assertIs(result, browser_result)
        zenrows.assert_not_called()

    def test_long_browser_error_continues_to_zenrows(self):
        browser_result = transport.FetchResult(
            url=SEARCH_URL,
            text="login error " + ("x" * 600),
            status_code=200,
            method="browser",
            error="browser_html_search_login_redirect",
        )
        zenrows_result = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        with patch.dict(os.environ, {"SEDA_RETRY_SLEEP_SECONDS": "0"}), patch.object(
            transport, "fetch_attempts", return_value=["browser", "zenrows"]
        ), patch.object(
            transport, "_fetch_browser", return_value=browser_result
        ), patch.object(
            transport, "_fetch_zenrows", return_value=zenrows_result
        ) as zenrows:
            result = transport.fetch_url(
                SEARCH_URL, mode="magalu_browser_zenrows", timeout=1
            )

        self.assertIs(result, zenrows_result)
        zenrows.assert_called_once_with(SEARCH_URL, 1)

    def test_browser_circuit_skip_continues_to_zenrows_fallback(self):
        browser_session._SEARCH_BROWSER_SKIP_REASON = (
            "browser_html_search_login_redirect"
        )
        zenrows_result = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        with patch.dict(
            os.environ,
            {
                "SEDA_ALLOW_ZENROWS": "1",
                "SEDA_RETRY_SLEEP_SECONDS": "0",
            },
        ), patch.object(
            transport,
            "_fetch_zenrows",
            return_value=zenrows_result,
        ) as zenrows:
            result = transport.fetch_url(
                SEARCH_URL,
                mode="magalu_browser_zenrows",
                timeout=1,
            )

        self.assertIs(result, zenrows_result)
        self.assertEqual(
            result.attempts[0]["inner_attempts"][0]["method"],
            "browser_skip",
        )
        zenrows.assert_called_once_with(SEARCH_URL, 1)

    def test_browser_only_mode_preserves_circuit_failure(self):
        browser_session._SEARCH_BROWSER_SKIP_REASON = (
            "browser_html_search_login_redirect"
        )
        result = transport.fetch_url(
            SEARCH_URL,
            mode="browser",
            timeout=1,
        )

        self.assertFalse(result.text)
        self.assertIn(
            "browser_html_search_login_redirect_circuit_open",
            result.error,
        )
        self.assertEqual(
            result.attempts[0]["inner_attempts"][0]["method"],
            "browser_skip",
        )

    def test_listing_semantic_validator_accepts_valid_payload(self):
        self.assertEqual(
            transport._magalu_listing_payload_error(SEARCH_URL, _listing_html()),
            "",
        )

    def test_listing_semantic_validator_rejects_invalid_payloads(self):
        invalid_cases = {
            "login": (
                "<html>Ocorreu um erro ao recuperar a sacola.</html>" + ("x" * 600)
            ),
            "search_null": (
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"props": {"pageProps": {"data": {"search": None}}}})
                + "</script>"
            ),
            "page_mismatch": _listing_html(page=2),
            "sort_mismatch": _listing_html(sort_type="price", orientation="asc"),
            "term_mismatch": _listing_html(term="geladeira"),
            "size_mismatch": _listing_html(page_size=20),
            "path_mismatch": _listing_html(path="/busca/tv/"),
        }
        for name, html in invalid_cases.items():
            with self.subTest(case=name):
                self.assertTrue(
                    transport._magalu_listing_payload_error(SEARCH_URL, html)
                )

    def test_wrong_term_10x_response_escalates_to_valid_25x(self):
        first = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(term="geladeira"),
            status_code=200,
            method="zenrows",
        )
        second = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[first, second],
        ) as fetch:
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertIs(result, second)
        self.assertEqual(fetch.call_count, 2)

    def test_valid_10x_response_does_not_escalate_to_25x(self):
        first = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport, "_fetch_zenrows_once", return_value=first
        ) as fetch:
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertIs(result, first)
        fetch.assert_called_once_with(SEARCH_URL, 45, "premium_html", 1, 2)

    def test_invalid_10x_response_escalates_once_to_valid_25x(self):
        first = transport.FetchResult(
            url=SEARCH_URL,
            text="<html>not a listing</html>" + ("x" * 600),
            status_code=200,
            method="zenrows",
        )
        second = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport, "_fetch_zenrows_once", side_effect=[first, second]
        ) as fetch:
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertIs(result, second)
        self.assertEqual(
            fetch.call_args_list,
            [
                call(SEARCH_URL, 45, "premium_html", 1, 2),
                call(SEARCH_URL, 45, "listing_next_data_js_wait", 2, 2),
            ],
        )

    def test_semantic_invalid_10x_is_skipped_on_later_listing_page(self):
        invalid = transport.FetchResult(
            url=SEARCH_URL,
            text="<html>not a listing</html>" + ("x" * 600),
            status_code=200,
            method="zenrows",
        )
        first_valid = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        second_valid = transport.FetchResult(
            url=SEARCH_URL_PAGE_2,
            text=_listing_html(page=2),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[invalid, first_valid, second_valid],
        ) as fetch:
            first = transport._fetch_zenrows(SEARCH_URL, 45)
            second = transport._fetch_zenrows(SEARCH_URL_PAGE_2, 45)

        self.assertIs(first, first_valid)
        self.assertIs(second, second_valid)
        self.assertEqual(
            fetch.call_args_list,
            [
                call(SEARCH_URL, 45, "premium_html", 1, 2),
                call(SEARCH_URL, 45, "listing_next_data_js_wait", 2, 2),
                call(SEARCH_URL_PAGE_2, 45, "listing_next_data_js_wait", 1, 1),
            ],
        )

    def test_25x_connection_error_retries_same_profile_once(self):
        invalid = transport.FetchResult(
            url=SEARCH_URL,
            text="<html>not a listing</html>" + ("x" * 600),
            status_code=200,
            method="zenrows",
        )
        connection_error = transport.FetchResult(
            url=SEARCH_URL,
            text="",
            method="zenrows",
            error="request_error:ConnectionError",
        )
        valid = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[invalid, connection_error, valid],
        ) as fetch:
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertIs(result, valid)
        self.assertEqual(
            fetch.call_args_list,
            [
                call(SEARCH_URL, 45, "premium_html", 1, 2),
                call(SEARCH_URL, 45, "listing_next_data_js_wait", 2, 2),
                call(SEARCH_URL, 45, "listing_next_data_js_wait", 2, 2),
            ],
        )

    def test_25x_timeout_retry_is_capped_at_one_extra_attempt(self):
        invalid = transport.FetchResult(
            url=SEARCH_URL,
            text="<html>not a listing</html>" + ("x" * 600),
            status_code=200,
            method="zenrows",
        )
        first_timeout = transport.FetchResult(
            url=SEARCH_URL,
            text="",
            method="zenrows",
            error="request_error:Timeout",
        )
        second_timeout = transport.FetchResult(
            url=SEARCH_URL,
            text="",
            method="zenrows",
            error="request_error:Timeout",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[invalid, first_timeout, second_timeout],
        ) as fetch:
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertIs(result, second_timeout)
        self.assertEqual(fetch.call_count, 3)

    def test_25x_remains_terminal_when_stale_extra_profiles_are_configured(self):
        invalid = transport.FetchResult(
            url=SEARCH_URL,
            text="<html>not a listing</html>" + ("x" * 600),
            status_code=200,
            method="zenrows",
        )
        first_timeout = transport.FetchResult(
            url=SEARCH_URL,
            text="",
            method="zenrows",
            error="request_error:Timeout",
        )
        second_timeout = transport.FetchResult(
            url=SEARCH_URL,
            text="",
            method="zenrows",
            error="request_error:Timeout",
        )
        unused = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait,listing_next_data_js"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[invalid, first_timeout, second_timeout, unused],
        ) as fetch:
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertIs(result, second_timeout)
        self.assertEqual(
            [item.args[2] for item in fetch.call_args_list],
            [
                "premium_html",
                "listing_next_data_js_wait",
                "listing_next_data_js_wait",
            ],
        )

    def test_25x_nontransient_failure_does_not_use_stale_extra_profile(self):
        invalid = transport.FetchResult(
            url=SEARCH_URL,
            text="<html>not a listing</html>" + ("x" * 600),
            status_code=200,
            method="zenrows",
        )
        http_error = transport.FetchResult(
            url=SEARCH_URL,
            text="",
            status_code=502,
            method="zenrows",
            error="http_502",
        )
        unused = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait,listing_next_data_js"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[invalid, http_error, unused],
        ) as fetch:
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertIs(result, http_error)
        self.assertEqual(fetch.call_count, 2)

    def test_25x_nontransient_failures_do_not_retry(self):
        cases = (
            transport.FetchResult(
                url=SEARCH_URL,
                text="<html>invalid</html>" + ("x" * 600),
                status_code=200,
                method="zenrows",
            ),
            transport.FetchResult(
                url=SEARCH_URL,
                text="",
                status_code=502,
                method="zenrows",
                error="http_502",
            ),
            transport.FetchResult(
                url=SEARCH_URL,
                text="blocked",
                status_code=403,
                method="zenrows",
                error="http_403",
            ),
            transport.FetchResult(
                url=SEARCH_URL,
                text="",
                method="zenrows",
                error="request_error:RuntimeError",
            ),
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        for failure in cases:
            with self.subTest(error=failure.error or "semantic"):
                transport._reset_magalu_listing_recovery_state()
                invalid = transport.FetchResult(
                    url=SEARCH_URL,
                    text="<html>not a listing</html>" + ("x" * 600),
                    status_code=200,
                    method="zenrows",
                )
                with patch.dict(os.environ, env), patch.object(
                    transport,
                    "_fetch_zenrows_once",
                    side_effect=[invalid, failure],
                ) as fetch:
                    result = transport._fetch_zenrows(SEARCH_URL, 45)

                self.assertIs(result, failure)
                self.assertEqual(fetch.call_count, 2)

    def test_10x_connection_error_does_not_open_semantic_skip_circuit(self):
        connection_error = transport.FetchResult(
            url=SEARCH_URL,
            text="",
            method="zenrows",
            error="request_error:ConnectionError",
        )
        first_valid = transport.FetchResult(
            url=SEARCH_URL,
            text=_listing_html(),
            status_code=200,
            method="zenrows",
        )
        second_valid = transport.FetchResult(
            url=SEARCH_URL_PAGE_2,
            text=_listing_html(page=2),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[connection_error, first_valid, second_valid],
        ) as fetch:
            transport._fetch_zenrows(SEARCH_URL, 45)
            transport._fetch_zenrows(SEARCH_URL_PAGE_2, 45)

        self.assertEqual(
            [item.args[2] for item in fetch.call_args_list],
            ["premium_html", "listing_next_data_js_wait", "premium_html"],
        )

    def test_all_invalid_profiles_return_error_even_with_long_html(self):
        first = transport.FetchResult(
            url=SEARCH_URL,
            text="<html>invalid first</html>" + ("x" * 600),
            status_code=200,
            method="zenrows",
        )
        second = transport.FetchResult(
            url=SEARCH_URL,
            text="<html>invalid second</html>" + ("x" * 600),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport, "_fetch_zenrows_once", side_effect=[first, second]
        ):
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertIs(result, second)
        self.assertIn("invalid_listing_payload", result.error)

    def test_profile_ladder_records_each_real_http_attempt(self):
        first_response = Mock(
            ok=True,
            status_code=200,
            text="<html>not a listing</html>" + ("x" * 600),
            headers={},
        )
        second_response = Mock(
            ok=True,
            status_code=200,
            text=_listing_html(),
            headers={},
        )
        env = {
            "SEDA_ALLOW_ZENROWS": "1",
            "SEDA_ZENROWS_DRY_RUN": "0",
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_ZENROWS_PROXY_COUNTRY": "de",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch(
            "seda.magalu.zenrows_client.api_key",
            return_value=sentinel.api_key,
        ), patch(
            "seda.magalu.zenrows_client.requests.get",
            side_effect=[first_response, second_response],
        ) as request_get, patch(
            "seda.zenrows_usage.record_http_request_attempt",
            return_value=True,
        ) as record_usage, patch(
            "seda.zenrows_usage.usage_required",
            return_value=True,
        ):
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertEqual(
            record_usage.call_args_list,
            [
                call("GET", "premium_html", "10x"),
                call("GET", "listing_next_data_js_wait", "25x"),
            ],
        )
        self.assertEqual(request_get.call_count, 2)
        first_kwargs = request_get.call_args_list[0].kwargs
        second_kwargs = request_get.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["timeout"], 45)
        self.assertEqual(second_kwargs["timeout"], 45)
        self.assertEqual(first_kwargs["params"]["proxy_country"], "br")
        self.assertEqual(second_kwargs["params"]["proxy_country"], "br")
        self.assertNotIn("js_render", first_kwargs["params"])
        self.assertEqual(second_kwargs["params"]["js_render"], "true")
        self.assertEqual(result.text, _listing_html())

    def test_transient_retry_records_every_real_http_attempt(self):
        first_response = Mock(
            ok=True,
            status_code=200,
            text="<html>not a listing</html>" + ("x" * 600),
            headers={},
        )
        final_response = Mock(
            ok=True,
            status_code=200,
            text=_listing_html(),
            headers={},
        )
        env = {
            "SEDA_ALLOW_ZENROWS": "1",
            "SEDA_ZENROWS_DRY_RUN": "0",
            "SEDA_ZENROWS_TIMEOUT": "45",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch(
            "seda.magalu.zenrows_client.api_key",
            return_value=sentinel.api_key,
        ), patch(
            "seda.magalu.zenrows_client.requests.get",
            side_effect=[
                first_response,
                zenrows_client.requests.exceptions.ConnectionError(
                    "connection failed"
                ),
                final_response,
            ],
        ) as request_get, patch(
            "seda.zenrows_usage.record_http_request_attempt",
            return_value=True,
        ) as record_usage, patch(
            "seda.zenrows_usage.usage_required",
            return_value=True,
        ):
            result = transport._fetch_zenrows(SEARCH_URL, 45)

        self.assertEqual(
            record_usage.call_args_list,
            [
                call("GET", "premium_html", "10x"),
                call("GET", "listing_next_data_js_wait", "25x"),
                call("GET", "listing_next_data_js_wait", "25x"),
            ],
        )
        self.assertEqual(request_get.call_count, 3)
        for request_call in request_get.call_args_list:
            self.assertEqual(request_call.kwargs["timeout"], 45)
            self.assertEqual(
                request_call.kwargs["params"]["proxy_country"],
                "br",
            )
        self.assertEqual(result.text, _listing_html())

    def test_non_listing_urls_are_not_subject_to_listing_validator(self):
        for url in (
            "https://www.magazineluiza.com.br/produto/p/item/",
            "https://www.casasbahia.com.br/busca/tv/",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    transport._magalu_listing_payload_error(
                        url, "<html>plain</html>"
                    ),
                    "",
                )

    def test_listing_helper_preserves_non_success_http_result(self):
        failed = zenrows_client.ZenRowsResult(
            success=False,
            url=SEARCH_URL,
            profile="premium_html",
            status_code=502,
            text=_listing_html(),
            error="http_502",
            estimated_multiplier="10x",
        )
        with patch(
            "seda.magalu.zenrows_client.request_url",
            return_value=failed,
        ):
            result = zenrows_client.fetch_listing_next_data_html(
                SEARCH_URL,
                profile="premium_html",
                timeout=45,
            )

        self.assertIs(result, failed)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "http_502")
        self.assertIn("__NEXT_DATA__", result.text)


if __name__ == "__main__":
    unittest.main()
