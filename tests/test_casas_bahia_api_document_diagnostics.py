"""Offline mode-3 failure snapshots; no live browser, requests or DB."""
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import Mock, patch
import zipfile

import pytest

from seda.casas_bahia import browser_api, browser_api_document as preparation
from seda.casas_bahia import browser_listing, listing_diagnostic_evidence as evidence
from test_casas_bahia_api_document import FakeDriver, URL


class DiagnosticDriver(FakeDriver):
    def __init__(self, *, completed=False, state="complete", error=None, moved=False, failed=False):
        super().__init__([{"completed": completed}])
        self.state, self.probe_error, self.moved, self.failed = state, error, moved, failed
        self.log_reads = 0
        self.probes = 0

    def get(self, url):
        super().get(url)
        if self.failed:
            self.logs.append({"message": json.dumps({"message": {
                "method": "Network.loadingFailed", "params": {
                    "requestId": self.frame["loaderId"], "errorText": "net::ERR_ABORTED", "canceled": True}}})})

    def get_log(self, name):
        self.log_reads += 1
        return super().get_log(name)

    def execute_cdp_cmd(self, method, params):
        if method != "Runtime.evaluate":
            return super().execute_cdp_cmd(method, params)
        self.probes += 1
        assert self.log_reads == 2  # Probe occurs after the collection deadline.
        assert params["timeout"] == 1000 and params["throwOnSideEffect"] is True
        if self.probe_error:
            raise self.probe_error
        if self.moved:
            self.frame["loaderId"] += "-moved"
        return {"result": {"value": {
            "ready_state": self.state, "dom_probe_succeeded": True,
            "dom_next_data_present": True, "dom_next_data_valid": True,
            "dom_products_present": True, "dom_product_count": 20,
            "raw": "private-test-marker", "requestId": "private-test-marker"}}}


def navigate(driver, requested=True):
    stream = io.StringIO()
    with patch.object(preparation.time, "monotonic", side_effect=[0, 1, 2, 33]), \
            patch.object(preparation.time, "sleep") as sleep, patch("sys.stdout", stream):
        result = preparation._navigate(driver, URL, browser_listing._request_identity(URL) if requested else None)
    sleep.assert_not_called()
    return result, stream.getvalue()


def test_complete_dom_with_products_does_not_turn_missing_event_into_success():
    driver = DiagnosticDriver()
    result, output = navigate(driver)
    assert result["success"] is False and result["error"] == "document_not_completed"
    assert result["status_code"] == 200 and driver.body_reads == []
    assert driver.visits == [URL] and driver.timeouts == [45] and driver.log_reads == 2
    d = result["trace"][0]["diagnostics"]
    assert d["ready_state"] == "complete" and d["dom_product_count"] == 20
    assert d["diagnostic_frame_matches_decision"] and d["diagnostic_frame_stable"]
    assert not d["selected_document_loading_finished"] and not d["selected_document_loading_failed"]
    assert d["document_stage"] == "listing" and d["evidence_wait_limit_seconds"] == 30
    assert "private-test-marker" not in output + json.dumps(result)
    assert "mode3_bootstrap_failure_decided" in output and "mode3_bootstrap_failure " in output


def test_real_abort_and_loading_state_are_distinguished():
    result, _ = navigate(DiagnosticDriver(state="loading", failed=True))
    d = result["trace"][0]["diagnostics"]
    assert result["error"] == "document_not_completed"
    assert d["ready_state"] == "loading" and d["selected_document_loading_failed"]
    assert d["selected_document_canceled"] and d["document_network_error"] == "net::ERR_ABORTED"


def test_probe_navigation_is_explicit_and_cannot_change_original_result():
    result, _ = navigate(DiagnosticDriver(moved=True))
    d = result["trace"][0]["diagnostics"]
    assert result["error"] == "document_not_completed"
    assert d["diagnostic_frame_matches_decision"] and not d["diagnostic_frame_stable"]


def test_probe_unavailable_and_logging_failure_preserve_original_failure():
    result, output = navigate(DiagnosticDriver(error=RuntimeError("private-test-marker")))
    d = result["trace"][0]["diagnostics"]
    assert result["error"] == "document_not_completed"
    assert d["ready_state"] == "unavailable" and not d["dom_probe_succeeded"]
    assert "dom_product_count" not in d and "private-test-marker" not in output
    with patch.object(browser_listing, "_log_browser_diagnostics", side_effect=OSError("private-test-marker")):
        result, _ = navigate(DiagnosticDriver())
    assert result["error"] == "document_not_completed"


def test_success_does_not_probe_or_change_payload():
    driver = DiagnosticDriver(completed=True)
    result, output = navigate(driver)
    assert result["success"] and result["trace"][0]["products"] == 1
    assert driver.probes == 0 and "diagnostics" not in result["trace"][0]
    assert "bootstrap_failure" not in output


