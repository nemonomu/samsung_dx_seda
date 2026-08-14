import csv
import json
import tempfile
import unittest
from pathlib import Path

from seda.magalu import listing_smoke_check, search_api


TV_URL = "https://www.magazineluiza.com.br/busca/tv/"


def _search():
    return {
        "products": [
            {
                "id": "offer-id",
                "title": 'Smart TV Teste 55" 4K',
                "path": "/smart-tv-teste/p/path-item/et/tv4k/",
                "available": True,
                "price": {"bestPrice": 1999.9, "fullPrice": 2199.9},
                "seller": {"id": "magazineluiza", "sku": "seller-offer"},
                "rating": {"count": 1, "score": 5},
                "shippingTag": {},
                "subcategory": {"id": "TV4K", "name": "TV 4K"},
            }
        ],
        "pagination": {
            "page": 1,
            "pages": 17,
            "records": 10000,
            "size": 60,
            "start": 0,
        },
        "sorts": [
            {
                "label": "selected",
                "selected": True,
                "type": "score",
                "orientation": "desc",
            }
        ],
        "term": {"raw": "tv", "refined": "tv"},
        "trackId": "smoke-test",
    }


def _usage_event(method, profile, multiplier):
    return {
        "event": "zenrows_http_request_attempt",
        "retailer": "magalu",
        "product_line": "tv",
        "method": method,
        "profile": profile,
        "estimated_multiplier": multiplier,
    }


