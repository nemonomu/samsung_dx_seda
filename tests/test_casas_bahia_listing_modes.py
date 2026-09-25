"""Listing-only mode selection, raw provenance and fail-closed regressions."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from seda import step01_main_list, transport
from seda.casas_bahia import (browser_api, browser_listing, browser_api_url_first,
                              browser_listing_url_first, listing_hybrid, listing_modes,
                              listing_url_first, search_api, rest_legacy)
from test_casas_bahia_listing_hybrid import TV_URL, _blocked_result, _listing_html, _successful_result


class ModeSelectionTests(unittest.TestCase):
    def test_default_is_one(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(listing_modes.selected_mode(), "1")
            self.assertEqual(listing_modes.fetch_mode(), "casas_listing_rest")

    def test_five_modes_and_no_invalid_fallback(self):
        for value, fetch in (("1", "casas_listing_rest"), ("2", "casas_listing_hybrid"),
                             ("1-1", "casas_listing_rest_url_first"),
                             ("3", "casas_listing_uc_api"), ("4", "casas_listing_uc_api_url_first")):
            self.assertEqual(listing_modes.fetch_mode(value), fetch)
        for value in ("", "0", "5", "rest", "3;anything", "4;anything"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                listing_modes.selected_mode(value)

    def test_mode_one_only_direct_rest(self):
        with patch.object(rest_legacy, "fetch_search_listing", return_value=_successful_result()) as rest, patch.object(
            browser_api, "fetch_listing", side_effect=AssertionError("wrong_mode")) as api, patch.object(
            browser_listing, "fetch_page", side_effect=AssertionError("wrong_mode")) as ssr, redirect_stdout(io.StringIO()):
            result = listing_modes.fetch_listing(TV_URL, timeout=7, mode="1")
        self.assertTrue(result["success"])
        rest.assert_called_once_with(TV_URL, timeout=7)
        api.assert_not_called()
        ssr.assert_not_called()

    def test_mode_one_failure_never_opens_browser(self):
        with patch.object(rest_legacy, "fetch_search_listing", return_value=_blocked_result()), patch.object(
            browser_api, "fetch_listing", side_effect=AssertionError("wrong_mode")), patch.object(
            browser_listing, "fetch_page", side_effect=AssertionError("wrong_mode")), redirect_stdout(io.StringIO()):
            result = listing_modes.fetch_listing(TV_URL, timeout=7, mode="1")
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 403)
        self.assertEqual(result["text"], "")

    def test_mode_two_keeps_existing_hybrid(self):
        with patch.object(listing_hybrid, "fetch_listing", return_value=_successful_result()) as hybrid:
            self.assertTrue(listing_modes.fetch_listing(TV_URL, timeout=7, mode="2")["success"])
        hybrid.assert_called_once_with(TV_URL, timeout=7)

    def test_mode_three_only_browser_api(self):
        with patch.object(browser_api, "fetch_listing", return_value=_successful_result("uc_api")) as api, patch.object(
            search_api, "fetch_search_listing", side_effect=AssertionError("direct_rest_forbidden")), patch.object(
            listing_hybrid, "fetch_listing", side_effect=AssertionError("hybrid_forbidden")), patch.object(
            browser_api_url_first, "fetch_listing", side_effect=AssertionError("mode4_forbidden")):
            self.assertTrue(listing_modes.fetch_listing(TV_URL, timeout=7, mode="3")["success"])
        api.assert_called_once_with(TV_URL, timeout=7)

    def test_mode_four_uses_only_separate_url_first_api(self):
        with patch.object(browser_api_url_first, "fetch_listing", return_value=_successful_result("uc_api_url_first")) as api, patch.object(
            browser_api, "fetch_listing", side_effect=AssertionError("legacy_mode3_forbidden")), patch.object(
            search_api, "fetch_search_listing", side_effect=AssertionError("direct_rest_forbidden")), patch.object(
            listing_hybrid, "fetch_listing", side_effect=AssertionError("legacy_hybrid_forbidden")):
            result = listing_modes.fetch_listing(TV_URL, timeout=7, mode="4")
        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "uc_api_url_first")
        api.assert_called_once_with(TV_URL, timeout=7)

    def test_cleanup_continues_if_first_close_fails(self):
        with patch.object(browser_api, "close_browser", side_effect=RuntimeError), patch.object(
            browser_listing, "close_browser") as close, patch.object(
            browser_api_url_first, "close_browser") as close_four, patch.object(
            browser_listing_url_first, "close_browser") as close_four_ssr, redirect_stdout(io.StringIO()):
            listing_modes.close_browsers()
        close.assert_called_once()
        close_four.assert_called_once()
        close_four_ssr.assert_called_once()

    def test_explicit_transports_do_not_call_generic_requests_or_zenrows(self):
        for numeric in ("1", "1-1", "3", "4"):
            with self.subTest(mode=numeric), patch.object(listing_modes, "fetch_listing", return_value=_successful_result()) as fetch, patch.object(
                transport, "_fetch_requests", side_effect=AssertionError("generic_forbidden")), patch.object(
                transport, "_fetch_zenrows", side_effect=AssertionError("zenrows_forbidden")):
                result = transport.fetch_url(TV_URL, mode=listing_modes.fetch_mode(numeric), timeout=7)
                self.assertFalse(result.error)
                fetch.assert_called_once_with(TV_URL, timeout=7, mode=numeric)

    def test_explicit_transports_reject_error_body(self):
        for numeric in ("1", "1-1", "3", "4"):
            bad = {**_successful_result(), "error": "bad_payload"}
            with self.subTest(mode=numeric), patch.object(listing_modes, "fetch_listing", return_value=bad), patch.object(transport.time, "sleep"):
                result = transport.fetch_url(TV_URL, mode=listing_modes.fetch_mode(numeric), timeout=7)
                self.assertTrue(result.error)
                self.assertFalse(result.text)

    def test_mode_four_transport_error_has_correct_label(self):
        with patch.object(listing_modes, "fetch_listing", side_effect=RuntimeError), patch.object(transport.time, "sleep"):
            result = transport.fetch_url(TV_URL, mode=listing_modes.fetch_mode("4"), timeout=7)
        self.assertEqual(result.method, "uc_api_url_first")
        self.assertEqual(result.error, "RuntimeError")

    def test_batch_explicit_default_one_and_all_mode_overrides(self):
        source = (Path(__file__).resolve().parents[1] / "run_casas_bahia_tv_ref_ldy_full.bat").read_text(encoding="utf-8-sig")
        self.assertIn('set "SEDA_CASAS_BAHIA_LISTING_MODE=1"', source)
        for mode in ("1", "1-1", "2", "3", "4"):
            self.assertIn(f'if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="{mode}" goto :listing_mode_valid', source)


class ListingModeStepTests(unittest.TestCase):
    def env(self, root, mode="3", run="main"):
        return {"SEDA_RETAILERS": "casas_bahia", "SEDA_ACTIVE_RETAILER": "casas_bahia", "SEDA_PRODUCT_LINE": "TV",
                "SEDA_RUN_ROOT": str(root), "SEDA_RUN_ID": run, "SEDA_REUSE_RAW": "0", "SEDA_ALLOW_EMPTY_LISTING": "0",
                "SEDA_FETCH_MODE": "graphql", "SEDA_MAIN_UNIQUE_TARGET": "0", "SEDA_BSR_UNIQUE_TARGET": "0",
                "SEDA_CASAS_BAHIA_LISTING_MODE": mode}

    def successful_fetch(self, url, **kwargs):
        return transport.FetchResult(url=url, text=_listing_html(), status_code=200, method="uc_api")

    def test_all_modes_main_and_bsr_use_explicit_route_and_keep_detail_mode(self):
        for mode in ("1", "1-1", "2", "3", "4"):
            for run in ("main", "bsr"):
                with self.subTest(mode=mode, run=run), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    with patch.dict(os.environ, self.env(root, mode, run)), patch.object(step01_main_list, "page_numbers", return_value=[1]), patch.object(
                        step01_main_list, "fetch_url", side_effect=self.successful_fetch) as fetch, patch.object(listing_modes, "close_browsers") as close, redirect_stdout(io.StringIO()):
                        step01_main_list.main()
                        self.assertEqual(os.environ["SEDA_FETCH_MODE"], "graphql")
                    fetch.assert_called_once()
                    self.assertEqual(fetch.call_args.kwargs["mode"], listing_modes.fetch_mode(mode))
                    close.assert_called_once()
                    manifest = json.loads((root / run / "manifest.json").read_text(encoding="utf-8"))
                    self.assertTrue(manifest["complete"])
                    self.assertEqual(manifest["casas_listing_mode"], mode)
                    self.assertEqual(manifest["fetch_mode"], listing_modes.fetch_mode(mode))
                    metadata = json.loads((root / run / "raw" / "casas_bahia" / "page_001.mode.json").read_text(encoding="utf-8"))
                    self.assertEqual(metadata["listing_mode"], mode)
                    if run == "bsr":
                        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
                        self.assertEqual(query["ordenacao"][0].replace("-", ""), "maisvendidos")

    def test_invalid_mode_stops_before_outputs_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, self.env(root, "5")), patch.object(step01_main_list, "fetch_url") as fetch, patch.object(listing_modes, "close_browsers"):
                with self.assertRaises(SystemExit):
                    step01_main_list.main()
            fetch.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_cross_mode_raw_is_refetched_same_mode_raw_can_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, self.env(root, "2")), patch.object(step01_main_list, "page_numbers", return_value=[1]), patch.object(
                step01_main_list, "fetch_url", side_effect=self.successful_fetch), patch.object(listing_modes, "close_browsers"), redirect_stdout(io.StringIO()):
                step01_main_list.main()
            with patch.dict(os.environ, {**self.env(root), "SEDA_REUSE_RAW": "1"}), patch.object(step01_main_list, "page_numbers", return_value=[1]), patch.object(
                step01_main_list, "fetch_url", side_effect=self.successful_fetch) as fetch, patch.object(listing_modes, "close_browsers"), redirect_stdout(io.StringIO()):
                step01_main_list.main()
                self.assertEqual(fetch.call_count, 1)
                step01_main_list.main()
                self.assertEqual(fetch.call_count, 1)

    def test_raw_missing_mode_and_wrong_source_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "page_001.html"
            self.assertEqual(step01_main_list._casas_raw_mode_error(raw, "3", TV_URL), "raw_listing_mode_unverified")
            sidecar = step01_main_list._casas_raw_mode_path(raw)
            sidecar.write_text(json.dumps({"listing_mode": "3", "source_url": "different"}), encoding="utf-8")
            self.assertEqual(step01_main_list._casas_raw_mode_error(raw, "3", TV_URL), "raw_listing_source_mismatch")

    def test_mode_four_raw_has_separate_provenance_and_url_first_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, self.env(root, "3")), patch.object(step01_main_list, "page_numbers", return_value=[1]), patch.object(
                    step01_main_list, "fetch_url", side_effect=self.successful_fetch), patch.object(listing_modes, "close_browsers"), redirect_stdout(io.StringIO()):
                step01_main_list.main()
            with patch.dict(os.environ, {**self.env(root, "4"), "SEDA_REUSE_RAW": "1"}), patch.object(
                    step01_main_list, "page_numbers", return_value=[1]), patch.object(
                    step01_main_list, "fetch_url", side_effect=self.successful_fetch) as fetch, patch.object(
                    listing_modes, "close_browsers"), patch.object(listing_url_first, "_validation_error", return_value="") as validate_new, patch.object(
                    listing_hybrid, "_validation_error", side_effect=AssertionError("legacy_validator_forbidden")), redirect_stdout(io.StringIO()):
                step01_main_list.main()
                self.assertEqual(fetch.call_count, 1)
                validate_new.assert_not_called()
                step01_main_list.main()
                self.assertEqual(fetch.call_count, 1)
                validate_new.assert_called_once()
                manifest = json.loads((root / "main" / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["casas_listing_mode"], "4")
                self.assertEqual(manifest["casas_listing_mode_label"], "uc_api_url_first")

    def test_partial_mode_three_stops_downstream_and_closes_browser(self):
        good = self.successful_fetch(TV_URL)
        bad = transport.FetchResult(url=TV_URL, text="", status_code=403, method="uc_api", error="api_http_not_200")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, self.env(root)), patch.object(step01_main_list, "page_numbers", return_value=[1, 2]), patch.object(
                step01_main_list, "fetch_url", side_effect=[good, bad]), patch.object(listing_modes, "close_browsers") as close, redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    step01_main_list.main()
            self.assertTrue((root / "main" / "parsed" / "main_occurrences.partial.csv").exists())
            self.assertFalse((root / "main" / "parsed" / "main_occurrences.csv").exists())
            self.assertFalse(json.loads((root / "main" / "manifest.json").read_text())["complete"])
            close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
