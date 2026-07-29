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

    def test_historical_sku_values_are_trusted_and_survive_output(self):
        values = (
            "Not tv",
            "Conversor Digital",
            "HDR10",
            "65 4K Led",
            "Smart TV Samsung 55 polegadas",
            "abc123",
        )
        for value in values:
            with self.subTest(value=value):
                row = self._row("TV", sku="")
                with self._enabled(), patch.object(
                    last_known_db,
                    "_read_history",
                    return_value=[self._history(row, sku=value)],
                ):
                    stats = last_known_db.backfill_magalu_last_known_fields(
                        [row],
                        active_retailer="magalu",
                        product_line_value="TV",
                    )
                self.assertEqual(row["sku"], value)
                self.assertTrue(
                    last_known_db.recovered_from_last_known_db(row, "sku")
                )
                self.assertEqual(stats["recovered_fields"], {"sku": 1})
                with patch.dict(
                    os.environ,
                    {
                        "SEDA_PRODUCT_LINE": "TV",
                        "SEDA_ACTIVE_RETAILER": "magalu",
                    },
                    clear=False,
                ):
                    output = step15_final_output._format_row(
                        row,
                        datetime(2026, 7, 29, 12, 0, 0),
                    )
                self.assertEqual(output["sku"], value)

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


    def test_newest_nonblank_colour_wins_without_semantic_validation(self):
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
        self.assertEqual(
            row["ldy_color"],
            "Conforme disponibilidade em estoque",
        )
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

    def test_retailer_and_identity_guards_skip_database(self):
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
                "identity_conflict",
                self._row(
                    parse_status=(
                        "detail_item_graphql+"
                        "zenrows_pdp:identity_conflict"
                    )
                ),
                "magalu",
            ),
            (
                "identity_mismatch",
                self._row(
                    parse_status=(
                        "detail_item_graphql+"
                        "zenrows_pdp:identity_mismatch"
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

    def test_blank_field_is_recovered_regardless_of_graphql_status(self):
        statuses = (
            "",
            "detail_item_graphql",
            "detail_blank_retry",
            "detail_graphql_failed:item_query_failed",
            (
                "detail_graphql_failed:item_query_failed+"
                "detail_item_graphql+detail_blank_retry"
            ),
        )
        for status in statuses:
            with self.subTest(status=status):
                row = self._row(
                    "TV",
                    parse_status=status,
                    screen_size="",
                )
                with self._enabled(), patch.object(
                    last_known_db,
                    "_read_history",
                    return_value=[
                        self._history(row, screen_size="55 inches")
                    ],
                ):
                    stats = last_known_db.backfill_magalu_last_known_fields(
                        [row],
                        active_retailer="magalu",
                        product_line_value="TV",
                    )
                self.assertEqual(row["screen_size"], "55 inches")
                self.assertEqual(
                    stats["recovered_fields"],
                    {"screen_size": 1},
                )

    def test_same_product_name_with_different_item_is_never_used(self):
        row = self._row(
            "TV",
            retailer_sku_name="Mesmo produto",
            screen_size="",
        )
        other = self._history(
            row,
            item="different",
            product_url=(
                "https://www.magazineluiza.com.br/produto/"
                "p/different/ed/teste/"
            ),
            retailer_sku_name="Mesmo produto",
            screen_size="65 inches",
        )
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[other],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )
        self.assertEqual(row["screen_size"], "")
        self.assertEqual(stats["recovered_fields"], {})

    def test_historical_item_url_mismatch_is_never_used(self):
        row = self._row("TV", screen_size="")
        mismatched = self._history(
            row,
            product_url=(
                "https://www.magazineluiza.com.br/produto/"
                "p/different/ed/teste/"
            ),
            screen_size="65 inches",
        )
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[mismatched],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )
        self.assertEqual(row["screen_size"], "")
        self.assertEqual(stats["recovered_fields"], {})

    def test_field_by_field_scan_uses_newest_raw_loading_value(self):
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
        self.assertEqual(row["ldy_loading_type"], "Autom\u00e1tica")
        self.assertEqual(row["ldy_capacity"], "13kg")
        self.assertEqual(stats["recovered_fields"], {"ldy_loading_type": 1})

    def test_all_product_lines_trust_latest_stored_field_values(self):
        cases = {
            "TV": {
                "sku": "Not tv",
                "screen_size": "Bivolt",
                "estimated_annual_electricity_use": (
                    "130W, Entradas 3 HDMI"
                ),
                "model_year": "620",
            },
            "REF": {
                "sku": "Smart Refrigerator descriptive value",
                "ref_refrigerator_type": "Duplex",
                "ref_capacity": "44 latas",
            },
            "LDY": {
                "sku": "13kg",
                "ldy_loading_type": "Top load,Front load",
                "ldy_color": "Conforme disponibilidade em estoque",
                "ldy_capacity": "1",
            },
        }
        for line, stored_values in cases.items():
            with self.subTest(line=line):
                row = self._row(line, sku="", ldy_color="")
                history = self._history(row, **stored_values)
                with self._enabled(), patch.object(
                    last_known_db,
                    "_read_history",
                    return_value=[history],
                ):
                    stats = last_known_db.backfill_magalu_last_known_fields(
                        [row],
                        active_retailer="magalu",
                        product_line_value=line,
                    )
                for field, value in stored_values.items():
                    self.assertEqual(row[field], value)
                    self.assertTrue(
                        last_known_db.recovered_from_last_known_db(
                            row,
                            field,
                        )
                    )
                self.assertEqual(
                    stats["recovered_fields"],
                    {field: 1 for field in sorted(stored_values)},
                )

    def test_each_field_scans_history_independently(self):
        row = self._row(
            "TV",
            screen_size="",
            estimated_annual_electricity_use="",
            model_year="2025",
        )
        newest = self._history(
            row,
            screen_size="",
            estimated_annual_electricity_use="Newest raw energy",
            model_year="2024",
        )
        older = self._history(
            row,
            screen_size="Older raw screen",
            estimated_annual_electricity_use="Older raw energy",
            model_year="2023",
        )
        with self._enabled(), patch.object(
            last_known_db,
            "_read_history",
            return_value=[newest, older],
        ):
            stats = last_known_db.backfill_magalu_last_known_fields(
                [row],
                active_retailer="magalu",
                product_line_value="TV",
            )
        self.assertEqual(row["screen_size"], "Older raw screen")
        self.assertEqual(
            row["estimated_annual_electricity_use"],
            "Newest raw energy",
        )
        self.assertEqual(row["model_year"], "2025")
        self.assertEqual(
            stats["recovered_fields"],
            {
                "estimated_annual_electricity_use": 1,
                "screen_size": 1,
            },
        )

    def test_all_current_nonblank_contract_fields_are_preserved(self):
        cases = {
            "TV": {
                "sku": "HDR10",
                "screen_size": "55 inches",
                "estimated_annual_electricity_use": "1",
                "model_year": "2025",
            },
            "REF": {
                "sku": "CRM44",
                "ref_refrigerator_type": "Duplex",
                "ref_capacity": "377L",
            },
            "LDY": {
                "sku": "MF200D130WB/WK-02",
                "ldy_loading_type": "Top load",
                "ldy_color": "Branco",
                "ldy_capacity": "13kg",
            },
        }
        for line, current_values in cases.items():
            with self.subTest(line=line):
                row = self._row(line, **current_values)
                with self._enabled(), patch.object(
                    last_known_db,
                    "_read_history",
                ) as read_history:
                    stats = last_known_db.backfill_magalu_last_known_fields(
                        [row],
                        active_retailer="magalu",
                        product_line_value=line,
                    )
                read_history.assert_not_called()
                self.assertEqual(stats["eligible_rows"], 0)
                for field, value in current_values.items():
                    self.assertEqual(row[field], value)

        for sku in (
            "Not tv",
            "Conversor Digital",
            "HDR10",
            "65 4K Led",
        ):
            with self.subTest(current_sku=sku):
                self.assertFalse(
                    last_known_db._current_sku_needs_recovery(
                        self._row("TV", sku=sku)
                    )
                )

    def test_appliance_spec_sku_that_would_save_blank_is_recovered(self):
        for line in ("REF", "LDY"):
            with self.subTest(line=line):
                row = self._row(line, sku="13kg")
                expected = f"Manual {line} SKU"
                with self._enabled(), patch.object(
                    last_known_db,
                    "_read_history",
                    return_value=[self._history(row, sku=expected)],
                ):
                    stats = last_known_db.backfill_magalu_last_known_fields(
                        [row],
                        active_retailer="magalu",
                        product_line_value=line,
                    )
                self.assertEqual(row["sku"], expected)
                self.assertEqual(stats["recovered_fields"], {"sku": 1})
                with patch.dict(
                    os.environ,
                    {
                        "SEDA_PRODUCT_LINE": line,
                        "SEDA_ACTIVE_RETAILER": "magalu",
                    },
                    clear=False,
                ):
                    output = step15_final_output._format_row(
                        row,
                        datetime(2026, 7, 29, 12, 0, 0),
                    )
                self.assertEqual(output["sku"], expected)

    def test_latest_nonblank_value_skips_only_sql_null_and_empty(self):
        candidates = [
            {"ldy_capacity": None},
            {"ldy_capacity": "   "},
            {"ldy_capacity": " NULL "},
            {"ldy_capacity": 0},
            {"ldy_capacity": "13kg"},
        ]
        self.assertEqual(
            last_known_db._latest_stored_value(
                candidates,
                "ldy_capacity",
            ),
            "NULL",
        )
        self.assertEqual(
            last_known_db._latest_stored_value(
                [
                    {"ldy_capacity": None},
                    {"ldy_capacity": ""},
                    {"ldy_capacity": "   "},
                ],
                "ldy_capacity",
            ),
            "",
        )
        self.assertEqual(
            last_known_db._latest_stored_value(
                [{"ldy_capacity": None}, {"ldy_capacity": 0}],
                "ldy_capacity",
            ),
            "0",
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
        self.assertNotIn('"retailer_sku_name"', select_sql)
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
        row["parse_status"] = "detail_item_graphql"
        history = self._history(row, screen_size="55")
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
            self.assertEqual(saved[0]["screen_size"], "55")

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
        self.assertEqual(inserted_values[0][screen_index], "55")

    def test_recovered_ldy_sku_and_colour_reach_csv_and_db_insert_values(self):
        row = {column: "" for column in OUTPUT_COLUMNS}
        row.update(self._row("LDY", sku="", ldy_color=""))
        row["parse_status"] = "detail_item_graphql"
        history = self._history(
            row,
            sku="13kg",
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
            self.assertEqual(saved[0]["sku"], "13kg")
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
            "13kg",
        )
        self.assertEqual(
            inserted_values[0][colour_index],
            "Azul marinho",
        )


    def test_recovered_ref_and_ldy_raw_values_reach_csv_and_db(self):
        cases = (
            (
                "REF",
                {
                    "ref_refrigerator_type": "Duplex",
                    "ref_capacity": "44 latas",
                },
            ),
            (
                "LDY",
                {
                    "ldy_loading_type": "Autom\u00e1tica",
                    "ldy_color": "Conforme disponibilidade em estoque",
                    "ldy_capacity": "1",
                },
            ),
        )
        for line, stored_values in cases:
            with self.subTest(line=line):
                row = {column: "" for column in OUTPUT_COLUMNS}
                row.update(self._row(line, sku="current-sku", ldy_color=""))
                row["parse_status"] = "detail_item_graphql"
                history = self._history(row, **stored_values)
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
                        "SEDA_PRODUCT_LINE": line,
                        "SEDA_ACTIVE_RETAILER": "magalu",
                        "SEDA_FINAL_OUTPUT_CSV": str(final_csv),
                        "SEDA_DB_LOAD_CSV": str(final_csv),
                        "SEDA_MAGALU_LAST_KNOWN_DB_FALLBACK": "1",
                        "SEDA_DB_TRUNCATE_BEFORE_LOAD": "0",
                        "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "0",
                        "SEDA_TRANSLATE_OUTPUT": "1",
                    }
                    with patch.dict(
                        os.environ,
                        env,
                        clear=False,
                    ), patch.object(
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
                    for field, value in stored_values.items():
                        self.assertEqual(saved[0][field], value)

                    with patch.dict(
                        os.environ,
                        env,
                        clear=False,
                    ), patch.object(
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
                inserted = execute_values.call_args.args[2][0]
                for field, value in stored_values.items():
                    self.assertEqual(inserted[columns.index(field)], value)


if __name__ == "__main__":
    unittest.main()
