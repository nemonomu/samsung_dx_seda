import io
import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from seda.magalu import last_known_db
from seda.magalu.recovery_contract import (
    MAGALU_LAST_KNOWN_DB_FIELD_MAP,
    MAGALU_RECOVERY_FIELD_MAP,
    last_known_fields,
)
from seda import step14_db_load, step15_final_output
from seda.step00_config import OUTPUT_COLUMNS


class MagaluLastKnownDbTests(unittest.TestCase):
    def _row(self, line="TV", **values):
        item = values.pop("item", "abc123")
        row = {
            "retailer": "Magalu",
            "product_line": line,
            "item": item,
            "product_url": (
                "https://www.magazineluiza.com.br/produto/"
                f"p/{item}/ed/teste/?seller_id=magalu"
            ),
            "retailer_sku_name": "Produto de teste",
            "parse_status": (
                "listing+detail_graphql_failed:item_query_failed+"
                "zenrows_pdp_html+detail_graphql_failed:item_query_failed+"
                "detail_blank_retry_failed"
            ),
            "sku": "current-sku",
            "ldy_color": "Branco",
        }
        for field in last_known_fields(line):
            row.setdefault(field, "")
        row.update(values)
        return row

    def _history(self, row, **values):
        historical = {
            "item": (
                row["item"]
                or last_known_db._url_item_key(row["product_url"])
            ),
            "product_url": row["product_url"],
            "retailer_sku_name": row["retailer_sku_name"],
            "crawl_strdatetime": "2026-07-26 12:00:00",
            "batch_id": "m_20260726_120000",
        }
        historical.update(
            {
                field: ""
                for field in last_known_fields(row["product_line"])
            }
        )
        historical.update(values)
        return historical

    def _enabled(self):
        return patch.dict(
            os.environ,
            {"SEDA_MAGALU_LAST_KNOWN_DB_FALLBACK": "1"},
            clear=False,
        )

    def test_zenrows_contract_stays_at_exactly_seven_semantic_fields(self):
        self.assertEqual(
            MAGALU_RECOVERY_FIELD_MAP,
            {
                "TV": (
                    "screen_size",
                    "estimated_annual_electricity_use",
                    "model_year",
                ),
                "REF": ("ref_refrigerator_type", "ref_capacity"),
                "LDY": ("ldy_loading_type", "ldy_capacity"),
            },
        )
        flattened = {
            field
            for fields in MAGALU_RECOVERY_FIELD_MAP.values()
            for field in fields
        }
        self.assertEqual(len(flattened), 7)
        self.assertNotIn("sku", flattened)
        self.assertNotIn("ldy_color", flattened)

    def test_last_known_db_contract_adds_sku_and_ldy_colour_only(self):
        self.assertEqual(
            MAGALU_LAST_KNOWN_DB_FIELD_MAP,
            {
                "TV": (
                    "sku",
                    "screen_size",
                    "estimated_annual_electricity_use",
                    "model_year",
                ),
                "REF": (
                    "sku",
                    "ref_refrigerator_type",
                    "ref_capacity",
                ),
                "LDY": (
                    "sku",
                    "ldy_loading_type",
                    "ldy_color",
                    "ldy_capacity",
                ),
            },
        )
        flattened = {
            field
            for fields in MAGALU_LAST_KNOWN_DB_FIELD_MAP.values()
            for field in fields
        }
        self.assertEqual(
            flattened,
            {
                "sku",
                "screen_size",
                "estimated_annual_electricity_use",
                "model_year",
                "ref_refrigerator_type",
                "ref_capacity",
                "ldy_loading_type",
                "ldy_color",
                "ldy_capacity",
            },
        )

    def test_fills_only_missing_fields_and_preserves_current_sku_and_colour(self):
        row = self._row(screen_size="55 inches")
        history = self._history(
            row,
            screen_size="65 inches",
            estimated_annual_electricity_use="1",
            model_year="2025",
            sku="historical-sku",
            ldy_color="Preto",
        )
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[history],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )

        self.assertEqual(row["screen_size"], "55 inches")
        self.assertEqual(row["estimated_annual_electricity_use"], "1")
        self.assertEqual(row["model_year"], "2025")
        self.assertEqual(row["sku"], "current-sku")
        self.assertEqual(row["ldy_color"], "Branco")
        self.assertEqual(stats["recovered_rows"], 1)
        self.assertEqual(
            stats["recovered_fields"],
            {
                "estimated_annual_electricity_use": 1,
                "model_year": 1,
            },
        )

    def test_item_placeholder_sku_is_replaced_for_each_product_line(self):
        for line, historical_sku in (
            ("TV", "QN55Q70DAGXZD"),
            ("REF", "CRM44AB"),
            ("LDY", "MF200D130WB/WK-02"),
        ):
            with self.subTest(line=line):
                row = self._row(line, sku="abc123")
                with self._enabled(), patch.object(
                    last_known_db,
                    "_read_history",
                    return_value=[
                        self._history(row, sku=historical_sku)
                    ],
                ):
                    stats = last_known_db.backfill_magalu_last_known_fields(
                        [row],
                        active_retailer="magalu",
                        product_line_value=line,
                    )
                self.assertEqual(row["sku"], historical_sku)
                self.assertEqual(stats["recovered_fields"], {"sku": 1})

    def test_trusted_current_tv_reference_is_not_replaced(self):
        row = self._row(
            "TV",
            sku="abc123",
            parse_status=(
                "listing+detail_graphql_failed:item_query_failed+"
                "sku_factsheet_reference_recovered+"
                "detail_blank_retry_failed"
            ),
        )
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[self._history(row, sku="QN55Q70DAGXZD")],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )
        self.assertEqual(row["sku"], "abc123")
        self.assertNotIn("sku", stats["recovered_fields"])

    def test_historical_sku_rejects_identity_and_known_noise(self):
        row = self._row("TV", sku="")
        for value in (
            "abc123",
            "Bivolt",
            "220V",
            "Smart TV Samsung 55 polegadas",
            "https://example.com/model",
            "-/-",
            "N/A",
            "Nao informado",
            "Sem referencia",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    last_known_db._validated_history_value(
                        "sku",
                        value,
                        current_row=row,
                        history_row=self._history(row),
                    ),
                    "",
                )
        self.assertEqual(
            last_known_db._validated_history_value(
                "sku",
                "ABC/123-X",
                current_row=row,
                history_row=self._history(row),
            ),
            "ABC/123-X",
        )

    def test_blank_ldy_colour_uses_latest_plausible_historical_value(self):
        row = self._row("LDY", ldy_color="")
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[self._history(row, ldy_color="Azul marinho")],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="LDY",
            )
        self.assertEqual(row["ldy_color"], "Azul marinho")
        self.assertEqual(stats["recovered_fields"], {"ldy_color": 1})

    def test_historical_ldy_colour_rejects_non_colour_noise(self):
        row = self._row("LDY", ldy_color="")
        for value in (
            "Automatica",
            "Roupa",
            "Front load",
            "Superior",
            "13kg",
            "Bivolt",
            "220V",
            "Nao informado",
            "N/A",
            "Conforme disponibilidade em estoque",
            "Preto conforme estoque",
            "Black Friday",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    last_known_db._validated_history_value(
                        "ldy_color",
                        value,
                        current_row=row,
                    ),
                    "",
                )
        for value in (
            "Branco",
            "Aco Inox",
            "Lilas",
            "Preto/Grafite",
            "Platinum",
            "Verde",
            "Chumbo",
            "Cinza Onix",
            "BEGE",
            "Amarelo",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    last_known_db._validated_history_value(
                        "ldy_color",
                        value,
                        current_row=row,
                    ),
                    value,
                )

    def test_invalid_newest_colour_falls_back_to_older_valid_colour(self):
        row = self._row("LDY", ldy_color="")
        newest = self._history(
            row,
            ldy_color="Conforme disponibilidade em estoque",
        )
        older = self._history(row, ldy_color="Cinza Onix")
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[newest, older],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="LDY",
            )
        self.assertEqual(row["ldy_color"], "Cinza Onix")
        self.assertEqual(stats["recovered_fields"], {"ldy_color": 1})

    def test_partial_pdp_success_still_allows_only_remaining_blank_fields(self):
        row = self._row(screen_size="55 inches")
        self.assertIn("zenrows_pdp_html", row["parse_status"])
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[self._history(row, model_year="2024")],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )
        self.assertEqual(row["screen_size"], "55 inches")
        self.assertEqual(row["model_year"], "2024")
        self.assertEqual(stats["recovered_fields"], {"model_year": 1})

    def test_success_identity_and_retailer_guards_skip_database(self):
        cases = (
            ("active_casas", self._row(), "casas_bahia"),
            (
                "row_casas",
                self._row(
                    retailer="Casas Bahia",
                    product_url="https://www.casasbahia.com.br/produto/p/abc123",
                ),
                "magalu",
            ),
            (
                "missing_final_failure",
                self._row(
                    parse_status="detail_graphql_failed:item_query_failed"
                ),
                "magalu",
            ),
            (
                "successful_retry",
                self._row(
                    parse_status=(
                        "detail_graphql_failed:item_query_failed+"
                        "detail_item_graphql+detail_blank_retry"
                    )
                ),
                "magalu",
            ),
            (
                "identity_conflict",
                self._row(
                    parse_status=(
                        "detail_graphql_failed:item_query_failed+"
                        "zenrows_pdp:identity_conflict+"
                        "detail_blank_retry_failed"
                    )
                ),
                "magalu",
            ),
            (
                "item_url_mismatch",
                self._row(
                    product_url=(
                        "https://www.magazineluiza.com.br/produto/"
                        "p/different/ed/teste/"
                    )
                ),
                "magalu",
            ),
        )
        for name, row, active in cases:
            with self.subTest(name=name), self._enabled(), patch.object(
                last_known_db,
                "_read_history",
            ) as read_history:
                last_known_db.backfill_magalu_last_known_fields(
                    [row],
                    active_retailer=active,
                    product_line_value="TV",
                )
            read_history.assert_not_called()

    def test_field_by_field_scan_skips_invalid_newest_loading_value(self):
        row = self._row("LDY", ldy_capacity="13kg")
        newest = self._history(
            row,
            ldy_loading_type="Autom\u00e1tica",
            ldy_capacity="17kg",
        )
        older = self._history(
            row,
            ldy_loading_type="Superior",
            ldy_capacity="11kg",
        )
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[newest, older],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="LDY",
            )
        self.assertEqual(row["ldy_loading_type"], "Top load")
        self.assertEqual(row["ldy_capacity"], "13kg")
        self.assertEqual(stats["recovered_fields"], {"ldy_loading_type": 1})

    def test_conflicting_historical_loading_directions_are_rejected(self):
        row = self._row("LDY")
        self.assertEqual(
            last_known_db._validated_history_value(
                "ldy_loading_type",
                "Top load,Front load",
                current_row=row,
            ),
            "",
        )

    def test_capacity_and_other_history_validators_block_known_noise(self):
        ref_row = self._row("REF")
        self.assertEqual(
            last_known_db._validated_history_value(
                "ref_capacity",
                "44 latas",
                current_row=ref_row,
            ),
            "",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ref_capacity",
                "2 litros reservat\u00f3rio de \u00e1gua",
                current_row=ref_row,
            ),
            "",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ref_capacity",
                "500 ml",
                current_row=ref_row,
            ),
            "",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ref_capacity",
                "at\u00e9 260 litros,260 litros",
                current_row=ref_row,
            ),
            "",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ref_capacity",
                "De 401 a 500 litros",
                current_row=ref_row,
            ),
            "",
        )
        approximate = "0,95 p\u00e9s c\u00fabicos (aprox. 26,9 litros)"
        self.assertEqual(
            last_known_db._validated_history_value(
                "ref_capacity",
                approximate,
                current_row=ref_row,
            ),
            approximate,
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ref_refrigerator_type",
                "Duplex",
                current_row=ref_row,
            ),
            "Freezer-on-Top",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ref_refrigerator_type",
                "Two Door",
                current_row=ref_row,
            ),
            "Two Door",
        )

        tv_row = self._row("TV")
        self.assertEqual(
            last_known_db._validated_history_value(
                "estimated_annual_electricity_use",
                "1",
                current_row=tv_row,
            ),
            "1",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "estimated_annual_electricity_use",
                "Bivolt",
                current_row=tv_row,
            ),
            "",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "estimated_annual_electricity_use",
                "130W, Entradas 3 HDMI",
                current_row=tv_row,
            ),
            "130W",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "model_year",
                "620",
                current_row=tv_row,
            ),
            "",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "screen_size",
                "Bivolt",
                current_row=tv_row,
            ),
            "",
        )

    def test_compact_ldy_liters_require_current_mini_washer_context(self):
        mini = self._row(
            "LDY",
            retailer_sku_name="Mini M\u00e1quina de Lavar 6,5L Port\u00e1til",
        )
        regular = self._row(
            "LDY",
            retailer_sku_name="Lavadora Autom\u00e1tica Midea",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ldy_capacity",
                "6,5L",
                current_row=mini,
            ),
            "6,5L",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ldy_capacity",
                "6,5L",
                current_row=regular,
            ),
            "",
        )

    def test_historical_ldy_compounds_and_known_bad_one_are_rejected(self):
        row = self._row("LDY")
        for value in (
            "10kg,11kg",
            "14,De 11 a 15kg",
            "9kg,8kg a 9kg",
            "11 kg,De 11 a 15kg",
            "1",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    last_known_db._validated_history_value(
                        "ldy_capacity",
                        value,
                        current_row=row,
                    ),
                    "",
                )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ldy_capacity",
                "6,5kg",
                current_row=row,
            ),
            "6,5kg",
        )

    def test_historical_mini_title_can_validate_liters_after_title_change(self):
        current = self._row(
            "LDY",
            retailer_sku_name="Lavadora Port\u00e1til",
        )
        historical = self._history(
            current,
            retailer_sku_name="Mini M\u00e1quina de Lavar 6,5L",
        )
        self.assertEqual(
            last_known_db._validated_history_value(
                "ldy_capacity",
                "6,5L",
                current_row=current,
                history_row=historical,
            ),
            "6,5L",
        )

    def test_latest_item_history_wins_even_when_seller_url_changed(self):
        row = self._row()
        exact = self._history(row, screen_size="55 inches", model_year="")
        other_url = self._history(
            row,
            product_url=(
                "https://www.magazineluiza.com.br/outro/"
                "p/abc123/ed/teste/?seller_id=other"
            ),
            screen_size="65 inches",
            model_year="2025",
        )
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[other_url, exact],
        ):
            last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )
        self.assertEqual(row["screen_size"], "65 inches")
        self.assertEqual(row["model_year"], "2025")

    def test_url_identity_is_secondary_when_current_item_is_blank(self):
        row = self._row()
        row["item"] = ""
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[self._history(row, model_year="2025")],
        ) as read_history:
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )
        self.assertEqual(row["model_year"], "2025")
        self.assertEqual(read_history.call_args.args[0], ["abc123"])
        self.assertEqual(stats["recovered_fields"], {"model_year": 1})

    def test_read_history_is_select_only_and_always_rolls_back_and_closes(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                "abc123",
                "https://www.magazineluiza.com.br/produto/p/abc123/",
                "Smart TV",
                "QN55Q70DAGXZD",
                "55 inches",
                "1",
                "2025",
                "2026-07-26 12:00:00",
                "m_20260726_120000",
            )
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with patch.object(
            last_known_db,
            "db_connect",
            return_value=connection,
        ), patch.object(
            last_known_db,
            "output_table",
            return_value="public.tv_retail_com_seda",
        ):
            rows = last_known_db._read_history(
                ["abc123"],
                "TV",
                last_known_fields("TV"),
            )

        self.assertEqual(rows[0]["sku"], "QN55Q70DAGXZD")
        self.assertEqual(rows[0]["model_year"], "2025")
        self.assertEqual(cursor.execute.call_count, 3)
        self.assertEqual(
            cursor.execute.call_args_list[0].args[0],
            "SET TRANSACTION READ ONLY",
        )
        self.assertEqual(
            cursor.execute.call_args_list[1].args[0],
            "SET LOCAL statement_timeout = 15000",
        )
        select_sql, params = cursor.execute.call_args_list[2].args
        self.assertIn('FROM "public"."tv_retail_com_seda"', select_sql)
        self.assertIn("ROW_NUMBER() OVER", select_sql)
        self.assertNotRegex(
            select_sql.upper(),
            re.compile(r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|CREATE|ALTER|DROP)\b"),
        )
        self.assertEqual(
            params,
            ("magalu", "magazineluiza", "TV", ["abc123"], 30),
        )
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()
        cursor.close.assert_called_once_with()

    def test_database_error_is_fail_open_and_does_not_print_exception_text(self):
        row = self._row()
        cursor = MagicMock()
        cursor.execute.side_effect = [
            None,
            None,
            RuntimeError("must-not-be-logged"),
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        output = io.StringIO()
        with self._enabled(), patch.object(
            last_known_db,
            "db_connect",
            return_value=connection,
        ), patch.object(
            last_known_db,
            "output_table",
            return_value="tv_retail_com_seda",
        ), patch("sys.stdout", output):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )

        self.assertEqual(row["screen_size"], "")
        self.assertEqual(stats["error"], "RuntimeError")
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("must-not-be-logged", output.getvalue())
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_invalid_table_identifier_fails_open_before_database_connect(self):
        row = self._row()
        with self._enabled(), patch.object(
            last_known_db,
            "output_table",
            return_value="tv_retail_com_seda; DROP TABLE x",
        ), patch.object(last_known_db, "db_connect") as db_connect:
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )
        self.assertEqual(stats["error"], "RuntimeError")
        db_connect.assert_not_called()

    def test_step15_formats_recovered_values_and_passes_stats_to_manifest(self):
        row = self._row()
        stats = {
            **last_known_db._empty_stats(),
            "enabled": True,
            "eligible_rows": 1,
            "queried_items": 1,
            "history_rows": 1,
            "recovered_rows": 1,
            "recovered_fields": {"screen_size": 1},
        }

        def recover(rows, **_kwargs):
            rows[0]["screen_size"] = "55 inches"
            return stats

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "TV",
                "SEDA_ACTIVE_RETAILER": "magalu",
            },
            clear=False,
        ), patch.object(
            step15_final_output,
            "_source_path",
            return_value=Path(directory) / "source.csv",
        ), patch.object(
            step15_final_output,
            "read_csv",
            return_value=[row],
        ), patch.object(
            step15_final_output,
            "_validate_source_context",
        ), patch.object(
            step15_final_output,
            "_validate_internal_source_schema",
        ), patch.object(
            step15_final_output,
            "backfill_magalu_last_known_fields",
            side_effect=recover,
        ), patch.object(
            step15_final_output,
            "_run_datetime",
            return_value=datetime(2026, 7, 27, 12, 0, 0),
        ), patch.object(
            step15_final_output,
            "write_csv",
        ) as write_csv, patch.object(
            step15_final_output,
            "_write_manifest",
        ) as write_manifest:
            step15_final_output._main(Path(directory))

        output_rows = write_csv.call_args.args[1]
        self.assertEqual(output_rows[0]["screen_size"], "55 inches")
        self.assertEqual(
            write_manifest.call_args.kwargs["magalu_last_known_db"],
            stats,
        )

    def test_step15_casas_path_does_not_call_magalu_recovery(self):
        row = self._row(
            retailer="Casas Bahia",
            product_url="https://www.casasbahia.com.br/produto/p/abc123",
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "TV",
                "SEDA_ACTIVE_RETAILER": "casas_bahia",
            },
            clear=False,
        ), patch.object(
            step15_final_output,
            "_source_path",
            return_value=Path(directory) / "source.csv",
        ), patch.object(
            step15_final_output,
            "read_csv",
            return_value=[row],
        ), patch.object(
            step15_final_output,
            "_validate_source_context",
        ), patch.object(
            step15_final_output,
            "_validate_internal_source_schema",
        ), patch.object(
            step15_final_output,
            "backfill_magalu_last_known_fields",
        ) as recover, patch.object(
            step15_final_output,
            "write_csv",
        ), patch.object(
            step15_final_output,
            "_write_manifest",
        ) as write_manifest:
            step15_final_output._main(Path(directory))

        recover.assert_not_called()
        self.assertIsNone(
            write_manifest.call_args.kwargs["magalu_last_known_db"]
        )

    def test_recovered_value_reaches_real_final_csv_and_db_insert_values(self):
        row = {column: "" for column in OUTPUT_COLUMNS}
        row.update(self._row())
        history = self._history(row, screen_size="55 inches")
        connection = MagicMock()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        connection.cursor.return_value = cursor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final_csv = root / "output" / "final_output.csv"
            env = {
                "SEDA_PRODUCT_LINE": "TV",
                "SEDA_ACTIVE_RETAILER": "magalu",
                "SEDA_FINAL_OUTPUT_CSV": str(final_csv),
                "SEDA_DB_LOAD_CSV": str(final_csv),
                "SEDA_MAGALU_LAST_KNOWN_DB_FALLBACK": "1",
                "SEDA_DB_TRUNCATE_BEFORE_LOAD": "0",
                "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "0",
                "SEDA_TRANSLATE_OUTPUT": "1",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                step15_final_output,
                "_source_path",
                return_value=root / "source.csv",
            ), patch.object(
                step15_final_output,
                "read_csv",
                return_value=[row],
            ), patch.object(
                last_known_db,
                "_read_history",
                return_value=[history],
            ):
                step15_final_output._main(root)

            saved = step15_final_output.read_csv(final_csv)
            self.assertEqual(saved[0]["screen_size"], "55 inches")

            with patch.dict(os.environ, env, clear=False), patch.object(
                step14_db_load,
                "db_connect",
                return_value=connection,
            ), patch.object(
                step14_db_load,
                "output_table",
                return_value="tv_retail_com_seda",
            ), patch.object(
                step14_db_load,
                "write_json",
            ), patch(
                "psycopg2.extras.execute_values",
            ) as execute_values:
                step14_db_load._main(root)

        columns = list(saved[0].keys())
        screen_index = columns.index("screen_size")
        inserted_values = execute_values.call_args.args[2]
        self.assertEqual(inserted_values[0][screen_index], "55 inches")

    def test_recovered_ldy_sku_and_colour_reach_csv_and_db_insert_values(self):
        row = {column: "" for column in OUTPUT_COLUMNS}
        row.update(self._row("LDY", sku="", ldy_color=""))
        history = self._history(
            row,
            sku="MF200D130WB/WK-02",
            ldy_color="Azul marinho",
        )
        connection = MagicMock()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        connection.cursor.return_value = cursor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final_csv = root / "output" / "final_output.csv"
            env = {
                "SEDA_PRODUCT_LINE": "LDY",
                "SEDA_ACTIVE_RETAILER": "magalu",
                "SEDA_FINAL_OUTPUT_CSV": str(final_csv),
                "SEDA_DB_LOAD_CSV": str(final_csv),
                "SEDA_MAGALU_LAST_KNOWN_DB_FALLBACK": "1",
                "SEDA_DB_TRUNCATE_BEFORE_LOAD": "0",
                "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "0",
                "SEDA_TRANSLATE_OUTPUT": "1",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                step15_final_output,
                "_source_path",
                return_value=root / "source.csv",
            ), patch.object(
                step15_final_output,
                "read_csv",
                return_value=[row],
            ), patch.object(
                last_known_db,
                "_read_history",
                return_value=[history],
            ):
                step15_final_output._main(root)

            saved = step15_final_output.read_csv(final_csv)
            self.assertEqual(saved[0]["sku"], "MF200D130WB/WK-02")
            self.assertEqual(saved[0]["ldy_color"], "Azul marinho")

            with patch.dict(os.environ, env, clear=False), patch.object(
                step14_db_load,
                "db_connect",
                return_value=connection,
            ), patch.object(
                step14_db_load,
                "output_table",
                return_value="tv_retail_com_seda",
            ), patch.object(
                step14_db_load,
                "write_json",
            ), patch(
                "psycopg2.extras.execute_values",
            ) as execute_values:
                step14_db_load._main(root)

        columns = list(saved[0].keys())
        sku_index = columns.index("sku")
        colour_index = columns.index("ldy_color")
        inserted_values = execute_values.call_args.args[2]
        self.assertEqual(
            inserted_values[0][sku_index],
            "MF200D130WB/WK-02",
        )
        self.assertEqual(
            inserted_values[0][colour_index],
            "Azul marinho",
        )


if __name__ == "__main__":
    unittest.main()
