import argparse
import sqlite3
from decimal import Decimal
from unittest.mock import patch

import pytest

from tools import collect_delivery_public as command
from tools.collect_delivery_spread import Contract
from tools.delivery_public_data import DATASETS, PublicData
from tools.delivery_spread_store import Output


def contract(ex):
    return Contract(ex, 'BTC-TEST', 1900000000000, '100' if ex in ('okx', 'binance') else '1')


def payload(ex, path, params):
    if path.endswith('mark-price'):
        return {'data': [{'markPx': '101', 'ts': '1000'}]}
    if path.endswith('index-tickers'):
        return {'data': [{'idxPx': '100', 'ts': '1001'}]}
    if path.endswith('premiumIndex'):
        return [{'time': 1000, 'markPrice': '101', 'indexPrice': '100'}]
    if 'open-interest' in path:
        return {'data': [{'ts': '1000', 'oi': '2', 'oiCcy': '0.002'}]}
    if path.endswith('openInterest'):
        return {'time': 1000, 'openInterest': '2'}
    if path.endswith('tickers'):
        return {'time': 1000, 'result': {'list': [{'markPrice': '101', 'indexPrice': '100',
                                                   'lastPrice': '100', 'openInterest': '200'}]}}
    if path.endswith('ticker'):
        if ex == 'okx':
            return {'data': [{'ts': '1000', 'last': '100', 'bidPx': '99', 'askPx': '101'}]}
        return {'result': {'timestamp': 1000, 'mark_price': 101, 'index_price': 100, 'open_interest': 200}}
    if 'trades' in path or 'recent-trade' in path:
        if ex == 'okx':
            return {'data': [{'tradeId': 'a', 'ts': '1000', 'px': '100', 'sz': '2', 'side': 'sell'}]}
        if ex == 'binance':
            return [{'id': 1, 'time': 1000, 'price': '100', 'qty': '2', 'isBuyerMaker': True}]
        if ex == 'deribit':
            return {'result': {'trades': [{'trade_id': 'a', 'timestamp': 1000, 'price': 100,
                                           'amount': 200, 'direction': 'sell'}]}}
        return {'result': {'list': [{'execId': 'a', 'time': '1000', 'price': '100', 'size': '200', 'side': 'Sell'}]}}
    if path.endswith('candles'):
        return {'data': [['0', '100', '110', '90', '101', '2', '0', '0', '1']]}
    if path.endswith('klines'):
        return [[0, '100', '110', '90', '101', '2', 59999]]
    if path.endswith('kline'):
        return {'result': {'list': [['0', '100', '110', '90', '101', '200', '2']]}}
    if path.endswith('get_tradingview_chart_data'):
        return {'result': {'status': 'ok', 'ticks': [0], 'open': [100], 'high': [110], 'low': [90],
                           'close': [101], 'volume': [2]}}
    if path.endswith('delivery-exercise-history'):
        assert params['instFamily'] == 'BTC-USD' and 'uly' not in params
        return {'data': [{'ts': '1000', 'details': [
            {'insId': 'BTC-USD-OLD', 'px': '100', 'type': 'delivery'},
            {'insId': 'BTC-USD_UM-OLD', 'px': '100', 'type': 'delivery'}]}]}
    if path.endswith('get_last_settlements_by_currency'):
        return {'result': {'settlements': [{'instrument_name': 'BTC-OLD', 'timestamp': 1000, 'index_price': 100},
                                           {'instrument_name': 'BTC-OLD-100-C', 'timestamp': 1000, 'index_price': 100}]}}
    if path.endswith('delivery-price'):
        return {'result': {'list': [{'symbol': 'BTCUSDOLD', 'deliveryTime': '1000', 'deliveryPrice': '100'}]}}
    raise AssertionError(path)


@pytest.mark.parametrize('ex', ['okx', 'binance', 'deribit', 'bybit'])
@pytest.mark.parametrize('dataset', DATASETS)
def test_public_routes_and_normalization(ex, dataset):
    paths = []

    def request(exchange, path, params):
        assert exchange == ex
        paths.append(path)
        return {'payload': payload(ex, path, params)}
    client = PublicData(request)
    if ex == 'binance' and dataset == 'settlements':
        with pytest.raises(NotImplementedError):
            client.fetch(dataset, contract(ex))
        assert paths == []
        return
    rows = client.fetch(dataset, contract(ex), limit=5, now_ms=120000)
    assert len(rows) == 1
    assert rows[0]['symbol'] and rows[0]['timestamp_ms'] is not None
    assert all(not any(word in p for word in ('/private/', '/account', '/order', '/position')) for p in paths)
    if dataset == 'open_interest':
        assert Decimal(rows[0]['open_interest_usd']) == 200
    if dataset == 'trades':
        assert Decimal(rows[0]['notional_usd']) == 200
        assert rows[0]['taker_side'] == 'sell'
    if dataset == 'candles':
        assert rows[0]['is_closed']


def test_deribit_no_candles_and_validation():
    client = PublicData(lambda *args: {'payload': {'result': {'status': 'no_data'}}})
    assert client.fetch('candles', contract('deribit')) == []
    with pytest.raises(ValueError):
        client.fetch('ticker', contract('deribit'), limit=101)


def test_storage_dedup_and_candle_updates(tmp_path):
    out = Output(None, tmp_path / 'public.sqlite')
    rows = [{'symbol': 'BTC-TEST', 'timestamp_ms': 0, 'open': '100', 'close': '101', 'is_closed': False}]
    record = {'type': 'public_data', 'exchange': 'okx', 'dataset': 'candles', 'base_asset': 'BTC', 'rows': rows}
    out.write_record(record)
    out.write_record(record)
    assert out.db.execute('SELECT count(*) FROM delivery_public_data').fetchone()[0] == 1
    rows[0].update(close='102', is_closed=True)
    out.write_record(record)
    result = out.db.execute('SELECT payload_json FROM delivery_public_data').fetchone()[0]
    assert '102' in result and 'true' in result
    assert out.db.execute('PRAGMA foreign_key_check').fetchall() == []
    out.close()


def test_collection_preserves_other_datasets_after_failure(tmp_path):
    args = argparse.Namespace(exchanges=['okx'], assets=['BTC'], symbols=None, datasets=['ticker', 'candles'],
                              limit=5, samples=1, interval=60, sqlite=tmp_path / 'public.sqlite',
                              output=None, transport='public-rest')
    with patch.object(command.core, 'discover', return_value=([{'okx': contract('okx')}], {})), \
            patch.object(command.PublicData, 'fetch', side_effect=[OSError('unavailable'), []]), \
            patch.object(command.time, 'sleep'):
        assert command.collect(args) == 0
    with sqlite3.connect(args.sqlite) as c:
        assert c.execute("SELECT count(*) FROM delivery_events WHERE type='error'").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM delivery_events WHERE type='public_status'").fetchone()[0] == 1
