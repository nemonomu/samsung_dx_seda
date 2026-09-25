"""Offline routing, raw provenance and unchanged legacy-mode contracts."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_disabled = Path(__file__).with_name("MODE11_ROUTE_ENV_DOES_NOT_EXIST")
assert not _disabled.exists()
os.environ["SEDA_ENV_PATH"] = str(_disabled)

from seda import step01_main_list, transport
from seda.casas_bahia import listing_modes, listing_url_first, listing_hybrid, rest_url_first
from test_casas_bahia_listing_pending_price import document
import test_casas_bahia_listing_modes as mode_fixtures


class Mode11RoutingTests(unittest.TestCase):
    def test_selector_dispatches_only_to_new_rest_module(self):
        url = "https://www.casasbahia.com.br/tv/b?page=1"
        expected = {"success": True, "text": document(), "method": "api_partner_url_first"}
        with patch.object(rest_url_first, "fetch_listing", return_value=expected) as fetch, \
                patch.object(listing_modes, "_fetch_rest", side_effect=AssertionError("legacy_rest")), \
                patch.object(listing_hybrid, "fetch_listing", side_effect=AssertionError("hybrid")):
            self.assertIs(listing_modes.fetch_listing(url, timeout=7, mode="1-1"), expected)
        fetch.assert_called_once_with(url, timeout=7)
        self.assertEqual(listing_modes.DEFAULT_MODE, "1")

    def test_mode11_transport_exception_is_not_mislabeled_as_legacy(self):
        with patch.object(listing_modes, "fetch_listing", side_effect=RuntimeError("private")), \
                patch.object(transport.time, "sleep"):
            result = transport.fetch_url("https://www.casasbahia.com.br/tv/b", mode=listing_modes.fetch_mode("1-1"))
        self.assertEqual(result.method, "api_partner_url_first")
        self.assertEqual(result.error, "RuntimeError")
        self.assertEqual(result.text, "")

    def test_pending_urls_publish_and_reuse_only_mode11_validated_raw(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            env = mode_fixtures.ListingModeStepTests().env(root, "1-1", "main")
            env["SEDA_TRANSLATE_OUTPUT"] = "0"
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch.object(step01_main_list, "page_numbers", return_value=[1]))
            stack.enter_context(patch.object(listing_modes, "close_browsers"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            result = transport.FetchResult(url="", text=document(), status_code=200, method="api_partner_url_first")
            fetch = stack.enter_context(patch.object(step01_main_list, "fetch_url", return_value=result))
            step01_main_list.main()
            self.assertEqual(fetch.call_count, 1)
            manifest = json.loads((root / "main" / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["casas_listing_mode"], "1-1")
            self.assertEqual(manifest["casas_listing_mode_label"], "rest_api_url_first")
            os.environ["SEDA_REUSE_RAW"] = "1"
            with patch.object(listing_hybrid, "_validation_error", side_effect=AssertionError("strict_price_guard")), \
                    patch.object(listing_url_first, "_validation_error", wraps=listing_url_first._validation_error) as validate:
                step01_main_list.main()
                validate.assert_called_once()
            self.assertEqual(fetch.call_count, 1)
            raw = root / "main" / "raw" / "casas_bahia" / "page_001.html"
            self.assertEqual(step01_main_list._casas_raw_mode_error(raw, "1", ""), "raw_listing_mode_mismatch")


if __name__ == "__main__":
    unittest.main()
