# 多交易所币本位交割合约行情采集

独立 Python 3.10+ 标准库程序，只读公共 REST API，不需要密钥、不读取账户、不下单。采集所有到期日，不支持 USDT/USDC 本位线性合约。

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
