import json
import os
import unittest
from unittest.mock import Mock, patch, sentinel

from seda.magalu import zenrows_client
from seda.magalu.detail_api import fetch_item_fields_via_zenrows


class ZenRowsClientTest(unittest.TestCase):
    def _enabled_env(self):
        return patch.dict(
            os.environ,
            {
                "SEDA_ALLOW_ZENROWS": "1",
                "SEDA_ZENROWS_DRY_RUN": "0",
            },
            clear=False,
        )

    def test_request_exception_is_reduced_to_exception_type(self):
        with self._enabled_env(), patch(
            "seda.magalu.zenrows_client.api_key",
            return_value=sentinel.api_key,
        ), patch(
            "seda.magalu.zenrows_client.requests.get",
            side_effect=RuntimeError("prepared request failed"),
        ):
            result = zenrows_client.request_url("https://example.com", profile="basic_html")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "request_error:RuntimeError")
        self.assertNotIn("prepared request failed", result.error)

    def test_request_json_forwards_post_with_br_premium_profile(self):
        response = Mock(ok=True, status_code=200, text='{"data":{"item":{"id":"abc"}}}')
        response.headers = {"X-Request-Cost": "0.01"}
        payload = {"operationName": "itemQuery", "variables": {"itemId": "abc"}}
        with self._enabled_env(), patch(
            "seda.magalu.zenrows_client.api_key",
            return_value=sentinel.api_key,
        ), patch(
            "seda.magalu.zenrows_client.requests.post",
            return_value=response,
        ) as request_post:
            result = zenrows_client.request_json(
                "https://federation.magazineluiza.com.br/graphql?operationName=itemQuery",
                payload,
                profile="premium_html",
                extra={"custom_headers": "true"},
                extra_headers={"content-type": "application/json"},
            )

        self.assertTrue(result.success)
        self.assertEqual(result.estimated_multiplier, "10x")
        self.assertEqual(result.params["premium_proxy"], "true")
        self.assertEqual(result.params["proxy_country"], "br")
        _, kwargs = request_post.call_args
        self.assertEqual(kwargs["json"], payload)
        self.assertEqual(kwargs["headers"]["content-type"], "application/json")
        self.assertNotIn("apikey", result.params)

    def test_unknown_profile_fails_without_transport_request(self):
        with patch("seda.magalu.zenrows_client.requests.get") as request_get:
            result = zenrows_client.request_url(
                "https://example.com",
                profile="not-a-profile",
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "unknown_profile:not-a-profile")
        request_get.assert_not_called()

    def test_itemquery_wrapper_requires_exact_response_identity(self):
        def response(item_id):
            return zenrows_client.ZenRowsResult(
                True,
                "https://federation.magazineluiza.com.br/graphql",
                "auto_custom_headers",
                status_code=200,
                text=json.dumps(
                    {
                        "data": {
                            "item": {
                                "id": item_id,
                                "title": 'Smart TV Samsung 55" 55U8600F',
                                "factsheet": [
                                    {
                                        "keyName": "Referencia",
                                        "value": "UN55U8600FGXZD",
                                    }
                                ],
                                "attributes": [],
                                "offers": [],
                                "rating": {},
                            }
                        }
                    }
                ),
                headers={"X-Request-Cost": "unit"},
                estimated_multiplier="auto:1x/5x/10x/25x_success_only",
            )

        with patch.dict(
            os.environ,
            {
                "SEDA_PRODUCT_LINE": "TV",
                "SEDA_MAGALU_ZENROWS_FIELD_PROFILE": "auto_custom_headers",
            },
            clear=False,
        ), patch(
            "seda.magalu.zenrows_client.request_json",
            return_value=response("abc"),
        ) as request_json:
            result = fetch_item_fields_via_zenrows(
                "abc",
                context_url="https://www.magazineluiza.com.br/product/p/abc/et/tv4k/",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["detail"]["_detail_item_id"], "abc")
        self.assertEqual(result["detail"]["sku"], "UN55U8600FGXZD")
        kwargs = request_json.call_args.kwargs
        self.assertEqual(kwargs["profile"], "auto_custom_headers")
        self.assertEqual(kwargs["extra"]["proxy_country"], "br")
        self.assertEqual(kwargs["extra"]["custom_headers"], "true")

        with patch.dict(
            os.environ,
            {"SEDA_PRODUCT_LINE": "TV"},
            clear=False,
        ), patch(
            "seda.magalu.zenrows_client.request_json",
            return_value=response("different"),
        ):
            mismatch = fetch_item_fields_via_zenrows("abc")
        self.assertFalse(mismatch["success"])
        self.assertEqual(mismatch["error"], "item_identity_mismatch")
        self.assertEqual(mismatch["detail"], {})


if __name__ == "__main__":
    unittest.main()
