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
    levels = json.loads(c.execute('SELECT bids_json FROM delivery_books LIMIT 1').fetchone()[0])
    assert levels[0][0] == '100.123456789012345678'
    assert c.execute('SELECT count(*) FROM delivery_events').fetchone()[0] == 0
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
        assert c.execute('SELECT count(*) FROM delivery_events').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM delivery_books').fetchone()[0] == 2
        assert '403' in c.execute('SELECT payload_json FROM delivery_events LIMIT 1').fetchone()[0]
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


def test_books_saved_once_and_spreads_reference_quotes(tmp_path):
    output = Output(None, tmp_path / 'quotes.sqlite')
    record = spread()
    output.write_record({'type': 'ws_books', 'observed_ms': 10050, 'books': record['books']})
    for _ in range(3):
        output.write_record(record)
    assert output.db.execute('SELECT count(*) FROM delivery_books').fetchone()[0] == 2
    assert output.db.execute('SELECT count(*) FROM delivery_spreads').fetchone()[0] == 6
    assert output.db.execute('SELECT count(*) FROM delivery_events').fetchone()[0] == 0
    assert output.db.execute('''SELECT count(*) FROM delivery_spreads s
        JOIN delivery_books b ON b.book_id=s.buy_book_id
        JOIN delivery_books a ON a.book_id=s.sell_book_id''').fetchone()[0] == 6
    changed = spread()
    changed['books']['okx']['received_ms'] += 1
    output.write_record(changed)
    assert output.db.execute('SELECT count(*) FROM delivery_books').fetchone()[0] == 3
    output.close()


def test_cap_five_levels_in_database_and_debug_json(tmp_path):
    stream = io.StringIO()
    output = Output(stream, tmp_path / 'quotes.sqlite')
    record = spread()
    for book in record['books'].values():
        book['bids'] = [[str(100 - i), '1'] for i in range(20)]
        book['asks'] = [[str(101 + i), '1'] for i in range(20)]
    output.write_record({'type': 'ws_books', 'books': record['books']})
    assert all(len(json.loads(row[0])) == len(json.loads(row[1])) == 5
               for row in output.db.execute('SELECT bids_json,asks_json FROM delivery_books'))
    assert len(json.loads(stream.getvalue())['books']['okx']['bids']) == 5
    # Debug raw messages are also capped; not copied into the SQLite event log.
    output.write_record({'type': 'raw_ws', 'message': {'data': {'b': list(range(20)), 'a': list(range(20))}}})
    assert len(json.loads(stream.getvalue().splitlines()[-1])['message']['data']['b']) == 5
    assert output.db.execute('SELECT count(*) FROM delivery_events').fetchone()[0] == 0
    output.close()


def test_discovery_saves_errors_but_not_raw_metadata(tmp_path):
    output = Output(None, tmp_path / 'quotes.sqlite')
    record = {'type': 'discovery', 'pairs': [], 'metadata': {
        'okx': {'samples': [{'payload': 'X' * 100000}], 'contract_count': 2},
        'bybit': {'error': '403'}}}
    output.write_record(record)
    payload = output.db.execute('SELECT payload_json FROM delivery_events').fetchone()[0]
    assert len(payload) < 500
    assert 'samples' not in payload and '403' in payload
    output.close()


def test_legacy_database_is_rejected_without_modification(tmp_path):
    path = tmp_path / 'old.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE delivery_meta(key TEXT PRIMARY KEY,value TEXT)')
        c.execute("INSERT INTO delivery_meta VALUES ('schema_version','1')")
        c.execute('CREATE TABLE user_data(value TEXT)')
        c.execute("INSERT INTO user_data VALUES ('keep')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match='new --sqlite path'):
        Output(None, path)
    assert path.read_bytes() == before


def test_default_sqlite_only_does_not_create_jsonl(tmp_path):
    args = argparse.Namespace(output=None, sqlite=tmp_path / 'quotes.sqlite', transport='ws')
    with open_output(args) as sink:
        core.write_record(sink, spread())
    assert list(tmp_path.glob('*.jsonl')) == []
    with sqlite3.connect(args.sqlite) as c:
        assert c.execute('SELECT count(*) FROM delivery_spreads').fetchone()[0] == 2
