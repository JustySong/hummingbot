"""Compact SQLite storage: deduplicated five-level books and linked spread observations."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager, nullcontext

SCHEMA = """
CREATE TABLE delivery_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO delivery_meta VALUES ('schema_version','2');
CREATE TABLE delivery_runs (
 run_id TEXT PRIMARY KEY, started_ms REAL NOT NULL, ended_ms REAL, transport TEXT NOT NULL);
CREATE TABLE delivery_events (
 event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES delivery_runs,
 observed_ms REAL NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE delivery_contracts (
 contract_id INTEGER PRIMARY KEY, exchange TEXT NOT NULL, symbol TEXT NOT NULL, expiry_ms INTEGER NOT NULL,
 base_asset TEXT NOT NULL, quote_asset TEXT NOT NULL, settle_asset TEXT NOT NULL, period TEXT NOT NULL,
 usd_per_quantity_unit TEXT NOT NULL, quantity_unit TEXT NOT NULL, quantity_step_usd TEXT NOT NULL,
 UNIQUE(exchange,symbol,expiry_ms));
CREATE TABLE delivery_books (
 book_id INTEGER PRIMARY KEY, fingerprint BLOB NOT NULL UNIQUE,
 contract_id INTEGER NOT NULL REFERENCES delivery_contracts, observed_ms REAL NOT NULL,
 exchange_ts_ms INTEGER NOT NULL, local_quote_ts_ms REAL NOT NULL, received_ms REAL NOT NULL, rtt_ms REAL,
 best_bid REAL NOT NULL, best_ask REAL NOT NULL, bid_usd REAL NOT NULL, ask_usd REAL NOT NULL,
 bids_json TEXT NOT NULL, asks_json TEXT NOT NULL);
CREATE INDEX delivery_books_market ON delivery_books(contract_id,exchange_ts_ms);
CREATE TABLE delivery_spreads (
 spread_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES delivery_runs,
 observed_ms REAL NOT NULL, base_asset TEXT NOT NULL, expiry_ms INTEGER NOT NULL,
 buy_exchange TEXT NOT NULL, sell_exchange TEXT NOT NULL,
 buy_book_id INTEGER NOT NULL REFERENCES delivery_books, sell_book_id INTEGER NOT NULL REFERENCES delivery_books,
 notional_usd REAL NOT NULL, buy_price REAL, sell_price REAL, gross_spread_bps REAL,
 sufficient_depth INTEGER NOT NULL, quality_ok INTEGER NOT NULL,
 quality_reasons_json TEXT NOT NULL, quote_skew_ms REAL NOT NULL);
CREATE INDEX delivery_spreads_pair
 ON delivery_spreads(base_asset,expiry_ms,buy_exchange,sell_exchange,observed_ms);
"""


PUBLIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_public_data (
 data_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES delivery_runs,
 observed_ms REAL NOT NULL, exchange TEXT NOT NULL, symbol TEXT NOT NULL, base_asset TEXT NOT NULL,
 dataset TEXT NOT NULL, source_ts_ms INTEGER NOT NULL, source_key TEXT NOT NULL, payload_json TEXT NOT NULL,
 UNIQUE(exchange,dataset,symbol,source_key));
CREATE INDEX IF NOT EXISTS delivery_public_market
 ON delivery_public_data(exchange,dataset,symbol,source_ts_ms);
"""


def encode(value):
    return json.dumps(value, default=str, separators=(',', ':'))


def cap_depth(value):
    if isinstance(value, dict):
        return {k: cap_depth(v[:5] if k in ('bids', 'asks', 'b', 'a') and isinstance(v, list) else v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [cap_depth(v) for v in value]
    return value


class Output:
    def __init__(self, stream=None, database=None, transport='rest'):
        self.stream, self.db = stream, None
        self.run_id = uuid.uuid4().hex
        if database is None:
            return
        database.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database, timeout=10)
        try:
            exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_meta'").fetchone()
            if exists:
                version = self.db.execute("SELECT value FROM delivery_meta WHERE key='schema_version'").fetchone()
                if version != ('2',):
                    raise ValueError('Legacy/unsupported SQLite schema: use a new --sqlite path '
                                     '(e.g. logs/delivery_spread_v2.sqlite); old data is unchanged')
            elif self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError('Use a new empty SQLite database for delivery observations')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA foreign_keys=ON')
            if not exists:
                self.db.executescript(SCHEMA)
            self.db.executescript(PUBLIC_SCHEMA)
            with self.db:
                self.db.execute('INSERT INTO delivery_runs VALUES (?,?,NULL,?)',
                                (self.run_id, time.time() * 1000, transport))
        except BaseException:
            self.db.close()
            raise

    def contract(self, c):
        key = (c['exchange'], c['symbol'], c['expiry_ms'])
        values = (c['base_asset'], c['quote_asset'], c['settle_asset'], c.get('period', ''),
                  c['usd_per_contract'], c.get('quantity_unit', 'contracts'), c.get('quantity_step_usd', ''))
        row = self.db.execute('SELECT * FROM delivery_contracts WHERE exchange=? AND symbol=? AND expiry_ms=?',
                              key).fetchone()
        if row is None:
            return self.db.execute('INSERT INTO delivery_contracts VALUES (NULL,?,?,?,?,?,?,?,?,?,?)',
                                   key + values).lastrowid
        if tuple(row[4:]) != values:
            self.db.execute('UPDATE delivery_contracts SET base_asset=?,quote_asset=?,settle_asset=?,period=?, '
                            'usd_per_quantity_unit=?,quantity_unit=?,quantity_step_usd=? WHERE contract_id=?',
                            values + (row[0],))
        return row[0]

    def book(self, book, observed):
        c = book['contract']
        contract_id = self.contract(c)
        # Include receive time and levels: same timestamp alone isn't sufficient to identify a quote.
        fingerprint = hashlib.blake2b(encode([contract_id, book['exchange_ts_ms'], book['local_quote_ts_ms'],
                                             book['received_ms'], book['bids'], book['asks']]).encode(),
                                      digest_size=16).digest()
        row = self.db.execute('SELECT book_id FROM delivery_books WHERE fingerprint=?', (fingerprint,)).fetchone()
        if row:
            return row[0]
        factor = float(c['usd_per_contract'])
        return self.db.execute('INSERT INTO delivery_books VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (fingerprint, contract_id, observed, book['exchange_ts_ms'],
                                book['local_quote_ts_ms'], book['received_ms'], book.get('rtt_ms'),
                                float(book['bids'][0][0]), float(book['asks'][0][0]),
                                float(book['bids'][0][1]) * factor, float(book['asks'][0][1]) * factor,
                                encode(book['bids']), encode(book['asks']))).lastrowid

    def write_record(self, record):
        record = cap_depth(record)
        observed = record.get('observed_ms', record.get('received_ms', time.time() * 1000))
        if self.db is not None:
            with self.db:
                for group in record.get('pairs', []):
                    for c in group:
                        self.contract(c)
                books = {ex: self.book(book, observed) for ex, book in record.get('books', {}).items()}
                if record['type'] == 'spread':
                    exchanges = record['exchanges']
                    for buy, sell in (exchanges, exchanges[::-1]):
                        direction = record['directions'][f'buy_{buy}_sell_{sell}']
                        self.db.execute('INSERT INTO delivery_spreads VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                        (self.run_id, observed, record['base_asset'], record['expiry_ms'], buy, sell,
                                         books[buy], books[sell], float(record['notional_usd']),
                                         number(direction['buy_price']), number(direction['sell_price']),
                                         number(direction['gross_spread_bps']), int(direction['sufficient_depth']),
                                         int(record['quality_ok']), encode(record['quality_reasons']),
                                         record['quote_skew_ms']))
                elif record['type'] == 'public_data':
                    for row in record['rows']:
                        key = str(row.get('id', row['timestamp_ms']))
                        self.db.execute(
                            'INSERT INTO delivery_public_data VALUES (NULL,?,?,?,?,?,?,?,?,?) '
                            'ON CONFLICT(exchange,dataset,symbol,source_key) DO UPDATE SET '
                            'run_id=excluded.run_id,observed_ms=excluded.observed_ms,payload_json=excluded.payload_json '
                            'WHERE payload_json!=excluded.payload_json',
                            (self.run_id, observed, record['exchange'], row['symbol'], record['base_asset'],
                             record['dataset'], row['timestamp_ms'], key, encode(row)))
                    summary = {k: v for k, v in record.items() if k != 'rows'}
                    summary['row_count'] = len(record['rows'])
                    self.db.execute('INSERT INTO delivery_events VALUES (NULL,?,?,?,?)',
                                    (self.run_id, observed, 'public_status', encode(summary)))
                elif record['type'] in ('error', 'ws_connected', 'discovery'):
                    if record['type'] == 'discovery':
                        # Contracts are normalized above; retain availability/errors and clocks, not raw responses.
                        record = {**record, 'metadata': {
                            ex: {k: v for k, v in info.items() if k in ('error', 'clock_error', 'contract_count')}
                            for ex, info in record.get('metadata', {}).items()},
                            'pairs': [[{'exchange': c['exchange'], 'symbol': c['symbol'], 'expiry_ms': c['expiry_ms']}
                                       for c in group] for group in record.get('pairs', [])]}
                    self.db.execute('INSERT INTO delivery_events VALUES (NULL,?,?,?,?)',
                                    (self.run_id, observed, record['type'], encode(record)))
        if self.stream is not None:
            self.stream.write(encode(record) + '\n')
            self.stream.flush()

    def close(self):
        if self.db is not None:
            try:
                with self.db:
                    self.db.execute('UPDATE delivery_runs SET ended_ms=? WHERE run_id=?',
                                    (time.time() * 1000, self.run_id))
            finally:
                self.db.close()
                self.db = None


def number(value):
    return None if value is None else float(value)


@contextmanager
def open_output(args):
    path = getattr(args, 'output', None)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') if path is not None else nullcontext(None) as stream:
        output = Output(stream, getattr(args, 'sqlite', None), getattr(args, 'transport', 'rest'))
        try:
            yield output
        finally:
            output.close()
