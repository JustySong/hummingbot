# 多交易所交割合约盘口价差采集

Python 3.10+，只读公共行情，无需 API key，不读取账户、不下单。REST 模式使用标准库；WS 模式额外需要 `tools/requirements-ws.txt`。

## 默认精简存储（SQLite schema v2）

- 只写 SQLite，不再默认双写 JSONL。
- 每个盘口最多保留买卖各 **5 档**；价差也只使用这 5 档计算。Bybit WS 为 1 档。
- 盘口保存原始价格和数量的十进制字符串，另有最优价格、最优档美元容量的数值列。
- 同一份盘口被多个交易所配对或多个采样周期使用时，仅存一份；价差通过 `buy_book_id/sell_book_id` 关联。
- 去重包含合约、交易所时间、校正时间、接收时间及档位内容；不能只靠价格不变就删除更新后的时间信息。
- 价差每个买卖方向一行，保留目标金额、深度加权买卖价、毛价差、时间错位和深度质量信息。缺失深度写 NULL，不伪造报价。
- 合约元数据单独保存，只有变化时更新。发现记录只保留精简清单、数量、错误和时钟信息。
- 不再将完整盘口、完整发现响应、原始 WS 消息复制到 `delivery_events`。
- 保留错误和连接事件。质量不合格的价差仍然保存，分析时显式筛选。

短期同输入存储对照：旧样本 7,802,880 字节，新版存储 647,168 字节（约减少 92%），价差记录数不变。该试验仅比较存储，不是旧数据迁移工具，也没有重新计算旧样本的价差。实际体积取决于市场数、盘口更新和采样间隔；数据库仍会持续增长。

## 启动与升级

```bash
python -m pip install -r tools/requirements-ws.txt
python tools/collect_delivery_spread.py --transport ws \
  --exchanges okx deribit binance bybit --interval 1 \
  --sqlite logs/delivery_spread_v2.sqlite

# 30 轮有限验证
python tools/collect_delivery_spread.py --transport ws --exchanges okx deribit \
  --interval 1 --samples 30 --sqlite logs/delivery_spread_v2.sqlite

# 查看所有挂牌币种、UTC 到期时间及配对情况
python tools/collect_delivery_spread.py --list-only --sqlite logs/delivery_spread_v2.sqlite
```

旧版 `logs/delivery_spread.sqlite` 为 schema v1，**不要覆盖或删除**。新版默认使用 `logs/delivery_spread_v2.sqlite`；如显式传入旧数据库，程序会报出使用新路径的提示，在修改旧库前停止。旧库可以继续用旧查询分析，不能直接套用新版关联字段。

同一新版数据库可追加多个运行批次，重启不清空历史数据。旧入口 `tools/collect_btc_delivery_spread.py` 仍可使用。`--exchanges` 默认四所，某所受地区限制不会阻断其他所。

`--interval`：REST 最小 2 秒，WS 输出间隔最小 0.1 秒。WS 持续接收行情，不按 interval 轮询。`--samples` 是整轮输出次数，0 持续运行，Ctrl-C 正常停止；WS 初始化阶段可能存在空轮次。

退出码 0 表示采集到至少一个行情分组，并不保证产生质量合格的跨所价差；1 表示没有有效行情；130 表示人工停止。

## SQLite UTC 固定分段

新增参数 `--sqlite-rotate-hours N`，默认 **6**，作用于 REST 和 WS；公共数据命令也支持相同参数。轮转按 UTC 固定窗口计算，**不从进程启动时间计时**。

```bash
python3 tools/collect_delivery_spread.py --transport ws \
  --exchanges okx deribit binance bybit --interval 1 \
  --sqlite logs/delivery_spread_v2.sqlite --sqlite-rotate-hours 6
```

`--sqlite` 现在默认作为文件名前缀，生成：

