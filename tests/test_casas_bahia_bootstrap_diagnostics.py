"""Offline-only diagnostics regression tests; no Chrome or network is used.

The test runner must select a verified nonexistent SEDA_ENV_PATH before importing
this module, and may preload staged browser modules under their production names.
"""

from copy import deepcopy
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from seda.casas_bahia import browser_api as api
from seda.casas_bahia import browser_listing as browser
from test_casas_bahia_browser_listing import FRAME, URL, document, event, network_events, offer


class DocumentDiagnosticTests(unittest.TestCase):
    def diagnostics(self, messages):
        documents, prices, completed = browser._network_evidence(
            messages, FRAME, browser._request_identity(URL))
        return browser._document_diagnostics(messages, documents, prices, completed)

    def test_completed_current_document_does_not_inherit_other_failures(self):
        messages = network_events() + [
            event("loadingFailed", "old-document", errorText="net::ERR_ABORTED", canceled=True),
            event("loadingFailed", "price", errorText="net::ERR_TIMED_OUT", canceled=True),
        ]
        source = deepcopy(messages)
        result = self.diagnostics(messages)
        self.assertTrue(result["selected_document_response_seen"])
        self.assertTrue(result["selected_document_loading_finished"])
        self.assertFalse(result["selected_document_loading_failed"])
        self.assertFalse(result["selected_document_canceled"])
        self.assertEqual("none", result["document_network_error"])
        self.assertEqual(1, result["current_price_response_count"])
        self.assertEqual(source, messages)

    def test_missing_completion_preserves_unconfirmed_state(self):
        messages = [item for item in network_events()
                    if not (item["method"] == "Network.loadingFinished"
                            and item["params"]["requestId"] == "doc")]
        result = self.diagnostics(messages)
        self.assertTrue(result["selected_document_response_seen"])
        self.assertFalse(result["selected_document_loading_finished"])
        self.assertFalse(result["selected_document_loading_failed"])
        self.assertEqual("none", result["document_network_error"])

    def test_matching_failure_and_cancellation_are_reported(self):
        messages = [network_events()[0], event(
            "loadingFailed", "doc", errorText="net::ERR_ABORTED", canceled=True)]
        result = self.diagnostics(messages)
        self.assertTrue(result["selected_document_loading_failed"])
        self.assertTrue(result["selected_document_canceled"])
        self.assertFalse(result["selected_document_loading_finished"])
        self.assertEqual("net::ERR_ABORTED", result["document_network_error"])

    def test_unknown_network_error_is_enum_not_raw_text(self):
        for raw in ("private-test-marker", {"raw": "private-test-marker"}, None):
            with self.subTest(raw=raw):
                messages = [network_events()[0], event(
                    "loadingFailed", "doc", errorText=raw, canceled=False)]
                result = self.diagnostics(messages)
                self.assertEqual("other", result["document_network_error"])
                self.assertNotIn("private-test-marker", json.dumps(result))

    def test_wrong_frame_loader_and_page_documents_do_not_qualify(self):
        for replacement in (dict(frameId="other-frame"), dict(loaderId="old"),
                            dict(response={"url": URL.replace("page=1", "page=2"), "status": 200})):
            with self.subTest(replacement=replacement):
                response = deepcopy(network_events()[0])
                response["params"].update(replacement)
                result = self.diagnostics([response, event("loadingFinished", "doc"),
                                           event("loadingFailed", "doc", errorText="net::ERR_ABORTED", canceled=True)])
                self.assertFalse(result["selected_document_response_seen"])
                self.assertFalse(result["selected_document_loading_finished"])
                self.assertFalse(result["selected_document_loading_failed"])
                self.assertFalse(result["selected_document_canceled"])
                self.assertEqual("none", result["document_network_error"])

    def test_last_selected_document_is_the_only_failure_target(self):
        messages = [network_events()[0], event("loadingFailed", "doc", errorText="net::ERR_ABORTED", canceled=True),
                    event("responseReceived", "new-doc", type="Document", response={"url": URL, "status": 200}),
                    event("loadingFinished", "new-doc")]
        result = self.diagnostics(messages)
        self.assertEqual(2, result["selected_document_response_count"])
        self.assertTrue(result["selected_document_loading_finished"])
        self.assertFalse(result["selected_document_loading_failed"])

    def test_only_fixed_fields_leave_document_diagnostics(self):
        response = deepcopy(network_events()[0])
        response["params"]["response"].update({"fromDiskCache": True, "fromServiceWorker": True,
                                               "headers": {"unrelated": "private-test-marker"},
                                               "body": "private-test-marker"})
        result = self.diagnostics([response])
        self.assertTrue(result["selected_document_cached"])
        self.assertTrue(result["selected_document_service_worker"])
        self.assertNotIn("private-test-marker", json.dumps(result))
        self.assertNotIn(URL, json.dumps(result))
        self.assertNotIn("requestId", result)


