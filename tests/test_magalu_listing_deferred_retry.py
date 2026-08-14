import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from seda import step01_main_list
from seda.common import orchestrator
from seda.magalu import search_api
from seda.transport import FetchResult


def _search(page, item, sort_type):
    return {
        "products": [
            {
                "id": f"offer-{item}",
                "title": f'Smart TV Teste {item} 55" 4K',
                "path": f"/smart-tv-teste-{item}/p/{item}/et/tv4k/",
                "available": True,
                "price": {"bestPrice": 1999.9, "fullPrice": 2199.9},
                "seller": {"id": "magazineluiza", "sku": f"seller-{item}"},
                "rating": {"count": 1, "score": 5},
                "shippingTag": {},
                "subcategory": {"id": "TV4K", "name": "TV 4K"},
            }
        ],
        "pagination": {
            "page": page,
            "pages": 10,
            "records": 600,
            "size": 60,
            "start": (page - 1) * 60,
        },
        "sorts": [
            {
                "label": "selected",
                "selected": True,
                "type": sort_type,
                "orientation": "desc",
            }
        ],
        "term": {"raw": "tv", "refined": "tv"},
        "trackId": f"test-{page}",
    }


def _url(page, run_id):
    sort_type = "soldQuantity" if run_id == "bsr" else "score"
    return (
        "https://www.magazineluiza.com.br/busca/tv/"
        f"?page={page}&sortType={sort_type}&sortOrientation=desc"
    )


def _html(page, run_id):
    item = f"aa{page:08d}"
    url = _url(page, run_id)
    sort_type = "soldQuantity" if run_id == "bsr" else "score"
    return search_api._as_next_data_html(
        _search(page, item, sort_type),
        url,
    )


