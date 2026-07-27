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
MAGALU_RESUME_BATCHES = (
    "resume_magalu_step08.bat",
    "resume_magalu_tv_step08.bat",
)


class MagaluBatchZenRowsContractTests(unittest.TestCase):
    def _text(self, name):
        return (ROOT / name).read_text(encoding="utf-8").lower()

    def test_full_batches_enable_bounded_itemquery_and_item_null_pdp_recovery(self):
        required = (
            "if not defined seda_allow_zenrows set seda_allow_zenrows=1",
            "if not defined seda_zenrows_dry_run set seda_zenrows_dry_run=0",
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
            "if not defined seda_magalu_listing_allow_zenrows set seda_magalu_listing_allow_zenrows=0",
        )
        for name in MAGALU_FULL_BATCHES:
            with self.subTest(batch=name):
                text = self._text(name)
                for line in required:
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

    def test_interleaved_batches_do_not_leak_global_allow_into_casas(self):
        for name in INTERLEAVED_BATCHES:
            with self.subTest(batch=name):
                text = self._text(name)
                self.assertIn(
                    'set "seda_magalu_allow_zenrows=%seda_allow_zenrows%"',
                    text,
                )
                magalu = text.rsplit("\n:run_magalu\n", 1)[1].split(
                    "\n:run_casas\n", 1
                )[0]
                casas = text.rsplit("\n:run_casas\n", 1)[1].split(
                    "\n:run_stage\n", 1
                )[0]
                self.assertIn(
                    'set "seda_allow_zenrows=%seda_magalu_allow_zenrows%"',
                    magalu,
                )
                self.assertIn('set "seda_allow_zenrows=0"', casas)

    def test_smoke_batch_remains_paid_fallback_free(self):
        text = self._text("run_magalu_tv_smoke.bat")
        self.assertIn("set seda_allow_zenrows=0", text)
        self.assertNotIn("seda_magalu_zenrows_field_fallback=1", text)


if __name__ == "__main__":
    unittest.main()
