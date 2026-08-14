import copy
import json
import os
import unittest
from unittest.mock import Mock, call, patch

from seda import transport
from seda.magalu import search_api, zenrows_client
from seda.parsers import parse_listing


TV_URL = (
    "https://www.magazineluiza.com.br/busca/tv/"
    "?page=1&sortType=score&sortOrientation=desc"
)
BSR_URL = TV_URL.replace("sortType=score", "sortType=soldQuantity")


def _product(item="path-item", product_id="offer-id"):
    return {
        "id": product_id,
        "title": 'Smart TV Teste 55" 4K',
        "path": f"/smart-tv-teste/p/{item}/et/tv4k/",
        "available": True,
        "price": {"bestPrice": 1999.9, "fullPrice": 2199.9},
        "seller": {"id": "magazineluiza", "sku": "seller-offer"},
        "rating": {"count": 1, "score": 5},
        "shippingTag": {},
        "subcategory": {"id": "TV4K", "name": "TV 4K"},
    }


def _search(
    *,
    page=1,
    size=60,
    sort_type="score",
    orientation="desc",
    term="tv",
    products=None,
):
    return {
        "products": [_product()] if products is None else products,
        "pagination": {
            "page": page,
            "pages": 17,
            "records": 10000,
            "size": size,
            "start": (page - 1) * size,
        },
        "sorts": [
            {
                "label": "selected",
                "selected": True,
                "type": sort_type,
                "orientation": orientation,
            }
        ],
        "term": {"raw": term, "refined": term},
        "trackId": "test-track",
    }


def _zenrows_result(body, *, success=True, status_code=200, error=""):
    return zenrows_client.ZenRowsResult(
        success=success,
        url=search_api.GRAPHQL_URL,
        profile="premium_html",
        status_code=status_code,
        text=json.dumps(body),
        error=error,
        headers={"X-Request-Cost": "0.001"},
        estimated_multiplier="10x",
    )


def _listing_html(search):
    return search_api._as_next_data_html(search, TV_URL) + (" " * 600)


