import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAGALU_FULL_BATCHES = (
    "run_magalu_full.bat",
    "run_magalu_tv_ref_ldy_full.bat",
    "run_magalu_tv_ref_ldy_drission_full.bat",
    "run_magalu_casas_interleaved_ref_ldy_full.bat",
    "run_magalu_casas_interleaved_tv_ref_ldy_full.bat",
)
INTERLEAVED_BATCHES = MAGALU_FULL_BATCHES[-2:]
MAGALU_STANDALONE_FULL_BATCHES = MAGALU_FULL_BATCHES[:-2]
CASAS_FULL_BATCHES = (
    "run_casas_bahia_tv_full.bat",
    "run_casas_bahia_ref_full.bat",
    "run_casas_bahia_ldy_full.bat",
    "run_casas_bahia_tv_ref_ldy_full.bat",
)
CASAS_ZENROWS_BATCHES = CASAS_FULL_BATCHES + INTERLEAVED_BATCHES
MAGALU_RESUME_BATCHES = (
    "resume_magalu_step08.bat",
    "resume_magalu_tv_step08.bat",
)


class MagaluBatchZenRowsContractTests(unittest.TestCase):
    def _text(self, name):
        return (ROOT / name).read_text(encoding="utf-8").lower()

    def test_full_batches_enable_bounded_itemquery_and_item_null_pdp_recovery(self):
        required = (
            "if not defined seda_magalu_zenrows_field_fallback set seda_magalu_zenrows_field_fallback=1",
            "if not defined seda_magalu_zenrows_field_profile set seda_magalu_zenrows_field_profile=auto_custom_headers",
            "if not defined seda_magalu_zenrows_field_timeout set seda_magalu_zenrows_field_timeout=90",
            "if not defined seda_magalu_zenrows_field_max_items set seda_magalu_zenrows_field_max_items=25",
            "if not defined seda_magalu_zenrows_field_failure_streak set seda_magalu_zenrows_field_failure_streak=3",
            "if not defined seda_magalu_zenrows_field_checkpoint_every set seda_magalu_zenrows_field_checkpoint_every=5",
            "set seda_magalu_zenrows_detail_fallback=0",
            "if not defined seda_magalu_zenrows_pdp_fallback set seda_magalu_zenrows_pdp_fallback=1",
            "if not defined seda_magalu_zenrows_pdp_timeout set seda_magalu_zenrows_pdp_timeout=120",
            "if not defined seda_magalu_last_known_db_fallback set seda_magalu_last_known_db_fallback=1",
            "if not defined seda_magalu_last_known_history_limit set seda_magalu_last_known_history_limit=30",
            "if not defined seda_magalu_last_known_db_timeout_ms set seda_magalu_last_known_db_timeout_ms=15000",
            "if not defined seda_magalu_listing_allow_zenrows set seda_magalu_listing_allow_zenrows=1",
        )
        for name in MAGALU_FULL_BATCHES:
            with self.subTest(batch=name):
                text = self._text(name)
                for line in required:
                    self.assertIn(line, text)
        actual_defaults = (
            "if not defined seda_allow_zenrows set seda_allow_zenrows=1",
            "if not defined seda_zenrows_dry_run set seda_zenrows_dry_run=0",
        )
        for name in MAGALU_STANDALONE_FULL_BATCHES:
            with self.subTest(batch=name, contract="standalone_global_default"):
                text = self._text(name)
                for line in actual_defaults:
                    self.assertIn(line, text)

    def test_resume_batches_keep_last_known_contract_enabled(self):
        required = (
            "if not defined seda_magalu_last_known_db_fallback set seda_magalu_last_known_db_fallback=1",
            "if not defined seda_magalu_last_known_history_limit set seda_magalu_last_known_history_limit=30",
            "if not defined seda_magalu_last_known_db_timeout_ms set seda_magalu_last_known_db_timeout_ms=15000",
        )
        for name in MAGALU_RESUME_BATCHES:
            with self.subTest(batch=name):
                text = self._text(name)
                for line in required:
                    self.assertIn(line, text)

    def test_full_batches_use_isolated_profile_and_bounded_listing_recovery(self):
        required = (
            "if not defined seda_magalu_listing_fetch_mode set seda_magalu_listing_fetch_mode=magalu_listing_graphql_zenrows",
            "if not defined seda_magalu_listing_allow_zenrows set seda_magalu_listing_allow_zenrows=1",
            "if not defined seda_magalu_listing_zenrows_graphql_first set seda_magalu_listing_zenrows_graphql_first=1",
            "if not defined seda_magalu_listing_zenrows_profile set seda_magalu_listing_zenrows_profile=premium_html",
            "if not defined seda_magalu_listing_zenrows_fallback_profiles set seda_magalu_listing_zenrows_fallback_profiles=listing_next_data_js_wait",
            "if not defined seda_magalu_listing_zenrows_timeout set seda_magalu_listing_zenrows_timeout=45",
            "if not defined seda_magalu_listing_deferred_retry_rounds set seda_magalu_listing_deferred_retry_rounds=1",
            "if not defined seda_magalu_listing_deferred_retry_sleep_seconds set seda_magalu_listing_deferred_retry_sleep_seconds=2",
            "if not defined seda_magalu_search_browser_attempts set seda_magalu_search_browser_attempts=1",
            "if not defined seda_magalu_search_retries set seda_magalu_search_retries=0",
            "if not defined seda_magalu_browser_profile set \"seda_magalu_browser_profile=c:/tmp/seda_magalu_profiles/",
            "%seda_run_timestamp%",
        )
        for name in MAGALU_FULL_BATCHES:
            with self.subTest(batch=name):
                text = self._text(name)
                for token in required:
                    self.assertIn(token, text)

    def test_casas_batches_default_zenrows_only_when_user_did_not_set_switches(self):
        required = (
            "if not defined seda_casas_bahia_default_allow_zenrows set seda_casas_bahia_default_allow_zenrows=1",
            "if not defined seda_casas_bahia_default_zenrows_dry_run set seda_casas_bahia_default_zenrows_dry_run=0",
        )
        for name in CASAS_ZENROWS_BATCHES:
            with self.subTest(batch=name):
                text = self._text(name)
                for line in required:
                    self.assertIn(line, text)

        for name in CASAS_FULL_BATCHES:
            with self.subTest(batch=name, contract="no_unconditional_override"):
                lines = [line.strip() for line in self._text(name).splitlines()]
                self.assertIn(required[0], lines)
                self.assertIn(required[1], lines)
                self.assertFalse(self._actual_global_assignments(lines, "seda_allow_zenrows"))
                self.assertFalse(self._actual_global_assignments(lines, "seda_zenrows_dry_run"))

    def test_interleaved_batches_use_retailer_defaults_without_actual_global_mutation(self):
        required = (
            "if not defined seda_magalu_default_allow_zenrows set seda_magalu_default_allow_zenrows=1",
            "if not defined seda_magalu_default_zenrows_dry_run set seda_magalu_default_zenrows_dry_run=0",
            "if not defined seda_casas_bahia_default_allow_zenrows set seda_casas_bahia_default_allow_zenrows=1",
            "if not defined seda_casas_bahia_default_zenrows_dry_run set seda_casas_bahia_default_zenrows_dry_run=0",
        )
        for name in INTERLEAVED_BATCHES:
            with self.subTest(batch=name):
                text = self._text(name)
                lines = [line.strip() for line in text.splitlines()]
                for line in required:
                    self.assertIn(line, lines)
                self.assertFalse(self._actual_global_assignments(lines, "seda_allow_zenrows"))
                self.assertFalse(self._actual_global_assignments(lines, "seda_zenrows_dry_run"))
                self.assertNotIn("seda_magalu_allow_zenrows", text)

    @staticmethod
    def _actual_global_assignments(lines, name):
        return [
            line
            for line in lines
            if line.startswith(f"set {name}=")
            or line.startswith(f'set "{name}=')
            or line.startswith(f"if not defined {name} ")
        ]

    def test_smoke_batch_remains_paid_fallback_free(self):
        text = self._text("run_magalu_tv_smoke.bat")
        self.assertIn("set seda_allow_zenrows=0", text)
        self.assertNotIn("seda_magalu_zenrows_field_fallback=1", text)


if __name__ == "__main__":
    unittest.main()
