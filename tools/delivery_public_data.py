"""Bounded, read-only delivery market datasets; no authenticated endpoints."""
import time
from decimal import Decimal

try:
    from . import collect_delivery_spread as core
except ImportError:
    import collect_delivery_spread as core

DATASETS = ('ticker', 'open_interest', 'trades', 'candles', 'settlements')


def usd(value, factor):
    return None if value in (None, '') else str(Decimal(str(value)) * Decimal(str(factor)))


class PublicData:
    def __init__(self, request=None):
        self.request = request or core.get_json

    def get(self, c, path, params=None):
        return self.request(c.exchange, path, params)['payload']

    def fetch(self, dataset, c, limit=20, now_ms=None):
        if dataset not in DATASETS:
            raise ValueError(f'Unknown dataset: {dataset}')
        if not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        if c.exchange not in core.BASE_URLS or c.quote_asset != 'USD' or c.settle_asset != c.base_asset:
            raise ValueError('Only supported USD inverse, base-settled contracts are accepted')
        now = int(time.time() * 1000) if now_ms is None else now_ms
        return getattr(self, dataset)(c, limit, now)

    def ticker(self, c, limit, now):
        ex = c.exchange
        if ex == 'okx':
            r = self.get(c, '/api/v5/market/ticker', {'instId': c.symbol})['data'][0]
            mark = self.get(c, '/api/v5/public/mark-price', {'instType': 'FUTURES', 'instId': c.symbol})['data'][0]
            index = self.get(c, '/api/v5/market/index-tickers', {'instId': f'{c.base_asset}-USD'})['data'][0]
            return [{'symbol': c.symbol, 'timestamp_ms': int(r['ts']), 'last_price': r['last'],
                     'bid_price': r['bidPx'], 'ask_price': r['askPx'], 'mark_price': mark['markPx'],
                     'mark_timestamp_ms': int(mark['ts']), 'index_price': index['idxPx'],
                     'index_timestamp_ms': int(index['ts'])}]
        if ex == 'binance':
            r = self.get(c, '/dapi/v1/premiumIndex', {'symbol': c.symbol})
            r = r[0] if isinstance(r, list) else r
            return [{'symbol': c.symbol, 'timestamp_ms': int(r['time']),
                     'mark_price': r['markPrice'], 'index_price': r['indexPrice']}]
        if ex == 'deribit':
            r = self.get(c, '/api/v2/public/ticker', {'instrument_name': c.symbol})['result']
            return [{'symbol': c.symbol, 'timestamp_ms': r['timestamp'], 'mark_price': str(r['mark_price']),
                     'index_price': str(r['index_price']), 'last_price': r.get('last_price'),
                     'bid_price': r.get('best_bid_price'), 'ask_price': r.get('best_ask_price')}]
        p = self.get(c, '/v5/market/tickers', {'category': 'inverse', 'symbol': c.symbol})
        r = p['result']['list'][0]
        return [{'symbol': c.symbol, 'timestamp_ms': int(p['time']), 'mark_price': r['markPrice'],
                 'index_price': r['indexPrice'], 'last_price': r['lastPrice'],
                 'bid_price': r.get('bid1Price'), 'ask_price': r.get('ask1Price')}]

    def open_interest(self, c, limit, now):
        if c.exchange == 'okx':
            r = self.get(c, '/api/v5/public/open-interest', {'instType': 'FUTURES', 'instId': c.symbol})['data'][0]
            return [{'symbol': c.symbol, 'timestamp_ms': int(r['ts']), 'open_interest': r['oi'],
                     'unit': 'contracts', 'open_interest_usd': r.get('oiUsd') or usd(r['oi'], c.usd_per_contract),
                     'open_interest_base': r.get('oiCcy')}]
        if c.exchange == 'binance':
            r = self.get(c, '/dapi/v1/openInterest', {'symbol': c.symbol})
            return [{'symbol': c.symbol, 'timestamp_ms': int(r['time']), 'open_interest': r['openInterest'],
                     'unit': 'contracts', 'open_interest_usd': usd(r['openInterest'], c.usd_per_contract)}]
        if c.exchange == 'deribit':
            r = self.get(c, '/api/v2/public/ticker', {'instrument_name': c.symbol})['result']
            return [{'symbol': c.symbol, 'timestamp_ms': r['timestamp'],
                     'open_interest': str(r['open_interest']), 'unit': 'USD',
                     'open_interest_usd': str(r['open_interest'])}]
        p = self.get(c, '/v5/market/tickers', {'category': 'inverse', 'symbol': c.symbol})
        r = p['result']['list'][0]
        return [{'symbol': c.symbol, 'timestamp_ms': int(p['time']), 'open_interest': r['openInterest'],
                 'unit': 'USD', 'open_interest_usd': r['openInterest']}]

    def trades(self, c, limit, now):
        ex = c.exchange
        if ex == 'okx':
            data = self.get(c, '/api/v5/market/trades', {'instId': c.symbol, 'limit': limit})['data']
            return [{'symbol': c.symbol, 'id': r['tradeId'], 'timestamp_ms': int(r['ts']), 'price': r['px'],
                     'size': r['sz'], 'unit': 'contracts', 'notional_usd': usd(r['sz'], c.usd_per_contract),
                     'taker_side': r['side']} for r in data]
        if ex == 'binance':
            data = self.get(c, '/dapi/v1/trades', {'symbol': c.symbol, 'limit': limit})
            return [{'symbol': c.symbol, 'id': str(r['id']), 'timestamp_ms': r['time'], 'price': r['price'],
                     'size': r['qty'], 'unit': 'contracts', 'notional_usd': usd(r['qty'], c.usd_per_contract),
                     'taker_side': 'sell' if r['isBuyerMaker'] else 'buy'} for r in data]
        if ex == 'deribit':
            data = self.get(c, '/api/v2/public/get_last_trades_by_instrument',
                            {'instrument_name': c.symbol, 'count': limit, 'sorting': 'desc'})['result']['trades']
            return [{'symbol': c.symbol, 'id': r['trade_id'], 'timestamp_ms': r['timestamp'],
                     'price': str(r['price']), 'size': str(r['amount']), 'unit': 'USD',
                     'notional_usd': str(r['amount']), 'taker_side': r['direction']} for r in data]
        data = self.get(c, '/v5/market/recent-trade',
                        {'category': 'inverse', 'symbol': c.symbol, 'limit': limit})['result']['list']
        return [{'symbol': c.symbol, 'id': r['execId'], 'timestamp_ms': int(r['time']), 'price': r['price'],
                 'size': r['size'], 'unit': 'USD', 'notional_usd': r['size'],
                 'taker_side': r['side'].lower()} for r in data]

    def candles(self, c, limit, now):
        """Latest bounded one-minute candles; unfinished bars are explicitly marked."""
        ex = c.exchange
        if ex == 'okx':
            data = self.get(c, '/api/v5/market/candles', {'instId': c.symbol, 'bar': '1m', 'limit': limit})['data']
            return [self.bar(c, r[:6], 'contracts', r[8] == '1') for r in data]
        if ex == 'binance':
            data = self.get(c, '/dapi/v1/klines', {'symbol': c.symbol, 'interval': '1m', 'limit': limit})
            return [self.bar(c, r[:6], 'contracts', int(r[6]) < now) for r in data]
        if ex == 'deribit':
            r = self.get(c, '/api/v2/public/get_tradingview_chart_data',
                         {'instrument_name': c.symbol, 'start_timestamp': now - limit * 60000,
                          'end_timestamp': now, 'resolution': '1'})['result']
            if r.get('status') == 'no_data':
                return []
            return [self.bar(c, [t, r['open'][i], r['high'][i], r['low'][i], r['close'][i], r['volume'][i]],
                             'base_asset', t + 60000 <= now) for i, t in enumerate(r['ticks'])][-limit:]
        data = self.get(c, '/v5/market/kline', {'category': 'inverse', 'symbol': c.symbol,
                                                'interval': '1', 'limit': limit})['result']['list']
        return [self.bar(c, r[:6], 'USD', int(r[0]) + 60000 <= now) for r in data]

    @staticmethod
    def bar(c, row, volume_unit, closed):
        return {'symbol': c.symbol, 'timestamp_ms': int(row[0]), 'interval_ms': 60000,
                'open': str(row[1]), 'high': str(row[2]), 'low': str(row[3]), 'close': str(row[4]),
                'volume': str(row[5]), 'volume_unit': volume_unit, 'is_closed': closed,
                'closure_source': 'exchange_confirm' if c.exchange == 'okx' else 'local_time_estimate'}

    def settlements(self, c, limit, now):
        """Recent underlying delivery history, including expired symbols; not a forecast."""
        if c.exchange == 'binance':
            raise NotImplementedError('No confirmed COIN-M public delivery-price-history endpoint in this adapter')
        if c.exchange == 'okx':
            data = self.get(c, '/api/v5/public/delivery-exercise-history',
                            {'instType': 'FUTURES', 'instFamily': f'{c.base_asset}-USD', 'limit': limit})['data']
            return [{'symbol': r.get('instId', r.get('insId')), 'timestamp_ms': int(group['ts']),
                     'delivery_price': r['px'], 'price_currency': 'USD'}
                    for group in data for r in group['details'] if r['type'] == 'delivery'
                    and (r.get('instId', r.get('insId')) or '').startswith(f'{c.base_asset}-USD-')]
        if c.exchange == 'deribit':
            data = self.get(c, '/api/v2/public/get_last_settlements_by_currency',
                            {'currency': c.base_asset, 'type': 'delivery', 'count': limit})['result']['settlements']
            # Include futures only, not options with C/P suffixes.
            return [{'symbol': r['instrument_name'], 'timestamp_ms': int(r['timestamp']),
                     'delivery_price': str(r['index_price']), 'price_currency': 'USD'}
                    for r in data if len(r['instrument_name'].split('-')) == 2 and not r['instrument_name'].endswith('PERPETUAL')]
        data = self.get(c, '/v5/market/delivery-price',
                        {'category': 'inverse', 'settleCoin': c.settle_asset, 'limit': limit})['result']['list']
        return [{'symbol': r['symbol'], 'timestamp_ms': int(r['deliveryTime']),
                 'delivery_price': r['deliveryPrice'], 'price_currency': 'USD'} for r in data
                if r['symbol'].startswith(f'{c.base_asset}USD')]
