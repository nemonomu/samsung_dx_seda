"""Offline Mode 1 parity against the 1290145 REST collection contract.

The historical search loop and product-first price join below were transcribed
from ``git show 1290145:seda/casas_bahia/{search_api,price_api}.py``. Request
headers are deliberately stubbed: no credential source or real header builder
is read or invoked. Shared price normalization/config/parser functions were
unchanged by the Mode 1 transport restoration; both sides use those functions.
No Git, network, browser, environment file, or DB action occurs in these tests.
"""

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import html
import io
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

_DISABLED_ENV = Path(__file__).with_name("HISTORICAL_PARITY_NO_ENV_FILE")
if _DISABLED_ENV.exists():
    raise RuntimeError("test_env_guard_exists")
os.environ["SEDA_ENV_PATH"] = str(_DISABLED_ENV)

import requests
from seda import parsers
from seda.casas_bahia import listing_modes, price_api, search_api, rest_legacy
from seda.step00_config import casas_bahia_search_term


def _historical_params(url):
    """1290145 search parameters with a fixed synthetic session in test env."""
    query = parse_qs(urlparse(url).query)
    first = lambda key: (query.get(key) or [""])[0]
    sort = first("ordenacao") or first("sortby")
    value = {
        "resultsperpage": os.getenv("SEDA_CASAS_BAHIA_RESULTS_PER_PAGE", "20"),
        "apikey": "casasbahia", "page": first("page") or "1",
        "variantconfiguration": os.getenv("SEDA_CASAS_BAHIA_VARIANT_CONFIGURATION", "q2"),
        "regionid": os.getenv("SEDA_CASAS_BAHIA_REGION_ID", "126000"),
        "multiselection": "true", "partnerkey": "elastic", "terms": casas_bahia_search_term(),
        "sessionid": os.environ["SEDA_CASAS_BAHIA_SESSION_ID"],
        "userid": os.getenv("SEDA_CASAS_BAHIA_USER_ID", ""), "wps": "true", "device": "desktop",
    }
    if sort:
        value["sortby"] = sort.replace("-", "")
    return value


def _historical_next_data(search, url):
    data = {"props": {"pageProps": {"initialState": {"search": {
        "query": search.get("queries") or {}, "searchTerm": casas_bahia_search_term(),
        "results": {"products": search.get("products") or []},
    }}}}, "page": urlparse(url).path or "/tv/b", "query": {"source_url": url}}
    return '<script id="__NEXT_DATA__" type="application/json">' + html.escape(
        json.dumps(data, ensure_ascii=False)) + "</script>"


def _historical_price_attach(products, timeout=None):
    """1290145 join order, intentionally including its product-first behavior."""
    if os.getenv("SEDA_CASAS_BAHIA_ATTACH_PRICES", "1").lower() not in {"1", "true", "yes", "y"}:
        return {"success": False, "prices": {}, "error": "price_attach_disabled"}
    try:
        # The non-include_offers normalization is the unchanged historical API.
        result = price_api.fetch_listing_prices(products, timeout=timeout)
        if not result.get("success"):
            return result
        prices = result.get("prices") or {}
        for product in products:
            if not isinstance(product, dict):
                continue
            price = prices.get(str(product.get("id"))) or prices.get(str(product.get("sku")))
            if not price:
                continue
            product["price"] = price
            if price.get("sellerId") and not product.get("lojista"):
                product["lojista"] = price.get("sellerId")
            if price.get("skuId") and not product.get("sku"):
                product["sku"] = price.get("skuId")
        return result
    except Exception as exc:
        return {"success": False, "prices": {}, "error": f"{type(exc).__name__}: {exc}"}


