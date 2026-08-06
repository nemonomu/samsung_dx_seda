import json
import unittest
from unittest.mock import Mock, call, patch

from seda.magalu import browser_session


SEARCH_URL = (
    "https://www.magazineluiza.com.br/busca/tv/"
    "?page=1&sortType=score&sortOrientation=desc"
)
LOGIN_URL = (
    "https://sacola.magazineluiza.com.br/?fr=1"
    "#/cliente/login/?next=https%3A%2F%2Fwww.magazineluiza.com.br%2Fbusca%2Ftv%2F"
)


def _fake_page(url):
    page = Mock()
    page.url = url
    page.html = ""
    page.run_cdp.return_value = {}
    return page


def _login_snapshot():
    return {
        "url": LOGIN_URL,
        "title": "Sacola de compras - Magazine Luiza",
        "ready_state": "complete",
        "html": "",
        "browser_text": "Ocorreu um erro ao recuperar a sacola.",
        "next_data_length": 0,
        "source": "script_text_js_missing",
        "cdp_source": "script_text_cdp",
        "cdp_success": 0,
        "cdp_empty": 1,
        "cdp_error": "",
        "fallback_used": 1,
        "fallback_source": "script_text_js",
        "fallback_success": 0,
        "fallback_empty": 1,
        "fallback_error": "",
        "error": "",
    }


def _valid_snapshot():
    snapshot = _login_snapshot()
    snapshot.update(
        {
            "url": SEARCH_URL,
            "title": "TV | Magazine Luiza",
            "html": _valid_search_html(),
            "browser_text": "",
            "next_data_length": len(_valid_search_html()),
            "source": "script_text_cdp",
            "cdp_success": 1,
            "cdp_empty": 0,
            "fallback_used": 0,
            "fallback_success": 0,
        }
    )
    return snapshot


