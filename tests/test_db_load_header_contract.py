import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from seda.step00_config import read_csv
from seda.step14_db_load import _validate_db_csv_schema
from seda.step15_final_output import final_output_columns


class DbLoadHeaderContractTests(unittest.TestCase):
    def _write(self, path, fieldnames):
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(fieldnames)
            writer.writerow(['value'] * len(fieldnames))

    def test_duplicate_header_is_rejected_before_database_access(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {'SEDA_PRODUCT_LINE': 'REF'},
        ):
            path = Path(tmp) / 'duplicate.csv'
            columns = final_output_columns()
            self._write(path, columns + [columns[-1]])
            with self.assertRaisesRegex(
                RuntimeError,
                'db_load_schema_duplicate_columns',
            ):
                _validate_db_csv_schema(read_csv(path), path)

    def test_valid_reordered_named_columns_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {'SEDA_PRODUCT_LINE': 'LDY'},
        ):
            path = Path(tmp) / 'reordered.csv'
            columns = list(reversed(final_output_columns()))
            self._write(path, columns)
            _validate_db_csv_schema(read_csv(path), path)

    def test_data_wider_than_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {'SEDA_PRODUCT_LINE': 'TV'},
        ):
            path = Path(tmp) / 'wide-row.csv'
            columns = final_output_columns()
            self._write(path, columns)
            with path.open('a', encoding='utf-8-sig', newline='') as handle:
                csv.writer(handle).writerow(['value'] * (len(columns) + 1))
            with self.assertRaisesRegex(RuntimeError, 'extra=None'):
                _validate_db_csv_schema(read_csv(path), path)

    def test_data_shorter_than_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {'SEDA_PRODUCT_LINE': 'TV'},
        ):
            path = Path(tmp) / 'short-row.csv'
            columns = final_output_columns()
            self._write(path, columns)
            with path.open('a', encoding='utf-8-sig', newline='') as handle:
                csv.writer(handle).writerow(['value'] * 4)
            with self.assertRaisesRegex(RuntimeError, 'short_row:row=2'):
                _validate_db_csv_schema(read_csv(path), path)


if __name__ == '__main__':
    unittest.main()
