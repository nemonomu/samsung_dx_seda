"""Offline lifecycle/dispatch tests; no browser, network, detail or DB calls."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
import zipfile

_DISABLED_ENV = Path(__file__).with_name("WORKER_TEST_ENV_DOES_NOT_EXIST")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ["SEDA_ENV_PATH"] = str(_DISABLED_ENV)

from seda import step01_main_list as listing
from seda.casas_bahia import listing_auto_diagnostics as diagnostics
from seda.casas_bahia import listing_modes, listing_worker as worker
from seda.common import orchestrator, retailer_runner


MAIN = "seda.casas_bahia.step01_main_list"
TARGETS = "seda.casas_bahia.step02_main_targets"
BSR = "seda.casas_bahia.step03_bsr_list"
WORKER = "seda.casas_bahia.listing_worker"


def listing_steps():
    return [orchestrator.Step(1, "main_list", MAIN),
            orchestrator.Step(2, "main_targets", TARGETS),
            orchestrator.Step(3, "bsr_list", BSR)]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.environment = {"SEDA_CASAS_BAHIA_LISTING_MODE": "3", "SEDA_RUN_ID": "original"}
        self.stack.enter_context(patch.object(worker.os, "environ", self.environment))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.output))
        self.configure = self.stack.enter_context(patch.object(retailer_runner, "configure_retailer"))
        self.close = self.stack.enter_context(patch.object(listing_modes, "close_browsers"))
        self.events = []

        def record_listing(**kwargs):
            self.events.append(("listing", self.environment.get("SEDA_RUN_ID"), kwargs))

        def record_targets(**kwargs):
            self.events.append(("targets", self.environment.get("SEDA_RUN_ID"), kwargs))

        self.listing_main = Mock(side_effect=record_listing)
        self.target_main = Mock(side_effect=record_targets)
        modules = {"seda.step01_main_list": SimpleNamespace(main=self.listing_main),
                   "seda.step02_main_targets": SimpleNamespace(main=self.target_main)}
        self.import_module = self.stack.enter_context(patch.object(
            worker.importlib, "import_module", side_effect=lambda name: modules[name]))

    def test_main_targets_bsr_share_one_worker_and_restore_run_context(self):
        self.assertEqual(worker.run_steps([MAIN, TARGETS, BSR]), 0)
        self.assertEqual(self.events, [
            ("listing", "main", {"retain_browser": True}),
            ("targets", "main", {}),
            ("listing", "bsr", {"retain_browser": True}),
        ])
        self.assertEqual(self.environment["SEDA_RUN_ID"], "original")
        self.configure.assert_called_once_with("casas_bahia")
        self.close.assert_called_once_with()
        self.assertEqual([call.args[0] for call in self.import_module.call_args_list],
                         ["seda.step01_main_list", "seda.step02_main_targets", "seda.step01_main_list"])

    def test_listing_worker_preserves_explicit_subset_without_extra_targets(self):
        worker.run_steps([MAIN, BSR])
        self.target_main.assert_not_called()
        self.assertEqual([event[1] for event in self.events], ["main", "bsr"])
        self.close.assert_called_once()

    def test_mode_four_uses_same_owned_worker_lifecycle(self):
        self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = "4"
        self.assertEqual(worker.run_steps([MAIN, TARGETS, BSR]), 0)
        self.assertEqual(self.events, [("listing", "main", {"retain_browser": True}),
                                      ("targets", "main", {}),
                                      ("listing", "bsr", {"retain_browser": True})])
        self.assertEqual(self.environment["SEDA_RUN_ID"], "original")
        self.close.assert_called_once()

    def test_bsr_only_still_owns_and_closes_its_browser(self):
        worker.run_steps([BSR])
        self.assertEqual(self.events, [("listing", "bsr", {"retain_browser": True})])
        self.close.assert_called_once()

    def test_bsr_context_does_not_leak_to_later_explicit_targets(self):
        worker.run_steps([BSR, TARGETS, MAIN])
        self.assertEqual([event[1] for event in self.events], ["bsr", "main", "main"])
        self.assertEqual(self.environment["SEDA_RUN_ID"], "original")

    def test_absent_run_id_remains_absent_after_success(self):
        self.environment.pop("SEDA_RUN_ID")
        worker.run_steps([MAIN, BSR])
        self.assertNotIn("SEDA_RUN_ID", self.environment)

    def test_listing_failure_stops_targets_and_bsr_and_closes_once(self):
        error = SystemExit(7)
        self.listing_main.side_effect = error
        with self.assertRaises(SystemExit) as caught:
            worker.run_steps([MAIN, TARGETS, BSR])
        self.assertIs(caught.exception, error)
        self.listing_main.assert_called_once_with(retain_browser=True)
        self.target_main.assert_not_called()
        self.close.assert_called_once()
        self.assertEqual(self.environment["SEDA_RUN_ID"], "original")

    def test_target_failure_stops_bsr_and_closes_once(self):
        error = RuntimeError("fixture_target_failure")
        self.target_main.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            worker.run_steps([MAIN, TARGETS, BSR])
        self.assertIs(caught.exception, error)
        self.listing_main.assert_called_once()
        self.close.assert_called_once()
        self.assertEqual(self.environment["SEDA_RUN_ID"], "original")

    def test_keyboard_interrupt_closes_browser_and_restores_run_id(self):
        self.listing_main.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            worker.run_steps([MAIN, BSR])
        self.close.assert_called_once()
        self.assertEqual(self.environment["SEDA_RUN_ID"], "original")

    def test_module_import_failure_still_closes_owned_browser(self):
        self.import_module.side_effect = ImportError("fixture")
        with self.assertRaises(ImportError):
            worker.run_steps([MAIN])
        self.close.assert_called_once()
        self.assertEqual(self.environment["SEDA_RUN_ID"], "original")

    def test_invalid_detail_or_db_module_rejected_before_execution(self):
        for modules in ([], ["seda.casas_bahia.step08_detail_enrichment"], [MAIN, "seda.step14_db_load"]):
            with self.subTest(modules=modules), self.assertRaises(ValueError):
                worker.run_steps(modules)
        self.configure.assert_not_called()
        self.import_module.assert_not_called()
        self.close.assert_not_called()

    def test_worker_rejects_mode_one_two_without_configuring_or_launching(self):
        for mode in ("1", "2"):
            self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "requires_mode_3_or_4"):
                worker.run_steps([MAIN])
        self.configure.assert_not_called()
        self.import_module.assert_not_called()
        self.close.assert_not_called()

    def test_run_context_touches_only_explicit_nonsecret_variable(self):
        class NonEnumerable(dict):
            def __iter__(self):
                raise AssertionError("no environment enumeration")

            def copy(self):
                raise AssertionError("no environment snapshot")

        environment = NonEnumerable(SEDA_RUN_ID="before", UNRELATED="retained")
        with patch.object(worker.os, "environ", environment):
            with worker._run_context("bsr"):
                self.assertEqual(environment.get("SEDA_RUN_ID"), "bsr")
            self.assertEqual(environment.get("SEDA_RUN_ID"), "before")
            self.assertEqual(environment.get("UNRELATED"), "retained")

    def test_cli_help_and_invalid_module_make_no_worker_calls(self):
        with patch.object(worker, "run_steps") as run:
            with self.assertRaises(SystemExit) as stopped:
                worker.main(["--help"])
            self.assertEqual(stopped.exception.code, 0)
            with self.assertRaises(SystemExit):
                worker.main(["seda.step08_detail_enrichment"])
        run.assert_not_called()


class RetainedListingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.environment = {"SEDA_RUN_ID": "main", "SEDA_CASAS_BAHIA_LISTING_MODE": "3"}
        self.stack.enter_context(patch.object(listing.os, "environ", self.environment))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(listing, "selected_retailers", return_value=["casas_bahia"]))
        self.stack.enter_context(patch.object(listing, "page_numbers", return_value=[1]))
        self.run = self.stack.enter_context(patch.object(listing, "_main_with_retries", return_value="fixture_result"))
        self.close = self.stack.enter_context(patch.object(listing_modes, "close_browsers"))
        self.start = self.stack.enter_context(patch.object(diagnostics, "start_diagnostics", return_value=object()))
        self.finish = self.stack.enter_context(patch.object(diagnostics, "finish_diagnostics"))

    def test_retained_browser_still_finishes_per_stage_diagnostic_zip(self):
        self.assertEqual(listing.main(retain_browser=True), "fixture_result")
        self.close.assert_not_called()
        self.finish.assert_called_once_with(self.start.return_value, None, False)
        self.run.assert_called_once_with()

    def test_default_direct_stage_still_closes_browser(self):
        listing.main()
        self.close.assert_called_once_with()
        self.finish.assert_called_once_with(self.start.return_value, None, False)

    def test_retained_stage_failure_finishes_diagnostic_without_stealing_worker_cleanup(self):
        self.run.side_effect = SystemExit(9)
        with self.assertRaises(SystemExit) as caught:
            listing.main(retain_browser=True)
        self.assertEqual(caught.exception.code, 9)
        self.close.assert_not_called()
        self.finish.assert_called_once_with(self.start.return_value, "SystemExit", False)

    def test_main_then_bsr_use_same_session_without_intermediate_close(self):
        sentinel = object()
        observed = []
        fake_browser_api = SimpleNamespace(session=None)

        def simulate_listing():
            if fake_browser_api.session is None:
                fake_browser_api.session = sentinel
            observed.append((self.environment["SEDA_RUN_ID"], fake_browser_api.session))

        self.run.side_effect = simulate_listing
        with worker._run_context("main"):
            listing.main(retain_browser=True)
        with worker._run_context("bsr"):
            listing.main(retain_browser=True)
        self.assertEqual(observed, [("main", sentinel), ("bsr", sentinel)])
        self.close.assert_not_called()
        self.assertEqual(self.finish.call_count, 2)
        self.assertEqual([call.args[1] for call in self.start.call_args_list], ["main", "bsr"])


class WorkerIntegratedDiagnosticsTests(unittest.TestCase):
    """Real worker, stage finalizers, API session and ZIP writers; fake I/O only."""

    def _exercise(self, fail_main_ssr=False):
        from seda import parsers, step00_config as config, step02_main_targets as targets
        from seda.casas_bahia import browser_api as api, browser_listing, search_api

        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory(
                prefix="worker_integrated_", dir=Path(__file__).parent))
            root = Path(directory)
            log_dir = root / "log"
            environment = {"SEDA_CASAS_BAHIA_LISTING_MODE": "3", "SEDA_PRODUCT_LINE": "TV",
                           "SEDA_RUN_ID": "original", "SEDA_TRANSLATE_OUTPUT": "0",
                           "SEDA_RUN_ROOT": str(root), "SEDA_CASAS_BAHIA_SEARCH_RETRIES": "2",
                           "SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "0"}
            stack.enter_context(patch.object(worker.os, "environ", environment))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(api, "_SESSION", None))
            stack.enter_context(patch.object(browser_listing, "_SESSION", None))
            stack.enter_context(patch.object(diagnostics, "_ACTIVE", None))
            stack.enter_context(patch.object(api.time, "sleep"))
            stack.enter_context(patch.object(listing, "selected_retailers", return_value=["casas_bahia"]))
            stack.enter_context(patch.object(targets, "run_root", return_value=root))
            stack.enter_context(patch.object(config, "db_connect", side_effect=AssertionError("DB forbidden")))
            pages_for = lambda run_id: [1, 2] if fail_main_ssr and run_id == "main" else [1]
            stack.enter_context(patch.object(listing, "page_numbers", side_effect=pages_for))

            original_constructor = diagnostics.AutomaticListingDiagnostics.__init__

            def owned_log_constructor(instance, **kwargs):
                kwargs["log_dir"] = log_dir
                original_constructor(instance, **kwargs)

            stack.enter_context(patch.object(diagnostics.AutomaticListingDiagnostics,
                                            "__init__", owned_log_constructor))
            finish = stack.enter_context(patch.object(diagnostics, "finish_diagnostics",
                                                      wraps=diagnostics.finish_diagnostics))
            browser = Mock()
            browser.major = 153
            browser.page_ids = {}
            original_frame = {"id": "owned-top", "loaderId": "initial-loader"}
            recovered_frame = {"id": "owned-top", "loaderId": "bsr-recovered-loader"}
            browser.driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": original_frame}}

            def source_products(count=1):
                return [{"id": 10 + index, "sku": 100 + index,
                         "title": f"Smart TV Samsung 55 DU8000 fixture {index}",
                         "url": f"/smart-tv/p/{100 + index}"} for index in range(count)]

            def offers(count=1):
                return {"Ofertas": [
                    {"PrecoVenda": {"IdProduto": 10 + index, "IdSku": 100 + index,
                                    "IdLojista": 7, "PrecoDe": 1200, "Preco": 900},
                     "Disponibilidade": {"IdSku": 100 + index, "IdLojista": 7}}
                    for index in range(count)]}

            def navigation(url, timeout=None):
                if browser.fetch.call_count == 1:
                    return {"success": True, "status_code": 200}
                if environment["SEDA_RUN_ID"] == "main":
                    return {"success": False, "status_code": 200,
                            "error": "document_not_completed", "trace": []}
                parser_data, _ = api._parser_context({"products": source_products()},
                                                      browser_listing._request_identity(url))
                raw = search_api._as_next_data_html(parser_data, url)
                enriched, _, _ = browser_listing._enrich_document(raw, url, [offers()])
                browser.driver.execute_cdp_cmd.return_value = {"frameTree": {"frame": recovered_frame}}
                return {"success": True, "status_code": 200, "text": enriched, "trace": []}

            browser.fetch.side_effect = navigation
            constructor = stack.enter_context(patch.object(api, "ProbeBrowserSession", return_value=browser))

            def browser_fetch(driver, endpoint, params, method, headers, body, timeout, document_frame):
                self.assertIs(driver, browser.driver)
                run_id = environment["SEDA_RUN_ID"]
                if fail_main_ssr and run_id == "main" and str(params.get("page")) == "2":
                    return {"status_code": 403, "error": "api_http_not_200"}, None
                count = 300 if fail_main_ssr and run_id == "main" else 1
                data = {"products": source_products(count)} if method == "GET" else offers(count)
                return {"status_code": 200, "ok": True, "json": True, "elapsed_seconds": 0.01}, data

            fetch = stack.enter_context(patch.object(api, "_browser_fetch", side_effect=browser_fetch))
            original_network = api._network_evidence
            stage_sessions, stage_observers, snapshots = [], [], []

            def run_listing_pages():
                run_id = environment["SEDA_RUN_ID"]
                rows, failures = [], []
                stage_observers.append(diagnostics._ACTIVE.observer)
                for page in pages_for(run_id):
                    diagnostics.record_event("page_start", page)
                    url = config.page_url(config.RETAILERS["casas_bahia"], page, run_id)
                    result = api.fetch_listing(url)
                    parsed = parsers.parse_listing(result.get("text", ""), "Casas Bahia",
                                                   "https://www.casasbahia.com.br", url, run_id=run_id)
                    if result["success"]:
                        for index, row in enumerate(parsed, len(rows) + 1):
                            row["bsr_rank" if run_id == "bsr" else "main_rank"] = index
                        rows.extend(parsed)
                    else:
                        failures.append({"retailer": "Casas Bahia", "page": page})
                    unique = len({config.product_identity(row) for row in rows})
                    diagnostics.record_event("page_end", page, result["success"], len(parsed),
                                             unique, result.get("error", ""), result["trace"])
                policy = listing._casas_listing_threshold(run_id, unique, failures, not failures)
                self.assertTrue(policy["downstream_allowed"])
                config.write_csv(root / run_id / "parsed" / "main_occurrences.csv", rows)
                diagnostics.record_event("manifest_ready", {"complete": not failures, **policy})
                stage_sessions.append(api._SESSION)
                snapshots.append((run_id, api._SESSION.document_frame, api._SESSION.ssr_recovery_required))

            stack.enter_context(patch.object(listing, "_main_with_retries", side_effect=run_listing_pages))
            real_targets = targets.main

            def check_then_build_targets():
                self.assertIsNone(diagnostics._ACTIVE)
                self.assertIs(api._browser_fetch, fetch)
                self.assertIs(api._network_evidence, original_network)
                self.assertIs(api._SESSION, stage_sessions[0])
                browser.close.assert_not_called()
                real_targets()

            stack.enter_context(patch.object(targets, "main", side_effect=check_then_build_targets))
            close = stack.enter_context(patch.object(listing_modes, "close_browsers", wraps=listing_modes.close_browsers))
            self.assertEqual(worker.run_steps([MAIN, TARGETS, BSR]), 0)

            constructor.assert_called_once_with()
            close.assert_called_once_with()
            browser.close.assert_called_once_with()
            self.assertIs(stage_sessions[0], stage_sessions[1])
            self.assertIsNone(api._SESSION)
            self.assertIsNone(diagnostics._ACTIVE)
            self.assertIs(api._browser_fetch, fetch)
            self.assertIs(api._network_evidence, original_network)
            self.assertIsNot(stage_observers[0], stage_observers[1])
            self.assertEqual(environment["SEDA_RUN_ID"], "original")
            self.assertEqual(finish.call_count, 2)
            archives = list(log_dir.glob("*.zip"))
            self.assertEqual(len(archives), 2)
            reports = {}
            for path in archives:
                with zipfile.ZipFile(path) as archive:
                    report = json.loads(archive.read("report.json"))
                reports[report["run"]["run_id"]] = report
            self.assertEqual(set(reports), {"main", "bsr"})
            self.assertEqual(reports["main"]["outcome"]["diagnostic_errors"], 0)
            self.assertEqual(reports["bsr"]["outcome"]["diagnostic_errors"], 0)
            self.assertEqual(reports["bsr"]["outcome"]["outcome"], "completed")
            main_targets = config.read_csv(root / "output" / "seda_main_targets.csv")
            self.assertEqual(len(main_targets), 300 if fail_main_ssr else 1)
            self.assertEqual(str(main_targets[0]["main_rank"]), "1")
            bsr_rows = config.read_csv(root / "bsr" / "parsed" / "main_occurrences.csv")
            self.assertEqual(str(bsr_rows[0]["bsr_rank"]), "1")

            get_calls = [call for call in fetch.call_args_list if call.args[3] == "GET"]
            if fail_main_ssr:
                self.assertEqual(snapshots[0], ("main", None, True))
                self.assertEqual(snapshots[1], ("bsr", recovered_frame, False))
                self.assertEqual(len(get_calls), 4)  # Main page 1 + three denied page-2 calls only.
                self.assertFalse(any(call.args[2].get("sortby") for call in get_calls))
                self.assertEqual(reports["main"]["outcome"]["outcome"], "accepted_with_failures")
                self.assertEqual(reports["bsr"]["api_timeline"], [])
                bsr_navigation = browser.fetch.call_args_list[-1].args[0]
                self.assertEqual(parse_qs(urlparse(bsr_navigation).query)["ordenacao"], ["maisvendidos"])
                self.assertEqual(browser.fetch.call_count, 4)  # Bootstrap, two failed SSRs, BSR recovery.
            else:
                self.assertEqual(browser.fetch.call_count, 1)
                self.assertEqual(len(get_calls), 2)
                self.assertFalse(get_calls[0].args[2].get("sortby"))
                self.assertEqual(get_calls[1].args[2]["sortby"], "maisvendidos")
                self.assertEqual(get_calls[0].args[2]["sessionid"], get_calls[1].args[2]["sessionid"])
                self.assertEqual([call["call_number"] for call in reports["main"]["api_timeline"]], [1, 2])
                self.assertEqual([call["call_number"] for call in reports["bsr"]["api_timeline"]], [1, 2])

    def test_real_worker_retains_session_and_finalizes_two_independent_zip_observers(self):
        self._exercise()

    def test_bsr_recovers_failed_main_ssr_document_before_any_api_call(self):
        self._exercise(fail_main_ssr=True)


class ExecutionGroupingTests(unittest.TestCase):
    def setUp(self):
        self.environment = {"SEDA_CASAS_BAHIA_LISTING_MODE": "3"}
        self.patch = patch.object(orchestrator.os, "environ", self.environment)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def groups(self, steps, retailer="casas_bahia", package="seda.casas_bahia"):
        return list(orchestrator._execution_groups(retailer, package, steps))

    def test_main_targets_bsr_are_one_worker_group(self):
        steps = listing_steps()
        self.assertEqual(self.groups(steps), [(tuple(steps), True)])

    def test_explicit_mode_four_keeps_main_bsr_together(self):
        steps = listing_steps()
        self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = "4"
        self.assertEqual(self.groups(steps), [(tuple(steps), True)])

    def test_unset_default_mode_one_keeps_stages_separate(self):
        steps = listing_steps()
        self.environment.pop("SEDA_CASAS_BAHIA_LISTING_MODE")
        self.assertEqual(self.groups(steps), [((step,), False) for step in steps])

    def test_explicit_subset_is_not_expanded(self):
        main, targets, bsr = listing_steps()
        self.assertEqual(self.groups([main, bsr]), [((main, bsr), True)])
        self.assertEqual(self.groups([bsr]), [((bsr,), True)])
        self.assertEqual(self.groups([targets]), [((targets,), False)])

    def test_selected_order_preserved_including_unusual_order(self):
        main, targets, bsr = listing_steps()
        self.assertEqual(self.groups([bsr, targets, main]), [((bsr, targets, main), True)])

    def test_nonlisting_step_breaks_lifecycle_group(self):
        main, targets, bsr = listing_steps()
        detail = orchestrator.Step(6, "detail_enrichment", "seda.casas_bahia.step08_detail_enrichment")
        self.assertEqual(self.groups([main, targets, detail, bsr]),
                         [((main, targets), True), ((detail,), False), ((bsr,), True)])

    def test_distinct_stage_environment_not_discarded_by_grouping(self):
        main, targets, bsr = listing_steps()
        bsr = orchestrator.Step(bsr.number, bsr.name, bsr.module, {"SEDA_FIXTURE_NON_SECRET": "changed"})
        self.assertEqual(self.groups([main, targets, bsr]), [((main, targets), True), ((bsr,), True)])

    def test_modes_one_two_remain_separate_subprocess_steps(self):
        steps = listing_steps()
        for mode in ("1", "2", "invalid"):
            self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = mode
            with self.subTest(mode=mode):
                self.assertEqual(self.groups(steps), [((step,), False) for step in steps])

    def test_magalu_and_noncanonical_package_never_group(self):
        steps = listing_steps()
        self.assertEqual(self.groups(steps, retailer="magalu", package="seda.magalu"),
                         [((step,), False) for step in steps])
        self.assertEqual(self.groups(steps, package="custom.casas_bahia"),
                         [((step,), False) for step in steps])

    def test_mismatched_step_name_or_module_never_grouped(self):
        wrong = orchestrator.Step(1, "main_list", "seda.magalu.step01_main_list")
        self.assertEqual(self.groups([wrong]), [((wrong,), False)])


class OrchestratorDispatchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.environment = {"SEDA_CASAS_BAHIA_LISTING_MODE": "3", "SEDA_RUN_ROOT": "C:/fixture/unused",
                            "SEDA_PRODUCT_LINE": "TV"}
        self.stack.enter_context(patch.object(orchestrator.os, "environ", self.environment))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.configure = self.stack.enter_context(patch.object(orchestrator, "configure_retailer"))
        self.env = self.stack.enter_context(patch.object(orchestrator, "step_env", return_value={"fixture": "context"}))
        self.usage = self.stack.enter_context(patch.object(orchestrator, "start_zenrows_usage_execution"))
        self.complete = self.stack.enter_context(patch.object(orchestrator, "assert_detail_publish_complete"))
        self.chosen = listing_steps() + [orchestrator.Step(4, "bsr_rank", "seda.casas_bahia.step04_bsr_rank")]
        self.stack.enter_context(patch.object(orchestrator, "selected_steps", side_effect=lambda *args: self.chosen))
        self.run = self.stack.enter_context(patch.object(orchestrator, "run_module", return_value=0))

    def dispatch(self, dry=False, retailer="casas_bahia", package="seda.casas_bahia"):
        argv = ["orchestrator", "--all"] + (["--dry-run"] if dry else [])
        with patch("sys.argv", argv):
            orchestrator.run_retailer_orchestrator(retailer, package, "offline fixture")

    def test_worker_then_rank_preserves_order_and_subprocess_boundary(self):
        self.dispatch()
        self.assertEqual([call.args[0] for call in self.run.call_args_list],
                         [WORKER, "seda.casas_bahia.step04_bsr_rank"])
        self.assertEqual(self.run.call_args_list[0].kwargs["args"], [MAIN, TARGETS, BSR])
        self.assertEqual(self.run.call_args_list[0].kwargs["env"], {"fixture": "context"})
        self.assertNotIn("args", self.run.call_args_list[1].kwargs)
        self.usage.assert_called_once()

    def test_worker_failure_stops_before_rank_or_detail(self):
        self.run.return_value = 7
        with self.assertRaises(SystemExit) as caught:
            self.dispatch()
        self.assertEqual(caught.exception.code, 7)
        self.run.assert_called_once()

    def test_mode_four_dispatches_shared_listing_worker_before_rank(self):
        self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = "4"
        self.dispatch()
        self.assertEqual([call.args[0] for call in self.run.call_args_list],
                         [WORKER, "seda.casas_bahia.step04_bsr_rank"])
        self.assertEqual(self.run.call_args_list[0].kwargs["args"], [MAIN, TARGETS, BSR])

    def test_dry_run_prints_worker_command_without_starting_usage_or_completion_actions(self):
        self.dispatch(dry=True)
        self.assertTrue(all(call.kwargs["dry_run"] for call in self.run.call_args_list))
        self.usage.assert_not_called()
        self.complete.assert_not_called()

    def test_mode_two_uses_original_module_sequence(self):
        self.environment["SEDA_CASAS_BAHIA_LISTING_MODE"] = "2"
        self.dispatch()
        self.assertEqual([call.args[0] for call in self.run.call_args_list],
                         [MAIN, TARGETS, BSR, "seda.casas_bahia.step04_bsr_rank"])
        self.assertTrue(all("args" not in call.kwargs for call in self.run.call_args_list))

    def test_detail_completion_gate_remains_before_consumer_subprocess(self):
        self.chosen = [orchestrator.Step(8, "review20", "seda.casas_bahia.step09_review20")]
        self.dispatch()
        self.complete.assert_called_once()
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], "seda.casas_bahia.step09_review20")


class RunnerArgumentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.environment = {}
        self.stack.enter_context(patch.object(retailer_runner.os, "environ", self.environment))
        self.log = self.stack.enter_context(patch.object(retailer_runner, "_log_line"))
        self.call = self.stack.enter_context(patch.object(retailer_runner.subprocess, "call", return_value=0))
        self.tee = self.stack.enter_context(patch.object(retailer_runner, "_call_with_live_log", return_value=0))

    def test_optional_worker_arguments_forwarded_without_shell(self):
        retailer_runner.run_module(WORKER, args=[MAIN, TARGETS, BSR])
        self.assertEqual(self.call.call_args.args[0], [retailer_runner.PYTHON, "-m", WORKER, MAIN, TARGETS, BSR])
        self.assertNotIn("shell", self.call.call_args.kwargs)

    def test_existing_no_arguments_call_signature_unchanged(self):
        retailer_runner.run_module(MAIN, {"FIXTURE": "1"}, False)
        self.assertEqual(self.call.call_args.args[0], [retailer_runner.PYTHON, "-m", MAIN])
        self.assertEqual(self.call.call_args.kwargs["env"], {"FIXTURE": "1"})

    def test_worker_output_uses_existing_live_log_tee(self):
        self.environment["SEDA_RUN_LOG_FILE"] = "fixture-unused.log"
        retailer_runner.run_module(WORKER, args=[MAIN, BSR])
        self.tee.assert_called_once()
        self.assertEqual(self.tee.call_args.args[0], [retailer_runner.PYTHON, "-m", WORKER, MAIN, BSR])
        self.assertEqual(self.tee.call_args.args[2], "fixture-unused.log")
        self.call.assert_not_called()

    def test_dry_run_never_launches_subprocess_or_tee(self):
        self.assertEqual(retailer_runner.run_module(WORKER, dry_run=True, args=[MAIN, BSR]), 0)
        self.call.assert_not_called()
        self.tee.assert_not_called()
        self.assertIn(WORKER, self.log.call_args.args[0])
        self.assertIn(BSR, self.log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
