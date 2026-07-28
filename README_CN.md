# up-down-spread-bot（中文说明 / README_CN）

> Polymarket 加密货币 **15 分钟 Up/Down 二元预测市场** 的自动化交易机器人（dry-run 模拟运行）。
> 原名 “Meridian / Late Entry V3”，本文档统一使用当前项目名 `up-down-spread-bot`。

---

## 1. 项目简介

本项目对接 Polymarket 的二元预测市场，针对 **BTC / ETH / SOL / XRP**（可配置）每个币种、每 15 分钟一个的 `*-updown-15m-*` 市场做自动交易：

- 在每个 15 分钟窗口的**尾盘阶段**，依据盘口（order book）的买卖价判断“热门方”，并跟随下注；
- 支持 **dry-run（模拟）** 与 **实盘（LIVE）** 两种模式，默认 `dry_run: true`；
- 自带 Web 仪表盘（`http://127.0.0.1:5050`）、PnL 曲线、成交记录、离线回测工具（`backtest.py`）。

工作目录：`D:\Code\PolyMarket_Projects\polymarket-5min-15min-1hour-arbitrage-trading-bot\up-down-spread-bot`

---

## 2. 市场环境与数据来源

| 项 | 说明 |
|---|---|
| 交易所 | Polymarket（链上二元预测市场，结算币种为 USDC） |
| 标的 | `btc / eth / sol / xrp` 四个币种的 `updown` 市场 |
| 窗口 | `15m`（也可切到 `5m`，见 `data_sources.polymarket.market_window`） |
| Slug 规则 | `{coin}-updown-15m-{slot}`，如 `eth-updown-15m-1784115900` |
| 行情 | CLOB **WebSocket**（`wss://ws-subscriptions-clob.polymarket.com/ws/market`）订阅 UP/DOWN 两侧 ask/bid |
| 元数据 | Gamma API（`https://gamma-api.polymarket.com`）发现活跃市场、结算时间、token id 等 |
| 代理 | 本机需走代理 `127.0.0.1:7897`（启动时设 `HTTP_PROXY/HTTPS_PROXY/NO_PROXY`） |

二元市场结算规则：**胜方每张合约兑付 \$1，败方 \$0**。盘口 ask 价即市场隐含概率（UP ask + DOWN ask ≈ \$1）。

---

## 3. 核心交易策略：Late Entry V3（尾盘跟随热门方）

策略实现：`src/strategy.py` 的 `LateEntryStrategy.should_enter()`。
核心思想：**在窗口临近结束时，盘口的“热门方”（ask 价更高的一方，即隐含概率更高的一方）往往就是最终胜方；在其价格未被买高到接近 \$1 之前买入，赚取结算兑付差。**

### 3.1 入场信号判定（全部条件满足才下单）

按顺序（`strategy.py:75-106`）：

1. **时间窗口**：`seconds_till_end <= entry_window_sec` 且 `> 0`。当前 `entry_window_sec = 600`，即在每个 15m 窗口的最后 **10 分钟**内才允许入场。
2. **下单频率**：同一市场距上次入场需 ≥ `entry_frequency_sec = 7` 秒（用于分批加仓 / DCA）。
3. **价差过滤**：`up_ask + down_ask <= max_spread (1.05)` 且 `> 0`。UP+DOWN ask 应≈1，偏离过大说明盘口异常/流动性差，跳过。
4. **置信度**：`|up_ask - down_ask| >= min_confidence (0.05)`。两侧 ask 的价差越大，代表多空分歧/共识越强。当前阈值极低（0.05），只要有 5% 的价格差就触发。
5. **热门方**：`favorite = 'UP' if up_ask > down_ask else 'DOWN'`（ask 更高 = 隐含概率更高 = 买入方向）。
6. **价格上限**：热门方 ask `fav_price <= price_max (0.88)`。超过 88¢ 就不买，保留上行空间。
7. **单市场投入上限**：若已有仓位且 `total_cost >= max_investment_per_market (300)`，停止加仓。

### 3.2 仓位与加仓（DCA）

- 每次入场的**合约数**按“距结算剩余时间”分三档（`strategy.py:112-116`，15m 窗口对应 180s/120s 阈值）：
  | 剩余时间 | 合约数 |
  |---|---|
  | `> 180s` | `above_180_sec = 8` |
  | `120s ~ 180s` | `above_120_sec = 10` |
  | `≤ 120s` | `below_120_sec = 12` |
- 越接近结算、单次买得越多；每 7 秒重新评估一次，条件满足就再下一笔，直到单市场累计投入达到 `max_investment_per_market = $300` 或窗口结束。
- 若热门方在窗口内发生切换（例如先 UP 后 DOWN 占优），后续加仓会买另一侧，因此一个市场可能同时持有 UP、DOWN 两侧合约。

### 3.3 出场与结算

