import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from seda.detail_publish import (
    assert_detail_publish_complete,
    detail_publish_lock,
    detail_run_lock,
    file_sha256,
    mark_detail_publish_incomplete,
    publish_detail_files,
    read_detail_publish_transaction,
    recover_detail_publish_transaction,
)
from seda import (
    step09_review20,
    step11_s3_sync,
    step13_db_prepare,
    step14_db_load,
    step15_final_output,
    step16_field_audit,
)
from seda.casas_bahia import freight_cdp_backfill, listing_discount_backfill
from seda.common.orchestrator import Step, step_complete
from seda.magalu.detail_api import _post_browser_graphql
from seda.step00_config import OUTPUT_COLUMNS
from seda.step08_detail_enrichment import (
    REVIEW_PAGE_TRACE_COLUMNS,
    SUBCALL_TRACE_COLUMNS,
    _detail_trace_path,
    _merge_parallel_detail_traces,
    _parallel_part_error,
    _publish_detail_snapshot,
    _record_result_trace,
    _run_detail_main,
    _resume_detail_trace_prefix,
    _write_trace_csv,
)


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _transaction_fixture(root):
    root = Path(root)
    canonicals = [
        root / "detail" / "trace" / "subcall_trace.csv",
        root / "detail" / "trace" / "magalu_review_page_trace.csv",
        root / "output" / "final_output_enriched.csv",
    ]
    stages = [
        root / "detail" / "trace" / ".subcall.stage",
        root / "detail" / "trace" / ".review.stage",
        root / "output" / ".product.stage",
    ]
    for index, canonical in enumerate(canonicals):
        _write(canonical, f"old-{index}")
    for index, staged in enumerate(stages):
        _write(staged, f"new-{index}")
    files = [
        {"name": "subcall", "canonical": canonicals[0], "staged": stages[0]},
        {"name": "review", "canonical": canonicals[1], "staged": stages[1]},
        {"name": "product", "canonical": canonicals[2], "staged": stages[2]},
    ]
    return canonicals, stages, files


