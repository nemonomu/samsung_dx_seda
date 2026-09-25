"""Mode 1 historical admission and raw reuse; no live requests."""

from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_disabled = Path(__file__).with_name("MODE1_RESTORE_ENV_DOES_NOT_EXIST")
assert not _disabled.exists()
os.environ["SEDA_ENV_PATH"] = str(_disabled)

from seda import step01_main_list, transport
from seda.casas_bahia import listing_hybrid, listing_modes, listing_url_first, rest_legacy, search_api
import test_casas_bahia_listing_modes as fixtures
from test_casas_bahia_listing_hybrid import TV_URL, _listing_html, _successful_result


class RestoredModeOneRoutes(unittest.TestCase):
    def test_mode_one_has_no_new_validator_or_search_ceiling(self):
        with patch.object(rest_legacy, "fetch_search_listing", return_value=_successful_result()) as fetch, \
                patch.object(listing_hybrid, "_validation_error", side_effect=AssertionError("new_validator")), \
                patch.object(search_api, "fetch_search_listing", side_effect=AssertionError("new_search")), \
                redirect_stdout(io.StringIO()):
            result = listing_modes.fetch_listing(TV_URL, mode="1", timeout=9)
        self.assertTrue(result["success"])
        fetch.assert_called_once_with(TV_URL, timeout=9)

    def test_mode_one_uses_legacy_text_admission_not_new_success_flag(self):
        value = {"text": _listing_html(), "trace": [], "success": False, "error": "ignored_legacy_metadata"}
        with patch.object(rest_legacy, "fetch_search_listing", return_value=value), redirect_stdout(io.StringIO()):
            result = listing_modes.fetch_listing(TV_URL, mode="1")
        self.assertTrue(result["success"])
        self.assertEqual(result["text"], value["text"])
        self.assertEqual(result["error"], "")

    def test_old_raw_without_mode_sidecar_reparses_without_new_validation(self):
        for run in ("main", "bsr"):
            with self.subTest(run=run), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                raw = root / run / "raw" / "casas_bahia" / "page_001.html"
                raw.parent.mkdir(parents=True)
                raw.write_text(_listing_html(), encoding="utf-8")
                env = fixtures.ListingModeStepTests().env(root, "1", run)
                env["SEDA_REUSE_RAW"] = "1"
                stack.enter_context(patch.dict(os.environ, env))
                stack.enter_context(patch.object(step01_main_list, "page_numbers", return_value=[1]))
                stack.enter_context(patch.object(listing_modes, "close_browsers"))
                stack.enter_context(patch.object(listing_hybrid, "_validation_error", side_effect=AssertionError("strict")))
                stack.enter_context(patch.object(listing_url_first, "_validation_error", side_effect=AssertionError("url_first")))
                fetch = stack.enter_context(patch.object(step01_main_list, "fetch_url", side_effect=AssertionError("raw_refetch")))
                stack.enter_context(redirect_stdout(io.StringIO()))
                step01_main_list.main()
                fetch.assert_not_called()
                manifest = json.loads((root / run / "manifest.json").read_text(encoding="utf-8"))
                self.assertTrue(manifest["complete"])
                self.assertEqual(manifest["listing_stats"][0]["method"], "raw_reparse")


if __name__ == "__main__":
    unittest.main()