class PublicDiagnosticTests(unittest.TestCase):
    def test_allowlist_copies_values_without_source_mutation(self):
        source = {"navigation_seconds": 45.25, "navigation_timeout": True, "ready_state": "interactive",
                  "document_network_error": "net::ERR_TIMED_OUT", "unknown": "private-test-marker",
                  "events": [{"unrelated": "private-test-marker"}]}
        original = deepcopy(source)
        result = browser._public_browser_diagnostics(source)
        self.assertEqual({key: source[key] for key in (
            "navigation_seconds", "navigation_timeout", "ready_state", "document_network_error")}, result)
        self.assertIsNot(result, source)
        self.assertEqual(original, source)

    def test_numbers_require_finite_nonnegative_numeric_values_not_bools(self):
        for bad in (-1, float("nan"), float("inf"), -float("inf"), 10 ** 1000, True, False, "45", [], {}):
            with self.subTest(bad=bad):
                self.assertEqual({}, browser._public_browser_diagnostics({"navigation_seconds": bad}))
        for value in (0, 1, 1.25):
            self.assertEqual({"navigation_seconds": value},
                             browser._public_browser_diagnostics({"navigation_seconds": value}))

    def test_flags_and_enums_are_strict(self):
        for bad in (1, 0, "true", "private-test-marker", None, [], {}):
            with self.subTest(bad=bad):
                self.assertEqual({}, browser._public_browser_diagnostics({"navigation_timeout": bad}))
        for bad in ("private-test-marker", None, 1, [], {}):
            with self.subTest(bad=bad):
                self.assertEqual({}, browser._public_browser_diagnostics({
                    "ready_state": bad, "document_network_error": bad}))

    def test_non_mapping_is_empty(self):
        for bad in (None, [], "private-test-marker", 1):
            self.assertEqual({}, browser._public_browser_diagnostics(bad))

    def test_logger_emits_only_allowlisted_diagnostics(self):
        stream = io.StringIO()
        with patch("sys.stdout", stream):
            browser._log_browser_diagnostics("failure", 1, {
                "ready_state": "complete", "raw": "private-test-marker"})
        self.assertIn('"ready_state": "complete"', stream.getvalue())
        self.assertNotIn("private-test-marker", stream.getvalue())

    def test_logger_output_failure_is_nonfatal(self):
        with patch("builtins.print", side_effect=OSError("private-test-marker")):
            browser._log_browser_diagnostics("failure", 1, {"ready_state": "complete"})


class ReadyStateTests(unittest.TestCase):
    def test_safe_enum_and_single_readonly_probe(self):
        for state in ("loading", "interactive", "complete"):
            with self.subTest(state=state):
                driver = Mock()
                driver.execute_cdp_cmd.return_value = {"result": {"value": state}}
                self.assertEqual(state, browser._diagnostic_ready_state(driver))
                driver.execute_cdp_cmd.assert_called_once_with("Runtime.evaluate", {
                    "expression": "document.readyState", "returnByValue": True, "silent": True,
                    "throwOnSideEffect": True, "timeout": 1000,
                })
                driver.get.assert_not_called()
                driver.get_log.assert_not_called()

    def test_malformed_or_unknown_state_is_unavailable(self):
        for response in (None, {}, {"result": None}, {"result": {}},
                         {"result": {"value": "private-test-marker"}}, {"result": {"value": []}}):
            with self.subTest(response=response):
                driver = Mock()
                driver.execute_cdp_cmd.return_value = response
                self.assertEqual("unavailable", browser._diagnostic_ready_state(driver))

    def test_probe_exception_is_unavailable_without_exception_text(self):
        driver = Mock()
        driver.execute_cdp_cmd.side_effect = RuntimeError("private-test-marker")
        self.assertEqual("unavailable", browser._diagnostic_ready_state(driver))


