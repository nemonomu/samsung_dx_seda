"""Exact listing identity and bounded seller recovery regressions."""
from copy import deepcopy
import os
import unittest
from unittest.mock import Mock, patch

from seda.casas_bahia import price_api, listing_hybrid, search_api
from seda.parsers import extract_next_data, parse_listing
from seda.casas_bahia.listing_price_identity import attach_prices
from test_casas_bahia_listing_scope import listing, product


def item(sku=201, seller=10, pid=101):
    p = product('Geladeira Samsung Frost Free 400L', pid, sku)
    p.pop('price', None)
    p.pop('sellerId', None)
    if seller is None:
        p.pop('lojista', None)
    else:
        p['lojista'] = seller
    return p


def offer(sku=201, seller=10, pid=101, price=100):
    return {'productId': pid, 'skuId': sku, 'sellerId': seller,
            'currentPrice': price, 'oldPrice': 150, 'availability': {}}


def response(*offers):
    prices = {}
    for o in offers:
        for key in ('productId', 'skuId'):
            prices[str(o[key])] = o
    return {'success': True, 'offers': list(offers), 'prices': prices, 'status_code': 200}


class PriceIdentityTests(unittest.TestCase):
    def attach(self, products, *responses):
        fetcher = Mock(side_effect=deepcopy(responses))
        result = attach_prices(products, timeout=9, fetcher=fetcher)
        return result, fetcher

    def test_product_key_collision_never_overrides_correct_sku(self):
        for offers in ((offer(), offer(sku=202)), (offer(sku=202), offer())):
            with self.subTest(offers=offers):
                p = item()
                result, fetcher = self.attach([p], response(*offers))
                self.assertEqual(p['price'], offer())
                self.assertEqual(p['sku'], 201)
                self.assertEqual(fetcher.call_count, 1)
                self.assertEqual(result['identity_unresolved_positions'], [])

    def test_same_sku_other_seller_cannot_replace_requested_seller(self):
        p = item()
        self.attach([p], response(offer(), offer(seller=11, price=1)))
        self.assertEqual(p['price'], offer())
        self.assertEqual(p['lojista'], 10)

    def test_missing_seller_uses_unique_exact_product_sku_offer(self):
        p = item(seller=None)
        _, fetcher = self.attach([p], response(offer()))
        self.assertEqual(p['lojista'], 10)
        self.assertEqual(p['price'], offer())
        self.assertEqual(fetcher.call_count, 1)

    def test_missing_seller_candidate_requires_verified_sku_requery(self):
        p = item(seller=None)
        result, fetcher = self.attach([p], response(offer(sku=202)), response(offer()))
        self.assertEqual(p['price'], offer())
        self.assertEqual(p['sku'], 201)
        self.assertEqual(p['lojista'], 10)
        self.assertTrue(result['seller_recovery_attempted'])
        self.assertEqual(fetcher.call_args_list[1].args[0], [{'id': '101', 'sku': '201', 'lojista': '10'}])

    def test_requery_wrong_identity_or_no_offer_does_not_attach_or_drop(self):
        for bad in (offer(sku=202), offer(pid=999), offer(seller=0)):
            with self.subTest(bad=bad):
                p = item(seller=None)
                products = [p]
                result, fetcher = self.attach(products, response(offer(sku=202)), response(bad))
                self.assertEqual(len(products), 1)
                self.assertNotIn('price', p)
                self.assertNotIn('lojista', p)
                self.assertEqual(p['sku'], 201)
                self.assertEqual(fetcher.call_count, 2)
                self.assertEqual(result['identity_unresolved_positions'], [1])

    def test_candidate_seller_is_not_taken_from_other_product(self):
        p = item(seller=None)
        _, fetcher = self.attach([p], response(offer(pid=999)))
        self.assertEqual(fetcher.call_count, 1)
        self.assertNotIn('price', p)

    def test_ambiguous_sellers_or_quotes_are_not_chosen(self):
        for quotes in ((offer(), offer(seller=11)), (offer(), offer(price=99))):
            p = item(seller=None)
            _, fetcher = self.attach([p], response(*quotes))
            self.assertNotIn('price', p)
            self.assertEqual(fetcher.call_count, 1)

    def test_identical_duplicate_quotes_are_not_ambiguous(self):
        p = item()
        self.attach([p], response(offer(), offer()))
        self.assertEqual(p['price'], offer())

    def test_known_seller_is_never_recovered_as_another(self):
        p = item()
        _, fetcher = self.attach([p], response(offer(seller=11)))
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(p['lojista'], 10)
        self.assertNotIn('price', p)

    def test_malformed_or_conflicting_source_identity_is_not_rewritten(self):
        for updates in ({'sku': 202}, {'sellerId': 11}, {'sku': 'bad'}, {'lojista': 'bad'}):
            p = item()
            p.update(updates)
            before = deepcopy(p)
            self.attach([p], response(offer()))
            self.assertEqual(p, before)

    def test_invalid_price_or_availability_is_rejected(self):
        for value in (0, -1, 'NaN', 'Infinity', None, True):
            p = item()
            self.attach([p], response(offer(price=value)))
            self.assertNotIn('price', p)
        for key in ('IdProduto', 'IdSku', 'IdLojista'):
            p = item()
            bad = offer()
            bad['availability'] = {key: 999}
            self.attach([p], response(bad))
            self.assertNotIn('price', p)

    def test_recovery_failure_is_bounded_and_other_products_are_retained(self):
        products = [item(), item(sku=202, seller=None)]
        result, fetcher = self.attach(products, response(offer()), {'success': False, 'offers': [], 'status_code': 403})
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual(len(products), 2)
        self.assertEqual(products[0]['price'], offer())
        self.assertNotIn('price', products[1])
        self.assertEqual(result['identity_unresolved_positions'], [2])
        with patch.dict(os.environ, {'SEDA_PRODUCT_LINE': 'REF'}):
            self.assertNotEqual(listing_hybrid._validation_error(listing(products), 'https://www.casasbahia.com.br/busca/b?page=1'), '')

    def test_old_unverified_price_is_removed_without_removing_product(self):
        p = item()
        p['price'] = offer(sku=202)
        self.attach([p], response(offer(sku=202)))
        self.assertNotIn('price', p)

    def test_one_recovery_batch_deduplicates_same_requested_identity(self):
        products = [item(seller=None), item(seller=None)]
        _, fetcher = self.attach(products, response(offer(sku=202), offer(sku=202)), response(offer()))
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual(len(fetcher.call_args_list[1].args[0]), 1)
        self.assertTrue(all(p['price'] == offer() for p in products))

    def test_real_price_normalizer_preserves_colliding_offers(self):
        raw = {'Ofertas': [
            {'PrecoVenda': {'IdProduto': 101, 'IdSku': sku, 'IdLojista': 10, 'Preco': price, 'PrecoDe': 150}}
            for sku, price in ((201, 100), (202, 90))
        ]}
        http = Mock(status_code=200)
        http.json.return_value = raw
        p = item()
        with patch.object(price_api.requests, 'post', return_value=http) as post, \
                patch.object(price_api, '_params', return_value={}), patch.object(price_api, '_headers', return_value={}):
            price_api.attach_listing_prices([p], timeout=9)
        self.assertEqual(p['price']['skuId'], 201)
        self.assertEqual(p['price']['currentPrice'], 100)
        self.assertEqual(post.call_count, 1)

    def test_product_lines_preserve_rows_prices_and_validator(self):
        for line, title in (('TV', 'Smart TV Samsung 43'), ('REF', 'Geladeira Samsung Frost Free 400L'), ('LDY', 'Lavadora Samsung 11kg')):
            with self.subTest(line=line), patch.dict(os.environ, {'SEDA_PRODUCT_LINE': line}):
                products = [item(), item(sku=202, seller=None)]
                for p in products:
                    p['title'] = p['name'] = title
                self.attach(products, response(offer()), response(offer(sku=202)))
                self.assertEqual(len(products), 2)
                self.assertEqual([p['sku'] for p in products], [201, 202])
                self.assertEqual(listing_hybrid._validation_error(listing(products), 'https://www.casasbahia.com.br/busca/b?page=1'), '')

    def test_search_to_price_recovery_to_parser_preserves_all_target_rows(self):
        for line, title in (('TV', 'Smart TV Samsung 43'), ('REF', 'Geladeira Samsung Frost Free 400L'), ('LDY', 'Lavadora Samsung 11kg')):
            with self.subTest(line=line), patch.dict(os.environ, {
                'SEDA_PRODUCT_LINE': line, 'SEDA_RUN_ID': 'main',
                'SEDA_CASAS_BAHIA_ATTACH_PRICES': '1',
            }):
                products = [item(), item(sku=202, pid=102, seller=None)]
                for p in products:
                    p['title'] = p['name'] = title
                listing_response = Mock(status_code=200, text='{}', headers={'content-type': 'application/json'})
                listing_response.json.return_value = {'products': products}
                def price_http(*offers):
                    result = Mock(status_code=200)
                    result.json.return_value = {'Ofertas': [
                        {'PrecoVenda': {'IdProduto': p, 'IdSku': s, 'IdLojista': v, 'Preco': value, 'PrecoDe': 300}}
                        for p, s, v, value in offers
                    ]}
                    return result
                first = price_http((101, 201, 10, 100), (101, 999, 10, 90), (102, 998, 11, 190))
                second = price_http((102, 998, 11, 190), (102, 202, 11, 200))
                session = Mock()
                session.get.return_value = listing_response
                with patch.object(search_api.requests, 'Session', return_value=session), \
                        patch.object(search_api, '_headers', return_value={}), \
                        patch.object(search_api, '_params', return_value={}), \
                        patch.object(price_api, '_headers', return_value={}), \
                        patch.object(price_api, '_params', return_value={}), \
                        patch.object(price_api.requests, 'post', side_effect=[first, second]) as post:
                    slug = next(iter(search_api.casas_bahia_listing_slugs())).strip('/')
                    url = f'https://www.casasbahia.com.br/{slug}/b?page=1'
                    result = search_api.fetch_search_listing(url, timeout=9, max_attempts=1,
                                                             validator=listing_hybrid._validation_error)
                self.assertTrue(result['success'], result.get('trace'))
                self.assertEqual(session.get.call_count, 1)
                self.assertEqual(post.call_count, 2)
                self.assertEqual(post.call_args_list[1].kwargs['json']['skus'], [{'idLojista': 11, 'idSku': 202}])
                attached = extract_next_data(result['text'])['props']['pageProps']['initialState']['search']['results']['products']
                self.assertEqual(len(attached), 2)
                self.assertEqual([p['price']['skuId'] for p in attached], [201, 202])
                rows = parse_listing(result['text'], 'Casas Bahia', 'https://www.casasbahia.com.br', url)
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(row.get('final_sku_price') for row in rows))

    def test_recovery_does_not_choose_among_two_verified_sellers(self):
        p = item(seller=None)
        _, fetcher = self.attach([p], response(offer(sku=202), offer(sku=202, seller=11)),
                                 response(offer(), offer(seller=11)))
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual(len(fetcher.call_args_list[1].args[0]), 2)
        self.assertNotIn('price', p)
        self.assertNotIn('lojista', p)
