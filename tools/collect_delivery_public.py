#!/usr/bin/env python3
"""Collect optional public delivery datasets without slowing the order-book collector."""
import argparse
import logging
import time
from pathlib import Path

try:
    from . import collect_delivery_spread as core
    from .delivery_public_data import DATASETS, PublicData
except ImportError:
    import collect_delivery_spread as core
    from delivery_public_data import DATASETS, PublicData


def collect(args):
    client, successes = PublicData(), 0
    with core.open_output(args) as output:
        for cycle in range(args.samples):
            groups, metadata = core.discover(args.exchanges)
            core.write_record(output, {'type': 'discovery', 'observed_ms': time.time() * 1000,
                                       'metadata': metadata, 'pairs': [[vars(c) for c in g.values()] for g in groups]})
            contracts = [c for g in groups for c in g.values() if c.base_asset in args.assets
                         and (not args.symbols or c.symbol in args.symbols)]
            seen = set()
            for c in contracts:
                for dataset in args.datasets:
                    scope = c.base_asset if dataset == 'settlements' else c.symbol
                    if (c.exchange, dataset, scope) in seen:
                        continue
                    seen.add((c.exchange, dataset, scope))
                    try:
                        rows = client.fetch(dataset, c, args.limit)
                        core.write_record(output, {'type': 'public_data', 'exchange': c.exchange,
                                                   'dataset': dataset, 'observed_ms': time.time() * 1000,
                                                   'base_asset': c.base_asset, 'rows': rows})
                        successes += 1
                        logging.info('%s %s %s: %s rows', c.exchange, c.symbol, dataset, len(rows))
                    except (*core.ERRORS, NotImplementedError) as error:
                        logging.warning('%s %s %s: %s', c.exchange, c.symbol, dataset, error)
                        core.write_record(output, {'type': 'error', 'exchange': c.exchange, 'dataset': dataset,
                                                   'symbol': c.symbol, 'observed_ms': time.time() * 1000,
                                                   'status': 'unsupported' if isinstance(error, NotImplementedError) else 'failed',
                                                   'error': str(error)})
                    time.sleep(0.15)
            if cycle + 1 < args.samples:
                time.sleep(args.interval)
    return 0 if successes else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--exchanges', nargs='+', choices=sorted(core.BASE_URLS), default=sorted(core.BASE_URLS))
    p.add_argument('--datasets', nargs='+', choices=DATASETS, default=['ticker', 'open_interest'])
    p.add_argument('--assets', nargs='+', default=['BTC', 'ETH'])
    p.add_argument('--symbols', nargs='+', help='Optional exact exchange symbol filter')
    p.add_argument('--limit', type=int, default=20, help='Recent rows per request/page, not full-history backfill')
    p.add_argument('--samples', type=int, default=1)
    p.add_argument('--interval', type=float, default=60)
    p.add_argument('--sqlite-rotate-hours', type=int, default=6,
                   help='UTC epoch-aligned SQLite windows (default 6; 0 disables rotation)')
    p.add_argument('--sqlite', type=Path, default=Path('logs/delivery_spread_v2.sqlite'))
    p.add_argument('--output', type=Path, default=None)
    args = p.parse_args()
    args.transport = 'public-rest'
    if (not 1 <= args.limit <= 100 or args.samples < 1 or not core.math.isfinite(args.interval)
            or args.interval < 10 or args.sqlite_rotate_hours < 0):
        p.error('Use limit 1..100, samples >= 1, interval >= 10 seconds and sqlite-rotate-hours >= 0')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        return collect(args)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
