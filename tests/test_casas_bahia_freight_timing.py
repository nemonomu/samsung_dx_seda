"""Offline regressions: no browser, network, environment loader or database."""
import asyncio
import json
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from seda.casas_bahia import diagnostics as timing
from seda.casas_bahia import freight_cdp_backfill as freight


TARGET = {'row_index': 0, 'sku_id': '123456', 'seller_id': '123',
          'product_url': 'https://example.invalid/washer/p/123456'}


def settings(**extra):
    return SimpleNamespace(**{'native_total_timeout_ms': 120, 'native_timeout_ms': 120,
                             'native_input_timeout_ms': 20, 'timeout_ms': 100,
                             'zipcode': '01010-010', **extra})


class Response:
    def __init__(self, status=200, text='{}', body_hangs=False):
        self.url = '/sku/123456/seller/123/frete'
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = {'content-type': 'application/json'}
        self.value = text
        self.body_hangs = body_hangs

    async def text(self):
        if self.body_hangs:
            await asyncio.Event().wait()
        return self.value


class Locator:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def is_visible(self):
        return self.page.stock


class Page:
    def __init__(self, responses=(), *, stock=False, navigation_hangs=False):
        self.responses = list(responses)
        self.stock = stock
        self.navigation_hangs = navigation_hangs
        self.listeners = []
        self.visits = 0
        self.clicks = 0
        self.url = TARGET['product_url']

    def on(self, event, callback):
        self.listeners.append(callback)

    def remove_listener(self, event, callback):
        self.listeners.remove(callback)

    def get_by_text(self, pattern):
        return Locator(self)

    async def goto(self, url, **kwargs):
        self.visits += 1
        if self.navigation_hangs:
            await asyncio.Event().wait()
        self.url = url
        if self.responses:
            response = self.responses.pop(0)
            if response:
                for listener in self.listeners[:]:
                    listener(response)

    async def fill(self, *args, **kwargs):
        pass

    async def click(self, *args, **kwargs):
        self.clicks += 1

    async def press(self, *args, **kwargs):
        self.clicks += 1


class NativeFreightTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict('os.environ', {'SEDA_CASAS_BAHIA_TIMING': '0'})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def collect(self, page, args=None):
        # Use the original endpoint matcher without depending on its suffix.
        original = freight._native_attempt
        async def attempt(page, target, args, expected, **kwargs):
            for response in page.responses:
                if response:
                    response.url = expected + 'zipcode/' + args.zipcode + '/source/CB'
            return await original(page, target, args, expected, **kwargs)
        with patch.object(freight, '_native_attempt', attempt):
            return asyncio.run(freight._native_freight_item(page, TARGET, args or settings()))

    def test_success_preserves_delivery_merge(self):
        page = Page([Response(text='{"ok":true}')])
        with patch.object(freight, '_detail_from_text', return_value={'delivery_availability': '2 dias'}):
            item = self.collect(page)
            rows = [{'delivery_availability': '', 'final_sku_price': '100', 'sku_status': ''}]
            stats = Counter()
            freight._merge_result(rows, item, stats, [])
        self.assertEqual(rows[0]['delivery_availability'], '2 dias')
        self.assertEqual(rows[0]['final_sku_price'], '100')
        self.assertEqual(stats['updated'], 1)
        self.assertEqual(page.visits, 1)
        self.assertEqual(page.listeners, [])

    def test_response_match_requires_configured_postal_code(self):
        expected = '/sku/123456/freight/seller/123/'
        self.assertTrue(freight._native_response_matches(
            'https://example.invalid' + expected + 'zipcode/01010010/source/CB', expected, '01010-010'))
        self.assertTrue(freight._native_response_matches(
            'https://example.invalid' + expected + 'zipcode/01010%2D010/source/CB', expected, '01010-010'))
        self.assertFalse(freight._native_response_matches(
            'https://example.invalid' + expected + 'zipcode/20040-020/source/CB', expected, '01010-010'))
        self.assertFalse(freight._native_response_matches(
            'https://example.invalid' + expected + 'zipcode/01010010/source/CB', expected, ''))

    def test_manual_retry_ignores_other_postal_automatic_response(self):
        expected = '/sku/123456/freight/seller/123/'
        class PostalPage(Page):
            def __init__(self):
                old = Response()
                old.url = expected + 'zipcode/20040-020/source/CB'
                super().__init__([old])
                self.fills = []
            async def fill(self, selector, value, **kwargs):
                self.fills.append(value)
                correct = Response()
                correct.url = expected + 'zipcode/' + value + '/source/CB'
                for callback in self.listeners[:]:
                    callback(correct)
        async def collect():
            page = PostalPage()
            item = await freight._native_attempt(page, TARGET, settings(), expected, manual=True)
            self.assertEqual(page.fills, ['01010-010'])
            self.assertEqual(page.clicks, 1)
            self.assertTrue(item['ok'])
            self.assertEqual(page.listeners, [])
        asyncio.run(collect())

    def test_empty_delivery_keeps_failure_record(self):
        row = {'delivery_availability': '', 'final_sku_price': '100'}
        freight._append_status(row, 'freight_cdp_failed:0')
        stats = Counter(failed=1, updated=0)
        errors = [{'row_index': 0, 'error': 'request_timeout'}]
        item = {**TARGET, 'status': 200, 'ok': True, 'text': '{}', 'reason': 'empty_delivery'}
        args = SimpleNamespace(native_limit=0)
        with patch.object(freight, '_native_freight_item', return_value=item):
            asyncio.run(freight._native_backfill(None, [row], [TARGET], args, stats, errors))
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(row['delivery_availability'], '')
        self.assertEqual(row['final_sku_price'], '100')

    def test_valid_delivery_clears_prior_failure(self):
        row = {'delivery_availability': ''}
        freight._append_status(row, 'freight_cdp_failed:0')
        stats = Counter(failed=1, updated=0)
        errors = [{'row_index': 0, 'error': 'request_timeout'}]
        item = {**TARGET, 'status': 200, 'ok': True, 'text': '{}'}
        with patch.object(freight, '_native_freight_item', return_value=item), patch.object(
                freight, '_detail_from_text', return_value={'delivery_availability': '2 dias'}):
            asyncio.run(freight._native_backfill(None, [row], [TARGET], SimpleNamespace(native_limit=0), stats, errors))
        self.assertEqual(stats['failed'], 0)
        self.assertEqual(errors, [])
        self.assertEqual(row['delivery_availability'], '2 dias')

    def test_old_delivery_value_does_not_make_empty_response_successful(self):
        row = {'delivery_availability': 'old value'}
        freight._append_status(row, 'freight_cdp_failed:0')
        stats = Counter(failed=1, updated=0)
        errors = [{'row_index': 0, 'error': 'request_timeout'}]
        item = {**TARGET, 'status': 200, 'ok': True, 'text': '{}', 'reason': 'empty_delivery'}
        with patch.object(freight, '_native_freight_item', return_value=item):
            asyncio.run(freight._native_backfill(None, [row], [TARGET], SimpleNamespace(native_limit=0), stats, errors))
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(row['delivery_availability'], 'old value')

    def test_404_is_not_retried(self):
        page = Page([Response(404)])
        item = self.collect(page)
        self.assertEqual(item['reason'], 'http_404')
        self.assertEqual(page.visits, 1)
        self.assertEqual(page.listeners, [])

    def test_transient_http_has_only_one_retry(self):
        page = Page([Response(503), Response(503), Response(200)])
        item = self.collect(page)
        self.assertEqual(item['status'], 503)
        self.assertEqual(page.visits, 2)
        self.assertEqual(len(page.responses), 1)

    def test_no_response_is_bounded(self):
        page = Page()
        start = time.perf_counter()
        item = self.collect(page)
        self.assertLess(time.perf_counter() - start, 0.5)
        self.assertFalse(item['ok'])
        self.assertEqual(page.visits, 1)
        self.assertEqual(page.listeners, [])

    def test_navigation_is_in_total_budget(self):
        page = Page(navigation_hangs=True)
        start = time.perf_counter()
        self.assertFalse(self.collect(page)['ok'])
        self.assertLess(time.perf_counter() - start, 0.5)
        self.assertEqual(page.listeners, [])

    def test_body_read_is_in_total_budget(self):
        page = Page([Response(body_hangs=True), Response(body_hangs=True)])
        start = time.perf_counter()
        self.assertFalse(self.collect(page)['ok'])
        self.assertLess(time.perf_counter() - start, 0.5)
        self.assertEqual(page.listeners, [])

    def test_normal_response_after_old_first_budget_is_collected(self):
        # 450ms lies above the old first-attempt budget (400ms), below total 600ms.
        # Wider margins avoid mistaking scheduler overhead for a business timeout.
        class DelayedResponse(Response):
            async def text(self):
                await asyncio.sleep(0.45)
                return self.value
        page = Page([DelayedResponse(), DelayedResponse()])
        with patch.object(freight, '_detail_from_text', return_value={'delivery_availability': 'Normal: 2 dias'}):
            item = self.collect(page, settings(native_timeout_ms=600, native_total_timeout_ms=600))
        self.assertTrue(item['ok'])
        self.assertEqual(item['reason'], 'completed')
        self.assertEqual(page.visits, 1)
        self.assertEqual(page.listeners, [])

    def test_explicit_short_attempt_limit_can_retry_with_remaining_budget(self):
        page = Page([None, Response()])
        item = self.collect(page, settings(native_timeout_ms=20))
        self.assertTrue(item['ok'])
        self.assertEqual(page.visits, 2)
        self.assertEqual(page.listeners, [])

    def test_retry_can_succeed(self):
        page = Page([Response(503), Response()])
        item = self.collect(page)
        self.assertTrue(item['ok'])
        self.assertEqual(page.visits, 2)

    def test_out_of_stock_skip_keeps_product(self):
        page = Page(stock=True)
        item = self.collect(page)
        rows = [{'delivery_availability': '', 'final_sku_price': '100', 'sku_status': ''}]
        stats = Counter()
        freight._merge_result(rows, item, stats, [])
        self.assertEqual(item['reason'], 'out_of_stock')
        self.assertEqual(stats['native_skipped'], 1)
        self.assertEqual(stats['updated'], 0)
        self.assertEqual(rows[0]['delivery_availability'], '')
        self.assertEqual(rows[0]['sku_status'], '')
        self.assertEqual(rows[0]['final_sku_price'], '100')
        self.assertEqual(page.visits, 1)

    def test_other_product_stock_message_is_ignored(self):
        async def check():
            page = Page(stock=True)
            page.url = 'https://example.invalid/washer/p/999999'
            return await freight._native_out_of_stock(page, TARGET)
        self.assertFalse(asyncio.run(check()))

    def test_calculation_error_is_not_delivery_unavailable(self):
        message = 'O cálculo de frete apresentou problemas. Aguarde um momento e tente novamente.'
        page = Page([Response(text=json.dumps({'error': {'message': message}}))])
        item = self.collect(page)
        self.assertEqual(item['reason'], 'calculation_error')
        self.assertFalse(freight._detail_from_text(item['text']).get('delivery_availability'))
        self.assertEqual(page.visits, 1)

    def test_cancellation_removes_response_listener(self):
        async def check():
            page = Page()
            task = asyncio.create_task(freight._native_freight_item(page, TARGET, settings()))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(page.listeners, [])
        asyncio.run(check())


