"""Offline contract tests for the Casas REST API + SSR Document hybrid."""

import html
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

from seda import step01_main_list, transport
from seda.casas_bahia import browser_listing, listing_hybrid, search_api


TV_URL = "https://www.casasbahia.com.br/tv/b?page=1"
PRODUCT_URL = "https://www.casasbahia.com.br/smart-tv-samsung-43/p/201"


def _listing_html(page=1, product_id=101, sku_id=201):
    product = {
        "id": product_id,
        "idSku": sku_id,
        "sku": sku_id,
        "href": PRODUCT_URL.replace("201", str(sku_id)),
        "url": PRODUCT_URL.replace("201", str(sku_id)),
        "title": 'Smart TV Samsung 43" DU7700',
        "name": 'Smart TV Samsung 43" DU7700',
        "lojista": 10037,
        "sellerId": 10037,
        "price": {
            "currentPrice": 1500,
            "oldPrice": 1800,
            "productId": product_id,
            "skuId": sku_id,
            "sellerId": 10037,
        },
        "rating": 4.5,
        "ratingCount": 12,
    }
    data = {
        "props": {
            "pageProps": {
                "initialState": {
                    "search": {
                        "query": {"page": str(page), "strbusca": "tv"},
                        "searchTerm": "tv",
                        "results": {"products": [product]},
                    }
                }
            }
        },
        "page": "/tv/b",
    }
    return '<script id="__NEXT_DATA__" type="application/json">' + html.escape(
        json.dumps(data)
    ) + "</script>"


def _successful_result(method="api_partner", page=1):
    return {
        "success": True,
        "text": _listing_html(page=page),
        "status_code": 200,
        "method": method,
        "products": 1,
        "trace": [{"attempt": 1, "status_code": 200, "method": method}],
    }


def _blocked_result():
    return {
        "success": False,
        "text": "",
        "status_code": 403,
        "method": "api_partner",
        "error": "casas_bahia_partner_api_failed",
        "trace": [
            {"attempt": attempt, "status_code": 403, "error": "non_json_or_blocked"}
            for attempt in range(1, 4)
        ],
    }