class MagaluListingSmokeCheckTests(unittest.TestCase):
    def _write_fixture(self, root, events=None):
        main = root / "main"
        raw = main / "raw" / "magalu" / "page_001.html"
        parsed = main / "parsed" / "main_occurrences.csv"
        raw.parent.mkdir(parents=True)
        parsed.parent.mkdir(parents=True)
        raw.write_text(search_api._as_next_data_html(_search(), TV_URL), encoding="utf-8")
        manifest = {
            "run_id": "main",
            "rows": 1,
            "failures": [],
            "retailers": ["magalu"],
            "pages": [1],
            "unique_target": 0,
            "fetch_mode": "zenrows",
            "listing_stats": [
                {
                    "retailer": "Magalu",
                    "page": 1,
                    "url": TV_URL,
                    "method": "zenrows_graphql_search",
                    "unique": 1,
                    "raw_products": 1,
                    "kept_products": 1,
                    "dropped_products": 0,
                    "parsed_rows": 1,
                    "pagination_page": 1,
                    "pagination_size": 60,
                    "selected_sort_type": "score",
                    "selected_sort_orientation": "desc",
                }
            ],
        }
        (main / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        with parsed.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "retailer",
                    "product_line",
                    "item",
                    "retailer_sku_name",
                    "product_url",
                    "source_url",
                ),
            )
            writer.writeheader()
            writer.writerow(
                {
                    "retailer": "Magalu",
                    "product_line": "TV",
                    "item": "path-item",
                    "retailer_sku_name": 'Smart TV Teste 55" 4K',
                    "product_url": (
                        "https://www.magazineluiza.com.br/"
                        "smart-tv-teste/p/path-item/et/tv4k/"
                    ),
                    "source_url": TV_URL,
                }
            )

        if events is None:
            events = [_usage_event("POST", "premium_html", "10x")]
        if events:
            ledger = root / "status" / "zenrows_usage" / "execution_a" / "worker.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="ascii",
            )
        return manifest

    def test_graphql_success_passes_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_fixture(root)
            result = listing_smoke_check.validate_smoke_run(root)
            saved = json.loads(
                (root / "status" / "listing_smoke_check.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(saved["passed"])
        self.assertEqual(saved["report_path"], result["report_path"])
        self.assertEqual(result["evidence"]["zenrows_usage"]["post_calls"], 1)
        self.assertEqual(result["evidence"]["zenrows_usage"]["get_calls"], 0)

    def test_complete_fallback_ladder_passes(self):
        events = [
            _usage_event("POST", "premium_html", "10x"),
            _usage_event("GET", "premium_html", "10x"),
            _usage_event("GET", "listing_next_data_js_wait", "25x"),
            _usage_event("GET", "listing_next_data_js_wait", "25x"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_fixture(root, events)
            manifest["listing_stats"][0]["method"] = "zenrows"
            (root / "main" / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            result = listing_smoke_check.validate_smoke_run(root)

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["evidence"]["zenrows_usage"]["http_calls"], 4)

    def test_missing_graphql_post_and_wrong_ladder_fail(self):
        events = [_usage_event("GET", "premium_html", "10x")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_fixture(root, events)
            result = listing_smoke_check.validate_smoke_run(root)

        self.assertFalse(result["passed"])
        self.assertIn("usage_post_calls:0!=1", result["errors"])
        self.assertIn("usage_ladder_order_mismatch", result["errors"])

    def test_raw_failed_and_manifest_arithmetic_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_fixture(root)
            manifest["listing_stats"][0]["dropped_products"] = 1
            (root / "main" / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            failed = root / "main" / "raw_failed" / "magalu" / "page_001.json"
            failed.parent.mkdir(parents=True)
            failed.write_text("{}", encoding="utf-8")
            result = listing_smoke_check.validate_smoke_run(root)

        self.assertFalse(result["passed"])
        self.assertIn("raw_failed_present", result["errors"])
        self.assertIn("listing_stats_product_arithmetic_mismatch", result["errors"])

    def test_non_tv_url_and_unknown_http_method_fail(self):
        events = [
            _usage_event("POST", "premium_html", "10x"),
            _usage_event("PATCH", "premium_html", "10x"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_fixture(root, events)
            manifest["listing_stats"][0]["url"] = (
                "https://www.magazineluiza.com.br/busca/geladeira/"
            )
            (root / "main" / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            result = listing_smoke_check.validate_smoke_run(root)

        self.assertFalse(result["passed"])
        self.assertTrue(
            any(error.startswith("listing_stats_url:") for error in result["errors"])
        )
        self.assertIn("usage_http_method_invalid", result["errors"])

    def test_production_browser_success_allows_zero_zenrows_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_fixture(root, events=[])
            manifest["fetch_mode"] = "magalu_listing_graphql_zenrows"
            manifest["listing_stats"][0]["method"] = "browser_graphql_search"
            (root / "main" / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            result = listing_smoke_check.validate_smoke_run(
                root,
                transport="production",
            )

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["evidence"]["zenrows_usage"]["http_calls"], 0)
        self.assertEqual(
            result["evidence"]["zenrows_usage"]["tracking_status"],
            "not_used",
        )


class MagaluListingSmokeBatchContractTests(unittest.TestCase):
    def test_batch_is_one_page_main_only_and_does_not_handle_the_api_key(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "run_magalu_tv_listing_zenrows_smoke.bat").read_text(
            encoding="utf-8-sig"
        )
        lowered = text.casefold()

        self.assertIn('set "seda_main_page_list=1"', lowered)
        self.assertIn('set "seda_run_id=main"', lowered)
        self.assertIn('set "seda_magalu_listing_fetch_mode=zenrows"', lowered)
        self.assertIn('set "seda_zenrows_proxy_country=br"', lowered)
        self.assertIn('set "seda_zenrows_custom_headers=1"', lowered)
        self.assertIn('set "seda_db_insert=0"', lowered)
        self.assertIn('set "seda_email_notify=0"', lowered)
        command = (
            "python -m seda.magalu.magalu_orchestrator "
            "--product-line tv main_list"
        )
        self.assertEqual(lowered.count(command), 1)
        self.assertNotIn("main_targets", lowered)
        self.assertNotIn("bsr_list", lowered)
        self.assertNotIn("detail_enrichment", lowered)
        self.assertNotIn("db_load", lowered)
        self.assertNotIn("--all", lowered)
        self.assertNotIn("--resume", lowered)
        self.assertNotIn("--from-step", lowered)
        self.assertNotIn("zenrows_api_key", lowered)

    def test_production_batch_is_graphql_first_one_page_main_only(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "run_magalu_tv_listing_production_smoke.bat").read_text(
            encoding="utf-8-sig"
        )
        lowered = text.casefold()

        self.assertIn('set "seda_main_page_list=1"', lowered)
        self.assertIn(
            'set "seda_magalu_listing_fetch_mode=magalu_listing_graphql_zenrows"',
            lowered,
        )
        self.assertIn('set "seda_magalu_browser_use_system_profile=0"', lowered)
        self.assertIn('set "seda_zenrows_proxy_country=br"', lowered)
        self.assertIn('set "seda_db_insert=0"', lowered)
        command = (
            "python -m seda.magalu.magalu_orchestrator "
            "--product-line tv main_list"
        )
        self.assertEqual(lowered.count(command), 1)
        self.assertIn(
            "python -m seda.magalu.listing_smoke_check --transport production",
            lowered,
        )
        for forbidden in (
            "main_targets",
            "bsr_list",
            "detail_enrichment",
            "db_load",
            "--all",
            "--resume",
            "--from-step",
            "zenrows_api_key",
        ):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