class TimingTests(unittest.TestCase):
    def test_stage_operation_and_child_product_line(self):
        events = []
        with patch.dict('os.environ', {'SEDA_CASAS_BAHIA_TIMING': '1', 'SEDA_PRODUCT_LINE': 'TV'}), patch.object(timing, '_write', side_effect=lambda event, root=None: events.append(event)):
            with timing.stage_span('seda.casas_bahia.step08_detail_enrichment',
                                   {'SEDA_RUN_ROOT': 'unused', 'SEDA_PRODUCT_LINE': 'LDY'}) as span:
                span.update(exit_code=1, success=False, reason='stage_failed')
        self.assertEqual(events[0]['operation'], 'step08_detail_enrichment')
        self.assertEqual(events[0]['product_line'], 'ldy')
        self.assertEqual(events[1]['exit_code'], 1)
        self.assertEqual(events[1]['reason'], 'stage_failed')

    def test_retry_attempt_metadata_is_not_forwarded_to_transport(self):
        events = []
        def request():
            return SimpleNamespace(status_code=503)
        with patch.dict('os.environ', {'SEDA_CASAS_BAHIA_TIMING': '1'}), patch.object(timing, '_write', side_effect=lambda event, root=None: events.append(event)):
            timing.timed_request('http_attempt', request, _timing_attempt=2)
        self.assertEqual(events[0]['attempt'], 2)
        self.assertEqual(events[1]['reason'], 'http_error')

    def test_browser_http_timing_is_distinct_from_logging_time(self):
        events = []
        with patch.dict('os.environ', {'SEDA_CASAS_BAHIA_TIMING': '1'}), patch.object(timing, '_write', side_effect=lambda event, root=None: events.append(event)):
            with timing.Span('cdp_freight_response') as span:
                span.update(http_elapsed_ms=25000, http_started_at_utc='2026-10-06T00:00:00Z',
                            http_finished_at_utc='2026-10-06T00:00:25Z')
        self.assertEqual(events[1]['http_elapsed_ms'], 25000)
        self.assertEqual(events[1]['http_started_at_utc'], '2026-10-06T00:00:00+00:00')

    def test_immediate_start_finish_and_no_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'SEDA_CASAS_BAHIA_TIMING': '1'}):
            path = Path(directory)
            with timing.Span('test_request', root=path, sku_id='123456',
                             headers={'sensitive': 'PRIVATE_PLACEHOLDER'}, url='PRIVATE_PLACEHOLDER') as span:
                files = list(path.rglob('*.jsonl'))
                initial = [json.loads(line) for line in files[0].read_text().splitlines()]
                self.assertEqual(len(initial), 1)
                self.assertEqual(initial[0]['event'], 'start')
                span.update(status_code=404, error='PRIVATE_PLACEHOLDER', reason='PRIVATE_PLACEHOLDER')
            text = files[0].read_text()
            events = [json.loads(line) for line in text.splitlines()]
            self.assertNotIn('PRIVATE_PLACEHOLDER', text)
            self.assertEqual(events[1]['event'], 'finish')
            self.assertEqual(events[1]['status_code'], 404)
            self.assertGreaterEqual(events[1]['elapsed_ms'], 0)
            self.assertEqual(events[0]['request_id'], events[1]['request_id'])

    def test_failure_to_write_does_not_break_collection(self):
        with patch.dict('os.environ', {'SEDA_CASAS_BAHIA_TIMING': '1'}), patch.object(Path, 'mkdir', side_effect=OSError('PRIVATE_PLACEHOLDER')):
            with timing.Span('test_request', root='unused'):
                pass

    def test_http_timing_retains_status_and_exception_class_only(self):
        events = []
        def request():
            raise TimeoutError('PRIVATE_PLACEHOLDER')
        with patch.dict('os.environ', {'SEDA_CASAS_BAHIA_TIMING': '1'}), patch.object(timing, '_write', side_effect=lambda event, root=None: events.append(event)):
            with self.assertRaises(TimeoutError):
                timing.timed_request('http_request', request)
        self.assertEqual(events[-1]['exception_type'], 'TimeoutError')
        self.assertEqual(events[-1]['reason'], 'timeout')
        self.assertNotIn('PRIVATE_PLACEHOLDER', json.dumps(events))

    def test_nested_request_identity(self):
        events = []
        @timing.trace('freight_api')
        def collect(sku_id, seller_id):
            return timing.timed_request('http_request', lambda: SimpleNamespace(status_code=200))
        with patch.dict('os.environ', {'SEDA_CASAS_BAHIA_TIMING': '1'}), patch.object(timing, '_write', side_effect=lambda event, root=None: events.append(event)):
            collect('123456', '123')
        self.assertEqual(events[1]['sku_id'], '123456')
        self.assertEqual(events[1]['parent_request_id'], events[0]['request_id'])
        self.assertEqual(events[2]['status_code'], 200)


if __name__ == '__main__':
    unittest.main()