class FetchDiagnosticTests(unittest.TestCase):
    def make_session(self, messages, state="complete", probe_error=None):
        session = browser._BrowserSession()
        session.major = 153
        driver = Mock()
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": item})} for item in messages]]
        frames = iter([dict(FRAME, loaderId="prior"), FRAME])

        def cdp(command, params):
            if command == "Page.getFrameTree":
                return {"frameTree": {"frame": next(frames)}}
            if command == "Network.getResponseBody":
                return {"body": document() if params["requestId"] == "doc" else json.dumps({"Ofertas": [offer()]})}
            if command == "Runtime.evaluate":
                if probe_error:
                    raise probe_error
                return {"result": {"value": state}}
            raise AssertionError(command)

        driver.execute_cdp_cmd.side_effect = cdp
        session.driver = driver
        return session, driver

    def missing_completion(self):
        return [item for item in network_events()
                if not (item["method"] == "Network.loadingFinished" and item["params"]["requestId"] == "doc")]

    def test_missing_completion_outcome_and_existing_limits_remain_unchanged(self):
        session, driver = self.make_session(self.missing_completion())
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS": "30"}), \
                patch.object(browser.time, "monotonic", side_effect=[0, 31]) as monotonic, \
                patch.object(browser.time, "perf_counter", side_effect=[0, 1, 2, 5, 6, 36, 36, 36.001]), \
                patch.object(browser.time, "sleep") as sleep, patch("sys.stdout", io.StringIO()):
            result = session.fetch(URL, timeout=60)
        self.assertFalse(result["success"])
        self.assertEqual("document_not_completed", result["error"])
        self.assertEqual(200, result["status_code"])
        self.assertEqual("", result["text"])
        driver.set_page_load_timeout.assert_called_once_with(45)
        driver.get.assert_called_once_with(URL)
        self.assertEqual(2, driver.get_log.call_count)
        self.assertEqual(2, monotonic.call_count)
        sleep.assert_not_called()
        commands = [call.args[0] for call in driver.execute_cdp_cmd.call_args_list]
        self.assertEqual(["Page.getFrameTree", "Page.getFrameTree", "Runtime.evaluate"], commands)
        diagnostics = result["trace"][0]["diagnostics"]
        self.assertEqual(45, diagnostics["page_load_timeout_seconds"])
        self.assertEqual(30, diagnostics["evidence_wait_limit_seconds"])
        self.assertEqual(1, diagnostics["browser_prepare_seconds"])
        self.assertEqual(3, diagnostics["navigation_seconds"])
        self.assertEqual(30, diagnostics["evidence_wait_seconds"])
        self.assertEqual("complete", diagnostics["ready_state"])
        self.assertFalse(diagnostics["selected_document_loading_finished"])

    def test_ready_state_probe_is_only_after_the_terminal_deadline(self):
        session, driver = self.make_session(self.missing_completion())
        original_cdp = driver.execute_cdp_cmd.side_effect
        ticks = []

        def clock():
            ticks.append(len(ticks))
            return 0 if len(ticks) == 1 else 31

        def cdp(command, params):
            if command == "Runtime.evaluate":
                self.assertEqual(2, len(ticks))
                self.assertEqual(2, driver.get_log.call_count)
            return original_cdp(command, params)

        driver.execute_cdp_cmd.side_effect = cdp
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS": "30"}), \
                patch.object(browser.time, "monotonic", side_effect=clock), patch("sys.stdout", io.StringIO()):
            result = session.fetch(URL)
        self.assertEqual("document_not_completed", result["error"])

    def test_ready_state_probe_failure_cannot_replace_original_failure(self):
        session, driver = self.make_session(self.missing_completion(), probe_error=RuntimeError("private-test-marker"))
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS": "30"}), \
                patch.object(browser.time, "monotonic", side_effect=[0, 31]), patch("sys.stdout", io.StringIO()):
            result = session.fetch(URL)
        self.assertEqual("document_not_completed", result["error"])
        self.assertEqual(200, result["status_code"])
        self.assertEqual("unavailable", result["trace"][0]["diagnostics"]["ready_state"])
        self.assertNotIn("private-test-marker", json.dumps(result))
        self.assertEqual(2, driver.get_log.call_count)

    def test_navigation_timeout_is_distinct_from_missing_completion(self):
        class TimeoutException(Exception):
            pass

        session, driver = self.make_session(self.missing_completion())
        driver.get.side_effect = TimeoutException("private-test-marker")
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS": "30"}), \
                patch.object(browser.time, "monotonic", side_effect=[0, 31]), patch("sys.stdout", io.StringIO()):
            result = session.fetch(URL, timeout=12)
        self.assertEqual("document_not_completed", result["error"])
        self.assertEqual(200, result["status_code"])
        driver.set_page_load_timeout.assert_called_once_with(12)
        diagnostics = result["trace"][0]["diagnostics"]
        self.assertTrue(diagnostics["navigation_raised"])
        self.assertTrue(diagnostics["navigation_timeout"])
        self.assertNotIn("private-test-marker", json.dumps(result))

    def test_document_failure_event_does_not_change_success_criteria(self):
        messages = self.missing_completion() + [event(
            "loadingFailed", "doc", errorText="net::ERR_CONNECTION_RESET", canceled=True)]
        session, _ = self.make_session(messages, state="interactive")
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS": "30"}), \
                patch.object(browser.time, "monotonic", side_effect=[0, 31]), patch("sys.stdout", io.StringIO()):
            result = session.fetch(URL)
        self.assertEqual("document_not_completed", result["error"])
        self.assertEqual(200, result["status_code"])
        diagnostics = result["trace"][0]["diagnostics"]
        self.assertTrue(diagnostics["selected_document_loading_failed"])
        self.assertTrue(diagnostics["selected_document_canceled"])
        self.assertEqual("net::ERR_CONNECTION_RESET", diagnostics["document_network_error"])

    def test_success_html_unchanged_and_no_ready_probe(self):
        session, driver = self.make_session(network_events())
        with patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"}), \
                patch.object(browser.time, "monotonic", return_value=0) as monotonic, \
                patch.object(browser.time, "sleep") as sleep, patch("sys.stdout", io.StringIO()):
            expected, _, _ = browser._enrich_document(document(), URL, [{"Ofertas": [offer()]}])
            result = session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual(expected, result["text"])
        self.assertEqual(200, result["status_code"])
        self.assertEqual(1, monotonic.call_count)
        self.assertEqual(2, driver.get_log.call_count)
        driver.get.assert_called_once_with(URL)
        sleep.assert_not_called()
        commands = [call.args[0] for call in driver.execute_cdp_cmd.call_args_list]
        self.assertEqual(["Page.getFrameTree", "Page.getFrameTree", "Network.getResponseBody", "Network.getResponseBody"], commands)
        self.assertEqual("not_probed", result["trace"][0]["diagnostics"]["ready_state"])
        self.assertTrue(result["trace"][0]["diagnostics"]["selected_document_loading_finished"])


