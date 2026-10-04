# Stablecoin depth panel

每日保存两套不同口径的数据：

- **CoinGecko proxy**：`data/snapshots/` 与原有 `panel/panel_depth_2pct.csv`。这是聚合服务的 2% 深度指标。
- **Native L2**：`data/exact_l2/` 与 `panel/native_l2_*.csv`。这是交易所公开订单簿的实际买卖档位及卖出稳定币的模拟成交曲线。两套数据不拼接、不互相填补。

## 自动执行与失败提示

GitHub Actions 每日 **UTC 05:41** 执行；也支持手动 Run workflow。修改采集代码、配置或工作流并推送 `main` 会立即触发一次。仅数据提交不会触发递归运行。

工作流先运行离线测试，再采集 native L2、校验并重建 native panel、提交原始数据；随后独立收集 CoinGecko proxy。某个启用的 native 数据源失败或 TRY/BRL/IDR/ZAR/THB 任一走廊缺失，工作流会失败，但仍保存成功的部分和失败原因。**不再使用 continue-on-error 隐藏 native 采集失败。** 推送前还会将当次 native 原始文件作为 Actions artifact 保留 14 天，避免提交冲突造成不可逆丢失。Actions summary 与每次 `manifest.json` 列出成功、失败及禁用来源。

绿色代表此次配置中启用的来源与五个走廊全部采到，不代表历史 Binance 数据已补齐，也不代表每个订单簿覆盖全部深度。查看 `depth_is_lower_bound`、`book_covers_threshold`、`book_exhausted` 和逐来源状态。

## 数据源与历史断点

| 走廊 | 启用的原生来源 | 市场 |
|---|---|---|
| TRY | BtcTurk | USDTTRY |
| BRL | Mercado Bitcoin | USDT-BRL |
| IDR | Indodax | usdtidr |
| ZAR | VALR | USDTZAR |
| THB | Bitkub v3 | usdt_thb |

原 Binance 配置保留但禁用：此前返回 HTTP 451，未采到的 Binance 订单簿仍然缺失。USDTIDR 原生 symbol 也未曾核实成功。新增交易所从首次成功的新快照开始，是**不同交易场所的新增序列**，不能称为 Binance 历史回补或无缝接续。不会使用代理绕过限制，也不会用今天的数据回填过去。

Bitso 的 BRL/MXN adapter 已保留为可选来源，默认禁用。来源可达性会随服务和运行地区变化，以 Actions 实际结果为准；禁用来源不会被伪记为成功。

## 时间、单位与复现

新版每次调用写入唯一目录：

`data/exact_l2/YYYY-MM-DD/runs/<snapshot_id>/`

包含配置存档、原始 JSON、`threshold_depth.csv`、`execution_curves.csv` 与 SHA256 manifest。重复运行不会覆盖当天早前观测。旧版日期根目录记录保持原样。`--date` 只接受 UTC 今天；`--force` 为兼容保留，仍不会覆盖历史。

- 所有启用市场的规范化 base 为 USDT，quote 为当地法币；卖出 USDT 消耗 bids。
- 档位价格单位为当地法币/USDT，数量为 USDT token，`quote_proceeds` 是当地法币金额。
- 新 `*_stablecoin_units` 是 token 数量。旧 `*_stablecoin_usd` 是相同数量的兼容别名，仅假设 1 token = 1 USD，**不是按市场汇率换算后的美元金额**。
- `collected_utc` 是请求发出时刻，`received_utc` 是本地接收时刻；交易所没有快照时间就留空。BtcTurk 的时间戳为毫秒；Mercado Bitcoin 未明确标注时间单位；2026-10-04 实际响应为纳秒，因此支持秒/毫秒/微秒/纳秒的数值量级解析并显式标注 inferred，原值保存在 raw JSON。提供交易所时间的快照会做新鲜度检查。
- 深度只针对 API 返回的档位计算。未覆盖阈值范围时数值是下界；固定流量未完全成交会保留 fill_rate 与 book_exhausted。
- `build_exact_l2_panel.py` 校验 manifest 中的 CSV/raw SHA256，只纳入 manifest 标记成功的 native 记录。每个日期、来源、场所、pair 和 snapshot 都保留，不跨市场加总。旧版没有的元数据留空并标明 legacy；其原始观测时刻不更改。

## 本地运行

```bash
python -m pip install -r requirements.txt
python test_depth_logic.py
python -m unittest discover -p 'test_*.py' -v
python collect_exact_l2.py
python build_exact_l2_panel.py
```

网络受限时采集器会保存失败 manifest 并返回非零。离线测试用合成 fixture，不会写入研究数据。原有 CoinGecko 流程仍为 `python collect_snapshot.py`。

## 官方接口依据

- [BtcTurk order book](https://docs.btcturk.com/docs/public-endpoints/orderbook/)
- [Mercado Bitcoin v4 API](https://api.mercadobitcoin.net/api/v4/docs)
- [Indodax public REST API](https://github.com/btcid/indodax-official-api-docs/blob/master/Public-RestAPI.md)
- [Bitkub v3 depth schema](https://github.com/bitkub/bitkub-official-api-docs/blob/master/rest-v3.md#get-apiv3marketdepth)
- [Bitso order book](https://docs.bitso.com/bitso-api/docs/list-order-book)
- [VALR API](https://docs.valr.com/)
