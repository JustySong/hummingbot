import argparse
import sqlite3
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from tools import collect_delivery_spread as core
from tools import delivery_spread_store as store


def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def spread():
    books = {}
    for ex in ('okx', 'deribit'):
        c = core.Contract(ex, 'BTC-TEST', 1900000000000, '1')
        books[ex] = {'contract': vars(c), 'bids': [[Decimal(100), Decimal(1000)]],
                     'asks': [[Decimal(101), Decimal(1000)]], 'exchange_ts_ms': 10000,
                     'local_quote_ts_ms': 10000, 'received_ms': 10010, 'rtt_ms': None}
    return core.analyze(books['okx'], books['deribit'], 10020, Decimal(100), 5000, 1000)


@pytest.mark.parametrize('moment,expected', [
    ('2026-11-01T00:00:00Z', '20261101T000000Z'),
    ('2026-11-01T05:59:59.999Z', '20261101T000000Z'),
    ('2026-11-01T06:00:00Z', '20261101T060000Z'),
    ('2026-11-01T12:15:00Z', '20261101T120000Z'),
    ('2026-11-01T23:59:59Z', '20261101T180000Z'),
    ('2026-11-02T00:00:00Z', '20261102T000000Z'),
    ('2026-12-01T00:00:00Z', '20261201T000000Z'),
])
def test_six_hour_utc_alignment(tmp_path, moment, expected):
    start, path = store.rotation_path(tmp_path / 'quotes.sqlite', 6, utc(moment))
    assert path.name == f'quotes_{expected}_6h.sqlite'
    assert start <= utc(moment) < start + 6 * 3600


def test_other_intervals_epoch_anchor_and_timezone(tmp_path):
    # Same instant represented with UTC+8 must select the same file.
    assert store.rotation_path(tmp_path / 'quotes', 3, utc('2026-11-01T16:30:00+08:00'))[1].name == \
        'quotes_20261101T060000Z_3h.sqlite'
    start, path = store.rotation_path(tmp_path / 'quotes.db', 5, utc('2026-11-01T17:00:00Z'))
    assert start % (5 * 3600) == 0 and path.suffix == '.db'
    assert store.rotation_path(tmp_path / 'quotes.db', 12, utc('2026-11-01T17:00:00Z'))[1] != path
    for hours in (0, -1, 1.5):
        with pytest.raises(ValueError):
            store.rotation_path(tmp_path / 'quotes.sqlite', hours, 0)


def test_boundary_switch_closes_run_and_recreates_referenced_books(tmp_path):
    before = utc('2026-11-01T05:59:59Z')
    after = utc('2026-11-01T06:00:00Z')
    with patch.object(store.time, 'time', return_value=before) as clock:
        output = store.RotatingOutput(None, tmp_path / 'quotes.sqlite', 'ws', 6)
        output.write_record(spread())
        first_path, first_run = output.path, output.active.run_id
        clock.return_value = after
        output.write_record(spread())
        second_path, second_run = output.path, output.active.run_id
        output.close()
    assert first_path != second_path and first_run != second_run
    for path in (first_path, second_path):
        with sqlite3.connect(path) as c:
            assert c.execute('SELECT count(*) FROM delivery_books').fetchone()[0] == 2
            assert c.execute('SELECT count(*) FROM delivery_spreads').fetchone()[0] == 2
            assert c.execute('PRAGMA foreign_key_check').fetchall() == []
            assert c.execute('SELECT ended_ms FROM delivery_runs').fetchone()[0] is not None
    # Routing uses write-time UTC, not old exchange timestamps or the launch-time offset.
    assert second_path.name == 'quotes_20261101T060000Z_6h.sqlite'


def test_restart_appends_current_window_and_disabled_mode(tmp_path):
    args = argparse.Namespace(output=None, sqlite=tmp_path / 'quotes.sqlite', transport='ws', sqlite_rotate_hours=6)
    with patch.object(store.time, 'time', return_value=utc('2026-11-01T07:00:00Z')):
        for _ in range(2):
            with store.open_output(args) as out:
                out.write_record(spread())
    paths = list(tmp_path.glob('*.sqlite'))
    assert len(paths) == 1
    with sqlite3.connect(paths[0]) as c:
        assert c.execute('SELECT count(*) FROM delivery_runs').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM delivery_spreads').fetchone()[0] == 4
    args.sqlite_rotate_hours = 0
    with store.open_output(args) as out:
        out.write_record(spread())
    assert args.sqlite.exists()


def test_new_file_failure_keeps_old_connection_and_does_not_misroute(tmp_path):
    before, after = utc('2026-11-01T05:59:59Z'), utc('2026-11-01T06:00:00Z')
    base = tmp_path / 'quotes.sqlite'
    bad_path = store.rotation_path(base, 6, after)[1]
    with sqlite3.connect(bad_path) as c:
        c.execute('CREATE TABLE delivery_meta(key TEXT PRIMARY KEY,value TEXT)')
        c.execute("INSERT INTO delivery_meta VALUES ('schema_version','1')")
    original = bad_path.read_bytes()
    with patch.object(store.time, 'time', return_value=before) as clock:
        out = store.RotatingOutput(None, base, 'ws', 6)
        out.write_record(spread())
        old_path = out.path
        clock.return_value = after
        with pytest.raises(ValueError, match='Legacy'):
            out.write_record(spread())
        assert out.path == old_path and out.active.db is not None
        assert out.active.db.execute('SELECT count(*) FROM delivery_spreads').fetchone()[0] == 2
        out.close()
    assert bad_path.read_bytes() == original


def test_cli_default_rotates_without_explicit_parameter(tmp_path):
    args = ['collector', '--list-only', '--sqlite', str(tmp_path / 'quotes.sqlite')]
    with patch('sys.argv', args), patch.object(core, 'discover', return_value=([], {})), \
            patch.object(store.time, 'time', return_value=utc('2026-11-01T13:10:00Z')):
        assert core.main() == 1
    assert (tmp_path / 'quotes_20261101T120000Z_6h.sqlite').exists()
    assert not (tmp_path / 'quotes.sqlite').exists()
