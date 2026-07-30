import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from seda.casas_bahia import last_known_db
from seda.common.translations import PRESERVE_TRANSLATION_FIELDS_KEY
from seda.step15_final_output import _format_row, _write_manifest


class CasasBahiaLastKnownDbTests(unittest.TestCase):
    def _row(self, line="TV", item="123", **values):
        row = {
            "retailer": "Casas Bahia",
            "product_line": line,
            "item": item,
            "product_url": f"https://www.casasbahia.com.br/produto/p/{item}",
            "retailer_sku_name": "Produto Principal",
        }
        row.update(values)
        return row

    def _enabled(self):
        return patch.dict(
            os.environ,
            {"SEDA_CASAS_BAHIA_LAST_KNOWN_DB_FALLBACK": "1"},
            clear=False,
        )

    def test_latest_nonblank_per_field_fills_only_current_blanks(self):
        row = self._row(
            screen_size="55 inches",
            estimated_annual_electricity_use="",
            model_year="",
        )
        history = [
            self._row(
                account_name="CasasBahia",
                screen_size="50 inches",
                estimated_annual_electricity_use="26,5",
                model_year="2025",
            )
        ]
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="TV",
            )
        self.assertEqual(row["screen_size"], "55 inches")
        self.assertEqual(row["estimated_annual_electricity_use"], "26,5")
        self.assertEqual(row["model_year"], "2025")
        self.assertEqual(stats["recovered_rows"], 1)

    def test_historical_url_identity_mismatch_is_rejected(self):
        row = self._row(
            screen_size="",
            estimated_annual_electricity_use="",
            model_year="",
        )
        history = [
            {
                **self._row(
                    item="123",
                    screen_size="55 inches",
                    estimated_annual_electricity_use="26,5",
                    model_year="2025",
                ),
                "product_url": "https://www.casasbahia.com.br/outro/p/999",
            }
        ]
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="TV",
            )
        self.assertEqual(row["screen_size"], "")
        self.assertEqual(stats["recovered_rows"], 0)

    def test_ref_legacy_full_in_short_column_is_promoted_atomically(self):
        row = self._row(
            line="REF",
            retailer_sku_name="Geladeira Consul 377L",
            sku="",
            sku_short_version="",
            ref_refrigerator_type="",
            ref_capacity="",
        )
        history = [
            self._row(
                line="REF",
                retailer_sku_name="Geladeira Consul CRM44MK 377L",
                sku="",
                sku_short_version="CRM44MK",
                ref_refrigerator_type="Freezer-on-Top",
                ref_capacity="377L",
            )
        ]
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=history,
        ):
            last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="REF",
            )
        self.assertEqual(row["sku"], "CRM44MK")
        self.assertEqual(row["sku_short_version"], "CRM44MK")
        self.assertTrue(last_known_db.recovered_from_last_known_db(row, "sku"))

    def test_ref_family_short_is_not_promoted_to_full_sku(self):
        row = self._row(
            line="REF",
            retailer_sku_name="Geladeira sem modelo explicito",
            sku="",
            sku_short_version="",
            ref_refrigerator_type="Duplex",
            ref_capacity="377L",
        )
        history = [
            self._row(
                line="REF",
                retailer_sku_name="Geladeira Samsung RF49A5202S9",
                sku="",
                sku_short_version="RF49A",
                ref_refrigerator_type="Duplex",
                ref_capacity="377L",
            )
        ]
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=history,
        ):
            last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="REF",
            )
        self.assertEqual(row["sku"], "")

    def test_standalone_dryer_capacity_and_loading_stay_blank(self):
        row = self._row(
            line="LDY",
            retailer_sku_name="Secadora de Roupas 11kg",
            ldy_capacity="",
            ldy_loading_type="",
            ldy_color="",
        )
        history = [
            self._row(
                line="LDY",
                retailer_sku_name="Secadora de Roupas 11kg",
                ldy_capacity="11kg",
                ldy_loading_type="Front load",
                ldy_color="Branco",
            )
        ]
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=history,
        ):
            last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="LDY",
            )
        self.assertEqual(row["ldy_capacity"], "")
        self.assertEqual(row["ldy_loading_type"], "")
        self.assertEqual(row["ldy_color"], "Branco")

    def test_read_history_is_read_only_and_rolls_back(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(
            last_known_db,
            "db_connect",
            return_value=connection,
        ), patch.object(
            last_known_db,
            "output_table",
            return_value="tv_retail_com_seda",
        ):
            last_known_db._read_history(
                ["123"],
                "TV",
                ("screen_size",),
            )
        commands = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(commands[0], "SET TRANSACTION READ ONLY")
        self.assertTrue(commands[1].startswith("SET LOCAL statement_timeout"))
        self.assertTrue(commands[2].startswith("WITH ranked_history AS"))
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_db_error_fails_open_without_mutating_row(self):
        row = self._row(
            screen_size="",
            estimated_annual_electricity_use="",
            model_year="",
        )
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            side_effect=RuntimeError("offline"),
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="TV",
            )
        self.assertEqual(row["screen_size"], "")
        self.assertEqual(stats["error"], "RuntimeError")

    def test_final_preserves_recovered_translation_and_manifest_is_separate(self):
        row = self._row(
            line="REF",
            retailer_sku_name="Geladeira Consul CRM44MK 377L",
            sku="CRM44MK",
            ref_refrigerator_type="Freezer-on-Top",
        )
        last_known_db._mark_recovered_field(row, "ref_refrigerator_type")
        with patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "REF",
                "SEDA_ACTIVE_RETAILER": "casas_bahia",
            },
            clear=False,
        ):
            formatted = _format_row(
                row,
                datetime(2026, 7, 30, 12, 0, 0),
            )
        self.assertIn(
            "ref_refrigerator_type",
            formatted[PRESERVE_TRANSLATION_FIELDS_KEY],
        )

        stats = {
            "enabled": True,
            "eligible_rows": 1,
            "queried_items": 1,
            "history_rows": 1,
            "recovered_rows": 1,
            "recovered_fields": {"ref_capacity": 1},
            "error": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "final_output.csv"
            manifest = Path(tmp) / "manifest.json"
            with patch.dict(
                os.environ,
                {"SEDA_FINAL_MANIFEST_JSON": str(manifest)},
                clear=False,
            ):
                _write_manifest(
                    Path(tmp),
                    Path(tmp) / "source.csv",
                    output,
                    [],
                    datetime(2026, 7, 30, 12, 0, 0),
                    casas_bahia_last_known_db=stats,
                )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["casas_bahia_last_known_db"], stats)
        self.assertNotIn("magalu_last_known_db", payload)

    def test_batch_files_enable_recovery_without_global_request_caps(self):
        root = Path(__file__).resolve().parents[1]
        files = (
            "run_casas_bahia_tv_full.bat",
            "run_casas_bahia_ref_full.bat",
            "run_casas_bahia_ldy_full.bat",
            "run_casas_bahia_tv_ref_ldy_full.bat",
            "run_magalu_casas_interleaved_tv_ref_ldy_full.bat",
            "run_magalu_casas_interleaved_ref_ldy_full.bat",
        )
        required = (
            "seda_casas_bahia_product_source_zenrows_retries=0",
            "seda_casas_bahia_zenrows_field_fallback=1",
            "seda_casas_bahia_zenrows_field_profile_10x=premium_html",
            "seda_casas_bahia_zenrows_field_profile_25x=pdp_js_full",
            "seda_casas_bahia_zenrows_field_timeout=45",
            "seda_casas_bahia_zenrows_field_failure_streak=3",
            "seda_casas_bahia_zenrows_field_checkpoint_every=5",
            "seda_casas_bahia_last_known_db_fallback=1",
            "seda_casas_bahia_last_known_history_limit=30",
            "seda_casas_bahia_last_known_db_timeout_ms=15000",
        )
        for filename in files:
            text = (root / filename).read_text(
                encoding="utf-8-sig",
            ).casefold()
            with self.subTest(filename=filename):
                for setting in required:
                    self.assertIn(setting, text)
                self.assertNotIn(
                    "seda_casas_bahia_zenrows_field_max_items",
                    text,
                )
                self.assertNotIn(
                    "seda_casas_bahia_zenrows_field_max_requests",
                    text,
                )


if __name__ == "__main__":
    unittest.main()