```text
logs/delivery_spread_v2_20261101T000000Z_6h.sqlite
logs/delivery_spread_v2_20261101T060000Z_6h.sqlite
logs/delivery_spread_v2_20261101T120000Z_6h.sqlite
logs/delivery_spread_v2_20261101T180000Z_6h.sqlite
```

例如 UTC 04:35 启动时，先写 `...T000000Z_6h.sqlite`，UTC 06:00 后第一次写入切到 `...T060000Z_6h.sqlite`。同一天 UTC 07:20 重启则追加 `...T060000Z_6h.sqlite`，不覆盖已有数据。文件名中的时间始终为 UTC，和机器系统时区无关。

N 是非负整数。默认 6 小时窗口为 UTC 00、06、12、18 时。通用公式是 `floor(当前 Unix UTC 秒 / (N*3600)) * (N*3600)`；不整除 24 的 N（例如 5）也从 Unix UTC epoch 固定对齐，窗口会跨午夜，不会每天重置或在启动时重置。文件名包含 N，避免不同分段长度混用同一个文件。

轮转按照**写入时刻**选择文件，而不是交易所行情时间、历史 K 线或历史交割时间。历史记录的真实时间仍保存在原字段。一个事件及其双向价差/盘口投影始终完整写入同一数据库，不会跨文件产生外键引用。新文件自带 schema、运行记录和本次所需合约/盘口；切换不重连 WS，也不清空行情缓存。

程序会在新文件打开成功后关闭旧连接，正常结束旧运行记录。没有新写入时不会仅为时间到点创建空分段。轮转只分文件，不删除历史数据库，也不对 JSONL 调试文件进行轮转。

更新后，已有无时间后缀的 `logs/delivery_spread_v2.sqlite` 保持不变。若要继续单文件追加，明确指定：

```bash
python3 tools/collect_delivery_spread.py --transport ws --exchanges okx deribit \
  --sqlite logs/delivery_spread_v2.sqlite --sqlite-rotate-hours 0
```

盘口和公共数据命令使用相同前缀、相同 N 时会写入同一 UTC 分段。查询/备份时请选择实际带时间后缀的文件，不要误把旧的无后缀数据库当作正在采集的文件。

## 交易所与数量单位

| 交易所 | 发现范围 | 数量换算 | WS 频道 |
| --- | --- | --- | --- |
| OKX | 所有 live、inverse、USD 面值、标的币结算的 FUTURES | 张数 × `ctVal` | `books5` 完整快照 |
| Binance COIN-M | 所有 TRADING、USD 报价、标的币保证金、非永续到期合约 | 张数 × `contractSize` | `<symbol>@depth20@100ms` 完整快照，收取后仅取 5 档 |
| Deribit | BTC、ETH 的 active/reversed/USD 报价、标的币结算到期 future | 盘口数量已经是 USD，不乘 `contract_size` | `book.<instrument>.none.20.100ms` 完整快照，仅取 5 档 |
| Bybit | inverse 下 Trading 的 InverseFutures、USD 报价、标的币结算 | 盘口数量为 USD | `orderbook.1.<symbol>` 最优一档快照 |

USDT/USDC 本位线性合约不在范围内。所有周期均按“标的、报价币、结算币、完整到期时间戳”分组，任意两所匹配即可计算；三所生成三组配对。单边挂牌的可用盘口也会保存，但不计算跨所价差。Bybit 自动分页，所有适配器均排除永续和已到期合约。

REST 仍负责每五分钟发现合约和同步时钟。WS 刷新合约或断线时清空相关缓存，重新等待完整快照；独立心跳、订阅分批提交及最大 30 秒退避重连。缓存超过 `--max-age-ms` 未收到更新则不输出，报价时间差超过 `--max-skew-ms` 则标记质量不合格，不伪造新时间戳。

## 表结构和分析

