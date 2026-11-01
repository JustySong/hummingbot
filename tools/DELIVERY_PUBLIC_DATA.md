# 交割合约公共数据接口

补充盘口采集器之外的只读数据，不含账户、余额、持仓、下单、撤单或杠杆设置。复用同样的币本位反向 USD 合约发现逻辑，Deribit 范围为 BTC/ETH；其他交易所可通过 `--assets` 选择已挂牌的币种。

这是独立的 **REST** 采集命令，不会自动增加每秒盘口采集器的网络请求。现有盘口 REST/WS 命令和最多 5 档存储不变。

## 支持范围

| 数据集 | OKX | Binance COIN-M | Deribit | Bybit inverse |
| --- | --- | --- | --- | --- |
| `ticker` 标记价/指数价，部分交易所附最新价与买卖价 | 支持 | 支持 | 支持 | 支持 |
| `open_interest` 当前持仓量及美元面值 | 支持 | 支持 | 支持 | 支持 |
| `trades` 最近成交与主动方向 | 支持 | 支持 | 支持 | 支持 |
| `candles` 最近一分钟 K 线 | 支持 | 支持 | 支持 | 支持 |
| `settlements` 标的最近交割价历史 | 支持 | **暂不支持，未确认官方 COIN-M 公共历史交割价接口** | 支持 | 支持 |

“支持”表示已有请求和解析实现。当前云机器只能对 OKX、Deribit 实测；Binance/Bybit 受地区限制，经过离线协议测试，未完成实时验证。Binance 的交割历史请求会明确记录 `status=unsupported`，不会拿标记价、预计结算价代替真实交割价。

默认只抓 `ticker open_interest`；其他数据需显式指定。每个接口请求最近 `--limit` 条/个时间组，范围 1..100，不是完整历史回溯。K 线目前固定一分钟，不提供任意周期或起止日期历史回填；Bybit 返回的下一页游标暂不自动用于交割历史回溯（合约发现的分页仍受支持）。

## 用法

```bash
# 默认单轮，所有交易所、BTC/ETH、ticker + open_interest
python3 tools/collect_delivery_public.py --sqlite logs/delivery_spread_v2.sqlite

# 当前可访问组合，采集五类数据；通过符号缩小采集范围
# 示例符号仅代表验证时挂牌情况，请先用 --list-only 查看实时清单。
python3 tools/collect_delivery_public.py --exchanges okx deribit \
  --symbols BTC-USD-261127 BTC-27NOV26 ETH-USD-261127 ETH-27NOV26 \
  --datasets ticker open_interest trades candles settlements --limit 20 \
  --sqlite logs/delivery_spread_v2.sqlite

# 按分钟采集 60 轮（处理时间之外每轮间隔 60 秒）
python3 tools/collect_delivery_public.py --exchanges okx deribit \
  --datasets ticker open_interest --samples 60 --interval 60 \
  --sqlite logs/delivery_spread_v2.sqlite
```

可以与盘口采集命令并行写同一个 **schema-v2** 数据库，使用 SQLite WAL 和 busy timeout。两个命令都默认按 UTC 固定 6 小时分段：`--sqlite` 是文件名前缀，实际文件例如 `delivery_spread_v2_20261101T060000Z_6h.sqlite`。使用 `--sqlite-rotate-hours N` 修改分段时长，或 `--sqlite-rotate-hours 0` 继续单文件追加。两命令需使用相同前缀与 N 才会写入同一分段；历史 K 线/交割数据按写入时刻分文件，源时间仍保留。默认不写 JSONL，显式 `--output` 才开启。不要向旧 schema-v1 数据库追加。

各交易所发现失败、某个接口失败或未实现时独立记录错误并继续。退出码 0 表示至少一个接口成功响应（结果可能为空），1 表示全部请求失败或没有匹配合约。实际执行/返回数量见 `public_status` 记录及日志，不能仅凭退出码认定有足够数据。