- 市场结算时，由胜方兑付：该侧每张合约收回 \$1，败方 \$0。
- **实盘（LIVE）**：通过 `SimpleRedeemCollector` / `process_redeem_async` 调用链上预言机结果 `api_result["winner"]`（`main.py:1280-1292`）确定胜方，再 `close_market` 计算收益。
- **模拟（dry-run）**：没有链上仓位，故在“市场切换”那一刻，用 `LAST_MARKET_STATE` 中记录的**最后盘口**（ask 更高的一方判为 winner）本地结算并记账（`main.py:1918-1943`）。这正是本项目默认路径。
- 单笔收益：
  ```
  PnL      = 胜方张数 × $1.0 − 总投入(total_cost)
  ROI%     = PnL / 总投入 × 100%
  ```

### 3.4 风控（当前默认全部关闭）

| 风控 | 配置 | 状态 |
|---|---|---|
| flip_stop（价格反转保护） | `exit.flip_stop.enabled` | **false**（已修复忽略该开关的 bug） |
| stop_loss（止损） | `exit.stop_loss.per_coin` | **false**（BTC/ETH/SOL/XRP 均关闭） |

> 说明：早期代码存在 bug——`flip_stop.enabled:false` 仍被 `main.py`/`trader.py` 强制执行。已修复：由 `strategy.flip_stop_enabled` 统一控制，关闭时不触发。

### 3.5 收益模型直观示例

以一笔 ETH 成交为例（来自仪表盘 Recent closed trades）：

```
eth-updown-15m-1784115900 | 入场均价 $0.862 | 合约数 104.0 | 金额 $89.68 | PnL +$14.32 | winner=UP
```

- 买入 104 张 UP @ 均价 \$0.862，投入 \$89.68；
- 结算 UP 胜，兑付 104 × \$1 = \$104；
- `PnL = 104 − 89.68 = +$14.32`（ROI ≈ +16%）。

这笔是**单边**（只买 UP），所以“均价/总合约/总投入”恰好等于该侧数值；只有当预测中途翻盘、双边都买过时，dash 上的三列才会是两侧合计。

---

## 4. 关键文件与目录结构

```
up-down-spread-bot/
├── config/config.json            # 所有运行参数（见第 5 节）
├── src/
│   ├── main.py                   # 主入口：行情回调、入场/结算编排、dry-run 记账
│   ├── strategy.py               # LateEntryStrategy 入场信号逻辑
│   ├── trader.py                 # 仓位记账、加仓、close_market 收益计算
│   ├── multi_trader.py           # 多策略/多币种交易器封装
│   ├── data_feed.py              # Polymarket WebSocket 行情 + Gamma 元数据 + 盘口录制
│   ├── order_executor.py         # 下单执行（FAK/FOK/分块/sweep）
│   ├── safety_guard.py           # 资金/频率安全限制
│   ├── simple_redeem_collector.py# 实盘结算/赎回扫描
│   ├── pnl_chart_generator.py    # 累计 PnL 曲线生成（matplotlib）
│   ├── backtest.py               # 离线回测（回放盘口历史）
│   ├── telegram_notifier.py      # Telegram 通知 / 指令
│   └── web_dashboard/            # Flask 仪表盘
│       ├── server.py             # API + /pnl_chart.png 路由
│       ├── snapshot_builder.py   # 组装 /api/status 快照（含 recent_trades 字段）
│       ├── templates/index.html  # 仪表盘页面
│       └── static/{app.js,app.css}
└── logs/
    ├── late_v3_{coin}/trades.jsonl   # 成交记录（图表/历史来源）
    ├── book_history/{coin}/*.jsonl   # 盘口历史（回测用，每 1s 一条）
    └── pnl_chart.png                 # 当前 PnL 曲线
```

---

## 5. 配置参数速查（config/config.json）

| 路径 | 当前值 | 含义 |
|---|---|---|
| `safety.dry_run` | `true` | 模拟模式（不花真钱） |
| `safety.max_order_size_usd` | `150` | 单笔最大金额 |
| `safety.max_total_investment` | `1000` | 总投入上限 |
| `trading.{btc,eth,sol,xrp}.enabled` | BTC/ETH/SOL 开，XRP 关 | 参与交易的币种 |
| `strategy.entry_window_sec` | `600` | 仅在窗口最后 600s 内入场 |
| `strategy.entry_frequency_sec` | `7` | 加仓最小间隔（秒） |
| `strategy.min_confidence` | `0.05` | 两侧 ask 最小价差门槛 |
| `strategy.max_spread` | `1.05` | UP+DOWN ask 上限（异常盘口过滤） |
| `strategy.price_max` | `0.88` | 热门方 ask 上限 |
| `strategy.max_investment_per_market` | `300` | 单市场累计投入上限 |
| `strategy.sizing.above_180_sec` | `8` | 剩余 >180s 时每次合约数 |
| `strategy.sizing.above_120_sec` | `10` | 剩余 120~180s |
| `strategy.sizing.below_120_sec` | `12` | 剩余 ≤120s |
| `exit.flip_stop.enabled` | `false` | 价格反转保护（关） |
| `exit.stop_loss.*.enabled` | `false` | 止损（关） |
| `data_sources.polymarket.market_window` | `15m` | 市场窗口 |
| `data_sources.polymarket.record_book_history` | `true` | 录制盘口供回测 |
| `notifications.chart_every_n_markets` | `10` | 每 10 个市场生成一次图表（发 Telegram 用） |

