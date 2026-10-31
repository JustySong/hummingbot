"""SQLite observations and queryable projections; original JSON preserves decimal precision."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_runs (
 run_id TEXT PRIMARY KEY, started_ms REAL NOT NULL, ended_ms REAL, transport TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_events (
 event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES delivery_runs(run_id),
 observed_ms REAL NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS delivery_events_time ON delivery_events(type, observed_ms);
CREATE TABLE IF NOT EXISTS delivery_contracts (
 exchange TEXT NOT NULL, symbol TEXT NOT NULL, expiry_ms INTEGER NOT NULL,
 base_asset TEXT NOT NULL, quote_asset TEXT NOT NULL, settle_asset TEXT NOT NULL,
 period TEXT NOT NULL, usd_per_quantity_unit TEXT NOT NULL, quantity_unit TEXT NOT NULL,
 quantity_step_usd TEXT NOT NULL, PRIMARY KEY(exchange, symbol, expiry_ms));
CREATE TABLE IF NOT EXISTS delivery_books (
 book_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES delivery_events(event_id),
 observed_ms REAL NOT NULL, exchange TEXT NOT NULL, symbol TEXT NOT NULL, base_asset TEXT NOT NULL,
 expiry_ms INTEGER NOT NULL, exchange_ts_ms INTEGER NOT NULL, received_ms REAL NOT NULL,
 best_bid REAL NOT NULL, best_ask REAL NOT NULL, bids_json TEXT NOT NULL, asks_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS delivery_books_market ON delivery_books(base_asset, expiry_ms, exchange, observed_ms);
CREATE TABLE IF NOT EXISTS delivery_spreads (
 spread_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES delivery_events(event_id),
 observed_ms REAL NOT NULL, base_asset TEXT NOT NULL, expiry_ms INTEGER NOT NULL,
 buy_exchange TEXT NOT NULL, sell_exchange TEXT NOT NULL, notional_usd REAL NOT NULL,
 buy_price REAL, sell_price REAL, gross_spread_bps REAL, sufficient_depth INTEGER NOT NULL,
 quality_ok INTEGER NOT NULL, quality_reasons_json TEXT NOT NULL, quote_skew_ms REAL NOT NULL);
CREATE INDEX IF NOT EXISTS delivery_spreads_pair
 ON delivery_spreads(base_asset, expiry_ms, buy_exchange, sell_exchange, observed_ms);
CREATE INDEX IF NOT EXISTS delivery_spreads_quality ON delivery_spreads(quality_ok, sufficient_depth, observed_ms);
"""


def encode(value):
    return json.dumps(value, default=str)


class Output:
    def __init__(self, stream, database=None, transport='rest'):
        self.stream, self.db = stream, None
        self.run_id = uuid.uuid4().hex
        if database is not None:
            database.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(database, timeout=10)
            try:
                self.db.execute('PRAGMA journal_mode=WAL')
                self.db.execute('PRAGMA foreign_keys=ON')
                self.db.executescript(SCHEMA)
                version = self.db.execute("SELECT value FROM delivery_meta WHERE key='schema_version'").fetchone()
                if version is not None and version[0] != '1':
                    raise ValueError('Unsupported delivery SQLite schema version')
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO delivery_meta VALUES ('schema_version','1')")
                    self.db.execute('INSERT INTO delivery_runs VALUES (?, ?, NULL, ?)',
                                    (self.run_id, time.time() * 1000, transport))
            except BaseException:
                self.db.close()
                raise

    def contract(self, c):
        self.db.execute('INSERT OR REPLACE INTO delivery_contracts VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (c['exchange'], c['symbol'], c['expiry_ms'], c['base_asset'], c['quote_asset'],
                         c['settle_asset'], c.get('period', ''), c['usd_per_contract'],
                         c.get('quantity_unit', 'contracts'), c.get('quantity_step_usd', '')))

    def write_record(self, record):
        payload = encode(record)
        if self.db is not None:
            # Event and projections commit together; malformed data cannot leave half an observation.
            with self.db:
                observed = record.get('observed_ms', record.get('received_ms', time.time() * 1000))
                event = self.db.execute('INSERT INTO delivery_events VALUES (NULL,?,?,?,?)',
                                        (self.run_id, observed, record['type'], payload)).lastrowid
                for group in record.get('pairs', []):
                    for c in group:
                        self.contract(c)
                for ex, book in record.get('books', {}).items():
                    c = book['contract']
                    self.contract(c)
                    self.db.execute('INSERT INTO delivery_books VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)',
                                    (event, observed, ex, c['symbol'], c['base_asset'], c['expiry_ms'],
                                     book['exchange_ts_ms'], book['received_ms'], float(book['bids'][0][0]),
                                     float(book['asks'][0][0]), encode(book['bids']), encode(book['asks'])))
                if record['type'] == 'spread':
                    exchanges = record['exchanges']
                    for buy, sell in (exchanges, exchanges[::-1]):
                        direction = record['directions'][f'buy_{buy}_sell_{sell}']
                        self.db.execute('INSERT INTO delivery_spreads VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                        (event, observed, record['base_asset'], record['expiry_ms'], buy, sell,
                                         float(record['notional_usd']), number(direction['buy_price']),
                                         number(direction['sell_price']), number(direction['gross_spread_bps']),
                                         int(direction['sufficient_depth']), int(record['quality_ok']),
                                         encode(record['quality_reasons']), record['quote_skew_ms']))
        self.stream.write(payload + '\n')
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
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('a') as stream:
        output = Output(stream, getattr(args, 'sqlite', None), getattr(args, 'transport', 'rest'))
        try:
            yield output
        finally:
            output.close()
