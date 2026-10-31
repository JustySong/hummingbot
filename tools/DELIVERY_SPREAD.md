# 多交易所币本位交割合约行情采集

独立 Python 3.10+ 程序，REST 模式仅使用标准库，WS 模式使用可选依赖。只读公共行情，不需要密钥、不读取账户、不下单。采集所有到期日，不支持 USDT/USDC 本位线性合约。

## 交易所范围

| 交易所 | 发现范围 | 盘口数量单位 |
| --- | --- | --- |
| OKX | 所有 live、inverse、USD 面值、标的币结算的 FUTURES | 张数，乘 `ctVal` 换算 USD |
| Binance COIN-M | 所有 TRADING、标的币保证金、USD 报价的非永续到期合约 | 张数，乘 `contractSize` 换算 USD |
| Deribit | BTC 和 ETH 的 active、reversed、USD 报价、标的币结算到期 future | 原始数量已经是 USD，不能再乘 `contract_size` |
| Bybit | inverse 类别下 Trading 的 InverseFutures，USD 报价、标的币结算 | USD；数量步长来自 `lotSizeFilter.qtyStep` |

Bybit 自动遍历 `nextPageCursor`。Deribit 排除 PERPETUAL；其最小 USD 数量约束来自 `min_trade_amount`。原始合约元数据会一起保存。

以“标的、报价币、结算币、完整到期时间戳”分组，不按季度等别名过滤。任意两所符合条件即可配对；三所匹配时生成三组双向价差。不同到期日、不同标的绝不混配。每五分钟重新发现和校准时钟。单所发现失败、时钟失败、单个合约行情失败不会阻断其他交易所；错误会记录在 discovery 元数据或 error 记录中。

## 运行

从仓库根目录运行：

```bash
# 推荐当前机器使用的组合：Bybit 即使被阻止也不影响另外两所
python tools/collect_delivery_spread.py --exchanges okx deribit bybit --list-only
python tools/collect_delivery_spread.py --exchanges okx deribit bybit --interval 5 --notional-usd 1000

# 完整单轮检查（四所，失败交易所会隔离）
python tools/collect_delivery_spread.py --samples 1 --output logs/delivery_smoke.jsonl
```

`--exchanges` 默认包含全部四所。`--list-only` 单次打印币种、UTC 到期时间、原始周期标签、匹配状态，保存原始发现元数据后退出。默认输出 `logs/delivery_spread.jsonl`，追加写入，请自行轮转长期运行的日志。旧入口 `tools/collect_btc_delivery_spread.py` 仍可使用。

`--samples` 是整轮采集次数，0 表示运行到 Ctrl-C。整轮请求较多时可能长于 `--interval`；每组合约请求后间隔 0.1 秒限制突发。退出码 0 表示至少采集到一个有效行情分组（可能仅单所），不保证产生双所有效价差；1 表示没有有效行情；130 表示人工中断。

网络需要 HTTPS 访问所选交易所：`www.okx.com`、`dapi.binance.com`、`www.deribit.com`、`api.bybit.com`。保留代理与 TLS 校验。交易所地域限制不等同于云端白名单问题。

## JSONL 格式（schema_version=2）

- `discovery`：合约分组、原始发现响应、每所数量或错误、时钟校准样本。
- `raw_books`：收到的原始订单簿响应、请求发送/接收时间和 RTT。
- `spread`：`exchanges=[left,right]`、币种、到期时间、标准化订单簿、双向价差、时间质量标记。
- `unmatched_book`：只有一所可用的标准化行情；可能因为只有一家挂牌，也可能另一家请求失败。不计算价差。
- `error`：失败时间、交易所或配对及错误描述。

相比旧版本，`spread.books` 按实际交易所名索引，`mid_okx_minus_binance_bps` 更名为 `mid_left_minus_right_bps`，方向由 `exchanges` 字段解释。

`directions.buy_deribit_sell_okx` 表示 Deribit 买、OKX 卖。两腿使用相同 USD 面值，沿深度计算有效价格：

```text
有效价格 = 总 USD 面值 / sum(各档 USD 面值 / 各档价格)
毛价差 bps = (卖出有效价格 / 买入有效价格 - 1) × 10000
中间价差 bps = (left 中间价 / right 中间价 - 1) × 10000
```

