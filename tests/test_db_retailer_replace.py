import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from seda import step14_db_load


class DbRetailerReplaceTests(unittest.TestCase):
    def _row(self, account_name="CasasBahia"):
        row = {column: "" for column in step14_db_load.final_output_columns("TV")}
        row.update(
            {
                "country": "Brazil",
                "product": "TV",
                "item": "fixture",
                "account_name": account_name,
                "page_type": "main",
                "retailer_sku_name": "Smart TV fixture",
                "product_url": "https://www.casasbahia.com.br/produto/p/123",
            }
        )
        return row

    def _db(self):
        cursor = MagicMock()
        cursor.rowcount = 7
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        connection.cursor.return_value = cursor
        return connection, cursor

    def test_replace_deletes_canonical_and_legacy_aliases_before_insert(self):
        connection, cursor = self._db()
        events = []
        cursor.execute.side_effect = lambda *args: events.append("delete")
        env = {
            "SEDA_PRODUCT_LINE": "TV",
            "SEDA_ACTIVE_RETAILER": "casas_bahia",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "0",
            "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "1",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            step14_db_load, "run_root", return_value=Path("C:/fixture")
        ), patch.object(step14_db_load, "read_csv", return_value=[self._row()]), patch.object(
            step14_db_load, "output_table", return_value="dx_seda_tv_retail_com"
        ), patch.object(step14_db_load, "db_connect", return_value=connection), patch.object(
            step14_db_load, "write_json"
        ) as write_json, patch(
            "psycopg2.extras.execute_values",
            side_effect=lambda *args: events.append("insert"),
        ):
            step14_db_load.main()

        self.assertEqual(events, ["delete", "insert"])
        sql, params = cursor.execute.call_args.args
        self.assertIn("regexp_replace", sql)
        self.assertIn('upper(trim(coalesce("product"', sql)
        self.assertEqual(params, ("casasbahia", "casasbahiacombr", "TV"))
        manifest = write_json.call_args.args[1]
        self.assertEqual(manifest["mode"], "replace_retailer")
        self.assertEqual(manifest["deleted"], 7)

    def test_retailer_delete_is_scoped_to_product_in_a_shared_table(self):
        _, cursor = self._db()
        deleted = step14_db_load._delete_retailer_rows(
            cursor,
            "dx_seda_retail_com",
            "magalu",
            "ref",
        )

        self.assertEqual(deleted, 7)
        sql, params = cursor.execute.call_args.args
        self.assertIn('upper(trim(coalesce("product"', sql)
        self.assertEqual(params, ("magalu", "magazineluiza", "REF"))

    def test_retailer_delete_rejects_unknown_product_line_before_sql(self):
        _, cursor = self._db()
        with self.assertRaisesRegex(RuntimeError, "replace_product_line_unknown"):
            step14_db_load._delete_retailer_rows(
                cursor,
                "dx_seda_retail_com",
                "magalu",
                "OTHER",
            )
        cursor.execute.assert_not_called()

    def test_default_append_does_not_delete_or_truncate(self):
        connection, cursor = self._db()
        env = {
            "SEDA_PRODUCT_LINE": "TV",
            "SEDA_ACTIVE_RETAILER": "magalu",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "0",
            "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "0",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            step14_db_load, "run_root", return_value=Path("C:/fixture")
        ), patch.object(step14_db_load, "read_csv", return_value=[self._row("Magalu")]), patch.object(
            step14_db_load, "output_table", return_value="dx_seda_tv_retail_com"
        ), patch.object(step14_db_load, "db_connect", return_value=connection), patch.object(
            step14_db_load, "write_json"
        ) as write_json, patch("psycopg2.extras.execute_values") as execute_values:
            step14_db_load.main()

        cursor.execute.assert_not_called()
        execute_values.assert_called_once()
        manifest = write_json.call_args.args[1]
        self.assertEqual(manifest["mode"], "append")
        self.assertEqual(manifest["deleted"], 0)

    def test_magalu_recovered_sku_and_blank_reach_insert_values_exactly(self):
        connection, cursor = self._db()
        recovered = self._row("Magalu")
        recovered.update(
            {
                "item": "240144500",
                "product_url": "https://www.magazineluiza.com.br/produto/p/240144500/et/tv4k/",
                "sku": "75P7K",
            }
        )
        unresolved = self._row("Magalu")
        unresolved.update(
            {
                "item": "cc2215dbaj",
                "product_url": "https://www.magazineluiza.com.br/produto/p/cc2215dbaj/et/tv4k/",
                "sku": "",
            }
        )
        rows = [recovered, unresolved]
        env = {
            "SEDA_PRODUCT_LINE": "TV",
            "SEDA_ACTIVE_RETAILER": "magalu",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "0",
            "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "0",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            step14_db_load, "run_root", return_value=Path("C:/fixture")
        ), patch.object(step14_db_load, "read_csv", return_value=rows), patch.object(
            step14_db_load, "output_table", return_value="dx_seda_tv_retail_com"
        ), patch.object(step14_db_load, "db_connect", return_value=connection), patch.object(
            step14_db_load, "write_json"
        ), patch("psycopg2.extras.execute_values") as execute_values:
            step14_db_load.main()

        columns = list(rows[0].keys())
        sku_index = columns.index("sku")
        inserted_values = execute_values.call_args.args[2]
        self.assertEqual(inserted_values[0][sku_index], "75P7K")
        self.assertIsNone(inserted_values[1][sku_index])
        cursor.execute.assert_not_called()

    def test_single_retailer_truncate_keeps_existing_table_wide_mode(self):
        connection, cursor = self._db()
        events = []
        cursor.execute.side_effect = lambda *args: events.append("truncate")
        env = {
            "SEDA_PRODUCT_LINE": "TV",
            "SEDA_ACTIVE_RETAILER": "magalu",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "1",
            "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "0",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            step14_db_load, "run_root", return_value=Path("C:/fixture")
        ), patch.object(step14_db_load, "read_csv", return_value=[self._row("Magalu")]), patch.object(
            step14_db_load, "output_table", return_value="dx_seda_tv_retail_com"
        ), patch.object(step14_db_load, "db_connect", return_value=connection), patch.object(
            step14_db_load, "write_json"
        ) as write_json, patch(
            "psycopg2.extras.execute_values",
            side_effect=lambda *args: events.append("insert"),
        ):
            step14_db_load.main()

        self.assertEqual(events, ["truncate", "insert"])
        self.assertEqual(
            cursor.execute.call_args.args[0],
            "TRUNCATE TABLE dx_seda_tv_retail_com",
        )
        manifest = write_json.call_args.args[1]
        self.assertEqual(manifest["mode"], "truncate")
        self.assertEqual(manifest["deleted"], 0)

    def test_conflicting_replace_modes_fail_before_db_connect(self):
        env = {
            "SEDA_PRODUCT_LINE": "TV",
            "SEDA_ACTIVE_RETAILER": "casas_bahia",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "1",
            "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "1",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            step14_db_load, "run_root", return_value=Path("C:/fixture")
        ), patch.object(step14_db_load, "read_csv", return_value=[self._row()]), patch.object(
            step14_db_load, "db_connect"
        ) as db_connect:
            with self.assertRaisesRegex(RuntimeError, "conflicting_replace_modes"):
                step14_db_load.main()
        db_connect.assert_not_called()

    def test_failed_insert_does_not_write_success_manifest(self):
        connection, _ = self._db()
        env = {
            "SEDA_PRODUCT_LINE": "TV",
            "SEDA_ACTIVE_RETAILER": "casas_bahia",
            "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "1",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            step14_db_load, "run_root", return_value=Path("C:/fixture")
        ), patch.object(step14_db_load, "read_csv", return_value=[self._row()]), patch.object(
            step14_db_load, "output_table", return_value="dx_seda_tv_retail_com"
        ), patch.object(step14_db_load, "db_connect", return_value=connection), patch.object(
            step14_db_load, "write_json"
        ) as write_json, patch(
            "psycopg2.extras.execute_values", side_effect=RuntimeError("insert failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                step14_db_load.main()
        write_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
