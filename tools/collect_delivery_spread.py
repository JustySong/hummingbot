#!/usr/bin/env python3
"""Read-only multi-asset inverse delivery futures snapshots; Python standard library only."""
import argparse
import concurrent.futures
import json
from itertools import combinations
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

BASE_URLS = {"okx": "https://www.okx.com", "binance": "https://dapi.binance.com",
             "deribit": "https://www.deribit.com", "bybit": "https://api.bybit.com"}
D = Decimal


@dataclass(frozen=True)
class Contract:
    exchange: str
    symbol: str
    expiry_ms: int
    usd_per_contract: str
    base_asset: str = "BTC"
    quote_asset: str = "USD"
    settle_asset: str = "BTC"
    period: str = ""
    quantity_unit: str = "contracts"
    quantity_step_usd: str = ""


def get_json(exchange, path, params=None):
    url = BASE_URLS[exchange] + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    start = time.time() * 1000
    request = urllib.request.Request(url, headers={"User-Agent": "btc-delivery-research/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    end = time.time() * 1000
    if exchange == "okx" and payload.get("code") != "0":
        raise ValueError(f"OKX API error: {payload}")
    if exchange == "deribit" and "error" in payload:
        raise ValueError(f"Deribit API error: {payload['error']}")
    if exchange == "bybit" and payload.get("retCode") != 0:
        raise ValueError(f"Bybit API error: {payload}")
    return {"payload": payload, "sent_ms": start, "received_ms": end, "rtt_ms": end - start}


def contract_groups(okx, binance, now_ms):
    """Group all active inverse delivery contracts by asset, settlement and exact expiry."""
    contracts = []
    for item in okx["data"]:
        underlying = item.get("uly", "").split("-")
        if (item.get("instType") == "FUTURES" and len(underlying) == 2
                and underlying[1] == "USD" and item.get("ctType") == "inverse"
                and item.get("settleCcy") == underlying[0] and item.get("ctValCcy") == "USD"
                and item.get("state") == "live"):
            contracts.append(Contract("okx", item["instId"], int(item["expTime"]), str(item["ctVal"]),
                                      underlying[0], "USD", item["settleCcy"], item.get("alias", "")))
    for item in binance["symbols"]:
        base = item.get("baseAsset") or item.get("marginAsset")
        if (base and item.get("pair") == f"{base}USD" and item.get("marginAsset") == base
                and item.get("contractStatus") == "TRADING"
                and item.get("contractType") and item["contractType"] != "PERPETUAL"):
            contracts.append(Contract("binance", item["symbol"], int(item["deliveryDate"]),
                                      str(item["contractSize"]), base, "USD", base, item["contractType"]))
    return group_contracts(contracts, now_ms)


def group_contracts(contracts, now_ms):
    result = {}
    for contract in contracts:
        if contract.expiry_ms <= now_ms:
            continue
        if not D(contract.usd_per_contract).is_finite() or D(contract.usd_per_contract) <= 0:
            raise ValueError("Invalid contract face value")
        key = (contract.base_asset, contract.quote_asset, contract.settle_asset, contract.expiry_ms)
        pair = result.setdefault(key, {})
        if contract.exchange in pair:
            raise ValueError(f"Ambiguous contracts for {key}")
        pair[contract.exchange] = contract
    return [group for _, group in sorted(result.items())]


def parse_contracts(okx, binance, now_ms):
    # Compatibility API: return only contracts that can be compared across venues.
    return [group for group in contract_groups(okx, binance, now_ms) if len(group) == 2]


def parse_deribit(payload, now_ms):
    contracts = []
    for item in payload["result"]:
        base = item.get("base_currency")
        if (item.get("kind") == "future" and item.get("is_active")
                and item.get("instrument_type") == "reversed" and item.get("quote_currency") == "USD"
                and item.get("settlement_currency") == base
                and not item["instrument_name"].endswith("PERPETUAL")):
            # Deribit book amounts are already USD, not numbers of contract_size lots.
            contracts.append(Contract("deribit", item["instrument_name"], int(item["expiration_timestamp"]),
                                      "1", base, "USD", base, item.get("settlement_period", ""),
                                      "USD", str(item["min_trade_amount"])))
    return [c for g in group_contracts(contracts, now_ms) for c in g.values()]


def parse_bybit(payload, now_ms):
    contracts = []
    for item in payload["result"]["list"]:
        base = item.get("baseCoin")
        if (item.get("contractType") == "InverseFutures" and item.get("status") == "Trading"
                and item.get("quoteCoin") == "USD" and item.get("settleCoin") == base):
            contracts.append(Contract("bybit", item["symbol"], int(item["deliveryTime"]), "1",
                                      base, "USD", base, item["contractType"], "USD",
                                      item["lotSizeFilter"]["qtyStep"]))
    return [c for g in group_contracts(contracts, now_ms) for c in g.values()]


def discover_exchange(exchange):
    now_ms = time.time() * 1000
    if exchange == "okx":
        sample = get_json(exchange, "/api/v5/public/instruments", {"instType": "FUTURES"})
        groups = contract_groups(sample["payload"], {"symbols": []}, now_ms)
        return [c for g in groups for c in g.values()], [sample]
    if exchange == "binance":
        sample = get_json(exchange, "/dapi/v1/exchangeInfo")
        groups = contract_groups({"data": []}, sample["payload"], now_ms)
        return [c for g in groups for c in g.values()], [sample]
    if exchange == "deribit":
        contracts, samples = [], []
        for currency in ("BTC", "ETH"):
            sample = get_json(exchange, "/api/v2/public/get_instruments",
                              {"currency": currency, "kind": "future", "expired": "false"})
            contracts.extend(parse_deribit(sample["payload"], now_ms))
            samples.append(sample)
        return contracts, samples
    contracts, samples, cursor, seen = [], [], "", set()
    while True:
        params = {"category": "inverse", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        sample = get_json(exchange, "/v5/market/instruments-info", params)
        contracts.extend(parse_bybit(sample["payload"], now_ms))
        samples.append(sample)
        cursor = sample["payload"]["result"].get("nextPageCursor", "")
        if not cursor:
            break
        if cursor in seen:
            raise ValueError("Bybit repeated pagination cursor")
        seen.add(cursor)
    return contracts, samples


ERRORS = (OSError, ValueError, KeyError, IndexError, ArithmeticError)


def discover(exchanges=None):
    contracts, metadata = [], {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {ex: pool.submit(discover_exchange, ex) for ex in (exchanges or BASE_URLS)}
        for ex, job in jobs.items():
            try:
                found, samples = job.result()
                contracts.extend(found)
                metadata[ex] = {"samples": samples, "contract_count": len(found)}
            except ERRORS as error:
                logging.warning("%s discovery failed: %s", ex, error)
                metadata[ex] = {"error": str(error)}
    return group_contracts(contracts, time.time() * 1000), metadata


def clock_sample(exchange):
    paths = {"okx": "/api/v5/public/time", "binance": "/dapi/v1/time",
             "deribit": "/api/v2/public/get_time", "bybit": "/v5/market/time"}
    sample = get_json(exchange, paths[exchange])
    payload = sample["payload"]
    if exchange == "okx":
        server_ms = int(payload["data"][0]["ts"])
    elif exchange == "binance":
        server_ms = int(payload["serverTime"])
    elif exchange == "deribit":
        server_ms = int(payload["result"])
    else:
        server_ms = int(payload["result"]["timeNano"]) / 1_000_000
    sample["offset_ms"] = server_ms - (sample["sent_ms"] + sample["received_ms"]) / 2
    return sample


def fetch_book(contract):
    ex = contract.exchange
    if ex == "okx":
        return get_json(ex, "/api/v5/market/books", {"instId": contract.symbol, "sz": 20})
    if ex == "binance":
        return get_json(ex, "/dapi/v1/depth", {"symbol": contract.symbol, "limit": 20})
    if ex == "deribit":
        return get_json(ex, "/api/v2/public/get_order_book", {"instrument_name": contract.symbol, "depth": 20})
    return get_json(ex, "/v5/market/orderbook", {"category": "inverse", "symbol": contract.symbol, "limit": 25})


def normalize_book(contract, sample, clock):
    payload = sample["payload"]
    if contract.exchange == "okx":
        data = payload["data"][0]
        timestamp = int(data["ts"])
    elif contract.exchange == "binance":
        data, timestamp = payload, int(payload["E"])
    elif contract.exchange == "deribit":
        data = payload["result"]
        if data.get("state") != "open":
            raise ValueError("Deribit book is not open")
        timestamp = int(data["timestamp"])
    else:
        result = payload["result"]
        data = {"bids": result["b"], "asks": result["a"]}
        timestamp = int(result["ts"])
    levels = {}
    for side in ("bids", "asks"):
        levels[side] = [(D(row[0]), D(row[1])) for row in data[side]]
        if not levels[side] or any(not p.is_finite() or not q.is_finite() or p <= 0 or q <= 0
                                   for p, q in levels[side]):
            raise ValueError(f"Invalid {side} for {contract.symbol}")
    levels["bids"].sort(reverse=True)
    levels["asks"].sort()
    if levels["bids"][0][0] >= levels["asks"][0][0]:
        raise ValueError(f"Crossed order book for {contract.symbol}")
    return {"contract": asdict(contract), "exchange_ts_ms": timestamp,
            "local_quote_ts_ms": timestamp - clock["offset_ms"],
            "received_ms": sample["received_ms"], "rtt_ms": sample["rtt_ms"],
            "bids": levels["bids"], "asks": levels["asks"]}


def inverse_price(levels, usd_per_contract, notional_usd):
    """Effective USD/base-asset for equal USD face value, using inverse-contract harmonic VWAP."""
    remaining, base_amount = notional_usd, D(0)
    for price, contracts in levels:
        taken = min(remaining, contracts * usd_per_contract)
        base_amount += taken / price
        remaining -= taken
        if remaining == 0:
            return notional_usd / base_amount
    return None  # Requested notional exceeds collected depth.


def analyze(left_book, right_book, now_ms, notional_usd, max_age_ms, max_skew_ms):
    reasons = []
    for book in (left_book, right_book):
        age = now_ms - book["local_quote_ts_ms"]
        if age > max_age_ms or age < -max_skew_ms:
            reasons.append(f"{book['contract']['exchange']}_stale_or_future_quote")
        if book["contract"]["expiry_ms"] <= now_ms:
            reasons.append("expired_contract")
    skew = abs(left_book["local_quote_ts_ms"] - right_book["local_quote_ts_ms"])
    if skew > max_skew_ms:
        reasons.append("quote_timestamp_skew")
    if left_book["contract"]["expiry_ms"] != right_book["contract"]["expiry_ms"]:
        raise ValueError("Cannot compare different expiries")
    for field in ("base_asset", "quote_asset", "settle_asset"):
        if left_book["contract"][field] != right_book["contract"][field]:
            raise ValueError(f"Cannot compare different {field}")
    for book in (left_book, right_book):
        step = book["contract"].get("quantity_step_usd") or book["contract"]["usd_per_contract"]
        if notional_usd % D(step) != 0:
            raise ValueError("USD notional must be a multiple of both contract face values")
    mids = [(book["bids"][0][0] + book["asks"][0][0]) / 2 for book in (left_book, right_book)]
    directions = {}
    for buy, sell in ((left_book, right_book), (right_book, left_book)):
        ask = inverse_price(buy["asks"], D(buy["contract"]["usd_per_contract"]), notional_usd)
        bid = inverse_price(sell["bids"], D(sell["contract"]["usd_per_contract"]), notional_usd)
        name = f"buy_{buy['contract']['exchange']}_sell_{sell['contract']['exchange']}"
        directions[name] = {"buy_price": ask, "sell_price": bid,
                            "gross_spread_bps": (bid / ask - 1) * 10000 if ask and bid else None,
                            "sufficient_depth": ask is not None and bid is not None}
    left, right = left_book["contract"]["exchange"], right_book["contract"]["exchange"]
    return {"type": "spread", "exchanges": [left, right], "observed_ms": now_ms, "expiry_ms": left_book["contract"]["expiry_ms"],
            "base_asset": left_book["contract"]["base_asset"], "notional_usd": notional_usd, "quality_ok": not reasons, "quality_reasons": reasons,
            "quote_skew_ms": skew, "mid_left_minus_right_bps": (mids[0] / mids[1] - 1) * 10000,
            "directions": directions, "books": {left: left_book, right: right_book}}


def write_record(stream, record):
    record = {"schema_version": 2, **record}
    stream.write(json.dumps(record, default=str) + "\n")
    stream.flush()


def collect(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a") as stream, concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        pairs, clocks, refresh_at = [], {}, 0
        successful = 0
        cycles = 1 if getattr(args, "list_only", False) else args.samples
        for cycle in range(cycles) if cycles else iter(int, 1):
            started = time.monotonic()
            try:
                if started >= refresh_at:
                    pairs, metadata = discover(getattr(args, "exchanges", None))
                    if getattr(args, "list_only", False):
                        write_record(stream, {"type": "discovery", "observed_ms": time.time() * 1000,
                                              "pairs": [[asdict(c) for c in p.values()] for p in pairs],
                                              "metadata": metadata})
                        for group in pairs:
                            c = next(iter(group.values()))
                            expiry = datetime.fromtimestamp(c.expiry_ms / 1000, timezone.utc).isoformat()
                            print(c.base_asset, expiry, "matched" if len(group) >= 2 else "unmatched",
                                  "; ".join(f"{ex}:{item.symbol} ({item.period})" for ex, item in group.items()))
                        return 0 if pairs else 1
                    active_exchanges = {ex for group in pairs for ex in group}
                    clock_jobs = {ex: pool.submit(clock_sample, ex) for ex in active_exchanges}
                    clocks = {}
                    for ex, job in clock_jobs.items():
                        try:
                            clocks[ex] = job.result()
                        except ERRORS as error:
                            metadata.setdefault(ex, {})["clock_error"] = str(error)
                            logging.warning("%s clock sync failed: %s", ex, error)
                    write_record(stream, {"type": "discovery", "observed_ms": time.time() * 1000,
                                          "pairs": [[asdict(c) for c in p.values()] for p in pairs],
                                          "metadata": metadata, "clocks": clocks})
                    refresh_at = started + 300
                for pair in pairs:
                    contract = next(iter(pair.values()))
                    try:
                        jobs = {ex: pool.submit(fetch_book, c) for ex, c in pair.items() if ex in clocks}
                        samples, books = {}, {}
                        for ex, job in jobs.items():
                            try:
                                samples[ex] = job.result()
                                books[ex] = normalize_book(pair[ex], samples[ex], clocks[ex])
                            except ERRORS as error:
                                logging.warning("%s %s book failed: %s", ex, pair[ex].symbol, error)
                                write_record(stream, {"type": "error", "exchange": ex,
                                                      "symbol": pair[ex].symbol, "observed_ms": time.time() * 1000,
                                                      "error": str(error)})
                        if samples:
                            write_record(stream, {"type": "raw_books", "observed_ms": time.time() * 1000,
                                                  "base_asset": contract.base_asset,
                                                  "expiry_ms": contract.expiry_ms, "samples": samples})
                        if not books:
                            time.sleep(0.1)
                            continue
                        if len(books) == 1:
                            write_record(stream, {"type": "unmatched_book", "observed_ms": time.time() * 1000,
                                                  "base_asset": contract.base_asset, "expiry_ms": contract.expiry_ms,
                                                  "reason": "No comparable book available on another venue",
                                                  "books": books})
                        else:
                            for left, right in combinations(sorted(books), 2):
                                try:
                                    record = analyze(books[left], books[right], time.time() * 1000,
                                                     args.notional_usd, args.max_age_ms, args.max_skew_ms)
                                    write_record(stream, record)
                                    logging.info("asset=%s expiry=%s pair=%s/%s quality=%s spreads=%s",
                                                 contract.base_asset, record["expiry_ms"], left, right,
                                                 record["quality_ok"],
                                                 {k: str(v["gross_spread_bps"]) for k, v in record["directions"].items()})
                                except ERRORS as error:
                                    write_record(stream, {"type": "error", "exchanges": [left, right],
                                                          "observed_ms": time.time() * 1000, "error": str(error)})
                        successful += 1
                    except (OSError, ValueError, KeyError, IndexError, ArithmeticError) as error:
                        # A bad market must not prevent other assets/expiries from being collected.
                        logging.error("%s: %s", contract.symbol, error)
                        write_record(stream, {"type": "error", "observed_ms": time.time() * 1000,
                                              "base_asset": contract.base_asset, "expiry_ms": contract.expiry_ms,
                                              "error": str(error)})
                    time.sleep(0.1)  # Bound public API bursts as the number of contracts grows.
            except (OSError, ValueError, KeyError, IndexError, ArithmeticError) as error:
                logging.error("Collection failed: %s", error)
                write_record(stream, {"type": "error", "observed_ms": time.time() * 1000, "error": str(error)})
                refresh_at = 0
            if not cycles or cycle + 1 < cycles:
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
    return 0 if successful else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchanges", nargs="+", choices=sorted(BASE_URLS), default=sorted(BASE_URLS),
                        help="Public venues to collect (default: all four)")
    parser.add_argument("--output", type=Path, default=Path("logs/delivery_spread.jsonl"))
    parser.add_argument("--interval", type=float, default=5, help="REST polling interval, minimum 2 seconds")
    parser.add_argument("--list-only", action="store_true", help="Print contract inventory with UTC expiries, then exit")
    parser.add_argument("--samples", type=int, default=0, help="Polling cycles; 0 runs until interrupted")
    parser.add_argument("--notional-usd", type=D, default=D("1000"), help="Equal USD contract face value per leg")
    parser.add_argument("--max-age-ms", type=float, default=5000)
    parser.add_argument("--max-skew-ms", type=float, default=1000)
    args = parser.parse_args()
    if (args.interval < 2 or args.samples < 0 or not args.notional_usd.is_finite()
            or args.notional_usd <= 0 or args.max_age_ms <= 0 or args.max_skew_ms <= 0):
        parser.error("Use interval >= 2, samples >= 0, positive finite notional and quality limits")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return collect(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
