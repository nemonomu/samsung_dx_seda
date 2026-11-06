import io
import unittest
from unittest.mock import MagicMock, patch

from seda.common import retailer_runner


class RetailerRunnerLoggingTests(unittest.TestCase):
    def test_log_write_or_flush_failure_does_not_abort_child(self):
        for method in ("write", "flush"):
            with self.subTest(method=method):
                process = MagicMock(stdout=iter(["first\n", "second\n"]))
                process.wait.return_value = 7
                log = MagicMock()
                getattr(log, method).side_effect = OSError("disk full")
                log.close.side_effect = OSError("disk full")
                with patch.object(retailer_runner.subprocess, "Popen", return_value=process), patch(
                    "builtins.open", return_value=log
                ), patch("sys.stdout", new_callable=io.StringIO) as output:
                    result = retailer_runner._call_with_live_log(["fake"], {}, "fake.log")
                self.assertEqual(result, 7)
                process.wait.assert_called_once()
                self.assertIn("second", output.getvalue())
                self.assertIn("log file disabled: OSError", output.getvalue())
                self.assertEqual(log.close.call_count, 1)

    def test_log_close_failure_preserves_child_exit_code(self):
        process = MagicMock(stdout=iter(["done\n"]))
        process.wait.return_value = 0
        log = MagicMock()
        log.close.side_effect = OSError("disk full")
        with patch.object(retailer_runner.subprocess, "Popen", return_value=process), patch(
            "builtins.open", return_value=log
        ), patch("sys.stdout", new_callable=io.StringIO):
            result = retailer_runner._call_with_live_log(["fake"], {}, "fake.log")
        self.assertEqual(result, 0)
        process.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