def _leaves(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        nested = item.get("inner_attempts") or []
        if nested:
            yield from _leaves(nested)
        else:
            yield item


class _BlockedResponse:
    status_code = 403
    text = "blocked"
    headers = {"content-type": "text/html"}


class CasasSearchAttemptCapTests(unittest.TestCase):
    def _blocked_search(self, environment=None, max_attempts=None):
        session = mock.Mock()
        session.get.return_value = _BlockedResponse()
        settings = {"SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "0"}
        settings.update(environment or {})
        kwargs = {"timeout": 1}
        if max_attempts is not None:
            kwargs["max_attempts"] = max_attempts
        with mock.patch.dict(os.environ, settings, clear=True), mock.patch.object(
            search_api.requests, "Session", return_value=session
        ), mock.patch.object(search_api.time, "sleep"):
            result = search_api.fetch_search_listing(TV_URL, **kwargs)
        return result, session

    def test_default_total_attempts_is_three(self):
        result, session = self._blocked_search()
        self.assertFalse(result["success"])
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([item["attempt"] for item in result["trace"]], [1, 2, 3])

    def test_large_environment_cannot_exceed_three_attempts(self):
        result, session = self._blocked_search({"SEDA_CASAS_BAHIA_SEARCH_RETRIES": "99"})
        self.assertFalse(result["success"])
        self.assertEqual(session.get.call_count, 3)

    def test_large_explicit_limit_cannot_exceed_three_attempts(self):
        result, session = self._blocked_search(max_attempts=99)
        self.assertFalse(result["success"])
        self.assertEqual(session.get.call_count, 3)

    def test_smaller_explicit_limit_is_respected(self):
        result, session = self._blocked_search(max_attempts=2)
        self.assertFalse(result["success"])
        self.assertEqual(session.get.call_count, 2)

    def test_successful_search_stops_after_first_call(self):
        response = mock.Mock(status_code=200, text="valid")
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"products": [{"id": 101, "sku": 201}]}
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            search_api.requests, "Session", return_value=session
        ), mock.patch.object(search_api, "_attach_prices", return_value={"count": 1}):
            result = search_api.fetch_search_listing(TV_URL, timeout=1, max_attempts=3)
        self.assertTrue(result["success"])
        self.assertEqual(session.get.call_count, 1)

    def test_semantic_validation_failure_retries_before_success(self):
        response = mock.Mock(status_code=200, text="valid")
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"products": [{"id": 101, "sku": 201}]}
        session = mock.Mock()
        session.get.return_value = response
        validator = mock.Mock(side_effect=["prices_missing", ""])
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            search_api.requests, "Session", return_value=session
        ), mock.patch.object(search_api, "_attach_prices", return_value={"count": 1}), mock.patch.object(
            search_api.time, "sleep"
        ):
            result = search_api.fetch_search_listing(TV_URL, timeout=1, max_attempts=3, validator=validator)
        self.assertTrue(result["success"])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(validator.call_count, 2)

    def test_semantic_validation_failure_is_capped_at_three(self):
        response = mock.Mock(status_code=200, text="valid")
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"products": [{"id": 101, "sku": 201}]}
        session = mock.Mock()
        session.get.return_value = response
        validator = mock.Mock(return_value="prices_missing")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            search_api.requests, "Session", return_value=session
        ), mock.patch.object(search_api, "_attach_prices", return_value={"count": 0}), mock.patch.object(
            search_api.time, "sleep"
        ):
            result = search_api.fetch_search_listing(TV_URL, timeout=1, max_attempts=3, validator=validator)
        self.assertFalse(result["success"])
        self.assertEqual(session.get.call_count, 3)

    def test_real_retry_loop_runs_three_times_then_browser_once(self):
        session = mock.Mock()
        session.get.return_value = _BlockedResponse()
        with mock.patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}), mock.patch.object(
            search_api.requests, "Session", return_value=session
        ), mock.patch.object(search_api.time, "sleep"), mock.patch.object(
            browser_listing, "fetch_page", return_value=_successful_result("browser_ssr")
        ) as browser, mock.patch.object(
            transport, "_fetch_zenrows", side_effect=AssertionError("ZenRows forbidden")
        ):
            result = listing_hybrid.fetch_listing(TV_URL, timeout=1)
        self.assertTrue(result["success"])
        self.assertEqual(session.get.call_count, 3)
        browser.assert_called_once_with(TV_URL, timeout=1)


class CasasListingHybridTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}))
        self.rest = self.stack.enter_context(mock.patch.object(search_api, "fetch_search_listing"))
        self.browser = self.stack.enter_context(mock.patch.object(browser_listing, "fetch_page"))
        self.zenrows = self.stack.enter_context(
            mock.patch.object(transport, "_fetch_zenrows", side_effect=AssertionError("ZenRows forbidden"))
        )

    def tearDown(self):
        self.zenrows.assert_not_called()

    def test_valid_rest_does_not_start_browser(self):
        self.rest.return_value = _successful_result()
        result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertTrue(result["success"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["text"], _listing_html())
        self.rest.assert_called_once_with(
            TV_URL, timeout=17, max_attempts=3, validator=listing_hybrid._validation_error
        )
        self.browser.assert_not_called()

    def test_three_failed_rest_attempts_are_preserved_before_browser_success(self):
        calls = []

        def rest(*args, **kwargs):
            calls.append("rest")
            return _blocked_result()

        def browser(*args, **kwargs):
            calls.append("browser")
            return _successful_result("browser_ssr")

        self.rest.side_effect = rest
        self.browser.side_effect = browser
        result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertTrue(result["success"])
        self.assertEqual(calls, ["rest", "browser"])
        self.browser.assert_called_once_with(TV_URL, timeout=17)
        statuses = [item.get("status_code") for item in _leaves(result["trace"])]
        self.assertEqual(statuses.count(403), 3)
        self.assertEqual(statuses[-1], 200)

    def test_success_flag_without_valid_listing_triggers_fallback(self):
        self.rest.return_value = {**_successful_result(), "text": "<html>invalid payload</html>"}
        self.browser.return_value = _successful_result("browser_ssr")
        result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertTrue(result["success"])
        self.browser.assert_called_once()

    def test_failed_rest_and_browser_do_not_return_a_success_body(self):
        self.rest.return_value = _blocked_result()
        self.browser.return_value = {
            "success": False,
            "text": "<html>blocked</html>",
            "status_code": 403,
            "method": "browser_ssr",
            "error": "document_blocked",
            "trace": [{"status_code": 403, "error": "document_blocked"}],
        }
        result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertFalse(result["success"])
        self.assertFalse(result.get("text"))
        self.assertTrue(result.get("error"))
        self.assertEqual(len(list(_leaves(result["trace"]))), 4)

    def test_browser_success_flag_with_invalid_payload_is_rejected(self):
        self.rest.return_value = _blocked_result()
        self.browser.return_value = {**_successful_result("browser_ssr"), "text": ""}
        result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertFalse(result["success"])
        self.assertFalse(result.get("text"))

    def test_exception_messages_are_not_exposed(self):
        forbidden_message = "untrusted request metadata must not appear"
        self.rest.side_effect = RuntimeError(forbidden_message)
        self.browser.side_effect = RuntimeError(forbidden_message)
        output = io.StringIO()
        with redirect_stdout(output):
            result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertFalse(result["success"])
        self.assertNotIn(forbidden_message, json.dumps(result))
        self.assertNotIn(forbidden_message, output.getvalue())

    def test_unrelated_url_never_opens_browser(self):
        self.rest.return_value = _blocked_result()
        result = listing_hybrid.fetch_listing("https://example.com/tv/b", timeout=17)
        self.assertFalse(result["success"])
        self.browser.assert_not_called()

    def test_missing_price_triggers_browser_fallback(self):
        raw = _listing_html().replace("&quot;currentPrice&quot;: 1500", "&quot;currentPrice&quot;: null")
        self.rest.return_value = {**_successful_result(), "text": raw}
        self.browser.return_value = _successful_result("browser_ssr")
        result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertTrue(result["success"])
        self.browser.assert_called_once()

    def test_price_identity_mismatch_triggers_browser_fallback(self):
        raw = _listing_html().replace("&quot;skuId&quot;: 201", "&quot;skuId&quot;: 999")
        self.rest.return_value = {**_successful_result(), "text": raw}
        self.browser.return_value = _successful_result("browser_ssr")
        result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertTrue(result["success"])
        self.browser.assert_called_once()

    def test_explicit_wrong_page_triggers_browser_fallback(self):
        self.rest.return_value = _successful_result(page=2)
        self.browser.return_value = _successful_result("browser_ssr")
        result = listing_hybrid.fetch_listing(TV_URL, timeout=17)
        self.assertTrue(result["success"])
        self.browser.assert_called_once()


class CasasHybridTransportTests(unittest.TestCase):
    def test_listing_only_mode_uses_hybrid_without_other_transports(self):
        with mock.patch.object(listing_hybrid, "fetch_listing", return_value=_successful_result()) as hybrid, mock.patch.object(
            transport, "_fetch_graphql", side_effect=AssertionError("global route must not run")
        ), mock.patch.object(transport, "_fetch_zenrows", side_effect=AssertionError("ZenRows forbidden")), mock.patch.object(
            transport, "_fetch_uc", side_effect=AssertionError("generic UC forbidden")
        ), mock.patch.object(transport, "_fetch_requests", side_effect=AssertionError("generic HTTP forbidden")):
            result = transport.fetch_url(TV_URL, mode="casas_listing_hybrid", timeout=17)
        hybrid.assert_called_once_with(TV_URL, timeout=17)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.method, "api_partner")
        self.assertFalse(result.error)

    def test_graphql_mode_is_not_changed_to_hybrid(self):
        expected = transport.FetchResult(
            url=TV_URL, text=_listing_html(), status_code=200, method="api_partner"
        )
        with mock.patch.object(listing_hybrid, "fetch_listing", side_effect=AssertionError("hybrid forbidden")), mock.patch.object(
            transport, "_fetch_graphql", return_value=expected
        ) as legacy:
            result = transport.fetch_url(TV_URL, mode="graphql", timeout=17)
        legacy.assert_called_once_with(TV_URL, 17)
        self.assertEqual(result.method, "api_partner")

    def test_failed_hybrid_does_not_add_unrequested_transports(self):
        failed = {**_blocked_result(), "method": "rest_ssr_hybrid"}
        with mock.patch.dict(os.environ, {"SEDA_RETRY_SLEEP_SECONDS": "0", "SEDA_ALLOW_ZENROWS": "1"}), mock.patch.object(
            listing_hybrid, "fetch_listing", return_value=failed
        ) as hybrid, mock.patch.object(transport, "_fetch_zenrows", side_effect=AssertionError("ZenRows forbidden")), mock.patch.object(
            transport, "_fetch_graphql", side_effect=AssertionError("extra REST round forbidden")
        ), mock.patch.object(transport, "_fetch_uc", side_effect=AssertionError("extra Chrome forbidden")), mock.patch.object(
            transport, "_fetch_requests", side_effect=AssertionError("extra HTTP forbidden")
        ):
            result = transport.fetch_url(TV_URL, mode="casas_listing_hybrid", timeout=17)
        hybrid.assert_called_once()
        self.assertTrue(result.error)