反向合约必须使用调和加权，不能直接按张数对价格做算术平均。标准化合约字段 `usd_per_contract` 为沿用旧版本的名称，实际含义是“每一个原始盘口数量单位对应的 USD”：OKX/Binance 为每张面值，Deribit/Bybit 为 1。`quantity_unit` 和 `quantity_step_usd` 用于区分单位、校验 USD 数量步长。Deribit 的真实 `contract_size` 保留在原始发现元数据中。

`--notional-usd` 必须满足两边步长。深度不足时价格和价差是 null，`sufficient_depth=false`。标准化订单簿保留原始数量单位，不做跨所张数等同假设。

时间质量使用服务器时钟偏移校正后的订单簿时间。默认行情超过 5 秒、未来时间异常或两所时间差超过 1 秒时 `quality_ok=false`。只分析质量通过、深度足够的记录。REST 快照并不同步，RTT、时间校准误差及更新频率会影响结果。

输出为毛价差，不包含手续费、保证金成本、交割结算指数差异或净收益，不代表已验证可成交利润。

## 验证

```bash
python -m pytest -q test/tools/test_collect_btc_delivery_spread.py
```

单元测试使用合成数据，覆盖多币种和到期日隔离、永续/线性/过期排除、不同数量单位、反向深度换算、Bybit 分页、任意交易所两两配对、地区封锁及失败隔离。

本机实测 OKX + Deribit 获得 BTC/ETH 各五个到期日共十组价差记录；Bybit 发现接口返回 403，因此 Bybit 解析与分页仅经离线测试，尚未实盘验证。快照仅表示测试时的挂牌情况。

## 接口参考

