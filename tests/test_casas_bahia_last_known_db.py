import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from seda.casas_bahia import last_known_db
from seda.casas_bahia.ldy_sku_contract import LAST_KNOWN_SELECTED_TOKEN
from seda.casas_bahia.sku_contract import PRODUCT_SOURCE_MODEL_TOKEN
from seda.common.translations import PRESERVE_TRANSLATION_FIELDS_KEY
from seda.step14_db_load import _db_value
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

    def test_tv_identity_conflict_recovers_only_latest_non_artifact_sku(self):
        title = 'Smart TV TCL QLED 50" 4K P7K Google TV'
        row = self._row(
            item="1582420985",
            retailer_sku_name=title,
            sku=title,
            screen_size="",
            estimated_annual_electricity_use="",
            model_year="",
            parse_status="casas_zenrows_field_failed:identity_conflict",
        )
        history = [
            self._row(
                item="1582420985",
                account_name="CasasBahia",
                retailer_sku_name=title,
                sku=title,
                screen_size="50 inches",
                estimated_annual_electricity_use="100 W",
                model_year="2025",
            ),
            self._row(
                item="1582420985",
                account_name="CasasBahia",
                retailer_sku_name=title,
                sku="QLED 50 4K P7K",
                screen_size="50 inches",
                estimated_annual_electricity_use="100 W",
                model_year="2025",
            ),
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
        self.assertEqual(row["sku"], "QLED 50 4K P7K")
        self.assertEqual(row["screen_size"], "")
        self.assertEqual(row["estimated_annual_electricity_use"], "")
        self.assertEqual(row["model_year"], "")
        self.assertTrue(last_known_db.recovered_from_last_known_db(row, "sku"))
        self.assertIn(
            last_known_db.TV_LAST_KNOWN_SELECTED_TOKEN,
            row["parse_status"].split("+"),
        )
        self.assertEqual(stats["recovered_fields"], {"sku": 1})
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "casas_bahia"},
            clear=False,
        ):
            formatted = _format_row(row, datetime(2026, 7, 31, 12, 0, 0))
        self.assertEqual(formatted["sku"], "QLED 50 4K P7K")

    def test_tv_only_artifact_history_stays_null(self):
        title = 'Smart TV Samsung Vision AI 55" QLED 4K Q7F 2025'
        row = self._row(
            item="1582487695",
            retailer_sku_name=title,
            sku="Q7F",
            screen_size="55 inches",
            estimated_annual_electricity_use="26,5",
            model_year="2025",
            parse_status="listing_casas_bahia_partner_api",
        )
        history = [
            self._row(
                item="1582487695",
                retailer_sku_name=title,
                sku=title,
            ),
            self._row(
                item="1582487695",
                retailer_sku_name=title,
                sku="1582487695",
            ),
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
        self.assertEqual(stats["recovered_rows"], 0)
        self.assertFalse(last_known_db.recovered_from_last_known_db(row, "sku"))
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "casas_bahia"},
            clear=False,
        ):
            formatted = _format_row(row, datetime(2026, 7, 31, 12, 0, 0))
        self.assertEqual(formatted["sku"], "")
        self.assertIsNone(_db_value("sku", formatted["sku"]))

    def test_tv_verified_model_is_not_replaced_by_history(self):
        row = self._row(
            sku="QLED 55 4K Q7F",
            screen_size="",
            estimated_annual_electricity_use="26,5",
            model_year="2025",
            parse_status=PRODUCT_SOURCE_MODEL_TOKEN,
        )
        history = [
            self._row(
                sku="OLDER-MODEL",
                screen_size="55 inches",
                estimated_annual_electricity_use="26,5",
                model_year="2025",
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
                product_line_value="TV",
            )
        self.assertEqual(row["sku"], "QLED 55 4K Q7F")
        self.assertEqual(row["screen_size"], "55 inches")
        self.assertFalse(last_known_db.recovered_from_last_known_db(row, "sku"))

    def test_tv_verified_model_survives_stale_failure_without_db_query(self):
        row = self._row(
            sku="QLED 55 4K Q7F",
            screen_size="55 inches",
            estimated_annual_electricity_use="26,5",
            model_year="2025",
            parse_status=(
                "product_source_failed:sku_mismatch:999+"
                + PRODUCT_SOURCE_MODEL_TOKEN
            ),
        )
        read_history = Mock()
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            read_history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="TV",
            )
        read_history.assert_not_called()
        self.assertEqual(row["sku"], "QLED 55 4K Q7F")
        self.assertFalse(last_known_db.recovered_from_last_known_db(row, "sku"))
        self.assertEqual(stats["eligible_rows"], 0)

    def test_tv_title_model_prevents_sku_history_query(self):
        row = self._row(
            retailer_sku_name=(
                'Smart TV LG 55" QNED Processador AI A7 55QNED73ASA'
            ),
            sku="",
            screen_size="55 inches",
            estimated_annual_electricity_use="26,5",
            model_year="2025",
            parse_status="listing_casas_bahia_partner_api",
        )
        read_history = Mock()
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            read_history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="TV",
            )
        read_history.assert_not_called()
        self.assertEqual(stats["eligible_rows"], 0)
        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV", "SEDA_ACTIVE_RETAILER": "casas_bahia"},
            clear=False,
        ):
            formatted = _format_row(row, datetime(2026, 7, 31, 12, 0, 0))
        self.assertEqual(formatted["sku"], "55QNED73ASA")

    def test_ref_and_ldy_sku_mismatch_status_keeps_legacy_field_recovery(self):
        cases = (
            (
                "REF",
                self._row(
                    line="REF",
                    retailer_sku_name="Geladeira Consul CRM44MK 377L",
                    sku="CRM44MK",
                    sku_short_version="CRM44MK",
                    ref_refrigerator_type="Duplex",
                    ref_capacity="",
                    parse_status="product_source_failed:sku_mismatch:999",
                ),
                self._row(
                    line="REF",
                    retailer_sku_name="Geladeira Consul CRM44MK 377L",
                    sku="CRM44MK",
                    sku_short_version="CRM44MK",
                    ref_refrigerator_type="Duplex",
                    ref_capacity="377L",
                ),
                "ref_capacity",
                "377L",
            ),
            (
                "LDY",
                self._row(
                    line="LDY",
                    retailer_sku_name="Lavadora Midea 13kg",
                    ldy_loading_type="Top load",
                    ldy_color="Branco",
                    ldy_capacity="",
                    parse_status="product_source_failed:sku_mismatch:999",
                ),
                self._row(
                    line="LDY",
                    retailer_sku_name="Lavadora Midea 13kg",
                    ldy_loading_type="Top load",
                    ldy_color="Branco",
                    ldy_capacity="13kg",
                ),
                "ldy_capacity",
                "13kg",
            ),
        )
        for line, row, historical, field, expected in cases:
            with self.subTest(line=line), self._enabled(), patch.object(
                last_known_db,
                "_read_history",
                return_value=[historical],
            ):
                stats = last_known_db.backfill_casas_bahia_last_known_fields(
                    [row],
                    active_retailer="casas_bahia",
                    product_line_value=line,
                )
            self.assertEqual(row[field], expected)
            self.assertEqual(stats["recovered_fields"], {field: 1})

    def test_tv_current_item_url_mismatch_never_queries_history(self):
        row = self._row(
            item="123",
            sku="",
            parse_status="identity_conflict",
        )
        row["product_url"] = "https://www.casasbahia.com.br/produto/p/999"
        read_history = Mock()
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            read_history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="TV",
            )
        read_history.assert_not_called()
        self.assertEqual(stats["eligible_rows"], 0)

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

    def test_ldy_uses_newest_valid_stored_sku_and_recomputes_short(self):
        row = self._row(
            line="LDY",
            retailer_sku_name="Lavadora Samsung 11kg",
            sku="",
            sku_short_version="STALE",
            ldy_loading_type="Top load",
            ldy_color="Branco",
            ldy_capacity="11kg",
        )
        history = [
            self._row(
                line="LDY",
                retailer_sku_name="Lavadora Samsung 11kg",
                sku="220V",
                sku_short_version="WRONG",
            ),
            self._row(
                line="LDY",
                retailer_sku_name="Lavadora Samsung 11kg",
                sku="WW11T4040BXFAZ",
                sku_short_version="UNTRUSTED",
            ),
        ]
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="LDY",
            )
        self.assertEqual(row["sku"], "WW11T4040BXFAZ")
        self.assertEqual(row["sku_short_version"], "WW11T")
        self.assertIn(
            LAST_KNOWN_SELECTED_TOKEN,
            row["parse_status"].split("+"),
        )
        self.assertTrue(last_known_db.recovered_from_last_known_db(row, "sku"))
        self.assertEqual(stats["recovered_fields"], {"sku": 1})

    def test_ldy_does_not_promote_legacy_short_or_invalid_sku(self):
        row = self._row(
            line="LDY",
            retailer_sku_name="Lavadora sem modelo explicito",
            sku="",
            sku_short_version="",
            ldy_loading_type="Top load",
            ldy_color="Branco",
            ldy_capacity="13kg",
        )
        history = [
            self._row(
                line="LDY",
                retailer_sku_name="Lavadora sem modelo explicito",
                sku="",
                sku_short_version="WW11T",
            ),
            self._row(
                line="LDY",
                retailer_sku_name="Lavadora sem modelo explicito",
                sku="AGITADOR",
                sku_short_version="PLR14A",
            ),
        ]
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="LDY",
            )
        self.assertEqual(row["sku"], "")
        self.assertEqual(row["sku_short_version"], "")
        self.assertEqual(stats["recovered_rows"], 0)

    def test_ref_and_ldy_identity_conflict_recovers_only_sku(self):
        cases = (
            (
                "REF",
                "Geladeira Consul 377L",
                "CRM44MK",
                "ref_capacity",
                "377L",
            ),
            (
                "LDY",
                "Lavadora Philco 14kg",
                "PLR14A",
                "ldy_capacity",
                "14kg",
            ),
        )
        for line, title, sku, other_field, historical_other in cases:
            row = self._row(
                line=line,
                retailer_sku_name=title,
                sku="",
                sku_short_version="",
                parse_status="casas_zenrows_field_failed:identity_conflict",
                **{other_field: ""},
            )
            historical = self._row(
                line=line,
                retailer_sku_name=title,
                sku=sku,
                sku_short_version=sku,
                **{other_field: historical_other},
            )
            with self.subTest(line=line), self._enabled(), patch.object(
                last_known_db,
                "_read_history",
                return_value=[historical],
            ):
                stats = last_known_db.backfill_casas_bahia_last_known_fields(
                    [row],
                    active_retailer="casas_bahia",
                    product_line_value=line,
                )
            self.assertEqual(row["sku"], sku)
            self.assertEqual(row["sku_short_version"], sku)
            self.assertEqual(row[other_field], "")
            self.assertEqual(stats["recovered_fields"], {"sku": 1})

    def test_ldy_valid_current_sku_skips_history_query(self):
        row = self._row(
            line="LDY",
            retailer_sku_name="Lavadora Philco PLR14A 14kg",
            sku="PLR14A",
            sku_short_version="PLR14A",
            ldy_loading_type="Top load",
            ldy_color="Preto",
            ldy_capacity="14kg",
        )
        read_history = Mock()
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            read_history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="LDY",
            )
        read_history.assert_not_called()
        self.assertEqual(stats["eligible_rows"], 0)

    def test_ref_valid_complete_row_skips_history_query(self):
        row = self._row(
            line="REF",
            retailer_sku_name="Geladeira Consul CRM44MK 377L",
            sku="CRM44MK",
            sku_short_version="CRM44MK",
            ref_refrigerator_type="Duplex",
            ref_capacity="377L",
        )
        read_history = Mock()
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            read_history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="REF",
            )
        read_history.assert_not_called()
        self.assertEqual(stats["eligible_rows"], 0)

    def test_ldy_current_item_url_mismatch_never_queries_history(self):
        row = self._row(
            line="LDY",
            item="123",
            retailer_sku_name="Lavadora sem modelo explicito",
            sku="",
            ldy_loading_type="Top load",
            ldy_color="Branco",
            ldy_capacity="13kg",
        )
        row["product_url"] = "https://www.casasbahia.com.br/produto/p/999"
        read_history = Mock()
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            read_history,
        ):
            stats = last_known_db.backfill_casas_bahia_last_known_fields(
                [row],
                active_retailer="casas_bahia",
                product_line_value="LDY",
            )
        read_history.assert_not_called()
        self.assertEqual(stats["eligible_rows"], 0)

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
