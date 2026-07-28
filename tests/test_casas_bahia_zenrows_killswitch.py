import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from seda.casas_bahia.detail_api import (
    _fetch_freight_zenrows_pdp,
    _fetch_product_source_zenrows,
)


def _failed_result(error):
    return SimpleNamespace(
        success=False,
        text="",
        error=error,
        status_code=0,
        estimated_multiplier="10x",
        headers={},
    )


class CasasBahiaZenRowsKillSwitchTests(unittest.TestCase):
    def test_product_source_never_overrides_global_kill_switch_or_dry_run(self):
        cases = (
            (
                {
                    "SEDA_ALLOW_ZENROWS": "0",
                    "SEDA_ZENROWS_DRY_RUN": "0",
                },
                "zenrows_disabled",
            ),
            (
                {
                    "SEDA_ALLOW_ZENROWS": "1",
                    "SEDA_ZENROWS_DRY_RUN": "1",
                },
                "zenrows_dry_run",
            ),
        )
        for env, expected_error in cases:
            with self.subTest(expected_error=expected_error), patch.dict(
                os.environ,
                env,
                clear=True,
            ), patch("seda.magalu.zenrows_client.requests.get") as network:
                result = _fetch_product_source_zenrows(
                    "https://www.casasbahia.com.br/product/p/1",
                    timeout=5,
                )
                self.assertEqual(
                    os.environ["SEDA_ALLOW_ZENROWS"],
                    env["SEDA_ALLOW_ZENROWS"],
                )
                self.assertEqual(
                    os.environ["SEDA_ZENROWS_DRY_RUN"],
                    env["SEDA_ZENROWS_DRY_RUN"],
                )
                network.assert_not_called()

            self.assertFalse(result["success"])
            self.assertIn(expected_error, result["error"])

    def test_freight_preflight_failures_stop_after_one_iteration(self):
        cases = (
            (
                "zenrows_disabled",
                {"SEDA_ALLOW_ZENROWS": "0", "SEDA_ZENROWS_DRY_RUN": "0"},
            ),
            (
                "zenrows_dry_run",
                {"SEDA_ALLOW_ZENROWS": "1", "SEDA_ZENROWS_DRY_RUN": "1"},
            ),
            (
                "key_missing",
                {"SEDA_ALLOW_ZENROWS": "1", "SEDA_ZENROWS_DRY_RUN": "0"},
            ),
        )
        for error, switches in cases:
            observed = []

            def request_url(*args, **kwargs):
                observed.append(
                    (
                        os.environ.get("SEDA_ALLOW_ZENROWS"),
                        os.environ.get("SEDA_ZENROWS_DRY_RUN"),
                    )
                )
                return _failed_result(error)

            env = {
                **switches,
                "SEDA_CASAS_BAHIA_FREIGHT_ZENROWS_ATTEMPTS": "3",
                "SEDA_ZENROWS_SESSION_ID": "777",
            }
            with self.subTest(error=error), patch.dict(
                os.environ,
                env,
                clear=True,
            ), patch(
                "seda.magalu.zenrows_client.request_url",
                side_effect=request_url,
            ) as request, patch(
                "seda.casas_bahia.detail_api._dump_zenrows_freight_response"
            ):
                result = _fetch_freight_zenrows_pdp(
                    "https://www.casasbahia.com.br/product/p/1",
                    "sku-1",
                    "seller-1",
                    timeout=5,
                )
                self.assertEqual(os.environ["SEDA_ALLOW_ZENROWS"], switches["SEDA_ALLOW_ZENROWS"])
                self.assertEqual(os.environ["SEDA_ZENROWS_DRY_RUN"], switches["SEDA_ZENROWS_DRY_RUN"])
                self.assertEqual(os.environ["SEDA_ZENROWS_SESSION_ID"], "777")

            self.assertFalse(result["success"])
            self.assertIn(error, result["error"])
            self.assertEqual(observed, [(switches["SEDA_ALLOW_ZENROWS"], switches["SEDA_ZENROWS_DRY_RUN"])])
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
