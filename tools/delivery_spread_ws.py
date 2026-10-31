"""Optional websocket transport for the read-only delivery spread collector."""
import asyncio
import json
import logging
import time
from dataclasses import asdict
from itertools import combinations

try:
    from . import collect_delivery_spread as core
except ImportError:
    import collect_delivery_spread as core

URLS = {
    "okx": "wss://ws.okx.com/ws/v5/public",
    "binance": "wss://dstream.binance.com/ws",
    "deribit": "wss://www.deribit.com/ws/api/v2",
    "bybit": "wss://stream.bybit.com/v5/public/inverse",
}


def subscription(exchange, contracts, request_id):
    if exchange == "okx":
        return {"op": "subscribe", "args": [{"channel": "books5", "instId": c.symbol} for c in contracts]}
    if exchange == "binance":
        return {"method": "SUBSCRIBE", "params": [f"{c.symbol.lower()}@depth20@100ms" for c in contracts],
                "id": request_id}
    if exchange == "deribit":
        return {"jsonrpc": "2.0", "id": request_id, "method": "public/subscribe",
                "params": {"channels": [f"book.{c.symbol}.none.20.100ms" for c in contracts]}}
    return {"op": "subscribe", "args": [f"orderbook.1.{c.symbol}" for c in contracts]}


def decode_book(exchange, message):
    """Return (symbol, REST-shaped payload) from full snapshots, or None for acknowledgments."""
    if message.get("event") == "notice":
        logging.info("%s WS notice: %s", exchange, message)
        return None
    if (message.get("error") or message.get("event") == "error"
            or message.get("success") is False or message.get("code") not in (None, 0, "0")):
        raise ValueError(f"{exchange} websocket error: {message}")
    if exchange == "okx" and message.get("arg", {}).get("channel") == "books5" and message.get("data"):
        return message["arg"]["instId"], {"data": message["data"]}
    if exchange == "binance":
        data = message.get("data", message)
        if data.get("e") == "depthUpdate":
            return data["s"], {"bids": data["b"], "asks": data["a"], "E": data["E"]}
    if exchange == "deribit" and message.get("method") == "subscription":
        params = message["params"]
        if params["channel"].startswith("book."):
            data = params["data"]
            # Grouped-depth channels supply complete [price, amount] snapshots, never incremental actions.
            if any(len(row) != 2 for side in ("bids", "asks") for row in data[side]):
                raise ValueError("Unexpected Deribit incremental book on snapshot channel")
            return data["instrument_name"], {"result": {**data, "state": "open"}}
    if exchange == "bybit" and message.get("topic", "").startswith("orderbook.1."):
        if message.get("type") != "snapshot":
            raise ValueError("Unexpected Bybit delta on L1 snapshot channel")
        data = message["data"]
        return data["s"], {"result": {"b": data["b"], "a": data["a"], "ts": message["ts"]}}
    return None


def clear_exchange(cache, exchange):
    for key in list(cache):
        if key[0] == exchange:
            del cache[key]


async def heartbeat(ws, exchange):
    while True:
        await asyncio.sleep(15)
        if exchange == "okx":
            await ws.send("ping")
        elif exchange == "bybit":
            await ws.send(json.dumps({"op": "ping"}))
        elif exchange == "deribit":
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 99999, "method": "public/test"}))
        else:
            await ws.ping()


