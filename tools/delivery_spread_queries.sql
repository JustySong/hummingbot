-- All timestamps are UTC epoch milliseconds; numerical projections use SQLite REAL.
-- Schema v2: original price/quantity strings are in delivery_books bids_json/asks_json.
-- delivery_events contains only discovery summaries, errors and connection statuses.

-- Collection totals by event type.
SELECT type, COUNT(*) AS observations FROM delivery_events GROUP BY type;

-- Valid, depth-sufficient gross spreads grouped by asset, maturity and direction.
SELECT base_asset, datetime(expiry_ms / 1000, 'unixepoch') AS expiry_utc,
       buy_exchange, sell_exchange, COUNT(*) AS samples,
       ROUND(AVG(gross_spread_bps), 4) AS avg_bps,
       ROUND(MAX(gross_spread_bps), 4) AS max_bps,
       SUM(gross_spread_bps > 0) AS positive_samples
FROM delivery_spreads
WHERE quality_ok = 1 AND sufficient_depth = 1
GROUP BY base_asset, expiry_ms, buy_exchange, sell_exchange
ORDER BY base_asset, expiry_ms, buy_exchange;

-- Largest valid gross spreads. Positive values have not had fees or other costs deducted.
SELECT datetime(observed_ms / 1000, 'unixepoch') AS observed_utc,
       base_asset, datetime(expiry_ms / 1000, 'unixepoch') AS expiry_utc,
       buy_exchange, sell_exchange, notional_usd, gross_spread_bps
FROM delivery_spreads
WHERE quality_ok = 1 AND sufficient_depth = 1
ORDER BY gross_spread_bps DESC LIMIT 20;

-- Invalid observations are retained for diagnosing quote timing issues.
SELECT quality_reasons_json, COUNT(*) AS direction_samples FROM delivery_spreads
WHERE quality_ok = 0 GROUP BY quality_reasons_json;

-- Linked buy and sell snapshots (one stored book may be reused by many observations).
SELECT s.observed_ms, s.base_asset, s.buy_exchange, s.sell_exchange, s.gross_spread_bps,
       b.best_bid AS buy_venue_bid, b.best_ask AS buy_venue_ask,
       a.best_bid AS sell_venue_bid, a.best_ask AS sell_venue_ask
FROM delivery_spreads s
JOIN delivery_books b ON b.book_id=s.buy_book_id
JOIN delivery_books a ON a.book_id=s.sell_book_id
WHERE s.quality_ok=1 AND s.sufficient_depth=1 LIMIT 20;