def _historical_fetch(url, timeout=None):
    """1290145 loop: nonempty products succeeds regardless of price result."""
    parsed = urlparse(url)
    if "casasbahia.com.br" not in parsed.netloc or not search_api._supported_listing_path(parsed.path):
        return {"success": False, "error": "not_casas_bahia_listing_url", "text": "", "trace": []}
    timeout = int(timeout or os.getenv("SEDA_TIMEOUT", "60"))
    retries = int(os.getenv("SEDA_CASAS_BAHIA_SEARCH_RETRIES", "2"))
    sleep_seconds = float(os.getenv("SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS", "3.0"))
    trace = []
    session = requests.Session()
    params = _historical_params(url)
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(sleep_seconds * attempt)
        try:
            response = session.get(search_api.SEARCH_URL, params=params, headers={}, timeout=timeout)
        except Exception as exc:
            trace.append({"attempt": attempt + 1, "status_code": 0,
                          "error": f"{type(exc).__name__}: {exc}"})
            continue
        trace_item = {"attempt": attempt + 1, "status_code": response.status_code,
                      "length": len(response.text or "")}
        trace.append(trace_item)
        if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
            trace_item["error"] = "non_json_or_blocked"
            continue
        try:
            parsed_response = response.json()
        except ValueError:
            trace_item["error"] = "invalid_json"
            continue
        products = parsed_response.get("products") or []
        if products:
            price_result = _historical_price_attach(products, timeout=timeout)
            trace_item["price_count"] = price_result.get("count", 0)
            if price_result.get("error"):
                trace_item["price_error"] = price_result.get("error")
            return {"success": True, "text": _historical_next_data(parsed_response, url),
                    "products": len(products), "trace": trace}
        trace_item["error"] = "empty_products"
    return {"success": False, "error": "casas_bahia_partner_api_failed", "text": "", "trace": trace}


def product(line="TV", product_id=10, sku=100, seller=7):
    names = {"TV": "Smart TV Samsung 55 Polegadas UN55DU8000",
             "REF": "Geladeira Samsung Frost Free RT38",
             "LDY": "Lavadora Samsung WW11T 11kg"}
    return {"id": product_id, "sku": sku, "lojista": seller,
            "name": names[line], "url": f"https://www.casasbahia.com.br/fixture-{sku}/p/{sku}",
            "flags": [{"description": "Cupom especial"}], "rating": 4.5, "ratingCount": 20}


def raw_offer(product_id=10, sku=100, seller=7, current=850, old=1200):
    return {"PrecoVenda": {"IdProduto": product_id, "IdSku": sku, "IdLojista": seller,
            "PrecoDe": old, "Preco": current, "PercentualDesconto": 25},
            "DescontoFormaPagamento": {"PrecoVendaComDesconto": current - 50,
                                      "DescricaoDesconto": "Pagamento Pix"},
            "Disponibilidade": {"IdProduto": product_id, "IdSku": sku, "IdLojista": seller}}


def response(payload, status=200, content_type="application/json", invalid_json=False):
    def parse():
        if invalid_json:
            raise ValueError("synthetic invalid json")
        return deepcopy(payload)
    return SimpleNamespace(status_code=status, headers={"content-type": content_type},
                           text="SYNTHETIC_RESPONSE", json=parse)


class HistoricalModeOneParityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "SEDA_ENV_PATH": str(_DISABLED_ENV), "SEDA_CASAS_BAHIA_LISTING_MODE": "1",
            "SEDA_PRODUCT_LINE": "TV", "SEDA_CASAS_BAHIA_ATTACH_PRICES": "1",
            "SEDA_CASAS_BAHIA_SEARCH_RETRIES": "2", "SEDA_CASAS_BAHIA_SEARCH_RETRY_SLEEP_SECONDS": "3.0",
            "SEDA_CASAS_BAHIA_SESSION_ID": "synthetic-session", "SEDA_CASAS_BAHIA_USER_ID": "",
            "SEDA_CASAS_BAHIA_RESULTS_PER_PAGE": "20", "SEDA_CASAS_BAHIA_VARIANT_CONFIGURATION": "q2",
            "SEDA_CASAS_BAHIA_REGION_ID": "126000", "SEDA_TIMEOUT": "60",
        }))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(search_api, "_headers", return_value={}))
        self.stack.enter_context(patch.object(rest_legacy, "_headers", return_value={}))
        self.stack.enter_context(patch.object(price_api, "_headers", return_value={}))
        self.stack.enter_context(patch.object(price_api, "_params", return_value={"fixture_region": "126000"}))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network_forbidden")))

    def collect(self, fetch, url, responses, price_response, run_id="main"):
        session = Mock()
        session.get.side_effect = list(responses)
        with patch.object(requests, "Session", return_value=session), \
                patch.object(requests, "post", side_effect=[price_response] if isinstance(price_response, Exception)
                             else None, return_value=price_response) as post, \
                patch.object(time, "sleep") as sleep:
            result = fetch(url, timeout=9)
            rows = parsers.parse_listing(result.get("text", ""), "Casas Bahia",
                "https://www.casasbahia.com.br", url, run_id=run_id) if result.get("success") else []
        # Clocks are outside collection-source parity; compare every other field.
        for row in rows:
            row.pop("crawl_datetime", None)
        return {"result": result, "rows": rows, "get": session.get.call_args_list,
                "post": post.call_args_list, "sleep": sleep.call_args_list}

    def assert_parity(self, url, responses, price_response, run_id="main"):
        old = self.collect(_historical_fetch, url, responses, price_response, run_id)
        new = self.collect(lambda url, timeout: listing_modes.fetch_listing(url, timeout=timeout, mode="1"),
                           url, responses, price_response, run_id)
        self.assertEqual(new["result"]["success"], old["result"]["success"])
        self.assertEqual(new["result"]["text"], old["result"]["text"])
        for key in ("rows", "get", "post", "sleep"):
            self.assertEqual(new[key], old[key], key)
        self.assertEqual([item.get("status_code") for item in new["result"]["trace"]],
                         [item.get("status_code") for item in old["result"]["trace"]])
        return old, new

    def matrix(self):
        for line, slug in (("TV", "tv"), ("REF", "geladeira"), ("LDY", "maquina-de-lavar")):
            for run_id in ("main", "bsr"):
                url = f"https://www.casasbahia.com.br/{slug}/b?page=6"
                if run_id == "bsr":
                    url += "&ordenacao=mais-vendidos"
                yield line, run_id, url

    def test_healthy_all_product_lines_main_bsr_full_output_and_requests(self):
        for line, run_id, url in self.matrix():
            with self.subTest(line=line, run_id=run_id), patch.dict(os.environ, {"SEDA_PRODUCT_LINE": line}):
                products = [product(line), product(line, 11, 101)]
                _, new = self.assert_parity(url, [response({"products": products, "queries": {}})],
                    response({"Ofertas": [raw_offer(), raw_offer(11, 101, current=950)]}), run_id)
                self.assertEqual(len(new["rows"]), 2)
                self.assertEqual([row["final_sku_price"] for row in new["rows"]], ["R$800,00", "R$900,00"])
                self.assertEqual([row["original_sku_price"] for row in new["rows"]], ["R$1.200,00"] * 2)
                self.assertEqual([row["savings"] for row in new["rows"]], ["Baixou 25%"] * 2)
                self.assertEqual([row["discount_type"] for row in new["rows"]], ["Cupom especial"] * 2)
                rank = "bsr_rank" if run_id == "bsr" else "main_rank"
                self.assertEqual([row[rank] for row in new["rows"]], [1, 2])
                self.assertEqual(new["get"][0].kwargs["params"]["resultsperpage"], "20")

    def test_price_http_failure_preserves_products_without_search_retry(self):
        for line, run_id, url in self.matrix():
            with self.subTest(line=line, run_id=run_id), patch.dict(os.environ, {"SEDA_PRODUCT_LINE": line}):
                _, new = self.assert_parity(url, [response({"products": [product(line)]})],
                    response({}, status=403), run_id)
                self.assertEqual(len(new["get"]), 1)
                self.assertEqual(len(new["post"]), 1)
                self.assertEqual(len(new["rows"]), 1)
                self.assertFalse(new["rows"][0]["final_sku_price"])

    def test_unmatched_price_offer_does_not_reject_page(self):
        _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b", [response({"products": [product()]})],
            response({"Ofertas": [raw_offer(99, 999)]}))
        self.assertEqual(len(new["get"]), 1)
        self.assertEqual(len(new["rows"]), 1)
        self.assertFalse(new["rows"][0]["final_sku_price"])

    def test_historical_product_first_join_kept_even_with_other_sku_price(self):
        _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b", [response({"products": [product()]})],
            response({"Ofertas": [raw_offer(), raw_offer(10, 101, seller=8, current=999)]}))
        self.assertEqual(len(new["get"]), 1)
        self.assertEqual(new["rows"][0]["product_url"], product()["url"])
        self.assertEqual(new["rows"][0]["final_sku_price"], "R$949,00")
        self.assertEqual(str(new["rows"][0]["seller_id"]), "7")

    def test_same_product_different_skus_and_duplicate_rows_not_deduped_by_transport(self):
        products = [product(), product(sku=101), product()]
        _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b?ordenacao=maisvendidos",
            [response({"products": products})], response({"Ofertas": [raw_offer(), raw_offer(10, 101, current=999)]}), "bsr")
        self.assertEqual(len(new["rows"]), 3)
        self.assertEqual([row["product_url"] for row in new["rows"]], [item["url"] for item in products])
        self.assertEqual([row["final_sku_price"] for row in new["rows"]], ["R$949,00"] * 3)
        self.assertEqual(new["post"][0].kwargs["json"]["produtos"], [{"idProduto": 10}])
        self.assertEqual(len(new["post"][0].kwargs["json"]["skus"]), 2)

    def test_nonmatching_query_and_sort_are_not_new_success_gates(self):
        _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b?page=6&ordenacao=maisvendidos",
            [response({"products": [product()], "queries": {"page": 1, "sortby": "relevancia"}})],
            response({"Ofertas": [raw_offer()]}), "bsr")
        self.assertEqual(len(new["get"]), 1)
        self.assertTrue(new["result"]["success"])

    def test_healthy_http_with_filtered_products_not_retried_in_transport(self):
        unrelated = {**product(), "name": "Suporte de parede para TV"}
        _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b",
            [response({"products": [unrelated]})], response({"Ofertas": [raw_offer()]}))
        self.assertTrue(new["result"]["success"])
        self.assertEqual(new["rows"], [])
        self.assertEqual(len(new["get"]), 1)

    def test_configured_retries_and_linear_waits_match_history_not_hard_three(self):
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_SEARCH_RETRIES": "4"}):
            _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b",
                [response({}, status=403)] * 4 + [response({"products": [product()]})],
                response({"Ofertas": [raw_offer()]}))
        self.assertEqual(len(new["get"]), 5)
        self.assertEqual([call.args[0] for call in new["sleep"]], [3.0, 6.0, 9.0, 12.0])

    def test_default_failed_attempts_and_empty_products_match_history(self):
        for responses in ([response({}, status=403)] * 3, [response({"products": []})] * 3,
                          [response({}, invalid_json=True)] * 3):
            _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b", responses,
                response({"Ofertas": [raw_offer()]}))
            self.assertFalse(new["result"]["success"])
            self.assertEqual(len(new["get"]), 3)
            self.assertEqual(len(new["post"]), 0)

    def test_request_exception_then_success_preserves_call_count_without_exposing_headers(self):
        _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b",
            [requests.Timeout("synthetic timeout"), response({"products": [product()]})],
            response({"Ofertas": [raw_offer()]}))
        self.assertEqual(len(new["get"]), 2)
        self.assertEqual(new["get"][0].kwargs["headers"], {})

    def test_price_attach_disabled_preserves_search_results(self):
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_ATTACH_PRICES": "0"}):
            _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b",
                [response({"products": [product()]})], response({"Ofertas": [raw_offer()]}))
        self.assertEqual(len(new["post"]), 0)
        self.assertEqual(len(new["rows"]), 1)

    def test_custom_request_context_is_not_overridden(self):
        with patch.dict(os.environ, {"SEDA_CASAS_BAHIA_RESULTS_PER_PAGE": "36",
                "SEDA_CASAS_BAHIA_VARIANT_CONFIGURATION": "fixture-variant",
                "SEDA_CASAS_BAHIA_REGION_ID": "555", "SEDA_CASAS_BAHIA_USER_ID": "synthetic-user"}):
            _, new = self.assert_parity("https://www.casasbahia.com.br/tv/b?page=2",
                [response({"products": [product()]})], response({"Ofertas": [raw_offer()]}))
        params = new["get"][0].kwargs["params"]
        self.assertEqual((params["resultsperpage"], params["variantconfiguration"], params["regionid"], params["userid"]),
                         ("36", "fixture-variant", "555", "synthetic-user"))


if __name__ == "__main__":
    unittest.main()
