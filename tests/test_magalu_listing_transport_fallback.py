import json
import os
import unittest
from unittest.mock import Mock, call, patch, sentinel

from seda import transport
from seda.common.orchestrator import _magalu_listing_env
from seda.magalu import zenrows_client


SEARCH_URL = (
    "https://www.magazineluiza.com.br/busca/tv/"
    "?page=1&sortType=score&sortOrientation=desc"
)


def _listing_html(page=1, sort_type="score", orientation="desc"):
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "search": {
                        "products": [{"id": "240144500"}],
                        "pagination": {"page": page, "size": 1},
                        "sorts": [
                            {
                                "selected": True,
                                "type": sort_type,
                                "orientation": orientation,
                            }
                        ],
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
    def test_listing_subprocess_forces_br_country_and_maps_scoped_settings(self):
        with patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_LISTING_FETCH_MODE": "magalu_browser_zenrows",
                "SEDA_MAGALU_LISTING_ALLOW_ZENROWS": "1",
                "SEDA_MAGALU_LISTING_ZENROWS_DRY_RUN": "0",
                "SEDA_MAGALU_LISTING_ZENROWS_PROFILE": "premium_html",
                "SEDA_MAGALU_LISTING_ZENROWS_TIMEOUT": "45",
                "SEDA_ZENROWS_PROXY_COUNTRY": "us",
            },
            clear=True,
        ):
            env = _magalu_listing_env("seda.magalu")

        self.assertEqual(env["SEDA_FETCH_MODE"], "magalu_browser_zenrows")
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
        }
        for name, html in invalid_cases.items():
            with self.subTest(case=name):
                self.assertTrue(
                    transport._magalu_listing_payload_error(SEARCH_URL, html)
                )

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
