"""Offline listing-only browser recovery; no live Chrome, network, or waits."""
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_DISABLED_ENV = Path(__file__).with_name("LISTING_RESTART_NO_ENV_FILE")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ.setdefault("SEDA_ENV_PATH", str(_DISABLED_ENV))

from seda.casas_bahia import browser_api as api, browser_api_document as document
from seda.casas_bahia.browser_probe_session import ProbeBrowserSession


URL = "https://www.casasbahia.com.br/tv/b?page=1"
FRAME = {"id": "top", "loaderId": "new"}


def reply(status=200):
    return {"success": status == 200, "status_code": status,
            "error": "" if status == 200 else "document_not_200",
            "trace": [{"status_code": status}], "document_frame": FRAME}


def session_with_browsers():
    browsers = [Mock(spec=ProbeBrowserSession) for _ in range(3)]
    for browser in browsers:
        browser.driver = Mock()
        browser.major = 158
        browser.driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": FRAME}}
        browser.prepare_api.side_effect = lambda url, timeout=None, restart_browser=None, owned=browser: document.prepare(
            owned, url, restart_browser=restart_browser)
    return browsers


@pytest.mark.parametrize("fail_first_recovery", [False, True])
def test_listing_restarts_owned_browser_and_revalidates_homepage(fail_first_recovery):
    browsers = session_with_browsers()
    responses = [reply(), reply(403), reply(403), reply(), reply(403 if fail_first_recovery else 200)]
    if fail_first_recovery:
        responses += [reply(), reply()]
    with patch.object(api, "ProbeBrowserSession", side_effect=browsers) as factory, \
            patch.object(document, "_navigate", side_effect=responses) as navigate, \
            patch.object(document.time, "sleep") as sleep:
        session = api._APISession(listing_recovery=True)
        result = session._bootstrap(URL, 45)
        count = 3 if fail_first_recovery else 2
        assert factory.call_count == count
        assert session.browser is browsers[count - 1]
        assert session.document_frame == FRAME
        assert result["document_navigations"] == (7 if fail_first_recovery else 5)
        assert [c.args[1] for c in navigate.call_args_list] == [document.HOME_URL, URL, URL] + [document.HOME_URL, URL] * (count - 1)
        assert [c.args[0] for c in navigate.call_args_list] == [browsers[0].driver] * 3 + [b.driver for b in browsers[1:count] for _ in range(2)]
        waits = [c.args[0] for c in sleep.call_args_list]
        assert waits == [5, 3] + [30] * 10 + [5] + ([30] * 20 + [5] if fail_first_recovery else [])
        for browser in browsers[:count - 1]:
            browser.close.assert_called_once()
        browsers[count - 1].close.assert_not_called()
        session._bootstrap(URL, 45)  # BSR/next page reuses the validated browser.
        assert navigate.call_count == len(responses)
        session.close()
        browsers[count - 1].close.assert_called_once()


def test_terminal_failure_is_cached_without_new_waits_or_api_requests():
    browsers = session_with_browsers()
    responses = [reply(), reply(403), reply(403), reply(), reply(403), reply(), reply(403)]
    with patch.object(api, "ProbeBrowserSession", side_effect=browsers) as factory, \
            patch.object(document, "_navigate", side_effect=responses) as navigate, \
            patch.object(document.time, "sleep") as sleep, \
            patch.object(api, "_browser_fetch") as fetch:
        session = api._APISession(listing_recovery=True)
        for page in (1, 2, 20):
            with pytest.raises(api._BootstrapError) as error:
                session.fetch(URL.replace("page=1", f"page={page}"))
            assert error.value.status_code == 403
        assert factory.call_count == 3 and navigate.call_count == 7
        assert sum(c.args[0] for c in sleep.call_args_list) == 918
        fetch.assert_not_called()


@pytest.mark.parametrize("failure", ["homepage", "listing_500", "session", "startup"])
def test_failed_first_fresh_attempt_still_gets_final_fresh_attempt(failure):
    browsers = session_with_browsers()
    responses = [reply(), reply(403), reply(403)]
    if failure == "homepage":
        responses += [reply(403)]
    elif failure == "listing_500":
        responses += [reply(), reply(500)]
    elif failure == "session":
        responses += [RuntimeError("private exception body")]
    else:
        browsers[1].start.side_effect = RuntimeError("private exception body")
    responses += [reply(), reply()]
    with patch.object(api, "ProbeBrowserSession", side_effect=browsers), \
            patch.object(document, "_navigate", side_effect=responses), \
            patch.object(document.time, "sleep"), patch("builtins.print") as output:
        session = api._APISession(listing_recovery=True)
        assert session._bootstrap(URL, 45)["status_code"] == 200
        assert session.browser is browsers[2]
        browsers[1].close.assert_called_once()
        assert "private exception body" not in str(output.call_args_list)


@pytest.mark.parametrize("responses", [[reply(403)], [reply(), reply(500)]])
def test_initial_homepage_failure_or_non403_does_not_restart(responses):
    browsers = session_with_browsers()
    with patch.object(api, "ProbeBrowserSession", side_effect=browsers) as factory, \
            patch.object(document, "_navigate", side_effect=responses), \
            patch.object(document.time, "sleep") as sleep:
        session = api._APISession(listing_recovery=True)
        with pytest.raises(api._BootstrapError):
            session._bootstrap(URL, 45)
        assert factory.call_count == 1
        assert all(c.args[0] == 5 for c in sleep.call_args_list)


def test_user_cancellation_during_wait_is_not_retried():
    browsers = session_with_browsers()
    with patch.object(api, "ProbeBrowserSession", side_effect=browsers) as factory, \
            patch.object(document, "_navigate", side_effect=[reply(), reply(403), reply(403)]), \
            patch.object(document.time, "sleep", side_effect=[None, None, KeyboardInterrupt]):
        session = api._APISession(listing_recovery=True)
        with pytest.raises(KeyboardInterrupt):
            session._bootstrap(URL, 45)
        assert factory.call_count == 1


def test_only_listing_factory_opts_in_and_default_keeps_detail_policy():
    with patch.object(api, "_SESSION", None), patch.object(api, "_APISession") as factory:
        api.fetch_listing(URL)
        factory.assert_called_once_with(listing_recovery=True)
    browser = Mock()
    browser.prepare_api.return_value = reply()
    browser.driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": FRAME}}
    with patch.object(api, "ProbeBrowserSession", return_value=browser):
        session = api._APISession()
        session._bootstrap(URL, 45)
    browser.prepare_api.assert_called_once_with(URL, timeout=45)
