import argparse
from dataclasses import replace
import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from tools import collect_delivery_spread as collector


EXPIRY = 1900000000000


def instruments():
    return ({"data": [{"instType": "FUTURES", "uly": "BTC-USD", "ctType": "inverse",
                       "settleCcy": "BTC", "ctValCcy": "USD", "state": "live", "alias": "quarter",
                       "instId": "BTC-USD-TEST", "expTime": str(EXPIRY), "ctVal": "100"}]},
            {"symbols": [{"pair": "BTCUSD", "marginAsset": "BTC", "contractStatus": "TRADING",
                          "contractType": "CURRENT_QUARTER", "symbol": "BTCUSD_TEST",
                          "deliveryDate": EXPIRY, "contractSize": 100}]})


def sample(exchange, timestamp=10000):
    book = {"bids": [["99900", "10"]], "asks": [["100000", "10"]]}
    payload = {"data": [{**book, "ts": str(timestamp)}]} if exchange == "okx" else {**book, "E": timestamp}
    return {"payload": payload, "sent_ms": timestamp, "received_ms": timestamp + 10, "rtt_ms": 10}


def books():
    pair = collector.parse_contracts(*instruments(), 0)[0]
    return [collector.normalize_book(pair[ex], sample(ex), {"offset_ms": 0}) for ex in ("okx", "binance")]


def test_match_full_expiry_and_exclude_perpetual():
    okx, binance = instruments()
    assert len(collector.parse_contracts(okx, binance, 0)) == 1
    binance["symbols"][0]["deliveryDate"] += 1000
    assert collector.parse_contracts(okx, binance, 0) == []
    binance["symbols"][0]["deliveryDate"] = EXPIRY
    binance["symbols"][0]["contractType"] = "PERPETUAL"
    assert collector.parse_contracts(okx, binance, 0) == []


def test_expired_and_linear_contracts_excluded():
    assert collector.parse_contracts(*instruments(), EXPIRY) == []
    okx, binance = instruments()
    okx["data"][0]["ctType"] = "linear"
    assert collector.parse_contracts(okx, binance, 0) == []


def test_inverse_depth_uses_face_value_and_harmonic_price():
    levels = [(Decimal(100), Decimal(1)), (Decimal(200), Decimal(1))]
    assert collector.inverse_price(levels, Decimal(100), Decimal(200)) == Decimal(200) / Decimal("1.5")
    assert collector.inverse_price(levels, Decimal(100), Decimal(300)) is None


def test_two_directions_and_depth():
    okx, binance = books()
    binance["bids"] = [(Decimal(100500), Decimal(10))]
    binance["asks"] = [(Decimal(100600), Decimal(10))]
    result = collector.analyze(okx, binance, 10050, Decimal(1000), 1000, 500)
    assert result["quality_ok"]
    assert result["directions"]["buy_okx_sell_binance"]["gross_spread_bps"] == Decimal(50)
    assert result["directions"]["buy_binance_sell_okx"]["gross_spread_bps"] < 0
    result = collector.analyze(okx, binance, 10050, Decimal(1100), 1000, 500)
    assert result["directions"]["buy_okx_sell_binance"]["gross_spread_bps"] is None


def test_stale_skew_and_mismatched_expiry():
    okx, binance = books()
    binance["local_quote_ts_ms"] = 8000
    result = collector.analyze(okx, binance, 10050, Decimal(100), 1000, 500)
    assert not result["quality_ok"]
    assert "quote_timestamp_skew" in result["quality_reasons"]
    binance["contract"]["expiry_ms"] += 1
    with pytest.raises(ValueError, match="different expiries"):
        collector.analyze(okx, binance, 10050, Decimal(100), 1000, 500)


def test_contract_lot_and_crossed_book_rejected():
    with pytest.raises(ValueError, match="multiple"):
        collector.analyze(*books(), 10050, Decimal(150), 1000, 500)
    pair = collector.parse_contracts(*instruments(), 0)[0]
    bad = sample("binance")
    bad["payload"]["bids"][0][0] = "100001"
    with pytest.raises(ValueError, match="Crossed"):
        collector.normalize_book(pair["binance"], bad, {"offset_ms": 0})


def test_collection_records_raw_and_spread_and_recovers(tmp_path):
    pair = collector.parse_contracts(*instruments(), 0)[0]
    args = argparse.Namespace(output=tmp_path / "records.jsonl", samples=2, interval=2,
                              notional_usd=Decimal(1000), max_age_ms=5000, max_skew_ms=1000)
    with patch.object(collector, "discover", side_effect=[ValueError("temporary failure"), ([pair], {})]), \
            patch.object(collector, "clock_sample", return_value={"offset_ms": 0}), \
            patch.object(collector, "fetch_book", side_effect=lambda c: sample(c.exchange)), \
            patch.object(collector.time, "time", return_value=10.05), \
            patch.object(collector.time, "sleep"):
        assert collector.collect(args) == 0
    records = [json.loads(line) for line in args.output.read_text().splitlines()]
    assert [r["type"] for r in records] == ["error", "discovery", "raw_books", "spread"]
    assert records[-1]["quality_ok"]