class CasasHybridListingStepTests(unittest.TestCase):
    def _environment(self, root):
        return {
            "SEDA_RETAILERS": "casas_bahia",
            "SEDA_ACTIVE_RETAILER": "casas_bahia",
            "SEDA_PRODUCT_LINE": "TV",
            "SEDA_RUN_ROOT": str(root),
            "SEDA_RUN_ID": "main",
            "SEDA_REUSE_RAW": "0",
            "SEDA_ALLOW_EMPTY_LISTING": "0",
            "SEDA_FETCH_MODE": "graphql",
            "SEDA_CASAS_BAHIA_LISTING_MODE": "2",
            "SEDA_MAIN_UNIQUE_TARGET": "0",
        }

    def test_main_listing_uses_hybrid_without_mutating_global_mode(self):
        calls = []

        def fetch(url, **kwargs):
            calls.append((url, kwargs))
            return transport.FetchResult(url=url, text=_listing_html(), status_code=200, method="api_partner")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, self._environment(root)), mock.patch.object(
                step01_main_list, "page_numbers", return_value=[1]
            ), mock.patch.object(step01_main_list, "fetch_url", side_effect=fetch), redirect_stdout(io.StringIO()):
                step01_main_list.main()
                self.assertEqual(os.environ["SEDA_FETCH_MODE"], "graphql")
            manifest = json.loads((root / "main" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].get("mode"), "casas_listing_hybrid")
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["rows"], 1)

    def test_partial_casas_listing_is_not_published_as_complete(self):
        calls = []

        def fetch(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return transport.FetchResult(url=url, text=_listing_html(), status_code=200, method="api_partner")
            return transport.FetchResult(
                url=url,
                text="",
                status_code=403,
                method="rest_ssr_hybrid",
                error="casas_bahia_listing_hybrid_failed",
                attempts=[{"method": "api_partner", "status_code": 403}],
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final_output = root / "main" / "parsed" / "main_occurrences.csv"
            final_output.parent.mkdir(parents=True)
            final_output.write_text("stale_complete_output", encoding="utf-8")
            with mock.patch.dict(os.environ, self._environment(root)), mock.patch.object(
                step01_main_list, "page_numbers", return_value=[1, 2]
            ), mock.patch.object(step01_main_list, "fetch_url", side_effect=fetch), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    step01_main_list.main()
            manifest = json.loads((root / "main" / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(final_output.exists())
            self.assertTrue((final_output.parent / "main_occurrences.partial.csv").exists())
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["rows"], 1)
        self.assertEqual([failure["page"] for failure in manifest["failures"]], [2])


class CasasSortValidationTests(unittest.TestCase):
    def test_explicit_wrong_sort_is_not_accepted(self):
        from seda.parsers import extract_next_data
        payload = extract_next_data(_listing_html())
        payload["props"]["pageProps"]["initialState"]["search"]["query"]["sortby"] = "relevancia"
        text = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>'
        self.assertEqual(listing_hybrid._validation_error(text, TV_URL + "&ordenacao=mais-vendidos"), "requested_sort_mismatch")

    def test_equivalent_sort_spelling_is_accepted(self):
        from seda.parsers import extract_next_data
        payload = extract_next_data(_listing_html())
        payload["props"]["pageProps"]["initialState"]["search"]["query"]["sortby"] = "maisvendidos"
        text = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>'
        self.assertEqual(listing_hybrid._validation_error(text, TV_URL + "&ordenacao=mais-vendidos"), "")


if __name__ == "__main__":
    unittest.main()
