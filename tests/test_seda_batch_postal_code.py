"""Offline CMD postal destination contracts; no live crawler or config files."""
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
CMD = Path(r"C:\Windows\System32\cmd.exe")
FIELDS = ("SEDA_POSTAL_CODE", "SEDA_MAGALU_ZIP_CODE",
          "SEDA_MAGALU_SHIPPING_ZIP_CODE", "SEDA_CASAS_BAHIA_ZIPCODE")
CASES = [
    ("run_casas_bahia_tv_full.bat", (), 1),
    ("run_casas_bahia_ref_full.bat", (), 1),
    ("run_casas_bahia_ldy_full.bat", (), 1),
    ("run_casas_bahia_tv_ref_ldy_full.bat", (), 3),
    ("run_magalu_full.bat", ("TV",), 1),
    ("run_magalu_full.bat", ("REF",), 1),
    ("run_magalu_full.bat", ("LDY",), 1),
    ("run_magalu_tv_full.bat", (), 1),
    ("run_magalu_ref_full.bat", (), 1),
    ("run_magalu_ldy_full.bat", (), 1),
    ("run_magalu_tv_ref_ldy_full.bat", (), 3),
    ("run_magalu_tv_ref_ldy_drission_full.bat", (), 3),
    ("run_magalu_casas_interleaved_tv_ref_ldy_full.bat", (), 6),
    ("run_magalu_casas_interleaved_ref_ldy_full.bat", (), 4),
    ("run_magalu_casas_ref_ldy_seq.bat", (), 4),
    ("run_magalu_tv_smoke.bat", (), 1),
    # These scripts use python.exe without CALL. The .bat stub captures the
    # first detail process only; continuation behavior is outside this test.
    ("resume_magalu_step08.bat", ("TV", "25", "20260929"), 1),
    ("resume_magalu_step08.bat", ("REF", "25", "20260929"), 1),
    ("resume_magalu_step08.bat", ("LDY", "25", "20260929"), 1),
    ("resume_magalu_tv_step08.bat", ("25", "20260929"), 1),
]


@unittest.skipUnless(os.name == "nt" and CMD.is_file(), "requires Windows CMD")
class BatchPostalCodeTests(unittest.TestCase):
    def check_scenario(self, inherited):
        with tempfile.TemporaryDirectory(prefix="seda postal ") as temporary:
            root = Path(temporary)
            for name in {case[0] for case in CASES}:
                shutil.copyfile(ROOT / name, root / name)
            stub = root / "stub"
            stub.mkdir()
            foreign_cwd = root / "scheduler working directory"
            foreign_cwd.mkdir()
            capture = root / "calls.txt"
            lines = ["@echo off", '>> "%HARNESS_CAPTURE%" echo BEGIN',
                     '>> "%HARNESS_CAPTURE%" echo ARGS=%*']
            lines += [f'>> "%HARNESS_CAPTURE%" echo {key}=%{key}%' for key in FIELDS]
            lines += ['>> "%HARNESS_CAPTURE%" echo END', "exit /b 0"]
            (stub / "python.bat").write_text("\n".join(lines) + "\n", encoding="ascii")
            for name, args, expected_calls in CASES:
                with self.subTest(batch=name, args=args):
                    capture.write_text("", encoding="utf-8")
                    env = {
                        "SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows",
                        "ComSpec": str(CMD), "PATHEXT": ".BAT;.CMD;.EXE;.COM",
                        "PATH": str(stub) + r";C:\Windows\System32;C:\Windows\System32\WindowsPowerShell\v1.0",
                        "TEMP": str(root), "TMP": str(root),
                        "HARNESS_CAPTURE": str(capture),
                        "SEDA_ENV_PATH": str(root / "ENV_NOT_PRESENT"),
                        "SEDA_RUN_LOG_FILE": str(root / "batch.log"), **inherited,
                    }
                    command = "call ..\\" + name + (" " + " ".join(args) if args else "")
                    result = subprocess.run([str(CMD), "/d", "/c", command],
                                            cwd=foreign_cwd, env=env,
                                            capture_output=True, timeout=45)
                    self.assertEqual(result.returncode, 0, name)
                    calls, current = [], None
                    for line in capture.read_text(encoding="utf-8").splitlines():
                        if line == "BEGIN":
                            self.assertIsNone(current)
                            current = {}
                        elif line == "END":
                            self.assertIsNotNone(current)
                            calls.append(current)
                            current = None
                        else:
                            self.assertIsNotNone(current)
                            key, separator, value = line.partition("=")
                            self.assertTrue(separator)
                            current[key] = value
                    self.assertIsNone(current)
                    collection = [call for call in calls if "_orchestrator " in call["ARGS"]
                                  or "step08_detail_enrichment" in call["ARGS"]]
                    self.assertEqual(len(collection), expected_calls)
                    for call in collection:
                        self.assertEqual(call["SEDA_POSTAL_CODE"], "01010-010")
                        if "seda.magalu." in call["ARGS"]:
                            self.assertEqual(call["SEDA_MAGALU_ZIP_CODE"], "01010-010")
                            self.assertEqual(call["SEDA_MAGALU_SHIPPING_ZIP_CODE"], "01010-010")
                        else:
                            self.assertEqual(call["SEDA_CASAS_BAHIA_ZIPCODE"], "01010010")
                    if name in ("run_magalu_tv_ref_ldy_full.bat", "run_casas_bahia_tv_ref_ldy_full.bat"):
                        for call in collection:
                            self.check_request_destination(call, root / "ENV_NOT_PRESENT")

    def check_request_destination(self, call, disabled_env):
        self.assertFalse(disabled_env.exists())
        with patch.dict(os.environ, {key: call[key] for key in FIELDS if call[key]} |
                        {"SEDA_ENV_PATH": str(disabled_env)}, clear=True):
            # Imports use only the nonexistent environment path above.
            if "seda.magalu." in call["ARGS"]:
                from seda.magalu import detail_api, search_api
                payload = search_api._payload("https://www.magazineluiza.com.br/busca/tv/", 20)
                self.assertEqual(payload["variables"]["zipCode"], "01010-010")
                with patch.object(detail_api, "_post", return_value={}) as post:
                    detail_api._request_item("fixture-item", 1, [])
                self.assertEqual(post.call_args.args[0]["variables"]["zipcode"], "01010010")
                self.assertEqual(detail_api._shipping_request({})["zipcode"], "01010010")
            else:
                from seda.casas_bahia import detail_api
                response = Mock(status_code=200, headers={"content-type": "application/json"})
                response.json.return_value = {"options": []}
                with ExitStack() as stack:
                    stack.enter_context(patch.object(detail_api, "_headers", return_value={}))
                    stack.enter_context(patch.object(detail_api, "throttle"))
                    stack.enter_context(patch.object(detail_api, "_freight_transports", return_value=["requests"]))
                    request = stack.enter_context(patch.object(detail_api, "_freight_get", return_value=response))
                    self.assertTrue(detail_api.fetch_freight("fixture-sku", "fixture-seller")["success"])
                    self.assertIn("/zipcode/01010-010/source/CB", request.call_args.args[1])

    def test_default_destination(self):
        self.check_scenario({})

    def test_legacy_common_destination_is_replaced(self):
        self.check_scenario({"SEDA_POSTAL_CODE": "01001-001"})

    def test_conflicting_retailer_destinations_are_replaced(self):
        self.check_scenario(dict(zip(FIELDS, ("01001-001", "01311-000", "20040-020", "01001001"))))

    def test_empty_overrides_use_fixed_destination(self):
        self.check_scenario({key: "" for key in FIELDS})


if __name__ == "__main__":
    unittest.main()