def test_homepage_stage_is_explicit():
    result, _ = navigate(DiagnosticDriver(), requested=False)
    assert result["trace"][0]["diagnostics"]["document_stage"] == "homepage"


def test_redirects_other_document_failures_and_post_failure_navigation_are_separate():
    frame = {"id": "top", "loaderId": "current", "url": URL}
    moved = dict(frame, loaderId="later")
    driver = Mock()
    driver.execute_cdp_cmd.side_effect = [
        {"frameTree": {"frame": moved}},
        {"result": {"value": {"ready_state": "complete", "dom_probe_succeeded": True}}},
        {"frameTree": {"frame": moved}},
    ]
    messages = [
        {"method": "Network.requestWillBeSent", "params": {"type": "Document", "frameId": "top"}},
        {"method": "Network.requestWillBeSent", "params": {"type": "Document", "frameId": "top", "redirectResponse": {}}},
        {"method": "Network.requestWillBeSent", "params": {"type": "Document", "frameId": "iframe", "redirectResponse": {}}},
        {"method": "Network.responseReceived", "params": {
            "type": "Document", "frameId": "top", "loaderId": "current", "requestId": "doc", "response": {"status": 200}}},
        {"method": "Network.loadingFailed", "params": {"requestId": "other", "errorText": "net::ERR_ABORTED", "canceled": True}},
        {"method": "Network.loadingFinished", "params": {"requestId": "other"}},
    ]
    trace = {"navigation_seconds": 1}
    preparation._failure_diagnostics(driver, trace, messages, frame, dict(frame, loaderId="old"),
                                     browser_listing._request_identity(URL))
    d = trace["diagnostics"]
    assert d["top_document_request_count"] == 2 and d["top_document_redirect_count"] == 1
    assert not d["selected_document_loading_failed"] and not d["selected_document_loading_finished"]
    assert not d["diagnostic_frame_matches_decision"] and d["diagnostic_frame_stable"]
    driver.get_log.assert_not_called()
    driver.get.assert_not_called()


def test_snapshot_survives_bootstrap_reuse_manifest_json_and_diagnostic_zip():
    browser_result, _ = navigate(DiagnosticDriver())
    owned = Mock()
    owned.prepare_api.return_value = browser_result
    with patch.object(browser_api, "ProbeBrowserSession", return_value=owned):
        session = browser_api._APISession()
    results = []
    with patch("sys.stdout", io.StringIO()) as output, patch.object(browser_api, "_browser_fetch") as fetch:
        for url in (URL, URL.replace("page=1", "page=2")):
            with pytest.raises(browser_api._BootstrapError) as caught:
                session._bootstrap(url, 45)
            results.append(json.loads(json.dumps(caught.value.trace)))
    assert owned.prepare_api.call_count == 1 and fetch.call_count == 0
    assert "bootstrap_failure_reused=true new_navigation=false new_api_request=false" in output.getvalue()
    assert results[0] == results[1]
    assert results[0][0]["diagnostics"]["dom_product_count"] == 20
    with tempfile.TemporaryDirectory() as tmp:
        writer = evidence.ReportWriter(Path(tmp))
        writer.emit("page_end", {"page": 2, "status_code": 200, "success": False,
                    "bootstrap_failure_reused": True, "trace": results[1]})
        paths = writer.finish()
        with zipfile.ZipFile(paths["share_zip"]) as archive:
            record = json.loads(archive.read("events.jsonl").decode("utf-8").splitlines()[0])
            public = record["payload"]
            assert public["trace"][0]["diagnostics"]["dom_product_count"] == 20
            assert public["bootstrap_failure_reused"]
            for name in archive.namelist():
                assert "private-test-marker" not in archive.read(name).decode("utf-8")


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed to validate the browser snapshot script")
def test_browser_javascript_handles_both_layouts_invalid_json_and_absent_data():
    samples = [None, "invalid", "null", "[]", json.dumps({"props": {"initialState": {
        "search": {"results": {"products": [{"title": "private-test-marker"}]}}}}}),
        json.dumps({"props": {"pageProps": {"initialState": {"search": {"results": {"products": []}}}}}})]
    script = "const samples = " + json.dumps(samples) + ";\n" + """
        const results = samples.map(text => {
            global.document = {readyState: 'complete', getElementById: () => text === null ? null : {textContent: text}};
            return eval(SCRIPT);
        });
        process.stdout.write(JSON.stringify(results));
    """.replace("SCRIPT", json.dumps(preparation._DOM_DIAGNOSTIC_SCRIPT))
    response = subprocess.run([shutil.which("node"), "-"], input=script, capture_output=True, text=True, check=True)
    results = json.loads(response.stdout)
    assert not results[0]["dom_next_data_present"]
    assert all(not r["dom_next_data_valid"] for r in results[1:4])
    assert results[4]["dom_product_count"] == 1 and results[5]["dom_product_count"] == 0
    assert "private-test-marker" not in response.stdout
