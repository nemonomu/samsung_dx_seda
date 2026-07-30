import os
import unittest
from pathlib import Path
from unittest.mock import patch

from seda import seda_orchestrator
from seda.common import orchestrator as retailer_orchestrator
from seda.common.retailer_runner import configure_retailer


class SplitOrchestratorTests(unittest.TestCase):
    def test_status_and_cleanup_run_without_detail_completion_gate(self):
        chosen = [
            retailer_orchestrator.Step(
                13,
                "status_check",
                "seda.magalu.step10_status_check",
            ),
            retailer_orchestrator.Step(
                14,
                "local_cleanup",
                "seda.magalu.step12_local_cleanup",
            ),
        ]
        with patch.dict(
            os.environ,
            {
                "SEDA_RUN_ROOT": "C:/diagnostic/run",
                "SEDA_PRODUCT_LINE": "TV",
            },
            clear=True,
        ), patch.object(
            retailer_orchestrator,
            "selected_steps",
            return_value=chosen,
        ), patch.object(
            retailer_orchestrator,
            "assert_detail_publish_complete",
        ) as completion, patch.object(
            retailer_orchestrator,
            "run_module",
            return_value=0,
        ) as run, patch(
            "sys.argv",
            ["magalu_orchestrator", "status_check", "local_cleanup"],
        ):
            retailer_orchestrator.run_retailer_orchestrator(
                "magalu",
                "seda.magalu",
                "test",
            )

        completion.assert_not_called()
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                "seda.magalu.step10_status_check",
                "seda.magalu.step12_local_cleanup",
            ],
        )
        usage_ids = [
            call.kwargs["env"]["SEDA_ZENROWS_USAGE_EXECUTION_ID"]
            for call in run.call_args_list
        ]
        self.assertEqual(len(set(usage_ids)), 1)
        self.assertRegex(usage_ids[0], r"^[0-9a-f]{32}$")
        self.assertTrue(
            all(
                call.kwargs["env"]["SEDA_ZENROWS_USAGE_REQUIRED"] == "1"
                for call in run.call_args_list
            )
        )

    def test_real_detail_consumer_is_gated_before_module_execution(self):
        chosen = [
            retailer_orchestrator.Step(
                7,
                "review20",
                "seda.magalu.step09_review20",
            )
        ]
        with patch.dict(
            os.environ,
            {
                "SEDA_RUN_ROOT": "C:/consumer/run",
                "SEDA_PRODUCT_LINE": "TV",
            },
            clear=True,
        ), patch.object(
            retailer_orchestrator,
            "selected_steps",
            return_value=chosen,
        ), patch.object(
            retailer_orchestrator,
            "assert_detail_publish_complete",
        ) as completion, patch.object(
            retailer_orchestrator,
            "run_module",
            return_value=0,
        ) as run, patch(
            "sys.argv",
            ["magalu_orchestrator", "review20"],
        ):
            retailer_orchestrator.run_retailer_orchestrator(
                "magalu",
                "seda.magalu",
                "test",
            )

        completion.assert_called_once()
        run.assert_called_once()

    def test_all_splits_an_explicit_root_and_retailer_runtime_context(self):
        clean_env = {
            "SEDA_RUN_ROOT": "C:/shared/root",
            "SEDA_ACTIVE_RETAILER": "stale",
            "SEDA_RETAILERS": "magalu,casas_bahia",
            "SEDA_FETCH_MODE": "stale_shared_mode",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "1",
        }
        with patch.dict(os.environ, clean_env, clear=True), patch.object(
            seda_orchestrator.subprocess, "call", return_value=0
        ) as run:
            seda_orchestrator.main(["--retailer", "all", "--product-line", "ref", "--all"])

        self.assertEqual(run.call_count, 2)
        magalu_call, casas_call = run.call_args_list
        self.assertEqual(
            magalu_call.args[0],
            [
                seda_orchestrator.PYTHON,
                "-m",
                "seda.magalu.magalu_orchestrator",
                "--all",
                "--product-line",
                "REF",
            ],
        )
        self.assertEqual(
            casas_call.args[0],
            [
                seda_orchestrator.PYTHON,
                "-m",
                "seda.casas_bahia.casas_bahia_orchestrator",
                "--all",
                "--product-line",
                "REF",
            ],
        )
        self.assertEqual(magalu_call.kwargs["env"]["SEDA_RETAILERS"], "magalu")
        self.assertEqual(magalu_call.kwargs["env"]["SEDA_ACTIVE_RETAILER"], "magalu")
        self.assertEqual(
            magalu_call.kwargs["env"]["SEDA_RUN_ROOT"],
            str(Path("C:/shared/root/magalu/ref")),
        )
        self.assertEqual(magalu_call.kwargs["env"]["SEDA_FETCH_MODE"], "magalu_graphql_first")
        self.assertEqual(magalu_call.kwargs["env"]["SEDA_DB_TRUNCATE_BEFORE_LOAD"], "0")
        self.assertEqual(magalu_call.kwargs["env"]["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"], "1")
        self.assertEqual(casas_call.kwargs["env"]["SEDA_RETAILERS"], "casas_bahia")
        self.assertEqual(casas_call.kwargs["env"]["SEDA_ACTIVE_RETAILER"], "casas_bahia")
        self.assertEqual(
            casas_call.kwargs["env"]["SEDA_RUN_ROOT"],
            str(Path("C:/shared/root/casas_bahia/ref")),
        )
        self.assertEqual(casas_call.kwargs["env"]["SEDA_FETCH_MODE"], "casas_bahia_uc_first")
        self.assertEqual(casas_call.kwargs["env"]["SEDA_DB_TRUNCATE_BEFORE_LOAD"], "0")
        self.assertEqual(casas_call.kwargs["env"]["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"], "1")

    def test_all_uses_canonical_retailer_roots_without_an_explicit_base(self):
        def fake_root(retailer=None, run_date_value=None, product_line_value=None):
            return Path("C:/runs") / str(retailer) / str(product_line_value).lower()

        with patch.dict(os.environ, {}, clear=True), patch.object(
            seda_orchestrator, "dated_run_root", side_effect=fake_root
        ), patch.object(seda_orchestrator.subprocess, "call", return_value=0) as run:
            seda_orchestrator.main(["--retailer", "all", "--product-line", "TV", "--all"])

        self.assertEqual(
            run.call_args_list[0].kwargs["env"]["SEDA_RUN_ROOT"],
            str(Path("C:/runs/magalu/tv")),
        )
        self.assertEqual(
            run.call_args_list[1].kwargs["env"]["SEDA_RUN_ROOT"],
            str(Path("C:/runs/casas_bahia/tv")),
        )

    def test_dispatcher_force_flag_ignores_stale_explicit_root(self):
        def fake_root(retailer=None, run_date_value=None, product_line_value=None):
            return Path("C:/forced") / str(retailer) / str(product_line_value).lower()

        env = {
            "SEDA_RUN_ROOT": "C:/stale/shared",
            "SEDA_FORCE_DATED_RUN_ROOT": "1",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            seda_orchestrator, "dated_run_root", side_effect=fake_root
        ), patch.object(seda_orchestrator.subprocess, "call", return_value=0) as run:
            seda_orchestrator.main(["--retailer", "all", "--product-line", "TV", "--all"])

        self.assertEqual(
            [call.kwargs["env"]["SEDA_RUN_ROOT"] for call in run.call_args_list],
            [str(Path("C:/forced/magalu/tv")), str(Path("C:/forced/casas_bahia/tv"))],
        )
        self.assertTrue(
            all(
                call.kwargs["env"]["SEDA_FORCE_DATED_RUN_ROOT"] == "0"
                for call in run.call_args_list
            )
        )

    def test_single_retailer_preserves_explicit_root_and_forwards_resume(self):
        with patch.dict(
            os.environ,
            {"SEDA_RUN_ROOT": "C:/custom/casas", "SEDA_FETCH_MODE": "graphql"},
            clear=True,
        ), patch.object(seda_orchestrator.subprocess, "call", return_value=0) as run:
            seda_orchestrator.main(
                ["--retailer", "casas_bahia", "--product-line", "LDY", "--resume"]
            )

        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertEqual(
            command,
            [
                seda_orchestrator.PYTHON,
                "-m",
                "seda.casas_bahia.casas_bahia_orchestrator",
                "--resume",
                "--product-line",
                "LDY",
            ],
        )
        self.assertEqual(env["SEDA_RUN_ROOT"], "C:/custom/casas")
        self.assertEqual(env["SEDA_ACTIVE_RETAILER"], "casas_bahia")
        self.assertEqual(env["SEDA_FETCH_MODE"], "graphql")
        self.assertEqual(env["SEDA_FORCE_DATED_RUN_ROOT"], "0")

    def test_batch_force_flag_overrides_a_stale_loaded_run_root(self):
        forced_root = Path("C:/runs/casas_bahia/tv/20260719")
        env = {
            "SEDA_RUN_ROOT": "C:/stale/shared",
            "SEDA_FORCE_DATED_RUN_ROOT": "1",
            "SEDA_PRODUCT_LINE": "TV",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            retailer_orchestrator, "dated_run_root", return_value=forced_root
        ), patch.object(retailer_orchestrator, "selected_steps", return_value=[]), patch(
            "sys.argv", ["casas_bahia_orchestrator"]
        ):
            retailer_orchestrator.run_retailer_orchestrator(
                "casas_bahia",
                "seda.casas_bahia",
                "test",
            )

            self.assertEqual(os.environ["SEDA_RUN_ROOT"], str(forced_root))
            self.assertEqual(os.environ["SEDA_ACTIVE_RETAILER"], "casas_bahia")

    def test_combined_batch_converts_loaded_truncate_to_retailer_replace(self):
        env = {
            "SEDA_COMBINED_RETAILER_RUN": "1",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "1",
            "SEDA_PRODUCT_LINE": "TV",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            retailer_orchestrator, "selected_steps", return_value=[]
        ), patch("sys.argv", ["magalu_orchestrator"]):
            retailer_orchestrator.run_retailer_orchestrator("magalu", "seda.magalu", "test")
            self.assertEqual(os.environ["SEDA_DB_TRUNCATE_BEFORE_LOAD"], "0")
            self.assertEqual(os.environ["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"], "1")
            self.assertEqual(os.environ["SEDA_COMBINED_RETAILER_RUN"], "0")
            retailer_orchestrator._configure_combined_db_mode()
            self.assertEqual(os.environ["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"], "1")

    def test_combined_batch_without_truncate_preserves_append_mode(self):
        env = {
            "SEDA_COMBINED_RETAILER_RUN": "1",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "0",
            "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD": "1",
            "SEDA_PRODUCT_LINE": "TV",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            retailer_orchestrator, "selected_steps", return_value=[]
        ), patch("sys.argv", ["casas_bahia_orchestrator"]):
            retailer_orchestrator.run_retailer_orchestrator(
                "casas_bahia", "seda.casas_bahia", "test"
            )
            self.assertEqual(os.environ["SEDA_DB_TRUNCATE_BEFORE_LOAD"], "0")
            self.assertEqual(os.environ["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"], "0")
            self.assertEqual(os.environ["SEDA_COMBINED_RETAILER_RUN"], "0")
            retailer_orchestrator._configure_combined_db_mode()
            self.assertEqual(os.environ["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"], "0")

    def test_all_preserves_supported_shared_fetch_mode_without_enabling_replace(self):
        env = {"SEDA_FETCH_MODE": "zenrows_first", "SEDA_DB_TRUNCATE_BEFORE_LOAD": "0"}
        with patch.dict(os.environ, env, clear=True), patch.object(
            seda_orchestrator.subprocess, "call", return_value=0
        ) as run:
            seda_orchestrator.main(["--retailer", "all", "--all"])
        for child_call in run.call_args_list:
            self.assertEqual(child_call.kwargs["env"]["SEDA_FETCH_MODE"], "zenrows_first")
            self.assertEqual(child_call.kwargs["env"]["SEDA_DB_TRUNCATE_BEFORE_LOAD"], "0")
            self.assertEqual(child_call.kwargs["env"]["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"], "0")

    def test_dispatcher_consumes_combined_marker_before_retailer_child(self):
        observed = []

        def run_child(command, env, cwd):
            self.assertEqual(env["SEDA_COMBINED_RETAILER_RUN"], "0")
            with patch.dict(os.environ, env, clear=True):
                retailer_orchestrator._configure_combined_db_mode()
                observed.append(
                    (
                        os.environ["SEDA_DB_TRUNCATE_BEFORE_LOAD"],
                        os.environ["SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD"],
                    )
                )
            return 0

        env = {
            "SEDA_COMBINED_RETAILER_RUN": "1",
            "SEDA_DB_TRUNCATE_BEFORE_LOAD": "1",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            seda_orchestrator.subprocess, "call", side_effect=run_child
        ):
            seda_orchestrator.main(["--retailer", "all", "--product-line", "TV", "--all"])

        self.assertEqual(observed, [("0", "1"), ("0", "1")])

    def test_named_step_is_forwarded_without_running_common_steps(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            seda_orchestrator.subprocess, "call", return_value=0
        ) as run:
            seda_orchestrator.main(
                [
                    "--retailer",
                    "magalu",
                    "detail_enrichment",
                    "--product-line",
                    "TV",
                ]
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                seda_orchestrator.PYTHON,
                "-m",
                "seda.magalu.magalu_orchestrator",
                "detail_enrichment",
                "--product-line",
                "TV",
            ],
        )

    def test_from_step_is_forwarded_to_the_selected_retailer(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            seda_orchestrator.subprocess, "call", return_value=0
        ) as run:
            seda_orchestrator.main(
                [
                    "--retailer",
                    "casas_bahia",
                    "--from-step",
                    "review20",
                    "--product-line",
                    "TV",
                ]
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                seda_orchestrator.PYTHON,
                "-m",
                "seda.casas_bahia.casas_bahia_orchestrator",
                "--from-step",
                "review20",
                "--product-line",
                "TV",
            ],
        )

    def test_numeric_step_requires_one_retailer(self):
        with patch.object(seda_orchestrator.subprocess, "call") as run:
            with self.assertRaisesRegex(SystemExit, "Numeric step identifiers require"):
                seda_orchestrator.main(["--retailer", "all", "08"])

        run.assert_not_called()

    def test_failure_stops_before_the_next_retailer(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            seda_orchestrator.subprocess, "call", side_effect=[7, 0]
        ) as run:
            with self.assertRaises(SystemExit) as raised:
                seda_orchestrator.main(["--retailer", "all", "--all"])

        self.assertEqual(raised.exception.code, 7)
        self.assertEqual(run.call_count, 1)

    def test_dry_run_is_executed_only_by_dry_run_child_orchestrators(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            seda_orchestrator.subprocess, "call", return_value=0
        ) as run:
            seda_orchestrator.main(["--retailer", "all", "--all", "--dry-run"])

        self.assertEqual(run.call_count, 2)
        for child_call in run.call_args_list:
            self.assertIn("--dry-run", child_call.args[0])
            self.assertIn("--all", child_call.args[0])

    def test_configure_retailer_replaces_stale_active_retailer(self):
        with patch.dict(
            os.environ,
            {
                "SEDA_ACTIVE_RETAILER": "magalu",
                "SEDA_RUN_ROOT": "C:/custom/casas",
                "SEDA_PRODUCT_LINE": "TV",
            },
            clear=True,
        ):
            configure_retailer("casas_bahia")
            self.assertEqual(os.environ["SEDA_RETAILERS"], "casas_bahia")
            self.assertEqual(os.environ["SEDA_ACTIVE_RETAILER"], "casas_bahia")
            self.assertEqual(os.environ["SEDA_RUN_ROOT"], "C:/custom/casas")

    def test_interleaved_batches_explicitly_isolate_both_retailers(self):
        root = Path(__file__).resolve().parents[1]
        for name in (
            "run_magalu_casas_interleaved_tv_ref_ldy_full.bat",
            "run_magalu_casas_interleaved_ref_ldy_full.bat",
        ):
            content = (root / name).read_text(encoding="utf-8").lower()
            self.assertIn('set "seda_retailers=magalu"', content)
            self.assertIn('set "seda_active_retailer=magalu"', content)
            self.assertIn('set "seda_retailers=casas_bahia"', content)
            self.assertIn('set "seda_active_retailer=casas_bahia"', content)
            self.assertIn('set "seda_combined_retailer_run=1"', content)
            self.assertIn('set "seda_force_dated_run_root=1"', content)

        sequential = (root / "run_magalu_casas_ref_ldy_seq.bat").read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn('set "seda_force_dated_run_root=1"', sequential)
        self.assertIn('set "seda_combined_retailer_run=1"', sequential)


if __name__ == "__main__":
    unittest.main()
