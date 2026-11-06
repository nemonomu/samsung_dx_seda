import unittest
from unittest.mock import patch

from seda.casas_bahia import casas_bahia_orchestrator


class CasasBahiaOrchestratorLoggingTests(unittest.TestCase):
    def test_unexpected_failure_logs_only_exception_type_and_still_fails(self):
        with patch.dict("os.environ", {"SEDA_RUN_LOG_FILE": "fixture.log"}), patch.object(
            casas_bahia_orchestrator, "run_retailer_orchestrator",
            side_effect=RuntimeError("sensitive exception details"),
        ), patch.object(casas_bahia_orchestrator, "_log_line") as log:
            with self.assertRaises(RuntimeError):
                casas_bahia_orchestrator.main()
        log.assert_called_once_with("[casas_bahia] orchestrator failed: RuntimeError", "fixture.log")


if __name__ == "__main__":
    unittest.main()