def test_all_failed_returns_nonzero(tmp_path):
    args = argparse.Namespace(output=tmp_path / "errors.jsonl", samples=1)
    with patch.object(collector, "discover", side_effect=OSError("blocked")):
        assert collector.collect(args) == 1


def test_all_assets_periods_and_unmatched_inventory():
    import copy
    okx, binance = instruments()
    eth_okx, eth_binance = copy.deepcopy(okx["data"][0]), copy.deepcopy(binance["symbols"][0])
    eth_okx.update(uly="ETH-USD", settleCcy="ETH", instId="ETH-USD-TEST", ctVal="10", alias="month")
    eth_binance.update(pair="ETHUSD", marginAsset="ETH", symbol="ETHUSD_TEST", contractSize=10,
                       contractType="CURRENT_MONTH")
    okx["data"].append(eth_okx)
    binance["symbols"].append(eth_binance)
    weekly = copy.deepcopy(okx["data"][0])
    weekly.update(instId="BTC-USD-WEEK", expTime=str(EXPIRY - 604800000), alias="this_week")
    okx["data"].append(weekly)
    groups = collector.contract_groups(okx, binance, 0)
    assert len(groups) == 3
    assert len(collector.parse_contracts(okx, binance, 0)) == 2
    assert {c.base_asset for g in groups for c in g.values()} == {"BTC", "ETH"}
    assert any(len(g) == 1 and g["okx"].period == "this_week" for g in groups)
    # Same expiry must never cause different underlyings to be paired.
    binance["symbols"] = [eth_binance]
    okx["data"] = [okx["data"][0]]
    assert collector.parse_contracts(okx, binance, 0) == []


def test_analysis_rejects_mismatched_asset():
    okx, binance = books()
    binance["contract"]["base_asset"] = "ETH"
    with pytest.raises(ValueError, match="different base_asset"):
        collector.analyze(okx, binance, 10050, Decimal(100), 1000, 500)


def test_unmatched_collection_and_list_only(tmp_path, capsys):
    pair = collector.parse_contracts(*instruments(), 0)[0]
    single = {"okx": pair["okx"]}
    args = argparse.Namespace(output=tmp_path / "single.jsonl", samples=1, list_only=False)
    with patch.object(collector, "discover", return_value=([single], {})), \
            patch.object(collector, "clock_sample", return_value={"offset_ms": 0}), \
            patch.object(collector, "fetch_book", side_effect=lambda c: sample(c.exchange)):
        assert collector.collect(args) == 0
    records = [json.loads(line) for line in args.output.read_text().splitlines()]
    assert [r["type"] for r in records] == ["discovery", "raw_books", "unmatched_book"]
    args.list_only = True
    with patch.object(collector, "discover", return_value=([single], {})), \
            patch.object(collector, "fetch_book") as fetch:
        assert collector.collect(args) == 0
        fetch.assert_not_called()
    assert "BTC" in capsys.readouterr().out


def test_one_bad_market_does_not_block_other_markets(tmp_path):
    pair = collector.parse_contracts(*instruments(), 0)[0]
    args = argparse.Namespace(output=tmp_path / "partial.jsonl", samples=1,
                              notional_usd=Decimal(1000), max_age_ms=5000, max_skew_ms=1000)

    def fetch(c):
        if c.symbol == "BROKEN":
            raise ValueError("bad market")
        return sample(c.exchange)
    broken = {"okx": replace(pair["okx"], symbol="BROKEN")}
    with patch.object(collector, "discover", return_value=([broken, pair], {})), \
            patch.object(collector, "clock_sample", return_value={"offset_ms": 0}), \
            patch.object(collector, "fetch_book", side_effect=fetch), \
            patch.object(collector.time, "sleep"):
        assert collector.collect(args) == 0
    records = [json.loads(line) for line in args.output.read_text().splitlines()]
    assert [r["type"] for r in records] == ["discovery", "error", "raw_books", "spread"]


def deribit_instruments():
    return {"result": [{"kind": "future", "is_active": True, "instrument_type": "reversed",
                        "quote_currency": "USD", "base_currency": "BTC", "settlement_currency": "BTC",
                        "instrument_name": "BTC-TEST", "expiration_timestamp": EXPIRY,
                        "settlement_period": "month", "contract_size": 10, "min_trade_amount": 10}]}


def bybit_instruments():
    return {"retCode": 0, "result": {"list": [{"baseCoin": "BTC", "quoteCoin": "USD", "settleCoin": "BTC",
                                               "symbol": "BTCUSDTEST", "status": "Trading",
                                               "contractType": "InverseFutures", "deliveryTime": str(EXPIRY),
                                               "lotSizeFilter": {"qtyStep": "1"}}], "nextPageCursor": ""}}


