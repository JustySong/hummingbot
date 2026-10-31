import argparse
import io
import json
import sqlite3
from decimal import Decimal

import pytest

from tools import collect_delivery_spread as core
from tools.delivery_spread_store import Output, open_output


def spread():
    books = {}
    for exchange in ('okx', 'deribit'):
        c = core.Contract(exchange, 'BTC-TEST', 1900000000000, '1')
        payload = {'bids': [[Decimal('100.123456789012345678'), 1000]], 'asks': [[Decimal('101'), 1000]]}
        books[exchange] = {'contract': vars(c), **payload, 'exchange_ts_ms': 10000,
                           'local_quote_ts_ms': 10000, 'received_ms': 10010, 'rtt_ms': None}
    return core.analyze(books['okx'], books['deribit'], 10050, Decimal(100), 5000, 1000)


def test_event_precision_and_queryable_projection(tmp_path):
    stream = io.StringIO()
    output = Output(stream, tmp_path / 'quotes.sqlite', 'ws')
    core.write_record(output, spread())
    c = sqlite3.connect(tmp_path / 'quotes.sqlite')
    payload = json.loads(c.execute('SELECT payload_json FROM delivery_events').fetchone()[0])
    assert payload['books']['okx']['bids'][0][0] == '100.123456789012345678'
    assert payload['schema_version'] == 2
    assert c.execute('SELECT count(*) FROM delivery_contracts').fetchone()[0] == 2
    assert c.execute('SELECT count(*) FROM delivery_books').fetchone()[0] == 2
    rows = c.execute('SELECT buy_exchange,sell_exchange,quality_ok,sufficient_depth,gross_spread_bps '
                     'FROM delivery_spreads ORDER BY spread_id').fetchall()
    assert [(r[0], r[1]) for r in rows] == [('okx', 'deribit'), ('deribit', 'okx')]
    assert all(r[2] == r[3] == 1 and r[4] < 0 for r in rows)
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    output.close()
    assert c.execute('SELECT ended_ms FROM delivery_runs').fetchone()[0] is not None
    c.close()


def test_insufficient_depth_and_invalid_quality_are_not_lost(tmp_path):
    record = spread()
    record['quality_ok'] = False
    record['quality_reasons'] = ['quote_timestamp_skew']
    for value in record['directions'].values():
        value.update(sufficient_depth=False, buy_price=None, gross_spread_bps=None)
    output = Output(io.StringIO(), tmp_path / 'quotes.sqlite')
    output.write_record(record)
    output.close()
    with sqlite3.connect(tmp_path / 'quotes.sqlite') as c:
        rows = c.execute('SELECT quality_ok,sufficient_depth,buy_price,gross_spread_bps FROM delivery_spreads').fetchall()
        assert rows == [(0, 0, None, None), (0, 0, None, None)]


def test_append_new_run_and_discovery_error_history(tmp_path):
    args = argparse.Namespace(output=tmp_path / 'quotes.jsonl', sqlite=tmp_path / 'quotes.sqlite', transport='ws')
    for _ in range(2):
        with open_output(args) as sink:
            core.write_record(sink, {'type': 'discovery', 'observed_ms': 10000,
                                     'pairs': [[book['contract'] for book in spread()['books'].values()]],
                                     'metadata': {'bybit': {'error': '403'}}})
            core.write_record(sink, spread())
    with sqlite3.connect(args.sqlite) as c:
        assert c.execute('SELECT count(*) FROM delivery_runs WHERE ended_ms IS NOT NULL').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM delivery_events').fetchone()[0] == 4
        assert c.execute('SELECT count(*) FROM delivery_contracts').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM delivery_spreads').fetchone()[0] == 4
    assert len(args.output.read_text().splitlines()) == 4


def test_malformed_record_rolls_back_entire_event(tmp_path):
    output = Output(io.StringIO(), tmp_path / 'quotes.sqlite')
    record = spread()
    del record['directions']['buy_deribit_sell_okx']
    with pytest.raises(KeyError):
        output.write_record(record)
    assert output.db.execute('SELECT count(*) FROM delivery_events').fetchone()[0] == 0
    assert output.db.execute('SELECT count(*) FROM delivery_books').fetchone()[0] == 0
    assert output.db.execute('SELECT count(*) FROM delivery_spreads').fetchone()[0] == 0
    output.close()