class MagaluZenRowsListingGraphQLTests(unittest.TestCase):
    def setUp(self):
        transport._reset_magalu_listing_recovery_state()

    def tearDown(self):
        transport._reset_magalu_listing_recovery_state()

    def test_native_direct_graphql_success_skips_browser_and_uses_page_size_60(self):
        payload = {"data": {"search": _search()}}
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
                "SEDA_MAGALU_SEARCH_PAGE_SIZE": "20",
                "SEDA_MAGALU_SEARCH_BROWSER_GRAPHQL": "1",
            },
        ), patch(
            "seda.magalu.search_api.requests.Session",
            return_value=session,
        ), patch.object(
            search_api,
            "_fetch_search_listing_browser",
        ) as browser:
            result = search_api.fetch_search_listing(TV_URL, timeout=10)

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "direct_graphql_search")
        browser.assert_not_called()
        posted_payload = session.post.call_args.kwargs["json"]
        self.assertEqual(posted_payload["variables"]["pageSize"], 60)

    def test_invalid_direct_payload_falls_through_to_browser_graphql(self):
        payload = {"data": {"search": _search(term="geladeira")}}
        response = Mock(
            status_code=200,
            text=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        response.json.return_value = payload
        session = Mock()
        session.post.return_value = response
        browser_result = {
            "success": True,
            "text": _listing_html(_search()),
            "products": 1,
            "page_size": 60,
            "trace": [],
            "method": "browser_graphql_search",
        }
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_SEARCH_RETRIES": "0"},
        ), patch(
            "seda.magalu.search_api.requests.Session",
            return_value=session,
        ), patch.object(
            search_api,
            "_fetch_search_listing_browser",
            return_value=browser_result,
        ) as browser:
            result = search_api.fetch_search_listing(TV_URL, timeout=10)

        self.assertIs(result, browser_result)
        self.assertEqual(
            result["method"],
            "browser_graphql_search",
        )
        self.assertEqual(
            browser.call_args.args[1],
            [search_api.MAGALU_LISTING_PAGE_SIZE],
        )

    def test_browser_graphql_rejects_wrong_search_context(self):
        browser_response = {
            "status_code": 200,
            "data": {"data": {"search": _search(term="geladeira")}},
            "trace": [],
            "error": "",
        }
        trace = []
        with patch.dict(
            os.environ,
            {"SEDA_MAGALU_SEARCH_BROWSER_ATTEMPTS": "1"},
        ), patch(
            "seda.magalu.browser_session.ensure_magalu_session",
            return_value={"success": True, "trace": []},
        ), patch(
            "seda.magalu.browser_session.graphql_post",
            return_value=browser_response,
        ):
            result = search_api._fetch_search_listing_browser(
                TV_URL,
                [60],
                10,
                trace,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "search_term_mismatch:geladeira!=tv")
        self.assertEqual(trace[-1]["error"], result["error"])

    def test_valid_response_is_parser_compatible_and_uses_path_identity(self):
        response = _zenrows_result({"data": {"search": _search()}})
        with patch(
            "seda.magalu.zenrows_client.request_json",
            return_value=response,
        ) as request_json, patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "TV",
                "SEDA_MAGALU_SEARCH_PAGE_SIZE": "60",
                "SEDA_MAGALU_SEARCH_FALLBACK_PAGE_SIZES": "",
            },
        ):
            result = search_api.fetch_search_listing_via_zenrows(
                TV_URL,
                timeout=45,
            )
            rows = parse_listing(
                result["text"],
                "Magalu",
                "https://www.magazineluiza.com.br",
                TV_URL,
                "main",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "zenrows_graphql_search")
        self.assertEqual(result["products"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item"], "path-item")
        self.assertIn("/p/path-item/", rows[0]["product_url"])
        self.assertNotEqual(rows[0]["item"], "offer-id")
        _, payload = request_json.call_args.args[:2]
        kwargs = request_json.call_args.kwargs
        self.assertEqual(payload["variables"]["pageSize"], 60)
        self.assertEqual(kwargs["profile"], "premium_html")
        self.assertEqual(kwargs["timeout"], 45)
        self.assertEqual(kwargs["extra"]["custom_headers"], "true")
        self.assertEqual(kwargs["extra"]["proxy_country"], "br")
        self.assertEqual(kwargs["extra_headers"]["content-type"], "application/json")
        self.assertEqual(result["zenrows"]["estimated_multiplier"], "10x")

    def test_ldy_plus_path_is_decoded_for_graphql_and_validation(self):
        url = (
            "https://www.magazineluiza.com.br/busca/maquina+de+lavar/"
            "?page=1&sortType=score&sortOrientation=desc"
        )
        payload = search_api._payload(url, 60)
        search = _search(term="maquina de lavar")

        self.assertEqual(payload["variables"]["term"], "maquina de lavar")
        self.assertEqual(search_api._strict_search_payload_error(url, search, 60), "")

    def test_graphql_listing_keeps_canonical_page_size_when_env_is_stale(self):
        response = _zenrows_result({"data": {"search": _search(size=60)}})
        with patch(
            "seda.magalu.zenrows_client.request_json",
            return_value=response,
        ) as request_json, patch.dict(
            os.environ,
            {
                "SEDA_MAGALU_SEARCH_PAGE_SIZE": "20",
                "SEDA_MAGALU_SEARCH_FALLBACK_PAGE_SIZES": "",
            },
        ):
            result = search_api.fetch_search_listing_via_zenrows(TV_URL, timeout=45)

        self.assertTrue(result["success"])
        _, payload = request_json.call_args.args[:2]
        self.assertEqual(payload["variables"]["pageSize"], 60)

    def test_bsr_sort_contract_is_accepted(self):
        search = _search(sort_type="soldQuantity")
        self.assertEqual(
            search_api._strict_search_payload_error(BSR_URL, search, 60),
            "",
        )

    def test_product_id_mismatch_missing_id_and_duplicate_are_allowed(self):
        first = _product(item="canonical-item", product_id="different-offer")
        second = copy.deepcopy(first)
        second.pop("id")
        search = _search(products=[first, second])

        self.assertEqual(
            search_api._strict_search_payload_error(TV_URL, search, 60),
            "",
        )

    def test_strict_validator_rejects_wrong_context_and_bad_products(self):
        missing_pagination = _search()
        missing_pagination.pop("pagination")
        missing_sorts = _search()
        missing_sorts.pop("sorts")
        missing_term = _search()
        missing_term.pop("term")
        cases = {
            "pagination": (missing_pagination, "missing_pagination"),
            "sorts": (missing_sorts, "selected_sort_missing"),
            "term_metadata": (missing_term, "search_term_missing"),
            "page": (_search(page=2), "page_mismatch"),
            "size": (_search(size=20), "page_size_mismatch"),
            "sort": (_search(sort_type="price", orientation="asc"), "sort_mismatch"),
            "term": (_search(term="geladeira"), "search_term_mismatch"),
            "empty": (_search(products=[]), "empty_products"),
            "not_object": (_search(products=["bad"]), "invalid_product:0:not_object"),
            "missing_title": (
                _search(products=[{"path": "/x/p/item/et/tv4k/"}]),
                "invalid_product:0:missing_title",
            ),
            "missing_path": (
                _search(products=[{"title": "Smart TV"}]),
                "invalid_product:0:missing_path",
            ),
            "bad_path": (
                _search(products=[{"title": "Smart TV", "path": "/busca/tv/"}]),
                "invalid_product:0:invalid_path",
            ),
        }
        for name, (search, expected) in cases.items():
            with self.subTest(case=name):
                error = search_api._strict_search_payload_error(TV_URL, search, 60)
                self.assertTrue(error.startswith(expected), error)

    def test_invalid_envelopes_return_stable_errors(self):
        cases = (
            (_zenrows_result({"errors": [{"message": "failed"}]}), "graphql_errors"),
            (_zenrows_result({"data": {"search": None}}), "invalid_search_payload"),
            (_zenrows_result({"data": {"search": {"products": "bad"}}}), "invalid_search_products"),
            (_zenrows_result({}, success=False, status_code=403, error="http_403"), "http_403"),
        )
        for response, expected in cases:
            with self.subTest(expected=expected), patch(
                "seda.magalu.zenrows_client.request_json",
                return_value=response,
            ), patch.dict(
                os.environ,
                {
                    "SEDA_MAGALU_SEARCH_PAGE_SIZE": "60",
                    "SEDA_MAGALU_SEARCH_FALLBACK_PAGE_SIZES": "",
                },
            ):
                result = search_api.fetch_search_listing_via_zenrows(TV_URL, timeout=45)

            self.assertFalse(result["success"])
            self.assertEqual(result["error"], expected)

    def test_invalid_json_is_rejected_before_listing_parse(self):
        response = zenrows_client.ZenRowsResult(
            success=True,
            url=search_api.GRAPHQL_URL,
            profile="premium_html",
            status_code=200,
            text="{invalid",
            headers={},
            estimated_multiplier="10x",
        )
        with patch(
            "seda.magalu.zenrows_client.request_json",
            return_value=response,
        ):
            result = search_api.fetch_search_listing_via_zenrows(TV_URL, timeout=45)

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "invalid_json")

    def test_graphql_flag_off_preserves_existing_html_profile_path(self):
        html_success = transport.FetchResult(
            url=TV_URL,
            text=_listing_html(_search()),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_FIRST": "0",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_graphql_once",
        ) as graphql, patch.object(
            transport,
            "_fetch_zenrows_once",
            return_value=html_success,
        ) as html:
            result = transport._fetch_zenrows(TV_URL, 45)

        self.assertIs(result, html_success)
        graphql.assert_not_called()
        html.assert_called_once_with(TV_URL, 45, "premium_html", 1, 2)

    def test_graphql_success_skips_all_existing_html_profiles(self):
        valid = transport.FetchResult(
            url=TV_URL,
            text=_listing_html(_search()),
            status_code=200,
            method="zenrows_graphql_search",
        )
        env = {
            "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_FIRST": "1",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_graphql_once",
            return_value=valid,
        ) as graphql, patch.object(transport, "_fetch_zenrows_once") as html:
            result = transport._fetch_zenrows(TV_URL, 45)

        self.assertIs(result, valid)
        graphql.assert_called_once_with(TV_URL, 45, 1, 3)
        html.assert_not_called()

    def test_graphql_failure_falls_through_without_retrying_or_disabling_html(self):
        failed = transport.FetchResult(
            url=TV_URL,
            text="",
            status_code=403,
            method="zenrows_graphql_search",
            error="http_403",
        )
        html_success = transport.FetchResult(
            url=TV_URL,
            text=_listing_html(_search()),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_FIRST": "1",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_graphql_once",
            return_value=failed,
        ) as graphql, patch.object(
            transport,
            "_fetch_zenrows_once",
            return_value=html_success,
        ) as html:
            result = transport._fetch_zenrows(TV_URL, 45)

        self.assertIs(result, html_success)
        graphql.assert_called_once_with(TV_URL, 45, 1, 3)
        html.assert_called_once_with(TV_URL, 45, "premium_html", 2, 3)
        self.assertNotIn(
            "premium_html",
            transport._MAGALU_LISTING_DISABLED_ZENROWS_PROFILES,
        )
        self.assertEqual(
            [item["method"] for item in result.attempts],
            ["zenrows_graphql_search", "zenrows"],
        )
        self.assertEqual(
            [item["request_number"] for item in result.attempts],
            [1, 2],
        )

    def test_graphql_failure_gets_one_fresh_attempt_on_later_page(self):
        first_failed = transport.FetchResult(
            url=TV_URL,
            text="",
            status_code=403,
            method="zenrows_graphql_search",
            error="http_403",
        )
        page_2 = TV_URL.replace("page=1", "page=2")
        second_failed = transport.FetchResult(
            url=page_2,
            text="",
            status_code=403,
            method="zenrows_graphql_search",
            error="http_403",
        )
        first_html = transport.FetchResult(
            url=TV_URL,
            text=_listing_html(_search()),
            status_code=200,
            method="zenrows",
        )
        second_html = transport.FetchResult(
            url=page_2,
            text=_listing_html(_search(page=2)),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_FIRST": "1",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_graphql_once",
            side_effect=[first_failed, second_failed],
        ) as graphql, patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[first_html, second_html],
        ) as html:
            first = transport._fetch_zenrows(TV_URL, 45)
            second = transport._fetch_zenrows(page_2, 45)

        self.assertIs(first, first_html)
        self.assertIs(second, second_html)
        self.assertEqual(
            graphql.call_args_list,
            [
                call(TV_URL, 45, 1, 3),
                call(page_2, 45, 1, 3),
            ],
        )
        self.assertEqual(
            html.call_args_list,
            [
                call(TV_URL, 45, "premium_html", 2, 3),
                call(page_2, 45, "premium_html", 2, 3),
            ],
        )

    def test_graphql_and_10x_fail_then_existing_25x_contract_recovers(self):
        graphql_failed = transport.FetchResult(
            url=TV_URL,
            text="",
            method="zenrows_graphql_search",
            error="invalid_search_payload",
        )
        html_invalid = transport.FetchResult(
            url=TV_URL,
            text="<html>invalid</html>" + (" " * 600),
            status_code=200,
            method="zenrows",
        )
        rendered_valid = transport.FetchResult(
            url=TV_URL,
            text=_listing_html(_search()),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_FIRST": "1",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_graphql_once",
            return_value=graphql_failed,
        ) as graphql, patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[html_invalid, rendered_valid],
        ) as html:
            result = transport._fetch_zenrows(TV_URL, 45)

        self.assertIs(result, rendered_valid)
        graphql.assert_called_once()
        self.assertEqual(
            html.call_args_list,
            [
                call(TV_URL, 45, "premium_html", 2, 3),
                call(TV_URL, 45, "listing_next_data_js_wait", 3, 3),
            ],
        )
        self.assertEqual(
            [
                (item["request_number"], item["method"], item["profile"])
                for item in result.attempts
            ],
            [
                (1, "zenrows_graphql_search", "premium_html"),
                (2, "zenrows", "premium_html"),
                (3, "zenrows", "listing_next_data_js_wait"),
            ],
        )

    def test_terminal_25x_transient_retry_has_distinct_request_numbers(self):
        graphql_failed = transport.FetchResult(
            url=TV_URL,
            text="",
            method="zenrows_graphql_search",
            error="http_403",
        )
        html_invalid = transport.FetchResult(
            url=TV_URL,
            text="<html>invalid</html>" + (" " * 600),
            status_code=200,
            method="zenrows",
        )
        first_timeout = transport.FetchResult(
            url=TV_URL,
            text="",
            method="zenrows",
            error="request_error:Timeout",
        )
        rendered_valid = transport.FetchResult(
            url=TV_URL,
            text=_listing_html(_search()),
            status_code=200,
            method="zenrows",
        )
        env = {
            "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_FIRST": "1",
            "SEDA_ZENROWS_LISTING_PROFILE": "premium_html",
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES": (
                "listing_next_data_js_wait"
            ),
            "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS": "0",
        }
        with patch.dict(os.environ, env), patch.object(
            transport,
            "_fetch_zenrows_graphql_once",
            return_value=graphql_failed,
        ), patch.object(
            transport,
            "_fetch_zenrows_once",
            side_effect=[html_invalid, first_timeout, rendered_valid],
        ):
            result = transport._fetch_zenrows(TV_URL, 45)

        self.assertIs(result, rendered_valid)
        self.assertEqual(
            [item["request_number"] for item in result.attempts],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [item["profile"] for item in result.attempts],
            [
                "premium_html",
                "premium_html",
                "listing_next_data_js_wait",
                "listing_next_data_js_wait",
            ],
        )


if __name__ == "__main__":
    unittest.main()