def _write_target_csv(root, *, path=None, rows=None):
    path = Path(path or (Path(root) / "output" / "seda_final_targets.csv"))
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = (
        [{"item": "one", "product_url": "https://example/p/one"}]
        if rows is None
        else list(rows)
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["item", "product_url"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def _publish_complete_snapshot(root, *, target_path=None, rows=None, output=None):
    root = Path(root)
    rows = (
        [{"item": "one", "product_url": "https://example/p/one"}]
        if rows is None
        else list(rows)
    )
    target_path = _write_target_csv(root, path=target_path, rows=rows)
    output = Path(output or (root / "output" / "final_output_enriched.csv"))
    _publish_detail_snapshot(
        root,
        output,
        rows,
        [],
        [],
        final_complete=True,
        expected_total=len(rows),
        target_sha256=file_sha256(target_path),
        target_path=str(target_path.resolve()),
        include_traces=False,
    )
    return target_path, output


class DetailPublishTransactionTests(unittest.TestCase):
    def test_product_part_identity_requires_both_item_and_url(self):
        expected = [{"item": "one", "product_url": "https://example/p/one"}]
        wrong_item = [
            {"item": "other", "product_url": "https://example/p/one"}
        ]
        wrong_url = [
            {"item": "one", "product_url": "https://example/p/other"}
        ]
        self.assertIn("identity_at:0", _parallel_part_error(expected, wrong_item))
        self.assertIn("identity_at:0", _parallel_part_error(expected, wrong_url))

    def test_success_publishes_traces_then_product_and_commits_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonicals, _stages, files = _transaction_fixture(root)
            real_replace = os.replace
            replacements = []

            def tracked_replace(source, destination):
                destination = Path(destination)
                if destination in canonicals:
                    replacements.append(destination)
                return real_replace(source, destination)

            with patch("seda.detail_publish.os.replace", side_effect=tracked_replace):
                journal = publish_detail_files(root, files, run_token="run_success")

            self.assertEqual(replacements[:3], canonicals)
            self.assertEqual(journal["status"], "committed")
            self.assertEqual([path.read_text(encoding="utf-8") for path in canonicals], [
                "new-0",
                "new-1",
                "new-2",
            ])
            for entry in journal["files"]:
                self.assertFalse(Path(entry["canonical"]).is_absolute())
                self.assertEqual(
                    file_sha256(root / entry["canonical"]),
                    entry["new_sha256"],
                )

    def test_browser_attempts_reach_canonical_subcall_trace(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SEDA_DETAIL_TRACE": "1", "SEDA_TRANSLATE_OUTPUT": "0"},
        ):
            root = Path(directory)
            row = {
                "retailer": "Magalu",
                "item": "one",
                "product_url": "https://example/p/one",
            }
            browser_result = {
                "status_code": 200,
                "text": '{"data":{"item":{"id":"one"}}}',
                "data": {"data": {"item": {"id": "one"}}},
                "error": "",
                "trace": [
                    {
                        "operation": "itemQuery",
                        "attempt": 1,
                        "method": "browser_graphql",
                        "status_code": 200,
                        "length": 22,
                        "error": "graphql_item_missing",
                        "item_present": 0,
                    },
                    {
                        "operation": "itemQuery",
                        "attempt": 2,
                        "method": "browser_graphql",
                        "status_code": 200,
                        "length": 31,
                        "error": "",
                        "item_present": 1,
                    },
                ],
            }
            api_trace = []
            with patch(
                "seda.magalu.browser_session.graphql_post",
                return_value=browser_result,
            ):
                data = _post_browser_graphql(
                    {
                        "operationName": "itemQuery",
                        "variables": {"itemId": "one"},
                    },
                    10,
                    api_trace,
                    "item",
                    context_url=row["product_url"],
                )
            self.assertEqual(data["data"]["item"]["id"], "one")

            subcall_rows = []
            _record_result_trace(
                subcall_rows,
                row,
                1,
                row["product_url"],
                "magalu_graphql_detail",
                {"success": True, "trace": api_trace},
            )
            target = _write_target_csv(
                root,
                rows=[
                    {
                        "item": row["item"],
                        "product_url": row["product_url"],
                    }
                ],
            )
            _publish_detail_snapshot(
                root,
                root / "output" / "final_output_enriched.csv",
                [row],
                subcall_rows,
                [],
                final_complete=True,
                expected_total=1,
                target_sha256=file_sha256(target),
                target_path=str(target.resolve()),
            )

            with (
                root / "detail" / "trace" / "subcall_trace.csv"
            ).open("r", encoding="utf-8-sig", newline="") as handle:
                saved = list(csv.DictReader(handle))
            self.assertEqual(
                [trace_row["attempt"] for trace_row in saved],
                ["1", "2"],
            )
            self.assertEqual(
                [trace_row["error"] for trace_row in saved],
                ["graphql_item_missing", ""],
            )
            self.assertEqual(
                [trace_row["item_present"] for trace_row in saved],
                ["0", "1"],
            )
            assert_detail_publish_complete(root)

    def test_product_replace_failure_rolls_back_every_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonicals, stages, files = _transaction_fixture(root)
            real_replace = os.replace

            def fail_product(source, destination):
                if Path(source) == stages[2] and Path(destination) == canonicals[2]:
                    raise OSError("locked product")
                return real_replace(source, destination)

            with patch("seda.detail_publish.os.replace", side_effect=fail_product):
                with self.assertRaisesRegex(OSError, "locked product"):
                    publish_detail_files(root, files, run_token="run_rollback")

            self.assertEqual([path.read_text(encoding="utf-8") for path in canonicals], [
                "old-0",
                "old-1",
                "old-2",
            ])
            self.assertEqual(read_detail_publish_transaction(root)["status"], "rolled_back")

    def test_locked_unchanged_product_does_not_block_trace_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonicals, _stages, files = _transaction_fixture(root)
            real_replace = os.replace

            def lock_product_destination(source, destination):
                if Path(destination) == canonicals[2]:
                    raise OSError("locked product destination")
                return real_replace(source, destination)

            with patch(
                "seda.detail_publish.os.replace",
                side_effect=lock_product_destination,
            ):
                with self.assertRaisesRegex(OSError, "locked product destination"):
                    publish_detail_files(
                        root,
                        files,
                        run_token="run_locked_destination",
                    )

            self.assertEqual(
                [path.read_text(encoding="utf-8") for path in canonicals],
                ["old-0", "old-1", "old-2"],
            )
            self.assertEqual(
                read_detail_publish_transaction(root)["status"],
                "rolled_back",
            )

    def test_startup_recovery_finalizes_all_new_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _canonicals, _stages, files = _transaction_fixture(root)
            publish_detail_files(root, files, run_token="run_finalize")
            journal_path = root / "detail" / "trace" / "detail_publish_transaction.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["status"] = "prepared"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")

            recovered = recover_detail_publish_transaction(root)
            self.assertEqual(recovered["status"], "committed")
            self.assertEqual(recovered["recovery"], "finalized_new_files")

    def test_startup_recovery_restores_mixed_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonicals, _stages, files = _transaction_fixture(root)
            publish_detail_files(root, files, run_token="run_mixed")
            journal_path = root / "detail" / "trace" / "detail_publish_transaction.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["status"] = "prepared"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            canonicals[2].write_text("partial", encoding="utf-8")

            recovered = recover_detail_publish_transaction(root)
            self.assertEqual(recovered["status"], "rolled_back")
            self.assertEqual([path.read_text(encoding="utf-8") for path in canonicals], [
                "old-0",
                "old-1",
                "old-2",
            ])

    def test_missing_backup_keeps_transaction_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonicals, _stages, files = _transaction_fixture(root)
            publish_detail_files(root, files, run_token="run_bad_backup")
            journal_path = root / "detail" / "trace" / "detail_publish_transaction.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["status"] = "prepared"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            (root / journal["files"][0]["backup"]).unlink()
            canonicals[2].write_text("partial", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "transaction_unresolved"):
                recover_detail_publish_transaction(root)
            self.assertEqual(read_detail_publish_transaction(root)["status"], "prepared")

    def test_second_publisher_cannot_enter_same_run_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with detail_publish_lock(root):
                with self.assertRaisesRegex(RuntimeError, "detail_publish_locked"):
                    recover_detail_publish_transaction(root)

    def test_second_parent_detail_run_cannot_enter_same_run_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with detail_run_lock(root):
                with self.assertRaisesRegex(RuntimeError, "detail_run_locked"):
                    with detail_run_lock(root):
                        pass

    def test_incomplete_run_marker_blocks_downstream_until_final_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SEDA_DETAIL_TRACE": "0", "SEDA_TRANSLATE_OUTPUT": "0"},
        ):
            root = Path(directory)
            output = root / "output" / "final_output_enriched.csv"
            rows = [
                {"item": "one", "product_url": "https://example/p/one"}
            ]
            target = _write_target_csv(root, rows=rows)
            mark_detail_publish_incomplete(
                root,
                expected_row_count=1,
                target_sha256=file_sha256(target),
                target_path=str(target.resolve()),
            )
            with self.assertRaisesRegex(RuntimeError, "detail_publish_incomplete"):
                assert_detail_publish_complete(root)

            _publish_detail_snapshot(
                root,
                output,
                rows,
                [],
                [],
                final_complete=True,
                expected_total=1,
                target_sha256=file_sha256(target),
                target_path=str(target.resolve()),
            )
            journal = assert_detail_publish_complete(root)
            self.assertIs(journal["metadata"]["complete"], True)
            self.assertEqual(journal["metadata"]["product_row_count"], 1)
            self.assertEqual(journal["metadata"]["expected_row_count"], 1)

    def test_complete_guard_verifies_product_and_target_files(self):
        for damage, expected_error in (
            ("product_missing", "product_missing"),
            ("product_changed", "product_hash_mismatch"),
            ("target_missing", "target_missing"),
            ("target_changed", "target_hash_mismatch"),
        ):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, product = _publish_complete_snapshot(root)
                assert_detail_publish_complete(root)
                if damage == "product_missing":
                    product.unlink()
                elif damage == "product_changed":
                    product.write_text("changed", encoding="utf-8")
                elif damage == "target_missing":
                    target.unlink()
                else:
                    target.write_text("changed", encoding="utf-8")
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"detail_publish_incomplete.*{expected_error}",
                ):
                    assert_detail_publish_complete(root)

    def test_complete_guard_requires_target_product_identity_alignment(self):
        cases = (
            {
                "item": "other-item",
                "product_url": "https://example/p/target",
            },
            {
                "item": "target-item",
                "product_url": "https://example/p/other",
            },
        )
        for product_row in cases:
            with self.subTest(product_row=product_row), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ,
                {"SEDA_DETAIL_TRACE": "0", "SEDA_TRANSLATE_OUTPUT": "0"},
            ):
                root = Path(directory)
                target = _write_target_csv(
                    root,
                    rows=[
                        {
                            "item": "target-item",
                            "product_url": "https://example/p/target",
                        }
                    ],
                )
                _publish_detail_snapshot(
                    root,
                    root / "output" / "final_output_enriched.csv",
                    [product_row],
                    [],
                    [],
                    final_complete=True,
                    expected_total=1,
                    target_sha256=file_sha256(target),
                    target_path=str(target.resolve()),
                    include_traces=False,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "detail_publish_incomplete.*product_identity_at=1",
                ):
                    assert_detail_publish_complete(root)

    def test_complete_guard_rejects_target_row_count_mismatch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SEDA_DETAIL_TRACE": "0", "SEDA_TRANSLATE_OUTPUT": "0"},
        ):
            root = Path(directory)
            target = _write_target_csv(
                root,
                rows=[
                    {"item": "one", "product_url": "https://example/p/one"},
                    {"item": "two", "product_url": "https://example/p/two"},
                ],
            )
            _publish_detail_snapshot(
                root,
                root / "output" / "final_output_enriched.csv",
                [{"item": "one", "product_url": "https://example/p/one"}],
                [],
                [],
                final_complete=True,
                expected_total=1,
                target_sha256=file_sha256(target),
                target_path=str(target.resolve()),
                include_traces=False,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "detail_publish_incomplete.*target_csv_rows=2",
            ):
                assert_detail_publish_complete(root)

    def test_changed_target_override_invalidates_previous_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _publish_complete_snapshot(root)
            replacement = _write_target_csv(
                root,
                path=root / "fixtures" / "replacement.csv",
            )
            with patch.dict(
                os.environ,
                {"SEDA_DETAIL_TARGET_CSV": str(replacement)},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "detail_publish_incomplete.*target_path_mismatch",
                ):
                    assert_detail_publish_complete(root)
                with patch(
                    "seda.common.orchestrator.run_root",
                    return_value=root,
                ):
                    complete, _reason = step_complete(
                        Step(6, "detail_enrichment", "unused")
                    )
            self.assertIs(complete, False)

    def test_relative_target_override_uses_same_cwd_contract_as_step08(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "fixtures" / "relative_targets.csv"
            _publish_complete_snapshot(root, target_path=target)
            relative_target = os.path.relpath(target, Path.cwd())
            with patch.dict(
                os.environ,
                {"SEDA_DETAIL_TARGET_CSV": relative_target},
                clear=False,
            ):
                journal = assert_detail_publish_complete(root)
            self.assertEqual(journal["status"], "committed")

    def test_final_source_selects_enriched_snapshot_for_custom_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom_target = root / "fixtures" / "custom_targets.csv"
            target, product = _publish_complete_snapshot(
                root,
                target_path=custom_target,
            )
            self.assertFalse(
                (root / "output" / "seda_final_targets.csv").exists()
            )
            with patch.dict(
                os.environ,
                {"SEDA_DETAIL_TARGET_CSV": str(target)},
                clear=False,
            ):
                source = step15_final_output._source_path(root)
            self.assertEqual(source.resolve(), product.resolve())

    def test_header_only_zero_target_is_complete_but_missing_target_is_not(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SEDA_DETAIL_TRACE": "0", "SEDA_TRANSLATE_OUTPUT": "0"},
        ):
            root = Path(directory)
            _publish_complete_snapshot(root, rows=[])
            journal = assert_detail_publish_complete(root)
            self.assertEqual(journal["metadata"]["product_row_count"], 0)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SEDA_DETAIL_TARGET_CSV": str(
                    Path(directory) / "missing_targets.csv"
                )
            },
            clear=False,
        ):
            root = Path(directory)
            with self.assertRaisesRegex(FileNotFoundError, "detail_target_missing"):
                _run_detail_main(root, is_worker=False)
            self.assertFalse(
                (
                    root
                    / "detail"
                    / "trace"
                    / "detail_publish_transaction.json"
                ).exists()
            )

    def test_short_target_row_is_rejected_before_run_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "output" / "short_targets.csv"
            target.parent.mkdir(parents=True)
            values = [""] * (OUTPUT_COLUMNS.index("product_url") + 1)
            values[OUTPUT_COLUMNS.index("item")] = "one"
            values[OUTPUT_COLUMNS.index("product_url")] = (
                "https://example/p/one"
            )
            with target.open(
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                writer = csv.writer(handle)
                writer.writerow(OUTPUT_COLUMNS)
                writer.writerow(values)

            with patch.dict(
                os.environ,
                {"SEDA_DETAIL_TARGET_CSV": str(target)},
                clear=False,
            ), self.assertRaisesRegex(
                RuntimeError,
                "detail_target_invalid_rows:short_row",
            ):
                _run_detail_main(root, is_worker=False)

            self.assertFalse(
                (
                    root
                    / "detail"
                    / "trace"
                    / "detail_publish_transaction.json"
                ).exists()
            )

    def test_zero_byte_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SEDA_DETAIL_TARGET_CSV": str(
                    Path(directory) / "empty_targets.csv"
                )
            },
            clear=False,
        ):
            root = Path(directory)
            Path(os.environ["SEDA_DETAIL_TARGET_CSV"]).write_bytes(b"")
            with self.assertRaisesRegex(
                RuntimeError,
                "detail_target_invalid_header",
            ):
                _run_detail_main(root, is_worker=False)

    def test_resume_recovers_prepared_complete_snapshot_but_dry_run_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom_target = root / "fixtures" / "custom_targets.csv"
            _publish_complete_snapshot(root, target_path=custom_target)
            journal_path = (
                root / "detail" / "trace" / "detail_publish_transaction.json"
            )
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["status"] = "prepared"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            before = journal_path.read_bytes()
            detail_step = Step(6, "detail_enrichment", "unused")

            with patch.dict(
                os.environ,
                {"SEDA_DETAIL_TARGET_CSV": str(custom_target)},
                clear=False,
            ), patch("seda.common.orchestrator.run_root", return_value=root):
                complete, _reason = step_complete(detail_step, recover=False)
            self.assertIs(complete, False)
            self.assertEqual(journal_path.read_bytes(), before)

            with patch.dict(
                os.environ,
                {"SEDA_DETAIL_TARGET_CSV": str(custom_target)},
                clear=False,
            ), patch("seda.common.orchestrator.run_root", return_value=root):
                complete, _reason = step_complete(detail_step)
            self.assertIs(complete, True)
            self.assertEqual(
                read_detail_publish_transaction(root)["status"],
                "committed",
            )

    def test_complete_guard_rejects_noncanonical_product_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _publish_complete_snapshot(
                root,
                output=root / "output" / "canary_enriched.csv",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "detail_publish_incomplete.*product_path_mismatch",
            ):
                assert_detail_publish_complete(root)

    def test_final_flag_cannot_override_row_count_mismatch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SEDA_DETAIL_TRACE": "0", "SEDA_TRANSLATE_OUTPUT": "0"},
        ):
            root = Path(directory)
            output = root / "output" / "final_output_enriched.csv"
            _publish_detail_snapshot(
                root,
                output,
                [{"item": "one", "product_url": "https://example/p/one"}],
                [],
                [],
                final_complete=True,
                expected_total=2,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "detail_publish_incomplete.*rows=1/2",
            ):
                assert_detail_publish_complete(root)

    def test_incomplete_committed_checkpoint_blocks_direct_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mark_detail_publish_incomplete(root, expected_row_count=2)
            env = {"SEDA_RUN_ROOT": str(root)}
            for module, patch_target in (
                (step09_review20, "seda.step09_review20.read_csv"),
                (
                    freight_cdp_backfill,
                    "seda.casas_bahia.freight_cdp_backfill.asyncio.run",
                ),
                (
                    listing_discount_backfill,
                    "seda.casas_bahia.listing_discount_backfill.run",
                ),
                (step15_final_output, "seda.step15_final_output.read_csv"),
                (step16_field_audit, "seda.step16_field_audit.read_csv"),
                (step11_s3_sync, "seda.step11_s3_sync.subprocess.call"),
                (step13_db_prepare, "seda.step13_db_prepare.db_connect"),
                (step14_db_load, "seda.step14_db_load.db_connect"),
            ):
                with self.subTest(module=module.__name__), patch.dict(
                    os.environ,
                    env,
                    clear=False,
                ), patch.object(
                    sys,
                    "argv",
                    [module.__name__],
                ), patch(patch_target) as side_effect:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "detail_publish_incomplete",
                    ):
                        module.main()
                    side_effect.assert_not_called()

    def test_direct_consumer_holds_run_lock_for_entire_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _product = _publish_complete_snapshot(root)
            env = {
                "SEDA_RUN_ROOT": str(root),
                "SEDA_DETAIL_TARGET_CSV": str(target),
            }

            def verify_locked(consumer_root):
                self.assertEqual(Path(consumer_root), root)
                with self.assertRaisesRegex(RuntimeError, "detail_run_locked"):
                    with detail_run_lock(root):
                        pass

            with patch.dict(os.environ, env, clear=False), patch(
                "seda.step15_final_output._main",
                side_effect=verify_locked,
            ) as downstream:
                step15_final_output.main()
            downstream.assert_called_once_with(root)

    def test_s3_excludes_private_publish_and_worker_trace_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SEDA_RUN_ROOT": directory,
                "SEDA_S3_DEST": "s3://bucket/path",
            },
            clear=False,
        ), patch("seda.step11_s3_sync.subprocess.call", return_value=0) as call:
            step11_s3_sync.main()
            command = call.call_args.args[0]
            excluded = [
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "--exclude"
            ]
            self.assertIn(
                "detail/trace/.detail_publish_transaction.json.*.tmp",
                excluded,
            )
            self.assertIn("detail/trace/detail_run.lock", excluded)
            self.assertIn("detail/trace/subcall_trace_*.csv", excluded)
            self.assertIn(
                "detail/trace/magalu_review_page_trace_*.csv",
                excluded,
            )

    def test_unresolved_transaction_blocks_final_s3_and_db_before_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "detail" / "trace" / "detail_publish_transaction.json"
            _write(journal, json.dumps({"status": "prepared", "files": []}))
            env = {"SEDA_RUN_ROOT": str(root)}
            with patch.dict(os.environ, env, clear=False), patch(
                "seda.step15_final_output.read_csv"
            ) as final_read:
                with self.assertRaisesRegex(RuntimeError, "transaction_unresolved"):
                    step15_final_output.main()
                final_read.assert_not_called()
            with patch.dict(os.environ, env, clear=False), patch(
                "seda.step11_s3_sync.subprocess.call"
            ) as s3_call:
                with self.assertRaisesRegex(RuntimeError, "transaction_unresolved"):
                    step11_s3_sync.main()
                s3_call.assert_not_called()
            with patch.dict(os.environ, env, clear=False), patch(
                "seda.step13_db_prepare.db_connect"
            ) as prepare_connect:
                with self.assertRaisesRegex(RuntimeError, "transaction_unresolved"):
                    step13_db_prepare.main()
                prepare_connect.assert_not_called()
            with patch.dict(os.environ, env, clear=False), patch(
                "seda.step14_db_load.db_connect"
            ) as load_connect:
                with self.assertRaisesRegex(RuntimeError, "transaction_unresolved"):
                    step14_db_load.main()
                load_connect.assert_not_called()

    def test_parallel_trace_requires_every_part_but_allows_empty_review(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SEDA_DETAIL_TRACE": "1"},
        ):
            root = Path(directory)
            token = "run_trace"
            tag = f"{token}_w0"
            rows = [
                {"item": "one", "product_url": "https://example/p/one"},
                {"item": "two", "product_url": "https://example/p/two"},
            ]
            subcall_rows = [
                {
                    "row_index": index,
                    "run_token": token,
                    "worker_id": "0",
                    "item": row["item"],
                    "product_url": row["product_url"],
                    "subcall": "detail",
                }
                for index, row in enumerate(rows, start=1)
            ]
            _write_trace_csv(
                _detail_trace_path(root, "subcall_trace", tag=tag),
                subcall_rows,
                SUBCALL_TRACE_COLUMNS,
            )
            parts = [(0, 0, 2, str(root / "part.csv"), tag)]
            with self.assertRaisesRegex(RuntimeError, "magalu_review_page_trace:missing"):
                _merge_parallel_detail_traces(root, parts, rows, token)

            _write_trace_csv(
                _detail_trace_path(root, "magalu_review_page_trace", tag=tag),
                [],
                REVIEW_PAGE_TRACE_COLUMNS,
            )
            merged_subcall, merged_review = _merge_parallel_detail_traces(
                root,
                parts,
                rows,
                token,
            )
            self.assertEqual(len(merged_subcall), 2)
            self.assertEqual(merged_review, [])

    def test_resume_loads_validated_prefix_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SEDA_DETAIL_TRACE": "1"},
        ):
            root = Path(directory)
            rows = [
                {"item": "one", "product_url": "https://example/p/one"},
                {"item": "two", "product_url": "https://example/p/two"},
            ]
            subcall = [
                {
                    "row_index": index,
                    "item": row["item"],
                    "product_url": row["product_url"],
                    "subcall": "detail",
                }
                for index, row in enumerate(rows, start=1)
            ]
            review = [
                {
                    "row_index": 1,
                    "item": "one",
                    "product_url": "https://example/p/one",
                    "page": 1,
                }
            ]
            _write_trace_csv(
                _detail_trace_path(root, "subcall_trace", tag=""),
                subcall,
                SUBCALL_TRACE_COLUMNS,
            )
            _write_trace_csv(
                _detail_trace_path(root, "magalu_review_page_trace", tag=""),
                review,
                REVIEW_PAGE_TRACE_COLUMNS,
            )
            loaded_subcall, loaded_review = _resume_detail_trace_prefix(root, rows)
            self.assertEqual([row["row_index"] for row in loaded_subcall], ["1", "2"])
            self.assertEqual([row["row_index"] for row in loaded_review], ["1"])

    def test_resume_prefix_and_suffix_publish_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SEDA_DETAIL_TRACE": "1", "SEDA_TRANSLATE_OUTPUT": "0"},
        ):
            root = Path(directory)
            output = root / "output" / "final_output_enriched.csv"
            rows = [
                {"item": "one", "product_url": "https://example/p/one"},
                {"item": "two", "product_url": "https://example/p/two"},
            ]
            prefix_trace = [
                {
                    "row_index": 1,
                    "item": "one",
                    "product_url": "https://example/p/one",
                    "subcall": "detail",
                }
            ]
            _publish_detail_snapshot(
                root,
                output,
                rows[:1],
                prefix_trace,
                [],
                run_token="prefix",
            )
            loaded_subcall, loaded_review = _resume_detail_trace_prefix(root, rows[:1])
            loaded_subcall.append(
                {
                    "row_index": 2,
                    "item": "two",
                    "product_url": "https://example/p/two",
                    "subcall": "detail",
                }
            )
            _publish_detail_snapshot(
                root,
                output,
                rows,
                loaded_subcall,
                loaded_review,
                run_token="suffix",
            )
            with (_detail_trace_path(root, "subcall_trace", tag="")).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                saved = list(csv.DictReader(handle))
            self.assertEqual([row["row_index"] for row in saved], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