def _valid_search_html():
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "search": {
                        "products": [{"id": "240144500"}],
                        "pagination": {"page": 1, "size": 1},
                        "sorts": [
                            {
                                "selected": True,
                                "type": "score",
                                "orientation": "desc",
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
    )


class MagaluSearchNavigationTests(unittest.TestCase):
    def setUp(self):
        browser_session._reset_search_browser_circuit()

    def tearDown(self):
        browser_session._reset_search_browser_circuit()

    def test_login_redirect_detector_is_narrow(self):
        self.assertTrue(browser_session._is_magalu_search_login_redirect(LOGIN_URL))
        self.assertFalse(browser_session._is_magalu_search_login_redirect(SEARCH_URL))
        self.assertFalse(
            browser_session._is_magalu_search_login_redirect(
                "https://sacola.magazineluiza.com.br/carrinho/"
            )
        )
        self.assertFalse(
            browser_session._is_magalu_search_login_redirect(
                "https://sacola.magazineluiza.com.br.evil.example/#/cliente/login/"
            )
        )
        self.assertFalse(
            browser_session._is_magalu_search_login_redirect(
                SEARCH_URL,
                "Ocorreu um erro ao recuperar a sacola.",
            )
        )

    def test_payload_state_classifies_login_before_missing_next_data(self):
        state = browser_session._magalu_search_payload_state(
            SEARCH_URL,
            LOGIN_URL,
            "",
            browser_text="Ocorreu um erro ao recuperar a sacola.",
        )
        self.assertFalse(state["valid"])
        self.assertTrue(state["terminal_redirect"])
        self.assertEqual(state["error"], "browser_html_search_login_redirect")

    def test_wait_stops_after_first_login_snapshot(self):
        page = Mock()
        with patch(
            "seda.magalu.browser_session._read_search_next_data_snapshot",
            return_value=_login_snapshot(),
        ) as read_snapshot, patch("seda.magalu.browser_session.time.sleep") as sleep:
            result = browser_session._wait_for_magalu_search_payload(
                page, SEARCH_URL, timeout_seconds=30, poll_seconds=0.25
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "browser_html_search_login_redirect")
        self.assertTrue(result["state"]["terminal_redirect"])
        read_snapshot.assert_called_once_with(page)
        sleep.assert_not_called()

    def test_wait_uses_page_url_when_context_loss_snapshot_has_no_url(self):
        page = _fake_page(LOGIN_URL)
        snapshot = _login_snapshot()
        snapshot.update(
            {
                "url": "",
                "browser_text": "",
                "error": "ContextLostError: execution context was destroyed",
            }
        )
        with patch(
            "seda.magalu.browser_session._read_search_next_data_snapshot",
            return_value=snapshot,
        ) as read_snapshot, patch("seda.magalu.browser_session.time.sleep") as sleep:
            result = browser_session._wait_for_magalu_search_payload(
                page, SEARCH_URL, timeout_seconds=30, poll_seconds=0.25
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "browser_html_search_login_redirect")
        self.assertEqual(result["state"]["url"], LOGIN_URL)
        self.assertTrue(result["state"]["terminal_redirect"])
        read_snapshot.assert_called_once_with(page)
        sleep.assert_not_called()

    def test_persistent_login_redirect_gets_one_original_url_retry(self):
        page = _fake_page(LOGIN_URL)
        with patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._read_search_next_data_snapshot",
            return_value=_login_snapshot(),
        ) as read_snapshot, patch(
            "seda.magalu.browser_session._restart_page"
        ) as restart:
            result = browser_session._fetch_search_page_html(
                SEARCH_URL,
                wait_seconds=0,
                attempts=3,
                recycle_attempts=1,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "browser_html_search_login_redirect")
        self.assertEqual(len(result["trace"]), 2)
        self.assertTrue(all(item["terminal_redirect"] for item in result["trace"]))
        self.assertEqual(read_snapshot.call_count, 2)
        self.assertEqual(
            page.run_cdp.call_args_list,
            [
                call("Page.navigate", url=SEARCH_URL),
                call("Page.navigate", url=SEARCH_URL),
            ],
        )
        restart.assert_not_called()
        self.assertEqual(
            browser_session._SEARCH_BROWSER_SKIP_REASON,
            "browser_html_search_login_redirect",
        )

    def test_persistent_login_redirect_skips_later_listing_browser_fetch(self):
        page = _fake_page(LOGIN_URL)
        with patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ) as page_for_use, patch(
            "seda.magalu.browser_session._read_search_next_data_snapshot",
            return_value=_login_snapshot(),
        ):
            first = browser_session._fetch_search_page_html(
                SEARCH_URL,
                wait_seconds=0,
                attempts=3,
                recycle_attempts=1,
            )
            calls_after_first = page_for_use.call_count
            second = browser_session.fetch_page_html(
                SEARCH_URL.replace("page=1", "page=2"),
                wait_seconds=0,
                attempts=3,
            )

        self.assertFalse(first["success"])
        self.assertFalse(second["success"])
        self.assertEqual(
            second["error"],
            "browser_html_search_login_redirect_circuit_open",
        )
        self.assertEqual(second["trace"][0]["method"], "browser_skip")
        self.assertEqual(page_for_use.call_count, calls_after_first)

    def test_login_redirect_original_url_retry_can_recover(self):
        page = _fake_page(LOGIN_URL)
        with patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ), patch(
            "seda.magalu.browser_session._read_search_next_data_snapshot",
            side_effect=[_login_snapshot(), _valid_snapshot()],
        ) as read_snapshot, patch(
            "seda.magalu.browser_session._restart_page"
        ) as restart:
            result = browser_session._fetch_search_page_html(
                SEARCH_URL,
                wait_seconds=0,
                attempts=3,
                recycle_attempts=1,
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["trace"]), 2)
        self.assertTrue(result["trace"][0]["terminal_redirect"])
        self.assertFalse(result["trace"][1]["terminal_redirect"])
        self.assertEqual(read_snapshot.call_count, 2)
        self.assertEqual(
            page.run_cdp.call_args_list,
            [
                call("Page.navigate", url=SEARCH_URL),
                call("Page.navigate", url=SEARCH_URL),
            ],
        )
        restart.assert_not_called()
        self.assertEqual(browser_session._SEARCH_BROWSER_SKIP_REASON, "")

    def test_search_circuit_does_not_skip_nonvalidated_fetch(self):
        browser_session._SEARCH_BROWSER_SKIP_REASON = (
            "browser_html_search_login_redirect"
        )
        page = _fake_page(SEARCH_URL)
        page.html = "x" * 100001
        with patch(
            "seda.magalu.browser_session._page_for_use",
            return_value=page,
        ) as page_for_use, patch(
            "seda.magalu.browser_session._stop_loading"
        ), patch(
            "seda.magalu.browser_session.time.sleep"
        ):
            result = browser_session.fetch_page_html(
                SEARCH_URL,
                wait_seconds=0,
                attempts=1,
                validate_search_payload=False,
            )

        self.assertTrue(result["success"])
        page_for_use.assert_called_once()

    def test_refresh_on_login_navigates_original_search_url(self):
        page = _fake_page(LOGIN_URL)
        method, error = browser_session._trigger_search_navigation(
            page, SEARCH_URL, refresh=True
        )
        self.assertEqual((method, error), ("cdp_navigate", ""))
        page.run_cdp.assert_called_once_with("Page.navigate", url=SEARCH_URL)

    def test_refresh_on_same_search_path_preserves_reload(self):
        page = _fake_page(SEARCH_URL)
        method, error = browser_session._trigger_search_navigation(
            page, SEARCH_URL, refresh=True
        )
        self.assertEqual((method, error), ("cdp_reload", ""))
        page.run_cdp.assert_called_once_with("Page.reload", ignoreCache=True)

    def test_refresh_on_other_path_navigates_original_search_url(self):
        page = _fake_page("https://www.magazineluiza.com.br/busca/geladeira/")
        method, error = browser_session._trigger_search_navigation(
            page, SEARCH_URL, refresh=True
        )
        self.assertEqual((method, error), ("cdp_navigate", ""))
        page.run_cdp.assert_called_once_with("Page.navigate", url=SEARCH_URL)

    def test_refresh_on_wrong_search_page_navigates_original_search_url(self):
        page = _fake_page(
            "https://www.magazineluiza.com.br/busca/tv/"
            "?page=2&sortType=score&sortOrientation=desc"
        )
        method, error = browser_session._trigger_search_navigation(
            page, SEARCH_URL, refresh=True
        )
        self.assertEqual((method, error), ("cdp_navigate", ""))
        page.run_cdp.assert_called_once_with("Page.navigate", url=SEARCH_URL)

    def test_refresh_on_wrong_search_sort_navigates_original_search_url(self):
        page = _fake_page(
            "https://www.magazineluiza.com.br/busca/tv/"
            "?page=1&sortType=price&sortOrientation=asc"
        )
        method, error = browser_session._trigger_search_navigation(
            page, SEARCH_URL, refresh=True
        )
        self.assertEqual((method, error), ("cdp_navigate", ""))
        page.run_cdp.assert_called_once_with("Page.navigate", url=SEARCH_URL)

    def test_non_refresh_keeps_navigate_contract(self):
        page = _fake_page(SEARCH_URL)
        method, error = browser_session._trigger_search_navigation(
            page, SEARCH_URL, refresh=False
        )
        self.assertEqual((method, error), ("cdp_navigate", ""))
        page.run_cdp.assert_called_once_with("Page.navigate", url=SEARCH_URL)

    def test_reload_error_falls_back_to_navigate(self):
        page = _fake_page(SEARCH_URL)
        page.run_cdp.side_effect = [RuntimeError("reload failed"), {}]
        method, error = browser_session._trigger_search_navigation(
            page, SEARCH_URL, refresh=True
        )
        self.assertEqual(method, "cdp_navigate")
        self.assertIn("RuntimeError: reload failed", error)
        self.assertEqual(
            page.run_cdp.call_args_list,
            [
                call("Page.reload", ignoreCache=True),
                call("Page.navigate", url=SEARCH_URL),
            ],
        )

    def test_normal_valid_search_payload_contract_is_unchanged(self):
        state = browser_session._magalu_search_payload_state(
            SEARCH_URL, SEARCH_URL, _valid_search_html()
        )
        self.assertTrue(state["valid"])
        self.assertFalse(state["terminal_redirect"])
        self.assertEqual(state["products"], 1)
        self.assertEqual(state["pagination_page"], 1)


if __name__ == "__main__":
    unittest.main()