- Binance: `/dapi/v1/exchangeInfo`、`/dapi/v1/depth`、`/dapi/v1/time`
- OKX: `/api/v5/public/instruments`、`/api/v5/market/books`、`/api/v5/public/time`
- [Deribit API](https://docs.deribit.com/): `public/get_instruments`、`public/get_order_book`、`public/get_time`
- [Bybit instruments](https://bybit-exchange.github.io/docs/v5/market/instrument)、[orderbook](https://bybit-exchange.github.io/docs/v5/market/orderbook)、[server time](https://bybit-exchange.github.io/docs/v5/market/time)

## WebSocket 模式

安装可选依赖，再选择 WS 传输；REST 模式仍为默认，无需安装额外依赖：

```bash
python -m pip install -r tools/requirements-ws.txt
python tools/collect_delivery_spread.py --transport ws --exchanges okx deribit bybit --interval 1
# 有限轮次验证；每轮输出一次缓存中的可用行情/价差
python tools/collect_delivery_spread.py --transport ws --exchanges okx deribit --interval 2 --samples 10
```

WS 模式持续接收行情，`--interval` 是输出/分析间隔（最小 0.1 秒），不是行情订阅频率；`--samples` 是输出轮次，可能在订阅尚未完成时产生空轮次。`--list-only` 始终通过 REST 查询合约清单。

| 交易所 | WS 公共频道 | 深度 / 消息类型 | 主机 |
| --- | --- | --- | --- |
| OKX | `books5` | 5 档完整快照 | `ws.okx.com`，TLS 443 |
| Binance COIN-M | `<symbol>@depth20@100ms` | 20 档局部深度完整快照，非增量 diff depth | `dstream.binance.com` |
| Deribit | `book.<instrument>.none.20.100ms` | 20 档分组深度完整快照 | `www.deribit.com` |
| Bybit inverse | `orderbook.1.<symbol>` | 最优一档完整快照 | `stream.bybit.com` |

Bybit 此版本使用 L1 快照，没有构建 50 档增量本地订单簿；若目标面值超过最优一档容量，价差为 null、`sufficient_depth=false`，不能与其他交易所的深度数量直接等同。行情可能在盘口不变时较少推送；本工具不会伪造新时间戳来使旧报价通过质量检查。

REST 仍负责每五分钟的合约发现和时钟校准，WS 不依赖 REST 订单簿快照初始化。每次刷新合约时，旧订阅会取消、缓存清空，再根据新清单订阅。连接断开或协议错误时立即清除该所行情，以 1、2、4…最多 30 秒间隔重连；应用心跳每 15 秒发送，订阅分批提交。连接恢复后重新等待完整快照，不使用断线前的缓存。各所独立连接和重试；受地区限制的交易所不会阻断可用交易所。

每个输出轮次保存 `ws_books`（标准化缓存快照），并生成 `transport=ws` 的 `spread`；`ws_connected` 记录成功连接。单程网络延迟无法由推送消息推算，故 WS 的 `rtt_ms=null`。超过 `--max-age-ms` 未收到更新的缓存不参与输出；报价时间错位仍会在 `quality_reasons` 中标记。

可加 `--record-ws-raw` 保存每条接收消息到 `raw_ws`，用于协议分析/回放。该选项会产生大量磁盘写入，默认关闭。正常模式仍保存输出时使用的完整标准化盘口。

WS 模式遵循环境代理和 TLS 验证。受限网络除原有 REST 域名外还需允许 `ws.okx.com`、`dstream.binance.com`、`stream.bybit.com`（Deribit 共用 `www.deribit.com`）。

实测：OKX + Deribit 已完成 WS 连接、心跳和双所价差输出；32 条价差样本中 17 条通过默认时间质量检查，15 条因两所时间差超过 1 秒被标记。该短时检查不能证明长期稳定性。Binance/Bybit 因当前出口地区限制尚未完成端到端 WS 验证，协议适配经过合成消息测试。

测试：

```bash
python -m pytest -q test/tools/test_collect_btc_delivery_spread.py test/tools/test_delivery_spread_ws.py
```

## SQLite 数据存储

默认同时追加写入 `logs/delivery_spread.sqlite` 与原有 JSONL；无需安装 SQLite Python 依赖。可通过 `--sqlite` 指定数据库路径：

```bash
python tools/collect_delivery_spread.py --transport ws --exchanges okx deribit \
  --interval 1 --samples 60 --sqlite logs/delivery_spread.sqlite
```

`--samples 60 --interval 1` 表示完成发现和订阅初始化后输出 60 轮，约一分钟；运行期间可用其他程序查询数据库，WAL 模式允许读写并行。程序不会清空现有数据；重启创建新 `run_id` 并继续追加。数据库、WAL 和 JSONL 位于被 Git 忽略的 `logs/`，不提交到仓库。

| 表 | 内容 |
| --- | --- |
| `delivery_runs` | 每次运行的 run_id、启动/结束时间和 REST/WS 模式 |
| `delivery_contracts` | 合约元数据，按交易所、符号、到期时间去重 |
| `delivery_events` | 所有事件及完整原始 JSON，包括发现失败和质量信息 |
| `delivery_books` | 标准化盘口，含交易所时间、接收时间、最优买卖价及完整档位 JSON |
| `delivery_spreads` | 每个配对的两个买卖方向各一行，含目标面值、有效买卖价格、毛价差 bps、时间和深度质量标记 |

每个事件及其结构化投影在同一事务内提交。`delivery_books` 与 `delivery_spreads` 通过 `event_id` 关联原始事件；`delivery_events.run_id` 关联运行批次。同一盘口可能出现在 `ws_books` 和 `spread` 两类事件内，按盘口采样统计时应连接 `delivery_events` 并筛选 `type='ws_books'`，避免重复计算。重复采样同一未变化报价也会保留；计数代表采样数，不代表独立套利机会数。

时间为 UTC epoch 毫秒。数值分析列使用 SQLite REAL，便于聚合；如需精确十进制运算，读取 `delivery_events.payload_json` 或档位 JSON 中的原始 Decimal 字符串。质量不合格或深度不足的数据不会删除，应在收益研究时显式筛选。

常用查询保存在 [delivery_spread_queries.sql](delivery_spread_queries.sql)，例如：

```sql
SELECT base_asset, buy_exchange, sell_exchange,
       datetime(expiry_ms / 1000, 'unixepoch') AS expiry_utc,
       COUNT(*) AS samples, AVG(gross_spread_bps) AS avg_bps,
       MAX(gross_spread_bps) AS max_bps
FROM delivery_spreads
WHERE quality_ok = 1 AND sufficient_depth = 1
GROUP BY base_asset, expiry_ms, buy_exchange, sell_exchange;
```

如系统没有 `sqlite3` CLI，可用标准库查询：

```bash
python - <<'PY'
import sqlite3
with sqlite3.connect('logs/delivery_spread.sqlite') as db:
    for row in db.execute('SELECT base_asset, COUNT(*) FROM delivery_spreads GROUP BY base_asset'):
        print(row)
PY
```

转移数据库前先正常停止采集器，或使用 SQLite 的 backup API 获取一致性备份；运行时不要只复制主数据库而遗漏 WAL。
