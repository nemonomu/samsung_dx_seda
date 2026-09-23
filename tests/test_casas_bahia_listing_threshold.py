"""Offline threshold acceptance tests; all transport/parsing inputs are fixtures."""

from contextlib import ExitStack, redirect_stdout
import csv
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

_DISABLED_ENV = Path(__file__).with_name("THRESHOLD_TEST_ENV_DOES_NOT_EXIST")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ["SEDA_ENV_PATH"] = str(_DISABLED_ENV)

from seda import step01_main_list as listing
from seda.casas_bahia import listing_modes


def product(number, retailer="Casas Bahia"):
    return {"retailer": retailer, "product_url": f"https://products.invalid/{number}/p",
            "sku": str(number), "product_name": f"TV {number}"}


class CasasListingThresholdTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="threshold_test_"))
        self.root = Path(directory)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.environment = {
            "SEDA_RUN_ID": "main", "SEDA_RETAILERS": "casas_bahia",
            "SEDA_ACTIVE_RETAILER": "casas_bahia", "SEDA_PRODUCT_LINE": "TV",
            "SEDA_REUSE_RAW": "0", "SEDA_ALLOW_EMPTY_LISTING": "0",
            "SEDA_TRANSLATE_OUTPUT": "0", "SEDA_CASAS_BAHIA_LISTING_MODE": "3",
        }
        self.stack.enter_context(patch.object(listing.os, "environ", self.environment))
        self.stack.enter_context(patch.object(listing, "run_root", return_value=self.root))
        self.retailers = ["casas_bahia"]
        self.stack.enter_context(patch.object(listing, "selected_retailers", side_effect=lambda: list(self.retailers)))
        self.pages = [1, 2, 3]
        self.stack.enter_context(patch.object(listing, "page_numbers", side_effect=lambda run_id: list(self.pages)))
        self.stack.enter_context(patch.object(listing, "unique_target", return_value=0))
        self.stack.enter_context(patch.object(listing, "_magalu_listing_deferred_retry_rounds", return_value=0))
        self.stack.enter_context(patch.object(listing, "_magalu_strict_listing_payload_error", return_value=""))
        self.stack.enter_context(patch.object(listing, "_magalu_next_listing_stats", return_value={}))
        self.stack.enter_context(patch.object(listing, "_should_magalu_browser_fill", return_value=False))
        self.rows = {("Casas Bahia", 1): [product(number) for number in range(300)]}
        self.failures = {("Casas Bahia", 2), ("Casas Bahia", 3)}
        self.calls = []

        def page_url(config, page, run_id=None):
            name = "casas" if config.name == "Casas Bahia" else "magalu"
            return f"https://{name}.invalid/listing?page={page}"

        def fetch(url, **kwargs):
            parsed = urlparse(url)
            retailer = "Casas Bahia" if parsed.hostname == "casas.invalid" else "Magalu"
            page = int(parse_qs(parsed.query)["page"][0])
            self.calls.append((retailer, page, kwargs.get("mode")))
            failed = (retailer, page) in self.failures
            return SimpleNamespace(
                text="" if failed else "fixture_raw_payload_products=999",
                method="fixture_mode", error="api_http_not_200" if failed else "",
                status_code=403 if failed else 200,
                attempts=[{"status_code": 403, "attempt": attempt} for attempt in (1, 2, 3)] if failed else [],
            )

        def parse(text, retailer, base_url, url, run_id=None):
            page = int(parse_qs(urlparse(url).query)["page"][0])
            return [dict(row) for row in self.rows.get((retailer, page), [])]

        self.stack.enter_context(patch.object(listing, "page_url", side_effect=page_url))
        self.stack.enter_context(patch.object(listing, "fetch_url", side_effect=fetch))
        self.stack.enter_context(patch.object(listing, "parse_listing", side_effect=parse))

    def manifest(self):
        return json.loads((self.root / self.environment["SEDA_RUN_ID"] / "manifest.json").read_text(encoding="utf-8"))

    def final_path(self):
        return self.root / self.environment["SEDA_RUN_ID"] / "parsed" / "main_occurrences.csv"

    def partial_path(self):
        return self.final_path().with_name("main_occurrences.partial.csv")

    def csv_rows(self, path):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_main_boundary_299_rejected_300_accepted(self):
        self.rows[("Casas Bahia", 1)] = [product(number) for number in range(299)]
        with self.assertRaisesRegex(SystemExit, "unresolved pages=2,3"):
            listing._main_with_retries()
        self.assertFalse(self.manifest()["downstream_allowed"])
        self.assertFalse(self.final_path().exists())
        self.assertEqual(len(self.csv_rows(self.partial_path())), 299)
        self.rows[("Casas Bahia", 1)].append(product(299))
        listing._main_with_retries()
        self.assertTrue(self.manifest()["downstream_allowed"])
        self.assertEqual(self.manifest()["filtered_unique_count"], 300)
        self.assertEqual(self.manifest()["minimum_unique_required"], 300)
        self.assertEqual(len(self.csv_rows(self.final_path())), 300)

    def test_bsr_boundary_99_rejected_100_accepted(self):
        self.environment["SEDA_RUN_ID"] = "bsr"
        self.rows[("Casas Bahia", 1)] = [product(number) for number in range(99)]
        with self.assertRaisesRegex(SystemExit, "unresolved pages=2,3"):
            listing._main_with_retries()
        self.assertFalse(self.manifest()["downstream_allowed"])
        self.rows[("Casas Bahia", 1)].append(product(99))
        listing._main_with_retries()
        self.assertTrue(self.manifest()["downstream_allowed"])
        self.assertEqual(self.manifest()["filtered_unique_count"], 100)
        self.assertEqual(self.manifest()["minimum_unique_required"], 100)
        self.assertEqual(len(self.csv_rows(self.final_path())), 100)

    def test_accepted_failures_preserve_false_complete_failed_pages_trace_and_partial(self):
        listing._main_with_retries()
        manifest = self.manifest()
        self.assertFalse(manifest["complete"])
        self.assertTrue(manifest["accepted_with_failures"])
        self.assertTrue(manifest["threshold_met"])
        self.assertEqual([failure["page"] for failure in manifest["failures"]], [2, 3])
        self.assertEqual(len(manifest["failures"][0]["attempts"]), 3)
        self.assertEqual(manifest["output_path"], str(self.final_path()))
        self.assertEqual(self.final_path().read_bytes(), self.partial_path().read_bytes())
        self.assertIn("complete=false downstream_allowed=true", self.output.getvalue())

    def test_threshold_does_not_stop_remaining_configured_pages(self):
        listing._main_with_retries()
        self.assertEqual([call[1] for call in self.calls], [1, 2, 3])
        self.assertEqual(self.manifest()["pages"], [1, 2, 3])
        self.assertEqual(self.manifest()["unique_target"], 0)

    def test_duplicates_and_missing_identity_do_not_inflate_filtered_unique_count(self):
        self.rows[("Casas Bahia", 1)] = [product(number) for number in range(299)]
        self.rows[("Casas Bahia", 1)] += [product(1), product(1), {"product_name": "missing identity"}]
        self.assertEqual(len(self.rows[("Casas Bahia", 1)]), 302)
        with self.assertRaises(SystemExit):
            listing._main_with_retries()
        self.assertEqual(self.manifest()["rows"], 302)
        self.assertEqual(self.manifest()["filtered_unique_count"], 299)
        self.assertFalse(self.manifest()["accepted_with_failures"])

    def test_cross_page_duplicates_use_existing_product_identity(self):
        self.rows[("Casas Bahia", 1)] = [product(number) for number in range(200)]
        self.rows[("Casas Bahia", 2)] = [product(number) for number in range(100, 300)]
        self.failures = {("Casas Bahia", 3)}
        listing._main_with_retries()
        self.assertEqual(self.manifest()["rows"], 400)
        self.assertEqual(self.manifest()["filtered_unique_count"], 300)
        self.assertTrue(self.manifest()["accepted_with_failures"])

    def test_raw_product_total_does_not_replace_filtered_parsed_count(self):
        self.rows[("Casas Bahia", 1)] = [product(number) for number in range(20)]
        with self.assertRaises(SystemExit):
            listing._main_with_retries()
        self.assertEqual(self.manifest()["filtered_unique_count"], 20)
        self.assertFalse(self.manifest()["threshold_met"])

    def test_existing_url_then_sku_identity_fallback_is_preserved(self):
        self.rows[("Casas Bahia", 1)] = [
            {"retailer": "Casas Bahia", "product_url": "", "sku": str(number)}
            for number in range(300)
        ]
        listing._main_with_retries()
        self.assertEqual(self.manifest()["filtered_unique_count"], 300)

    def test_all_three_modes_keep_their_fetch_route_and_same_acceptance(self):
        for mode in ("1", "2", "3"):
            with self.subTest(mode=mode):
                self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = mode
                self.calls.clear()
                listing._main_with_retries()
                self.assertTrue(self.manifest()["accepted_with_failures"])
                self.assertEqual(self.manifest()["casas_listing_mode"], mode)
                self.assertEqual({call[2] for call in self.calls}, {listing_modes.fetch_mode(mode)})
        self.assertEqual(listing_modes.DEFAULT_MODE, "3")

    def test_no_failure_below_minimum_keeps_existing_success_behavior(self):
        self.rows[("Casas Bahia", 1)] = [product(1)]
        self.failures = set()
        listing._main_with_retries()
        manifest = self.manifest()
        self.assertTrue(manifest["complete"])
        self.assertTrue(manifest["downstream_allowed"])
        self.assertFalse(manifest["accepted_with_failures"])
        self.assertFalse(manifest["threshold_met"])
        self.assertTrue(self.final_path().exists())
        self.assertFalse(self.partial_path().exists())

    def test_empty_listing_still_fails_closed(self):
        self.rows = {}
        self.failures = set()
        with self.assertRaisesRegex(SystemExit, "listing produced 0 rows"):
            listing._main_with_retries()
        self.assertFalse(self.manifest()["downstream_allowed"])
        self.assertFalse(self.final_path().exists())

    def test_rejected_run_retires_stale_final_and_retains_backup(self):
        self.rows[("Casas Bahia", 1)] = [product(1)]
        self.final_path().parent.mkdir(parents=True)
        self.final_path().write_text("previous_fixture", encoding="utf-8")
        with self.assertRaises(SystemExit):
            listing._main_with_retries()
        self.assertFalse(self.final_path().exists())
        backups = list(self.final_path().parent.glob("main_occurrences.previous.*.csv"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "previous_fixture")
        self.assertTrue(self.partial_path().exists())

    def test_accepted_run_retires_stale_final_and_publishes_only_current_rows(self):
        self.final_path().parent.mkdir(parents=True)
        self.final_path().write_text("previous_fixture", encoding="utf-8")
        listing._main_with_retries()
        self.assertEqual(len(self.csv_rows(self.final_path())), 300)
        self.assertNotIn("previous_fixture", self.final_path().read_text(encoding="utf-8-sig"))
        backups = list(self.final_path().parent.glob("main_occurrences.previous.*.csv"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "previous_fixture")

    def test_magalu_only_failures_are_never_accepted(self):
        self.retailers = ["magalu"]
        self.environment["SEDA_ACTIVE_RETAILER"] = "magalu"
        self.rows[("Magalu", 1)] = [product(number, "Magalu") for number in range(400)]
        self.failures = {("Magalu", 2), ("Magalu", 3)}
        with self.assertRaisesRegex(SystemExit, "Magalu listing incomplete"):
            listing._main_with_retries()
        self.assertNotIn("accepted_with_failures", self.manifest())
        self.assertFalse(self.manifest()["complete"])
        self.assertFalse(self.final_path().exists())

    def test_mixed_magalu_failure_cannot_be_masked_by_casas_threshold(self):
        self.retailers = ["casas_bahia", "magalu"]
        self.rows[("Magalu", 1)] = [product(1000, "Magalu")]
        self.failures.add(("Magalu", 3))
        with self.assertRaises(SystemExit):
            listing._main_with_retries()
        self.assertEqual(self.manifest()["filtered_unique_count"], 300)
        self.assertFalse(self.manifest()["accepted_with_failures"])
        self.assertFalse(self.manifest()["downstream_allowed"])
        self.assertFalse(self.final_path().exists())

    def test_other_retailer_rows_do_not_help_casas_reach_threshold(self):
        self.retailers = ["casas_bahia", "magalu"]
        self.rows[("Casas Bahia", 1)] = [product(number) for number in range(299)]
        self.rows[("Magalu", 1)] = [product(number + 1000, "Magalu") for number in range(400)]
        with self.assertRaises(SystemExit):
            listing._main_with_retries()
        self.assertEqual(self.manifest()["rows"], 699)
        self.assertEqual(self.manifest()["filtered_unique_count"], 299)
        self.assertFalse(self.manifest()["accepted_with_failures"])

    def test_mixed_retailer_can_proceed_when_only_casas_has_failures_and_meets_minimum(self):
        self.retailers = ["casas_bahia", "magalu"]
        self.rows[("Magalu", 1)] = [product(1000, "Magalu")]
        listing._main_with_retries()
        self.assertEqual(self.manifest()["rows"], 301)
        self.assertTrue(self.manifest()["accepted_with_failures"])
        self.assertTrue(self.manifest()["downstream_allowed"])

    def test_unknown_run_id_does_not_receive_threshold_override(self):
        self.environment["SEDA_RUN_ID"] = "custom"
        with self.assertRaises(SystemExit):
            listing._main_with_retries()
        self.assertIsNone(self.manifest()["minimum_unique_required"])
        self.assertFalse(self.manifest()["accepted_with_failures"])

    def test_main_and_bsr_thresholds_do_not_accumulate_across_runs(self):
        self.rows[("Casas Bahia", 1)] = [product(number) for number in range(100)]
        self.environment["SEDA_RUN_ID"] = "bsr"
        listing._main_with_retries()
        self.assertTrue(self.manifest()["accepted_with_failures"])
        self.environment["SEDA_RUN_ID"] = "main"
        with self.assertRaises(SystemExit):
            listing._main_with_retries()
        self.assertEqual(self.manifest()["filtered_unique_count"], 100)
        self.assertFalse(self.manifest()["accepted_with_failures"])


if __name__ == "__main__":
    unittest.main()