class MagaluListingDeferredRetryTests(unittest.TestCase):
    def _env(self, root, run_id):
        return {
            "SEDA_RETAILERS": "magalu",
            "SEDA_ACTIVE_RETAILER": "magalu",
            "SEDA_PRODUCT_LINE": "TV",
            "SEDA_RUN_ROOT": str(root),
            "SEDA_RUN_ID": run_id,
            "SEDA_MAGALU_LISTING_DEFERRED_RETRY_ROUNDS": "1",
            "SEDA_MAGALU_LISTING_DEFERRED_RETRY_SLEEP_SECONDS": "0",
            "SEDA_MAGALU_LISTING_FAIL_FAST": "1",
            "SEDA_REUSE_RAW": "0",
            "SEDA_ALLOW_EMPTY_LISTING": "0",
        }

    def test_main_and_bsr_retry_only_failed_page_and_preserve_rank_order(self):
        for run_id, rank_field in (("main", "main_rank"), ("bsr", "bsr_rank")):
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls = []
                page_two_calls = 0

                def fetch(url):
                    nonlocal page_two_calls
                    page = int(parse_qs(urlparse(url).query)["page"][0])
                    calls.append(page)
                    if page == 2:
                        page_two_calls += 1
                        if page_two_calls == 1:
                            return FetchResult(
                                url=url,
                                text="",
                                method="zenrows",
                                error="request_error:ConnectionError",
                            )
                    return FetchResult(
                        url=url,
                        text=_html(page, run_id),
                        status_code=200,
                        method="zenrows",
                    )

                with patch.dict(os.environ, self._env(root, run_id), clear=False), patch.object(
                    step01_main_list,
                    "page_numbers",
                    return_value=[1, 2, 3],
                ), patch.object(
                    step01_main_list,
                    "unique_target",
                    return_value=0,
                ), patch.object(
                    step01_main_list,
                    "page_url",
                    side_effect=lambda config, page, run_id=None: _url(page, run_id),
                ), patch.object(
                    step01_main_list,
                    "fetch_url",
                    side_effect=fetch,
                ):
                    step01_main_list.main()
                    self.assertEqual(
                        os.environ["SEDA_MAGALU_LISTING_FAIL_FAST"],
                        "1",
                    )
                    self.assertEqual(os.environ["SEDA_REUSE_RAW"], "0")

                output = root / run_id / "parsed" / "main_occurrences.csv"
                with output.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                manifest = json.loads(
                    (root / run_id / "manifest.json").read_text(
                        encoding="utf-8"
                    )
                )

                self.assertEqual(calls, [1, 2, 3, 2])
                self.assertEqual(
                    [row["item"] for row in rows],
                    ["aa00000001", "aa00000002", "aa00000003"],
                )
                self.assertEqual(
                    [row[rank_field] for row in rows],
                    ["1", "2", "3"],
                )
                self.assertTrue(manifest["complete"])
                self.assertEqual(manifest["failures"], [])
                self.assertEqual(
                    manifest["deferred_retry"]["recovered_pages"],
                    [2],
                )
                self.assertFalse(
                    (
                        root
                        / run_id
                        / "raw_failed"
                        / "magalu"
                        / "page_002.json"
                    ).exists()
                )
                self.assertFalse(
                    (
                        root
                        / run_id
                        / "parsed"
                        / "main_occurrences.partial.csv"
                    ).exists()
                )

    def test_unresolved_page_stays_partial_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            stale_final = root / "main" / "parsed" / "main_occurrences.csv"
            stale_final.parent.mkdir(parents=True)
            stale_final.write_text("item\nstale-item\n", encoding="utf-8")

            def fetch(url):
                calls.append(1)
                return FetchResult(
                    url=url,
                    text="",
                    method="zenrows",
                    error="request_error:ConnectionError",
                )

            with patch.dict(os.environ, self._env(root, "main"), clear=False), patch.object(
                step01_main_list,
                "page_numbers",
                return_value=[1],
            ), patch.object(
                step01_main_list,
                "unique_target",
                return_value=0,
            ), patch.object(
                step01_main_list,
                "page_url",
                return_value=_url(1, "main"),
            ), patch.object(
                step01_main_list,
                "fetch_url",
                side_effect=fetch,
            ):
                with self.assertRaisesRegex(
                    SystemExit,
                    "unresolved_pages=1",
                ):
                    step01_main_list.main()

            manifest = json.loads(
                (root / "main" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(calls, [1, 1])
            self.assertFalse(manifest["complete"])
            self.assertEqual(
                manifest["deferred_retry"]["unresolved_pages"],
                [1],
            )
            self.assertTrue(
                (
                    root
                    / "main"
                    / "parsed"
                    / "main_occurrences.partial.csv"
                ).exists()
            )
            self.assertFalse(stale_final.exists())

    def test_zero_retry_magalu_still_stays_partial_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final_output = root / "main" / "parsed" / "main_occurrences.csv"
            final_output.parent.mkdir(parents=True)
            final_output.write_text("item\nstale-item\n", encoding="utf-8")

            def fetch(url):
                page = int(parse_qs(urlparse(url).query)["page"][0])
                if page == 1:
                    return FetchResult(
                        url=url,
                        text=_html(page, "main"),
                        status_code=200,
                        method="browser_graphql_search",
                    )
                return FetchResult(
                    url=url,
                    text="",
                    method="zenrows",
                    error="request_error:ConnectionError",
                )

            env = self._env(root, "main")
            env.update(
                {
                    "SEDA_MAGALU_LISTING_DEFERRED_RETRY_ROUNDS": "0",
                    "SEDA_MAGALU_LISTING_FAIL_FAST": "0",
                }
            )
            with patch.dict(os.environ, env, clear=False), patch.object(
                step01_main_list,
                "page_numbers",
                return_value=[1, 2],
            ), patch.object(
                step01_main_list,
                "unique_target",
                return_value=0,
            ), patch.object(
                step01_main_list,
                "page_url",
                side_effect=lambda config, page, run_id=None: _url(page, run_id),
            ), patch.object(
                step01_main_list,
                "fetch_url",
                side_effect=fetch,
            ):
                with self.assertRaisesRegex(
                    SystemExit,
                    "unresolved pages=2",
                ):
                    step01_main_list.main()

            manifest = json.loads(
                (root / "main" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["complete"])
            self.assertEqual(
                [failure["page"] for failure in manifest["failures"]],
                [2],
            )
            self.assertFalse(final_output.exists())
            self.assertTrue(
                (
                    root
                    / "main"
                    / "parsed"
                    / "main_occurrences.partial.csv"
                ).exists()
            )

    def test_invalid_cached_page_is_not_reusable(self):
        config = step01_main_list.RETAILERS["magalu"]
        error = step01_main_list._magalu_raw_reuse_error(
            _html(2, "main"),
            config,
            _url(1, "main"),
            "main",
        )
        self.assertIn("page_mismatch", error)

    def test_empty_cached_product_payload_is_not_reusable(self):
        config = step01_main_list.RETAILERS["magalu"]
        search = _search(1, "aa00000001", "score")
        search["products"] = []
        error = step01_main_list._magalu_raw_reuse_error(
            search_api._as_next_data_html(search, _url(1, "main")),
            config,
            _url(1, "main"),
            "main",
        )
        self.assertEqual(error, "empty_products")

    def test_cached_payload_requires_exact_term_size_and_product_path(self):
        config = step01_main_list.RETAILERS["magalu"]
        cases = {
            "term": ("search_term_mismatch", lambda search: search["term"].update(raw="geladeira")),
            "size": ("page_size_mismatch", lambda search: search["pagination"].update(size=20)),
            "path": ("invalid_product", lambda search: search["products"][0].update(path="/busca/tv/")),
        }
        for name, (expected, mutate) in cases.items():
            with self.subTest(case=name):
                search = _search(1, "aa00000001", "score")
                mutate(search)
                error = step01_main_list._magalu_raw_reuse_error(
                    search_api._as_next_data_html(
                        search,
                        _url(1, "main"),
                    ),
                    config,
                    _url(1, "main"),
                    "main",
                )
                self.assertTrue(error.startswith(expected), error)

    def test_intentionally_filtered_page_is_not_a_parse_failure(self):
        config = step01_main_list.RETAILERS["magalu"]
        search = _search(1, "aa00000001", "score")
        search["products"][0]["title"] = "Suporte para TV de Parede"
        search["products"][0]["path"] = (
            "/suporte-para-tv-de-parede/p/aa00000001/et/suar/"
        )
        error = step01_main_list._magalu_raw_reuse_error(
            search_api._as_next_data_html(search, _url(1, "main")),
            config,
            _url(1, "main"),
            "main",
        )
        self.assertEqual(error, "")

    def test_retry_metadata_does_not_mark_unneeded_page_as_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            manifest = {
                "complete": True,
                "failures": [],
            }
            history = [
                {
                    "pass": 1,
                    "failed_pages": [2, 5],
                    "attempted_pages": [1, 2, 3, 4, 5],
                },
                {
                    "pass": 2,
                    "failed_pages": [],
                    "attempted_pages": [1, 2, 3],
                },
            ]
            recovered, unresolved = step01_main_list._write_listing_retry_metadata(
                path,
                manifest,
                history,
                1,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(recovered, [2])
        self.assertEqual(unresolved, [])
        self.assertEqual(saved["deferred_retry"]["retried_pages"], [2])
        self.assertEqual(saved["deferred_retry"]["not_required_pages"], [5])

    def test_casas_does_not_enter_magalu_managed_retry(self):
        with patch.dict(
            os.environ,
            {
                "SEDA_RETAILERS": "casas_bahia",
                "SEDA_ACTIVE_RETAILER": "casas_bahia",
                "SEDA_MAGALU_LISTING_DEFERRED_RETRY_ROUNDS": "1",
            },
            clear=False,
        ), patch.object(step01_main_list, "_main_once") as run_once:
            step01_main_list.main()

        run_once.assert_called_once_with()

    def test_resume_rejects_incomplete_magalu_listing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parsed = root / "main" / "parsed" / "main_occurrences.csv"
            parsed.parent.mkdir(parents=True)
            parsed.write_text("item\nold-row\n", encoding="utf-8")
            manifest = {
                "run_id": "main",
                "rows": 1,
                "failures": [{"page": 2, "error": "failed"}],
                "complete": False,
                "retailers": ["magalu"],
            }
            (root / "main" / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            step = orchestrator.Step(
                1,
                "main_list",
                "seda.magalu.step01_main_list",
            )
            with patch.dict(
                os.environ,
                {"SEDA_RUN_ROOT": str(root)},
                clear=False,
            ):
                complete, reason = orchestrator.step_complete(step)

        self.assertFalse(complete)
        self.assertIn("incomplete listing manifest", reason)


if __name__ == "__main__":
    unittest.main()
