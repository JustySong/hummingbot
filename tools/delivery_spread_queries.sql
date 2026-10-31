-- All timestamps are UTC epoch milliseconds; numerical projections use SQLite REAL.
-- Original decimal strings and quality/error details remain in delivery_events.payload_json.

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