请求限速为每个数据集之间至少 0.15 秒；一轮可能处理很多合约。推荐通过 `--symbols` 限定研究对象、使用分钟级间隔，避免全市场高频重复请求。

## 字段与语义

- `ticker`：`mark_price`、`index_price`，可选 `last_price/bid_price/ask_price`。OKX 分别保留标记价和指数价的时间，三个请求不是同一时刻的原子快照；其他交易所也是以返回值为准。不同交易所的指数成分不一定相同。
- `open_interest`：保留交易所原始值与 `unit`，另有 `open_interest_usd`。OKX/Binance 原始值按张数，使用面值换算；Deribit/Bybit 的反向合约按 USD。它不是账户持仓。
- `trades`：成交 ID、时间、价格、原始数量/单位、USD 面值及 `taker_side`。Binance 的买方为 maker 时主动方是 sell。
- `candles`：开盘时间、OHLC、成交量/单位、`interval_ms=60000`、`is_closed`、`closure_source`。OKX 使用交易所确认标记；其他适配使用本地时间和分钟结束时间估计收盘，研究时可筛选已闭合 K 线并核对时钟。
- `settlements`：**标的范围**的已到期合约历史，返回 `symbol` 是真实已交割符号，而不是当前采集入口的活跃符号；每所每标的每轮只请求一次。只保留最终交割价/时间，不保存 Deribit 响应中与目标无关的其他字段。OKX 使用 `instFamily` 并排除不同系列，Deribit 排除期权和永续。

## SQLite

新增 `delivery_public_data` 表，是 schema-v2 的兼容扩展，不改动盘口表：

- `exchange / symbol / base_asset / dataset / source_ts_ms` 用于筛选。
- `payload_json` 保存紧凑的标准化数据，价格与数量尽量保留源精度。
- 按交易所、数据集、符号及源 ID 去重。成交按成交 ID，其他数据按源时间。
- 同一个未收盘 K 线更新时覆盖该行，最终值保留在原行，不重复堆积；数据不变时不重写。
- `delivery_events` 中保存 `public_status`，记录每次成功请求的行数（包括空响应）；错误单独保存。

```sql
SELECT exchange, dataset, base_asset, COUNT(*) AS rows
FROM delivery_public_data GROUP BY exchange,dataset,base_asset;

SELECT exchange, symbol, datetime(source_ts_ms/1000,'unixepoch') AS time_utc,
       json_extract(payload_json,'$.mark_price') AS mark_price,
       json_extract(payload_json,'$.index_price') AS index_price
FROM delivery_public_data WHERE dataset='ticker' ORDER BY source_ts_ms DESC LIMIT 20;

SELECT exchange,symbol,source_ts_ms,
       json_extract(payload_json,'$.delivery_price') AS delivery_price
FROM delivery_public_data WHERE dataset='settlements' ORDER BY source_ts_ms DESC;
```

## 接口映射

| 数据集 | OKX | Binance COIN-M | Deribit | Bybit |
| --- | --- | --- | --- | --- |
| ticker | market/ticker + public/mark-price + market/index-tickers | dapi/v1/premiumIndex | public/ticker | v5/market/tickers |
| open_interest | public/open-interest | dapi/v1/openInterest | public/ticker | v5/market/tickers |
| trades | market/trades | dapi/v1/trades | public/get_last_trades_by_instrument | v5/market/recent-trade |
| candles | market/candles | dapi/v1/klines | public/get_tradingview_chart_data | v5/market/kline |
| settlements | public/delivery-exercise-history | 不调用未确认接口 | public/get_last_settlements_by_currency | v5/market/delivery-price |

OKX 路径前缀为 `/api/v5/`，Deribit 为 `/api/v2/`。所有请求均走原有公共主机，沿用代理与 TLS 验证。

验证：`python3 -m pytest -q test/tools`。OKX、Deribit 已完成 BTC/ETH 五类接口和 SQLite 落盘验证；成交和 K 线是限量样本，非全历史验证。