def venue_sample(ex):
    book = {"bids": [["99900", "1000"]], "asks": [["100000", "1000"]]}
    payload = ({"result": {**book, "timestamp": 10000, "state": "open"}} if ex == "deribit" else
               {"result": {"b": book["bids"], "a": book["asks"], "ts": 10000}})
    return {"payload": payload, "sent_ms": 10000, "received_ms": 10010, "rtt_ms": 10}


def test_deribit_units_not_multiplied_by_contract_size():
    contract = collector.parse_deribit(deribit_instruments(), 0)[0]
    book = collector.normalize_book(contract, venue_sample("deribit"), {"offset_ms": 0})
    assert contract.quantity_unit == "USD"
    assert contract.quantity_step_usd == "10"
    assert collector.inverse_price(book["asks"], Decimal(contract.usd_per_contract), Decimal(1000)) == 100000
    assert collector.inverse_price(book["asks"], Decimal(contract.usd_per_contract), Decimal(1010)) is None
    data = deribit_instruments()
    data["result"][0]["instrument_name"] = "BTC-PERPETUAL"
    assert not collector.parse_deribit(data, 0)
    data["result"][0]["instrument_name"] = "BTC-TEST"
    data["result"][0]["instrument_type"] = "linear"
    assert not collector.parse_deribit(data, 0)


def test_bybit_units_and_perpetual_exclusion():
    data = bybit_instruments()
    contract = collector.parse_bybit(data, 0)[0]
    book = collector.normalize_book(contract, venue_sample("bybit"), {"offset_ms": 0})
    assert book["bids"][0][1] == 1000
    assert contract.usd_per_contract == "1"
    assert not collector.parse_bybit(data, EXPIRY)
    data["result"]["list"][0]["contractType"] = "InversePerpetual"
    assert not collector.parse_bybit(data, 0)


def test_bybit_pagination():
    first, second = bybit_instruments(), bybit_instruments()
    first["result"]["nextPageCursor"] = "next-page"
    second["result"]["list"][0].update(symbol="BTCUSD_NEXT", deliveryTime=str(EXPIRY + 100000))
    with patch.object(collector, "get_json", side_effect=[{"payload": first}, {"payload": second}]) as get:
        contracts, samples = collector.discover_exchange("bybit")
    assert len(contracts) == len(samples) == 2
    assert get.call_args.args[2]["cursor"] == "next-page"


def test_discovery_isolates_blocked_venue():
    contract = collector.parse_deribit(deribit_instruments(), 0)[0]

    def discover(ex):
        if ex == "bybit":
            raise OSError("403 geographic restriction")
        return [contract], []
    with patch.object(collector, "discover_exchange", side_effect=discover):
        groups, metadata = collector.discover(["deribit", "bybit"])
    assert len(groups) == 1
    assert "error" in metadata["bybit"]
    assert metadata["deribit"]["contract_count"] == 1


def test_three_venues_produce_all_pairwise_spreads(tmp_path):
    pair = collector.parse_contracts(*instruments(), 0)[0]
    pair["deribit"] = collector.parse_deribit(deribit_instruments(), 0)[0]
    args = argparse.Namespace(output=tmp_path / "triple.jsonl", samples=1, notional_usd=Decimal(1000),
                              max_age_ms=5000, max_skew_ms=1000)

    def fetch(c):
        return venue_sample(c.exchange) if c.exchange == "deribit" else sample(c.exchange)
    with patch.object(collector, "discover", return_value=([pair], {})), \
            patch.object(collector, "clock_sample", return_value={"offset_ms": 0}), \
            patch.object(collector, "fetch_book", side_effect=fetch), \
            patch.object(collector.time, "sleep"):
        assert collector.collect(args) == 0
    spreads = [r for r in map(json.loads, args.output.read_text().splitlines()) if r["type"] == "spread"]
    assert len(spreads) == 3
    assert {tuple(r["exchanges"]) for r in spreads} == {("binance", "deribit"), ("binance", "okx"), ("deribit", "okx")}
    assert all(set(r["books"]) == set(r["exchanges"]) for r in spreads)


def test_blocked_book_does_not_lose_available_pair(tmp_path):
    pair = collector.parse_contracts(*instruments(), 0)[0]
    pair["deribit"] = collector.parse_deribit(deribit_instruments(), 0)[0]
    args = argparse.Namespace(output=tmp_path / "blocked.jsonl", samples=1, notional_usd=Decimal(1000),
                              max_age_ms=5000, max_skew_ms=1000)

    def fetch(c):
        if c.exchange == "binance":
            raise OSError("451")
        return venue_sample(c.exchange) if c.exchange == "deribit" else sample(c.exchange)
    with patch.object(collector, "discover", return_value=([pair], {})), \
            patch.object(collector, "clock_sample", return_value={"offset_ms": 0}), \
            patch.object(collector, "fetch_book", side_effect=fetch), \
            patch.object(collector.time, "sleep"):
        assert collector.collect(args) == 0
    records = list(map(json.loads, args.output.read_text().splitlines()))
    assert any(r["type"] == "error" and r["exchange"] == "binance" for r in records)
    assert any(r["type"] == "spread" and r["exchanges"] == ["deribit", "okx"] for r in records)
