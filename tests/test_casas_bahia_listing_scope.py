"""Offline regressions: validate only target products without weakening identity checks."""
from copy import deepcopy
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from seda.casas_bahia import listing_hybrid, listing_modes, search_api, browser_listing
from seda.parsers import extract_next_data, parse_listing
from test_casas_bahia_listing_hybrid import TV_URL, _listing_html, _successful_result


def product(title='Smart TV Samsung 43 DU7700', product_id=101, sku=201):
    payload = extract_next_data(_listing_html(product_id=product_id, sku_id=sku))
    item = payload['props']['pageProps']['initialState']['search']['results']['products'][0]
    item['title'] = item['name'] = title
    return item


def listing(items, page=1):
    payload = extract_next_data(_listing_html(page=page))
    payload['props']['pageProps']['initialState']['search']['results']['products'] = items
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>'


class ListingScopeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'SEDA_PRODUCT_LINE': 'TV'})
        env.start()
        self.addCleanup(env.stop)

    def excluded(self, title='Rack para TV de ate 75'):
        item = product(title, 102, 202)
        item['price']['skuId'] = 999
        return item

    def test_irrelevant_bad_offer_does_not_reject_or_change_valid_tv(self):
        tv = product()
        for title in ('Rack para TV de ate 75', 'Suporte para TV 55'):
            for items in ([self.excluded(title), tv], [tv, self.excluded(title)]):
                with self.subTest(title=title, first=items[0]['title']):
                    before = deepcopy(items)
                    text = listing(items)
                    self.assertEqual(listing_hybrid._validation_error(text, TV_URL), '')
                    rows = parse_listing(text, 'Casas Bahia', 'https://www.casasbahia.com.br', TV_URL, run_id='bsr')
                    self.assertEqual(len(rows), 1)
                    expected = parse_listing(listing([tv]), 'Casas Bahia', 'https://www.casasbahia.com.br', TV_URL, run_id='bsr')[0]
                    for key in ('sku', 'retailer_product_id', 'seller_id', 'product_url', 'final_sku_price', 'bsr_rank'):
                        self.assertEqual(rows[0][key], expected[key])
                    self.assertEqual(items, before)

    def test_excluded_missing_identity_and_price_do_not_block_tv(self):
        self.assertEqual(listing_hybrid._validation_error(listing([product(), {'title': 'Rack para TV'}]), TV_URL), '')

    def test_only_excluded_products_are_not_published_as_success(self):
        self.assertEqual(listing_hybrid._validation_error(listing([self.excluded()]), TV_URL), 'no_relevant_parsed_products')

    def test_target_price_identity_conflicts_remain_blocked(self):
        for field in ('productId', 'skuId', 'sellerId'):
            with self.subTest(field=field):
                tv = product()
                tv['price'][field] = 999
                self.assertEqual(listing_hybrid._validation_error(listing([tv, self.excluded()]), TV_URL), 'price_identity_mismatch')

    def test_target_missing_price_or_seller_remains_blocked(self):
        tv = product()
        tv['price']['currentPrice'] = None
        self.assertEqual(listing_hybrid._validation_error(listing([tv]), TV_URL), 'missing_price')
        tv = product()
        tv.pop('lojista')
        tv.pop('sellerId')
        self.assertEqual(listing_hybrid._validation_error(listing([tv]), TV_URL), 'missing_product_identity')

    def test_duplicate_target_and_malformed_products_remain_blocked(self):
        self.assertEqual(listing_hybrid._validation_error(listing([product(), product()]), TV_URL), 'duplicate_product_identity')
        for malformed in (None, [], 'bad'):
            with self.subTest(malformed=malformed):
                self.assertEqual(listing_hybrid._validation_error(listing([product(), malformed]), TV_URL), 'invalid_listing_payload')

    def test_wrong_page_is_not_hidden_by_exclusion(self):
        self.assertEqual(listing_hybrid._validation_error(listing([product(), self.excluded()], page=7), TV_URL), 'requested_page_mismatch')

    def test_ref_ldy_use_their_existing_relevance_rules(self):
        for line, title, excluded in (
            ('REF', 'Geladeira Samsung Frost Free 400L', 'Filtro para geladeira'),
            ('LDY', 'Lavadora Samsung 11kg', 'Mangueira para lavadora'),
        ):
            with self.subTest(line=line), patch.dict(os.environ, {'SEDA_PRODUCT_LINE': line}):
                url = 'https://www.casasbahia.com.br/busca/b?page=1'
                text = listing([product(title), self.excluded(excluded)])
                self.assertEqual(listing_hybrid._validation_error(text, url), '')
                self.assertEqual(len(parse_listing(text, 'Casas Bahia', 'https://www.casasbahia.com.br', url)), 1)

    def test_rest_and_hybrid_keep_api_success_without_browser_fallback(self):
        text = listing([self.excluded(), product()])
        for mode in ('1', '2'):
            with self.subTest(mode=mode), patch.object(search_api, 'fetch_search_listing', return_value={**_successful_result(), 'text': text}) as rest, patch.object(browser_listing, 'fetch_page') as browser, redirect_stdout(io.StringIO()):
                result = listing_modes.fetch_listing(TV_URL, timeout=1, mode=mode)
                self.assertTrue(result['success'])
                self.assertEqual(rest.call_args.kwargs['validator'](text, TV_URL), '')
                browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
