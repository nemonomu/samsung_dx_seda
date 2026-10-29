"""Offline reproduction of mode-3 HTTP 200 -> missing_ssr_listing.

Synthetic data only; no user HTML, browser, network, env file, or DB access.
"""

import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

_DISABLED_ENV = Path(__file__).with_name("MODE3_ROOT_STATE_NO_ENV_FILE")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ.setdefault("SEDA_ENV_PATH", str(_DISABLED_ENV))

from seda.casas_bahia import browser_listing as browser
from seda.casas_bahia.browser_probe_session import ProbeBrowserSession
from seda.parsers import extract_next_data, parse_listing
from test_casas_bahia_browser_listing import FRAME, URL, document, network_events, offer, product


def root_document(raw=None):
    raw = document() if raw is None else raw
    payload = extract_next_data(raw)
    payload["props"]["initialState"] = payload["props"]["pageProps"].pop("initialState")
    payload["props"]["pageProps"]["unrelated"] = "preserved"
    encoded = json.dumps(payload).replace("<", "\\u003c").replace("&", "\\u0026")
    return browser._NEXT_SCRIPT.sub(lambda m: m[1] + encoded + m[3], raw)


class RootStateTests(unittest.TestCase):
    def setUp(self):
        self.session = ProbeBrowserSession()
        self.env = patch.dict(os.environ, {"SEDA_PRODUCT_LINE": "TV"})
        self.env.start()
        self.addCleanup(self.env.stop)
        no_network = patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden"))
        no_network.start()
        self.addCleanup(no_network.stop)

    def merge(self, raw, prices=None, url=URL):
        return browser._enrich_document(
            self.session._prepare_document(raw), url,
            [{"Ofertas": [offer()]}] if prices is None else prices)

    def test_real_layout_reaches_same_rows_and_output_contract_as_legacy(self):
        legacy = document()
        modern = root_document(legacy)
        # Reproduce the old failure before adapting the real response layout.
        with self.assertRaisesRegex(browser.EvidenceError, "missing_ssr_listing"):
            browser._enrich_document(modern, URL, [{"Ofertas": [offer()]}])
        expected, old_ids, old_count = self.merge(legacy)
        actual, ids, count = self.merge(modern)
        self.assertEqual((old_ids, old_count), (ids, count))
        self.assertEqual(
            parse_listing(expected, "Casas Bahia", "https://www.casasbahia.com.br", URL),
            parse_listing(actual, "Casas Bahia", "https://www.casasbahia.com.br", URL))
        data = extract_next_data(actual)
        self.assertEqual(extract_next_data(modern)["props"]["initialState"], data["props"]["initialState"])
        self.assertEqual("preserved", data["props"]["pageProps"]["unrelated"])
        self.assertIn('<div id="preserved">other content</div>', actual)

    def test_legacy_document_is_byte_for_byte_unchanged(self):
        raw = document()
        self.assertEqual(raw, self.session._prepare_document(raw))

    def test_existing_legacy_state_is_not_overridden_by_another_root_state(self):
        raw = document()
        payload = extract_next_data(raw)
        payload["props"]["initialState"] = {"search": {"query": {"page": 99}}}
        raw = browser._NEXT_SCRIPT.sub(lambda m: m[1] + json.dumps(payload) + m[3], raw)
        self.assertEqual(raw, self.session._prepare_document(raw))

    def test_page_sort_product_and_price_guards_are_preserved(self):
        cases = [
            (document(page=2), [{"Ofertas": [offer()]}], URL, "requested_page_mismatch"),
            (document(sort="relevancia"), [{"Ofertas": [offer()]}], URL + "&ordenacao=maisvendidos", "requested_sort_mismatch"),
            (document([product(), product()]), [{"Ofertas": [offer()]}], URL, "duplicate_listing_sku"),
            (document([dict(product(), href="/wrong/p/999")]), [{"Ofertas": [offer()]}], URL, "listing_identity_invalid"),
            (document([product(seller=7)]), [{"Ofertas": [offer(seller=8)]}], URL, "price_or_seller_identity_incomplete"),
            (document(), [{"Ofertas": []}], URL, "price_or_seller_identity_incomplete"),
            (document(), [{"Ofertas": [offer(), offer(seller=8)]}], URL, "ambiguous_listing_seller"),
        ]
        for raw, prices, url, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(browser.EvidenceError, error):
                self.merge(root_document(raw), prices, url)

    def test_missing_page_evidence_is_not_invented(self):
        raw = root_document()
        payload = extract_next_data(raw)
        payload["props"]["initialState"]["search"]["query"] = {}
        raw = browser._NEXT_SCRIPT.sub(lambda m: m[1] + json.dumps(payload) + m[3], raw)
        with self.assertRaisesRegex(browser.EvidenceError, "requested_page_mismatch"):
            self.merge(raw)

    def test_script_content_is_escaped_and_duplicate_script_rejected(self):
        raw = root_document(document([product(title="Smart TV </script> & test")]).replace(
            "</script> & test", "\\u003c/script> & test"))
        prepared = self.session._prepare_document(raw)
        self.assertEqual(1, prepared.count("</script>"))
        with self.assertRaisesRegex(browser.EvidenceError, "next_data_script_count_invalid"):
            self.session._prepare_document(raw + raw)

    def test_mode2_keeps_original_input_and_mode4_keeps_own_fetch_loop(self):
        from seda.casas_bahia import browser_api_url_first, browser_listing_url_first
        raw = root_document()
        self.assertEqual(raw, browser._BrowserSession()._prepare_document(raw))
        self.assertIs(browser_api_url_first.URLFirstProbeBrowserSession.fetch,
                      browser_listing_url_first._BrowserSession.fetch)

    def test_real_fetch_loop_adapts_before_price_coverage_and_validation(self):
        driver = Mock()
        self.session.driver = driver
        self.session.major = 156
        driver.get_log.side_effect = [[], [{"message": json.dumps({"message": m})} for m in network_events()]]
        frames = iter([dict(FRAME, loaderId="prior"), FRAME])

        def cdp(command, params):
            if command == "Page.getFrameTree":
                return {"frameTree": {"frame": next(frames)}}
            if command == "Network.getResponseBody":
                return {"body": root_document() if params["requestId"] == "doc"
                        else json.dumps({"Ofertas": [offer()]})}
            raise AssertionError(command)

        driver.execute_cdp_cmd.side_effect = cdp
        result = self.session.fetch(URL)
        self.assertTrue(result["success"])
        self.assertEqual(200, result["status_code"])
        self.assertEqual(1, result["trace"][0]["parsed_rows"])
        self.assertEqual(1, result["trace"][0]["price_coverage"]["matched_source_products"])
        driver.get.assert_called_once_with(URL)


if __name__ == "__main__":
    unittest.main()
