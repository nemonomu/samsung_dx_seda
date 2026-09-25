"""Mode 1 restores 1290145 partial-row publication; other modes stay fail-closed.

All search/parser results are fixtures. Real CSV/rank/manifest code is exercised
inside temporary directories; no collector, child process, credential or DB use.
"""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import test_casas_bahia_listing_threshold as threshold
from seda import step01_main_list as listing
from seda import step02_main_targets as targets
from seda import step04_bsr_rank as ranking
from seda import step07_final_targets as final_targets
from seda.casas_bahia import listing_auto_diagnostics as auto
from seda.casas_bahia import listing_modes
from seda.common import orchestrator


class ModeOnePublicationTests(unittest.TestCase):
    def setUp(self):
        threshold.CasasListingThresholdTests.setUp(self)
        self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = "1"
        self.rows[("Casas Bahia", 1)] = [threshold.product(12), threshold.product(8)]

    manifest = threshold.CasasListingThresholdTests.manifest
    final_path = threshold.CasasListingThresholdTests.final_path
    partial_path = threshold.CasasListingThresholdTests.partial_path
    csv_rows = threshold.CasasListingThresholdTests.csv_rows

    def test_main_below_300_publishes_and_continues_with_failed_page_evidence(self):
        self.assertIsNone(listing._main_with_retries())
        manifest = self.manifest()
        self.assertEqual(manifest["rows"], 2)
        self.assertFalse(manifest["complete"])
        self.assertTrue(manifest["accepted_with_failures"])
        self.assertTrue(manifest["downstream_allowed"])
        self.assertIsNone(manifest["minimum_unique_required"])
        self.assertFalse(manifest["threshold_met"])
        self.assertEqual(manifest["publication_policy"], "legacy_mode1")
        self.assertEqual([failure["page"] for failure in manifest["failures"]], [2, 3])
        self.assertEqual(len(manifest["failures"][0]["attempts"]), 3)
        self.assertEqual(self.final_path().read_bytes(), self.partial_path().read_bytes())
        self.assertEqual([row["sku"] for row in self.csv_rows(self.final_path())], ["12", "8"])
        self.assertEqual([row["main_rank"] for row in self.csv_rows(self.final_path())], ["1", "2"])
        self.assertIn("policy=legacy_mode1", self.output.getvalue())
        self.assertIn("complete=false downstream_allowed=true", self.output.getvalue())

    def test_bsr_below_100_publishes_all_rows_and_preserves_rank_order(self):
        self.environment["SEDA_RUN_ID"] = "bsr"
        self.rows[("Casas Bahia", 1)].append(threshold.product(12))
        self.assertIsNone(listing._main_with_retries())
        rows = self.csv_rows(self.final_path())
        self.assertEqual([row["sku"] for row in rows], ["12", "8", "12"])
        self.assertEqual([row["bsr_rank"] for row in rows], ["1", "2", "3"])
        self.assertEqual(self.manifest()["filtered_unique_count"], 2)
        self.assertEqual(self.manifest()["rows"], 3)
        self.assertTrue(self.manifest()["downstream_allowed"])
        self.assertIsNone(self.manifest()["minimum_unique_required"])

    def test_failures_do_not_skip_later_configured_pages_or_create_rank_gaps(self):
        self.failures = {("Casas Bahia", 2)}
        self.rows[("Casas Bahia", 3)] = [threshold.product(27)]
        listing._main_with_retries()
        self.assertEqual([call[1] for call in self.calls], [1, 2, 3])
        self.assertEqual({call[2] for call in self.calls}, {listing_modes.fetch_mode("1")})
        self.assertEqual([row["sku"] for row in self.csv_rows(self.final_path())], ["12", "8", "27"])
        self.assertEqual([row["main_rank"] for row in self.csv_rows(self.final_path())], ["1", "2", "3"])

    def test_zero_rows_stop_after_writing_empty_final_as_1290145_did(self):
        self.rows = {}
        with self.assertRaisesRegex(SystemExit, "listing produced 0 rows"):
            listing._main_with_retries()
        self.assertEqual(self.csv_rows(self.final_path()), [])
        self.assertEqual(self.csv_rows(self.partial_path()), [])
        self.assertFalse(self.manifest()["downstream_allowed"])
        self.assertFalse(self.manifest()["accepted_with_failures"])

    def test_zero_rows_without_fetch_errors_still_stop(self):
        self.rows, self.failures = {}, set()
        with self.assertRaisesRegex(SystemExit, "listing produced 0 rows"):
            listing._main_with_retries()
        self.assertEqual(self.csv_rows(self.final_path()), [])
        self.assertFalse(self.manifest()["complete"])

    def test_existing_explicit_allow_empty_setting_remains_opt_in(self):
        self.rows = {}
        self.environment["SEDA_ALLOW_EMPTY_LISTING"] = "1"
        listing._main_with_retries()
        self.assertEqual(self.csv_rows(self.final_path()), [])
        self.assertTrue(self.manifest()["downstream_allowed"])
        self.assertTrue(self.manifest()["accepted_with_failures"])
        self.assertFalse(self.manifest()["complete"])

    def test_success_removes_previous_partial_without_inventing_threshold(self):
        listing._main_with_retries()
        self.assertTrue(self.partial_path().exists())
        self.failures = set()
        listing._main_with_retries()
        self.assertFalse(self.partial_path().exists())
        self.assertTrue(self.manifest()["complete"])
        self.assertTrue(self.manifest()["downstream_allowed"])
        self.assertFalse(self.manifest()["accepted_with_failures"])

    def test_partial_replaces_stale_final_only_with_current_rows(self):
        self.final_path().parent.mkdir(parents=True)
        self.final_path().write_text("old_fixture_only", encoding="utf-8")
        listing._main_with_retries()
        self.assertEqual(len(self.csv_rows(self.final_path())), 2)
        self.assertNotIn("old_fixture_only", self.final_path().read_text(encoding="utf-8-sig"))
        backups = list(self.final_path().parent.glob("main_occurrences.previous.*.csv"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "old_fixture_only")

    def test_custom_run_id_keeps_old_mode_one_no_minimum_behavior(self):
        self.environment["SEDA_RUN_ID"] = "custom"
        listing._main_with_retries()
        self.assertTrue(self.manifest()["downstream_allowed"])
        self.assertIsNone(self.manifest()["minimum_unique_required"])

    def test_other_modes_still_stop_below_minimum_and_accept_only_at_threshold(self):
        for mode in ("1-1", "2", "3", "4"):
            for stage, count in (("main", 300), ("bsr", 100)):
                with self.subTest(mode=mode, stage=stage):
                    self.environment.update(SEDA_CASAS_BAHIA_LISTING_MODE=mode, SEDA_RUN_ID=stage)
                    self.rows[("Casas Bahia", 1)] = [threshold.product(1)]
                    with self.assertRaisesRegex(SystemExit, "listing incomplete"):
                        listing._main_with_retries()
                    self.assertFalse(self.final_path().exists())
                    self.assertFalse(self.manifest()["downstream_allowed"])
                    self.assertEqual(self.manifest()["minimum_unique_required"], count)
                    self.assertNotIn("publication_policy", self.manifest())
                    self.rows[("Casas Bahia", 1)] = [threshold.product(number) for number in range(count)]
                    listing._main_with_retries()
                    self.assertTrue(self.manifest()["downstream_allowed"])

    def test_mixed_retailer_selection_never_uses_legacy_mode_one_override(self):
        self.retailers = ["casas_bahia", "magalu"]
        self.rows[("Magalu", 1)] = [threshold.product(number + 1000, "Magalu") for number in range(400)]
        with self.assertRaisesRegex(SystemExit, "listing incomplete"):
            listing._main_with_retries()
        self.assertFalse(self.manifest()["downstream_allowed"])
        self.assertEqual(self.manifest()["minimum_unique_required"], 300)
        self.assertNotIn("publication_policy", self.manifest())

    def test_magalu_only_remains_fail_closed_even_if_mode_one_is_selected(self):
        self.retailers = ["magalu"]
        self.rows[("Magalu", 1)] = [threshold.product(1, "Magalu")]
        self.failures = {("Magalu", 2)}
        with self.assertRaisesRegex(SystemExit, "Magalu listing incomplete"):
            listing._main_with_retries()
        self.assertFalse(self.final_path().exists())
        self.assertNotIn("publication_policy", self.manifest())

    def test_main_targets_and_bsr_rank_consume_partial_rows_via_ordinary_csv(self):
        self.rows[("Casas Bahia", 1)].append(threshold.product(12))
        listing._main_with_retries()
        with patch.object(targets, "run_root", return_value=self.root):
            targets.main()
        target_rows = self.csv_rows(self.root / "output" / "seda_main_targets.csv")
        self.assertEqual([row["sku"] for row in target_rows], ["12", "8"])
        self.assertEqual([row["main_rank"] for row in target_rows], ["1", "2"])
        self.environment["SEDA_RUN_ID"] = "bsr"
        listing._main_with_retries()
        with patch.object(ranking, "run_root", return_value=self.root):
            ranking.main()
        rank_rows = self.csv_rows(self.root / "bsr" / "parsed" / "bsr_rank_map.csv")
        self.assertEqual([row["sku"] for row in rank_rows], ["12", "8"])
        self.assertEqual([row["bsr_rank"] for row in rank_rows], ["1", "2"])

    def test_orchestrator_completion_recognizes_published_rows_despite_failed_pages(self):
        for run_id, step_name in (("main", "main_list"), ("bsr", "bsr_list")):
            self.environment["SEDA_RUN_ID"] = run_id
            listing._main_with_retries()
            with patch.object(orchestrator, "run_root", return_value=self.root):
                complete, _ = orchestrator.step_complete(SimpleNamespace(name=step_name), recover=False)
            self.assertTrue(complete)

    def _run_fixture_orchestrator(self, calls):
        chosen = [step for step in orchestrator.steps_for("seda.casas_bahia")
                  if step.name in {"main_list", "main_targets", "bsr_list", "bsr_rank",
                                   "final_targets", "detail_enrichment"}]

        def run(module_name, *, env=None, dry_run=False):
            self.assertFalse(dry_run)
            self.assertEqual(env.get("SEDA_CASAS_BAHIA_LISTING_MODE",
                                     self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"]), "1")
            calls.append(module_name.rsplit(".", 1)[-1])
            try:
                if module_name.endswith("step01_main_list"):
                    self.environment["SEDA_RUN_ID"] = "main"
                    listing._main_with_retries()
                elif module_name.endswith("step03_bsr_list"):
                    self.environment["SEDA_RUN_ID"] = "bsr"
                    listing._main_with_retries()
                elif module_name.endswith("step02_main_targets"):
                    with patch.object(targets, "run_root", return_value=self.root):
                        targets.main()
                elif module_name.endswith("step04_bsr_rank"):
                    with patch.object(ranking, "run_root", return_value=self.root):
                        ranking.main()
                elif module_name.endswith("step07_final_targets"):
                    with patch.object(final_targets, "run_root", return_value=self.root):
                        final_targets.main()
                else:
                    self.assertTrue(module_name.endswith("step08_detail_enrichment"))
                    self.assertEqual(len(self.csv_rows(self.root / "output" / "seda_final_targets.csv")), 2)
                return 0
            except SystemExit:
                return 1

        self.environment["SEDA_RUN_ROOT"] = str(self.root)
        with patch.object(orchestrator, "selected_steps", return_value=chosen), \
                patch.object(orchestrator, "start_zenrows_usage_execution"), \
                patch.object(orchestrator, "run_module", side_effect=run), \
                patch("sys.argv", ["fixture_orchestrator", "--all"]):
            orchestrator.run_retailer_orchestrator("casas_bahia", "seda.casas_bahia", "fixture")

    def test_runner_continues_partial_main_and_bsr_to_detail_entry(self):
        calls = []
        self._run_fixture_orchestrator(calls)
        self.assertEqual(calls, ["step01_main_list", "step02_main_targets", "step03_bsr_list",
                                 "step04_bsr_rank", "step07_final_targets", "step08_detail_enrichment"])

    def test_runner_stops_zero_rows_before_targets_or_detail(self):
        self.rows = {}
        calls = []
        with self.assertRaises(SystemExit):
            self._run_fixture_orchestrator(calls)
        self.assertEqual(calls, ["step01_main_list"])

    def test_automatic_zip_reports_partial_coverage_and_downstream_permission(self):
        original_constructor = auto.AutomaticListingDiagnostics.__init__

        def constructor(instance, **kwargs):
            kwargs["log_dir"] = self.root / "log"
            original_constructor(instance, **kwargs)

        with patch.object(auto, "_ACTIVE", None), \
                patch.object(auto.AutomaticListingDiagnostics, "__init__", constructor), \
                patch.object(listing_modes, "close_browsers"):
            listing.main()
        archives = list((self.root / "log").glob("casas_listing_*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as archive:
            report = json.loads(archive.read("report.json"))
        self.assertEqual(report["run"]["mode"], 1)
        self.assertEqual(report["outcome"]["outcome"], "accepted_with_failures")
        self.assertTrue(report["outcome"]["downstream_allowed"])
        self.assertFalse(report["outcome"]["coverage_complete"])
        self.assertEqual(report["outcome"]["failed_page_numbers"], [2, 3])
        self.assertEqual(report["outcome"]["filtered_unique_count"], 2)


if __name__ == "__main__":
    unittest.main()
