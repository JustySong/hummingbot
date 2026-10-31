import argparse
import asyncio
import io
import json
import sqlite3
from decimal import Decimal
from unittest.mock import patch

import pytest

from tools import collect_delivery_spread as core
from tools import delivery_spread_ws as ws


def contract(exchange):
    return core.Contract(exchange, 'BTC-TEST', 1900000000000, '1', quantity_unit='USD')


def message(exchange):
    levels = {'bids': [['100', '1000']], 'asks': [['101', '1000']]}
    if exchange == 'okx':
        return {'arg': {'channel': 'books5', 'instId': 'BTC-TEST'}, 'data': [{**levels, 'ts': '10000'}]}
    if exchange == 'binance':
        return {'e': 'depthUpdate', 's': 'BTC-TEST', 'b': levels['bids'], 'a': levels['asks'], 'E': 10000}
    if exchange == 'deribit':
        return {'method': 'subscription', 'params': {'channel': 'book.BTC-TEST.none.20.100ms',
                'data': {**levels, 'timestamp': 10000, 'instrument_name': 'BTC-TEST', 'type': 'snapshot'}}}
    return {'topic': 'orderbook.1.BTC-TEST', 'type': 'snapshot', 'ts': 10000,
            'data': {'s': 'BTC-TEST', 'b': levels['bids'], 'a': levels['asks']}}


@pytest.mark.parametrize('exchange', sorted(ws.URLS))
def test_snapshot_decode_and_units(exchange):
    symbol, payload = ws.decode_book(exchange, message(exchange))
    assert symbol == 'BTC-TEST'
    book = core.normalize_book(contract(exchange), {'payload': payload, 'received_ms': 10010, 'rtt_ms': None},
                               {'offset_ms': 0})
    assert book['bids'] == [(Decimal(100), Decimal(1000))]
    assert book['exchange_ts_ms'] == 10000
    assert book['rtt_ms'] is None
    assert core.inverse_price(book['asks'], Decimal(1), Decimal(1000)) == 101


@pytest.mark.parametrize('exchange', sorted(ws.URLS))
def test_subscriptions_are_public_and_snapshot_channels(exchange):
    sub = ws.subscription(exchange, [contract(exchange)], 1)
    assert 'auth' not in json.dumps(sub)
    if exchange == 'okx':
        assert sub['args'][0]['channel'] == 'books5'
    elif exchange == 'binance':
        assert sub['params'] == ['btc-test@depth20@100ms']
    elif exchange == 'deribit':
        assert sub['params']['channels'] == ['book.BTC-TEST.none.20.100ms']
    else:
        assert sub['args'] == ['orderbook.1.BTC-TEST']


def test_acknowledgments_notice_and_rejections():
    assert ws.decode_book('okx', {'event': 'subscribe', 'arg': {}}) is None
    assert ws.decode_book('okx', {'event': 'notice', 'code': '64010'}) is None
    assert ws.decode_book('deribit', {'jsonrpc': '2.0', 'id': 1, 'result': []}) is None
    with pytest.raises(ValueError, match='websocket error'):
        ws.decode_book('bybit', {'success': False, 'ret_msg': 'invalid topic'})
    bad = message('bybit')
    bad['type'] = 'delta'
    with pytest.raises(ValueError, match='delta'):
        ws.decode_book('bybit', bad)
    bad = message('deribit')
    bad['params']['data']['bids'] = [['change', 100, 1]]
    with pytest.raises(ValueError, match='incremental'):
        ws.decode_book('deribit', bad)


def test_reporting_excludes_old_receipts_and_preserves_quote_timestamp_quality():
    group, cache = {}, {}
    for exchange in ('okx', 'deribit'):
        group[exchange] = contract(exchange)
        _, payload = ws.decode_book(exchange, message(exchange))
        cache[(exchange, 'BTC-TEST')] = core.normalize_book(
            group[exchange], {'payload': payload, 'received_ms': 10010, 'rtt_ms': None}, {'offset_ms': 0})
    args = argparse.Namespace(notional_usd=Decimal(100), max_age_ms=5000, max_skew_ms=1000)
    output = io.StringIO()
    with patch.object(ws.time, 'time', return_value=10.1):
        assert ws.report([group], cache, args, output) == 1
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(records) == 2 and records[1]['type'] == 'spread'
    assert records[1]['quality_ok'] and records[1]['transport'] == 'ws'
    cache[('okx', 'BTC-TEST')]['local_quote_ts_ms'] = 1000
    output = io.StringIO()
    with patch.object(ws.time, 'time', return_value=10.1):
        ws.report([group], cache, args, output)
    assert not json.loads(output.getvalue().splitlines()[1])['quality_ok']
    with patch.object(ws.time, 'time', return_value=20):
        assert ws.report([group], cache, args, io.StringIO()) == 0


@pytest.mark.asyncio
async def test_disconnect_clears_cache_and_reconnects_then_cancels_cleanly():
    disconnect, reconnect, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Socket:
        calls = 0

        async def send(self, raw):
            pass

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(message('okx'))
            waiting.set()
            await disconnect.wait()
            raise OSError('test disconnected')

    class Connection:
        attempts = 0

        async def __aenter__(self):
            self.attempts += 1
            if self.attempts == 2:
                reconnect.set()
                await asyncio.Event().wait()
            return Socket()

        async def __aexit__(self, *args):
            pass
    connection, cache, output = Connection(), {}, io.StringIO()
    task = asyncio.create_task(ws.stream_exchange('okx', [contract('okx')], {'offset_ms': 0},
                                                  cache, output, lambda *a, **k: connection))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert ('okx', 'BTC-TEST') in cache
        disconnect.set()
        await asyncio.wait_for(reconnect.wait(), 3)
        assert not cache
        assert 'test disconnected' in output.getvalue()
    finally:
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_bounded_session_reports_and_cleans_up_tasks(tmp_path):
    group = {ex: contract(ex) for ex in ('okx', 'deribit')}
    args = argparse.Namespace(output=tmp_path / 'ws.jsonl', sqlite=tmp_path / 'ws.sqlite', transport='ws',
                              exchanges=['okx', 'deribit'],
                              record_ws_raw=False, interval=0.01, samples=1, notional_usd=Decimal(100),
                              max_age_ms=5000, max_skew_ms=1000)
    stopped = []

    async def fake_stream(ex, contracts, clock, cache, *args):
        _, payload = ws.decode_book(ex, message(ex))
        cache[(ex, 'BTC-TEST')] = core.normalize_book(contracts[0],
                                                      {'payload': payload, 'received_ms': 10010, 'rtt_ms': None}, clock)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(ex)
    with patch.object(core, 'discover', return_value=([group], {})), \
            patch.object(core, 'clock_sample', return_value={'offset_ms': 0}), \
            patch.object(ws, 'stream_exchange', side_effect=fake_stream), \
            patch.object(ws.time, 'time', return_value=10.1):
        assert await ws.session(args, None) == 0
    assert set(stopped) == {'okx', 'deribit'}
    assert any(json.loads(line)['type'] == 'spread' for line in args.output.read_text().splitlines())

    with sqlite3.connect(args.sqlite) as database:
        assert database.execute('SELECT count(*) FROM delivery_spreads').fetchone()[0] == 2
        assert database.execute('SELECT ended_ms FROM delivery_runs').fetchone()[0] is not None