async def stream_exchange(exchange, contracts, clock, cache, output, connect, record_raw=False):
    mapping = {c.symbol: c for c in contracts}
    retry = 1
    while True:
        clear_exchange(cache, exchange)
        heart = None
        try:
            async with connect(URLS[exchange], open_timeout=20, close_timeout=5,
                               ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024) as ws:
                for index in range(0, len(contracts), 10):
                    await ws.send(json.dumps(subscription(exchange, contracts[index:index + 10], index + 1)))
                    await asyncio.sleep(0.25)
                heart = asyncio.create_task(heartbeat(ws, exchange))
                core.write_record(output, {"type": "ws_connected", "exchange": exchange,
                                           "observed_ms": time.time() * 1000, "url": URLS[exchange]})
                while True:
                    # Reconnect on silence even when the socket itself still answers protocol pings.
                    raw = await asyncio.wait_for(ws.recv(), timeout=45)
                    received = time.time() * 1000
                    if heart.done():
                        heart.result()
                    if raw == "pong":
                        continue
                    message = json.loads(raw)
                    if record_raw:
                        core.write_record(output, {"type": "raw_ws", "exchange": exchange,
                                                   "received_ms": received, "message": message})
                    decoded = decode_book(exchange, message)
                    if decoded is None:
                        continue
                    symbol, payload = decoded
                    if symbol not in mapping:
                        continue
                    sample = {"payload": payload, "sent_ms": None, "received_ms": received, "rtt_ms": None}
                    book = core.normalize_book(mapping[symbol], sample, clock)
                    book["transport"] = "ws"
                    key = (exchange, symbol)
                    if key not in cache or book["exchange_ts_ms"] >= cache[key]["exchange_ts_ms"]:
                        cache[key] = book
                    retry = 1
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.warning("%s WS disconnected: %s", exchange, error)
            core.write_record(output, {"type": "error", "transport": "ws", "exchange": exchange,
                                       "observed_ms": time.time() * 1000, "error": str(error)})
        finally:
            clear_exchange(cache, exchange)
            if heart:
                heart.cancel()
                await asyncio.gather(heart, return_exceptions=True)
        await asyncio.sleep(retry)
        retry = min(retry * 2, 30)


def report(groups, cache, args, output):
    count = 0
    for group in groups:
        now = time.time() * 1000
        # Receipt age prevents a silent connection from recycling a cached book indefinitely.
        books = {ex: cache[(ex, c.symbol)] for ex, c in group.items()
                 if (ex, c.symbol) in cache and now - cache[(ex, c.symbol)]["received_ms"] <= args.max_age_ms}
        if not books:
            continue
        count += 1
        core.write_record(output, {"type": "ws_books", "observed_ms": now, "books": books})
        for left, right in combinations(sorted(books), 2):
            try:
                record = core.analyze(books[left], books[right], now, args.notional_usd,
                                      args.max_age_ms, args.max_skew_ms)
                record["transport"] = "ws"
                core.write_record(output, record)
                logging.info("WS %s expiry=%s %s/%s quality=%s", record["base_asset"],
                             record["expiry_ms"], left, right, record["quality_ok"])
            except core.ERRORS as error:
                core.write_record(output, {"type": "error", "transport": "ws", "exchanges": [left, right],
                                           "observed_ms": now, "error": str(error)})
    return count


async def session(args, connect):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cache, tasks, groups = {}, [], []
    successful, cycles, refresh_at = 0, 0, 0
    with core.open_output(args) as output:
        try:
            while True:
                if time.monotonic() >= refresh_at:
                    groups, metadata = await asyncio.to_thread(core.discover, args.exchanges)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks, cache = [], {}
                    exchanges = {ex for group in groups for ex in group}
                    clock_results = await asyncio.gather(
                        *(asyncio.to_thread(core.clock_sample, ex) for ex in sorted(exchanges)),
                        return_exceptions=True)
                    clocks = {}
                    for ex, result in zip(sorted(exchanges), clock_results):
                        if isinstance(result, Exception):
                            metadata.setdefault(ex, {})["clock_error"] = str(result)
                        else:
                            clocks[ex] = result
                    core.write_record(output, {"type": "discovery", "transport": "ws",
                                               "observed_ms": time.time() * 1000, "metadata": metadata,
                                               "pairs": [[asdict(c) for c in group.values()] for group in groups],
                                               "clocks": clocks})
                    for ex, clock in clocks.items():
                        contracts = [group[ex] for group in groups if ex in group]
                        tasks.append(asyncio.create_task(stream_exchange(
                            ex, contracts, clock, cache, output, connect, args.record_ws_raw)))
                    refresh_at = time.monotonic() + 300
                await asyncio.sleep(args.interval)
                successful += report(groups, cache, args, output)
                cycles += 1
                if args.samples and cycles >= args.samples:
                    return 0 if successful else 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def collect(args):
    try:
        from websockets.asyncio.client import connect
    except ImportError as error:
        raise SystemExit('WS mode requires: python -m pip install "websockets>=15,<16"') from error
    return asyncio.run(session(args, connect))