---

## 6. 运行方式

### 6.1 手动启动（命令行）

```powershell
cd up-down-spread-bot
# 设置本地代理（Polymarket 访问所需）
$env:HTTP_PROXY="http://127.0.0.1:7897"
$env:HTTPS_PROXY="http://127.0.0.1:7897"
$env:NO_PROXY="127.0.0.1,localhost"

# 方式一：直接调用自带 venv 的 python
.\venv\Scripts\python.exe src\main.py --web --web-host 127.0.0.1 --web-port 5050

# 方式二：先激活 venv 再运行（等价）
.\venv\Scripts\activate
cd src
python main.py --web --web-host 127.0.0.1 --web-port 5050
```

不带 `--web` 则只跑终端，不启动网页仪表盘：

```powershell
.\venv\Scripts\python.exe src\main.py
```

启动后：
- 控制台实时打印每个币种的盘口、入场、结算 PnL；
- 浏览器打开 `http://127.0.0.1:5050` 查看仪表盘；
- 键盘 `[M]` 手动赎回所有仓位；`Ctrl+C` 优雅退出。

> ⚠️ 每个币种**第一个市场会被跳过**（中段开始，没有完整窗口），从第二次市场切换起才正式交易。

---

## 7. Web 仪表盘

`http://127.0.0.1:5050` 包含：

1. **Session**：运行时长、模式（DRY RUN）、钱包余额、总 PnL、成交数、ROI%。
2. **Markets (live)**：每个币种的实时盘口（UP/DN ask）、剩余时间、当前仓位（未实现盈亏、投入、方向/笔数、UP/DN 赢时的盈亏）。
3. **Recent closed trades**：已结算成交表，列包括 `Strategy / Slug / Datetime / Entry $（加权均价）/ Contracts（总合约）/ Amount（总投入）/ PnL / Winner`，可在右上角切换显示 10 / 50 条。
4. **PnL curve**：累计 PnL 曲线（`/pnl_chart.png` 路由按需重生成，每 15s 刷新一次，读取 `logs/late_v3_*/trades.jsonl`）。
5. **Settings**：在线查看/编辑 `config.json`（修改后需重启 bot 生效）。

---

## 8. 离线回测（backtest.py）

`src/backtest.py` 可回放 `logs/book_history/{coin}/*.jsonl` 中录制的盘口历史，按策略逻辑重新模拟入场/结算，并用 Gamma API 解析真实 winner 计算收益；支持参数网格扫描（`--grid`）。适合在不连实盘的情况下验证参数（如 `min_confidence`、`entry_window_sec`、`sizing`）。

---

## 9. 已知限制与注意事项

1. **钱包余额 vs 投入上限不一致**：仪表盘 `Wallet` 显示 \$100（模拟），但下单逻辑只看 `max_investment_per_market=300`，**dry-run 下不校验钱包余额**，3 个币合计最多可投入约 \$900，远超 \$100。若希望“总资金 \$100、按比例下注”，需把 `sizing` 改为基于钱包余额的百分比。
2. **周期图表路径为 Linux 硬编码**：`main.py` 中给 Telegram 发图的路径仍是 `/root/4coins_live/logs/pnl_chart_{n}.png`，在本机（Windows）不会生成；网页曲线走的是 `server.py` 中独立的按需生成逻辑（`logs/pnl_chart.png`），两者互不影响。
3. **dry-run 结算口径**：胜方判定依赖“最后盘口 ask 更高的一方”，与链上预言机结果在极端盘口下可能存在偏差。
4. **策略本质**：这是“尾盘跟随盘口热门方”的共识/动量策略；在 fair price 附近纯跟随热门方理论上是 break-even，实际期望收益依赖“临近结算时盘口热门方的预测力优于中间价隐含概率”这一假设，以及 `price_max` 对成本的上限保护。历史成交样本量较小，参数仍偏激进（`min_confidence=0.05`），实盘前建议先用 `backtest.py` 充分验证。
5. **Recent closed trades 为内存态**：`closed_trades` 在进程启动时清空，重启后需等本会话内市场结算才会重新出现（历史 `trades.jsonl` 仍保留在磁盘）。