| 表 | 主要内容 |
| --- | --- |
| `delivery_runs` | 运行批次、启动/结束时间、传输模式 |
| `delivery_contracts` | contract_id、交易所、符号、到期、标的、结算币、面值和数量步长 |
| `delivery_books` | 去重盘口、contract_id、交易所/接收/校正时间、最优买卖价及容量、最多 5 档 JSON |
| `delivery_spreads` | run_id、采样时间、双腿 book_id、方向、面值、有效价格、毛价差、质量标记 |
| `delivery_events` | 稀疏的发现摘要、错误、连接状态；不含重复盘口 |

时间为 UTC epoch 毫秒。分析列用 SQLite REAL；精确价格及原始数量在 `bids_json/asks_json` 的 Decimal 字符串中，`usd_per_quantity_unit` 用于将原始数量换成 USD。保留 `rtt_ms`，WS 单程延迟不可测，值为 NULL。

```sql
SELECT base_asset, datetime(expiry_ms / 1000, 'unixepoch') AS expiry_utc,
       buy_exchange, sell_exchange, COUNT(*) AS samples,
       AVG(gross_spread_bps) AS avg_bps, MAX(gross_spread_bps) AS max_bps
FROM delivery_spreads
WHERE quality_ok=1 AND sufficient_depth=1
GROUP BY base_asset,expiry_ms,buy_exchange,sell_exchange;
```

查询双腿原始盘口：

```sql
SELECT s.observed_ms, s.base_asset, s.buy_exchange, s.sell_exchange,
       s.gross_spread_bps, b.bids_json, b.asks_json, a.bids_json, a.asks_json
FROM delivery_spreads s
JOIN delivery_books b ON b.book_id=s.buy_book_id
JOIN delivery_books a ON a.book_id=s.sell_book_id
WHERE s.quality_ok=1 AND s.sufficient_depth=1 LIMIT 20;
```

更多 SQL 见 [delivery_spread_queries.sql](delivery_spread_queries.sql)。价差样本数是采样次数，不是独立套利机会数。

目标金额必须满足双方数量步长；有效价格按反向合约计算：`总 USD / sum(各档 USD / 各档价格)`。第 6 档之后一律不参与计算，若前 5 档不足则深度标记失败。已落盘数据不能回算超过 5 档的成交规模。

## 可选调试输出

显式传 `--output logs/debug.jsonl` 才会双写 JSONL。`--record-ws-raw` 还要求指定 `--output`，用于协议调试；即使打开，保存的原始消息盘口也最多 5 档，不会把这些消息复制进 SQLite。不要长期启用调试输出。

WAL 模式允许同时读取和采集；长时间不结束的读事务可能使 WAL 文件变大。需要转移数据库时，先正常停止采集，或通过 SQLite backup API 获取一致性备份。不要在采集中仅复制主库文件。程序不会自动删除旧数据或自动压缩旧库。

## 验证与限制

```bash
python -m pytest -q test/tools/test_collect_btc_delivery_spread.py \
  test/tools/test_delivery_spread_ws.py test/tools/test_delivery_spread_store.py
```

已验证 OKX + Deribit 实时 REST/WS 采集、SQLite 落盘和读写一致性。Binance、Bybit 受当前出口地区限制，接口解析、分页、订阅和消息处理经离线测试，尚未完成端到端验证。

REST 主机：`www.okx.com`、`dapi.binance.com`、`www.deribit.com`、`api.bybit.com`；WS 另需 `ws.okx.com`（TLS 443）、`dstream.binance.com`、`stream.bybit.com`。遵循环境代理和 TLS 校验。

本工具只分析毛价差，不包含手续费、资金成本或最终结算指数差异；正价差不等于可锁定利润。

## 其他公共数据

标记价/指数价、持仓量、近期成交、分钟 K 线及交割历史通过独立命令 `tools/collect_delivery_public.py` 按需采集，可写同一新版数据库。支持矩阵、已知缺口及用法见 [DELIVERY_PUBLIC_DATA.md](DELIVERY_PUBLIC_DATA.md)。默认盘口采集不会自动调用这些额外接口。