class BootstrapDiagnosticTests(unittest.TestCase):
    def test_cached_failure_logging_error_preserves_original_bootstrap_error(self):
        owned_browser = Mock()
        with patch.object(api, "ProbeBrowserSession", return_value=owned_browser):
            session = api._APISession()
        trace = [{"method": "uc_api", "stage": "bootstrap", "status_code": 200,
                  "error": "document_not_completed", "elapsed_seconds": 91.0}]
        session.bootstrap_attempted = True
        session.bootstrap_error = "document_not_completed"
        session.bootstrap_status = 200
        session.bootstrap_trace = trace
        original = deepcopy(trace)

        def failing_reuse_print(*args, **kwargs):
            self.assertIn("bootstrap_failure_reused=true", args[0])
            raise OSError("private-test-marker")

        with patch("builtins.print", side_effect=failing_reuse_print), \
                self.assertRaises(api._BootstrapError) as caught:
            session._bootstrap(URL.replace("page=1", "page=2"), 60)
        self.assertEqual("document_not_completed", str(caught.exception))
        self.assertEqual(200, caught.exception.status_code)
        self.assertIs(trace, caught.exception.trace)
        self.assertEqual(original, trace)
        owned_browser.fetch.assert_not_called()

    def test_diagnostic_summary_failure_cannot_change_bootstrap_outcome(self):
        for successful in (False, True):
            with self.subTest(successful=successful):
                owned_browser = Mock()
                owned_browser.major = 153
                owned_browser.fetch.return_value = {
                    "success": successful, "status_code": 200, "error": "document_not_completed",
                    "trace": [{"diagnostics": {"ready_state": "complete"}}]}
                with patch.object(api, "ProbeBrowserSession", return_value=owned_browser):
                    session = api._APISession()
                with patch.object(browser, "_public_browser_diagnostics", side_effect=RuntimeError("private-test-marker")), \
                        patch.object(api, "_frame", return_value=FRAME), \
                        patch.object(api.time, "monotonic", return_value=0), patch("sys.stdout", io.StringIO()):
                    if successful:
                        result = session._bootstrap(URL, 60)
                        self.assertEqual(200, result["status_code"])
                        self.assertNotIn("diagnostics", result)
                        self.assertFalse(session.bootstrap_error)
                    else:
                        with self.assertRaises(api._BootstrapError) as caught:
                            session._bootstrap(URL, 60)
                        self.assertEqual("document_not_completed", str(caught.exception))
                        self.assertEqual(200, caught.exception.status_code)
                        self.assertNotIn("diagnostics", caught.exception.trace[0])
                owned_browser.fetch.assert_called_once_with(URL, timeout=45)

    def test_allowlisted_diagnostics_survive_sticky_failure_without_extra_requests(self):
        browser_result = {"success": False, "status_code": 200, "error": "document_not_completed", "trace": [{
            "status_code": 200, "diagnostics": {"ready_state": "complete", "navigation_seconds": 45.0,
                "selected_document_loading_finished": False, "raw": "private-test-marker"},
            "body": "private-test-marker"}]}
        original = deepcopy(browser_result)
        owned_browser = Mock()
        owned_browser.fetch.return_value = browser_result
        with patch.object(api, "ProbeBrowserSession", return_value=owned_browser):
            session = api._APISession()
        stream = io.StringIO()
        with patch.object(api, "_SESSION", session), patch.object(api, "_browser_fetch") as api_fetch, \
                patch.object(api.time, "monotonic", return_value=0), patch("sys.stdout", stream):
            first = api.fetch_listing(URL)
            second = api.fetch_listing(URL.replace("page=1", "page=2"))
        for result in (first, second):
            self.assertFalse(result["success"])
            self.assertEqual(200, result["status_code"])
            self.assertEqual("document_not_completed", result["error"])
            self.assertEqual({"ready_state": "complete", "navigation_seconds": 45.0,
                              "selected_document_loading_finished": False}, result["trace"][0]["diagnostics"])
            self.assertNotIn("private-test-marker", json.dumps(result))
        owned_browser.fetch.assert_called_once()
        api_fetch.assert_not_called()
        self.assertEqual(original, browser_result)
        self.assertIn("bootstrap_failure_reused=true new_navigation=false new_api_request=false", stream.getvalue())
        self.assertNotIn("private-test-marker", stream.getvalue())
        self.assertIsNot(first["trace"][0]["diagnostics"], browser_result["trace"][0]["diagnostics"])

    def test_successful_bootstrap_copies_diagnostics_and_is_not_repeated(self):
        owned_browser = Mock()
        owned_browser.major = 153
        owned_browser.fetch.return_value = {"success": True, "status_code": 200, "trace": [{
            "diagnostics": {"ready_state": "not_probed", "navigation_seconds": 2.5, "raw": "private-test-marker"}}]}
        source = deepcopy(owned_browser.fetch.return_value)
        with patch.object(api, "ProbeBrowserSession", return_value=owned_browser):
            session = api._APISession()
        with patch.object(api, "_frame", return_value=FRAME), \
                patch.object(api.time, "monotonic", return_value=0), patch("sys.stdout", io.StringIO()):
            first = session._bootstrap(URL, 60)
            second = session._bootstrap(URL.replace("page=1", "page=2"), 60)
        self.assertEqual({"ready_state": "not_probed", "navigation_seconds": 2.5}, first["diagnostics"])
        self.assertEqual(200, first["status_code"])
        self.assertIsNone(second)
        owned_browser.fetch.assert_called_once_with(URL, timeout=45)
        self.assertEqual(source, owned_browser.fetch.return_value)
        self.assertNotIn("private-test-marker", json.dumps(first))


if __name__ == "__main__":
    unittest.main()
