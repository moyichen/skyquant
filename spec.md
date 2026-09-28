# SkyQuant 量化回测系统 Spec 文档

**版本**：v1\.0

**日期**：2026\-09\-21

**适用项目**：SkyQuant A股全自动量化回测流水线

**文档用途**：项目规范、开发基准、迭代依据、系统复盘、交付归档

**免责声明**：本系统仅用于量化策略学术回测研究，不构成任何投资建议，回测结果不代表未来收益。

---

## 1\. 项目概述

### 1\.1 系统定位

SkyQuant 是一套**全自动、可复现、可校验、可迭代**的 A 股日线量化策略回测流水线。

一站式完成：行情拉取 → 缓存管理 → 股票池前置筛选（质量门/趋势门/regime 分类路由）→ 网格参数寻优 → 过拟合剔除 → 滚动稳定性校验 → 最优参数聚合 → 自动写入配置 → 策略回测 → 指标计算 → 可视化绘图 → 日志留存 → 实盘成交复盘与次日信号。

系统按层解耦：配置层 config.yaml → 数据层 dataprovider.py → 筛选层 stock_filter.py → 策略插件层 strategy/ → 回测引擎层 main.py → 指标报表层 report.py → 寻优层 opt_pipeline/ → 统一 CLI skyquant.py；公共能力（手续费、路径、日志、broker 装配）收敛于 comm.py。策略只负责信号规则，不读文件、不拉数据、不筛选标的、不绘图；新增策略只需新增文件并注册 STRATEGY_MAPPING。

### 1\.2 核心能力

- Tushare 日线行情自动拉取 \+ 增量更新 \+ 本地缓存

- 完整 A 股交易成本模型（佣金、印花税、过户费）+ 可选百分比滑点（config 开关，回测/寻优同口径）

- 股票池前置筛选：Pairlist Filters 质量门（AgeFilter/PriceFilter/VolumeFilter + turnover/liquidity A 股扩展 + ST 名称规则，常驻）+ trend 趋势门（--screen）+ regime 自动路由（trend/range/breakout）

- 多策略、多标的网格参数遍历寻优（按 regime 只寻优路由策略；profit_rate/sharpe/calmar 三种优化目标可配）

- 双层过拟合校验：外样本验证 \+ 滚动窗口稳定性验证

- 自动参数筛选、聚合、写入配置文件

- 专业量化指标体系：年化收益、最大回撤、夏普比率、卡玛比率、胜率、盈亏比

- 自动可视化：自包含 HTML 回测报表（KPI 卡片、Strategy Summary、净值-回撤联动图、Exit Reason、Monthly Returns、交易表、Analyzer 字段表）、Plotly freqtrade 风格 K 线交互图

- 全局日志系统：控制台 \+ 文件双输出

- 实盘真实成交 VS 策略信号自动复盘匹配

- 实盘持仓管理：真实成交解析持仓/成本/浮盈，现算策略共识信号与次日操作建议，输出自包含 HTML 实盘持仓报告

- 一键全流程启动脚本，支持缓存加速模式

- 每日收盘后一条命令完成实盘复盘与次日信号生成（live_trading.py）

### 1\.3 系统约束

- 仅支持 A 股日线级别回测

- 所有参数必须经过“寻优 → 外样本 → 滚动校验”三道过滤

- 所有策略必须统一输出净值曲线、交易记录

- 所有运行过程必须可日志追溯、可复现

- 项目需兼容 Python 3.10 以下旧版本，类型注解使用 `Optional[X]` 而非 `X | None`

---

## 2\. 项目目录规范（固定不变）

所有新增代码、输出文件必须严格遵循目录结构。

```Plain Text
skyquant/
├── skyquant.py                 # 统一 CLI（dashboard/backtest/opt/fetch/filter/live/all）
├── main.py                    # 回测引擎层：Cerebro 封装/标的遍历/regime 自动路由
├── live_trading.py            # 实盘交易模块：真实成交复盘 + 次日信号 + HTML 持仓报告
├── dataprovider.py            # 数据层（DataProvider）：行情拉取与缓存
├── stock_filter.py            # 股票池前置筛选层：Pairlist Filters + trend 趋势门 + regime 分类
├── comm.py                    # 公共工具层：手续费/路径常量/日志/broker 装配/黑名单
├── report.py                  # 指标与报表层：指标计算 + HTML 报表 + K 线图
├── config.yaml                # 全局配置文件（全部可调参数外置）
├── live_trades.csv            # 实盘真实成交记录
├── cache/
│   ├── stock_cache/           # 股票K线缓存CSV
│   └── index_cache/           # 板块指数K线缓存CSV（index_daily，close 系字段）
├── output/
│   ├── run.log                # 全流程运行日志（自动生成）
│   ├── stock_filter.csv       # 前置筛选报告（pairlist/trend/regime，驱动策略路由）
│   ├── equity_curve/         # 每标的每日净值序列
│   ├── plots/                 # 每标的 HTML 回测报表、Plotly K 线图、live_portfolio_report.html 实盘持仓报告
│   ├── metrics_summary.csv    # 指标汇总表
│   ├── param_optimize_result.csv
│   ├── out_sample_verify_result.csv
│   ├── rolling_verify.csv
│   ├── aggregate_common_param.csv
│   ├── live_trade_review.csv  # 实盘成交 VS 策略信号匹配复盘
│   └── live_signal_*.csv      # 实盘持仓信号报告（按日期生成）
├── strategy/
│   ├── __init__.py
│   ├── base.py
│   ├── trend.py            # 趋势策略（合并 trend_follow+momentum）
│   ├── range.py            # 震荡策略（合并 boll_ma+short_reversal；bb_*band 指标）
│   └── breakout.py         # 突破策略（唐奇安通道）
└── opt_pipeline/
    ├── common.py                # 流水线共享基础设施（多指标 worker/目标解析/路由）
    ├── param_optimize.py
    ├── out_sample_verify.py
    ├── rolling_window_verify.py
    ├── aggregate_best_param.py
    └── write_param_to_config.py
```

---

## 3\. 流水线执行流程（寻优段按标的循环）

**run\_all\.py 执行顺序（数据层 → 筛选层 → 寻优 → 回测报表）**

1. 全量行情拉取 / 跳过缓存（批量，I/O 密集）

2. 股票池前置筛选（常驻，只读缓存，写 output/stock_filter.csv）：
   1. Pairlist Filters 质量门：上市天数/零成交量/缺口/成交额/换手率/均价/ST，未过者剔除
   2. trend 趋势门：仅 `--screen` 时强制（ADX/EMA/效率比等 7 项）
   3. regime 分类：trend/range/breakout 标签（不剔除，驱动策略路由）

3. 对每个通过 Pairlist Filters（--screen 时 pairlist+trend）的标的依次执行完整寻优链（单标的闭环后再处理下一个标的；按 regime 只寻优路由到的策略）：
   1. 网格参数优化遍历（profit_rate/sharpe/calmar 多指标，按 optimize_metric 排序）
   2. 外样本校验（剔除训练集过拟合）
   3. 滚动窗口稳定性校验（防止偶然收益）
   4. 聚合该标的每策略最优稳定参数
   5. 自动写入 config\.yaml 策略参数（仅触及该标的的条目）

4. 正式回测（main.py 不带 --strategy，按 regime 自动路由）、指标计算、绘图（全部标的批量）

5. 实盘成交复盘匹配、次日信号与 HTML 持仓报告（批量）

**目标标的集合**（寻优/回测/复盘共用同一集合）：

- 默认：回归标的集（`REGRESSION_STOCKS = ["000725"]`，定义在 opt\_pipeline/common\.py，与 tests/regression 同源）——每次修改参数后的快速迭代门槛
- `--all-stocks`：手动触发全量标的池
- `--stock-list`：显式指定子集

阶段 CSV（param\_optimize\_result / out\_sample\_verify\_result / rolling\_verify / aggregate\_common\_param）按标的合并写：重跑某标的只替换该标的的行，其余标的行保留；不带 `--stock-list`（批量模式）时整体重写。

运行模式：

- `python3 skyquant.py all` 回归标的集流水线（默认）

- `python3 skyquant.py all --all-stocks` 完整全量流水线（手动触发）

- `python3 skyquant.py all --skip-data`缓存加速流水线

实盘复盘与次日信号（也可独立于 skyquant.py 每日收盘后单独运行）：

- `python live_trading.py` 每日收盘后运行：解析 live_trades.csv 真实持仓，现算各持仓标的策略信号，输出买卖/持有/观望的次日操作建议与自包含 HTML 实盘持仓报告
- `python live_trading.py --force-refresh` 强制全量下载行情
- `python live_trading.py --stock-list 000725,601633` 仅复盘指定标的

---

## 4\. 模块详细规范

### 4\.1 skyquant.py 统一 CLI

**唯一职责**：命令行入口分发、日志管理、异常终止。

**子命令**：`dashboard`（本地看板）、`backtest`（单次回测）、`opt`（寻优流水线）、`fetch`（拉取行情）、`filter`（前置筛选）、`live`（实盘复盘）、`all`（全流水线调度）。

**all 编排顺序**：行情拉取/缓存 → 股票池前置筛选（Pairlist Filters 常驻、--screen 追加 trend、regime 分类）→ 逐标的寻优 5 阶段闭环 → 批量回测（regime 自动路由）→ 实盘成交复盘与次日信号（live_trading）。

**日志规范**：

- 同时输出：控制台 \+ output/run\.log（comm.setup_logging 统一初始化）

- 每次运行覆盖日志

- 子进程报错立即终止流水线

### 4.1.1 strategy/ 策略模块规范

**统一机制（所有策略共享）**：

- **入场过滤（基类不设方向性门控，各策略自管）**：参考 freqtrade 分层——全局层只负责风控与执行（仓位/止损/摊低/执行模型），方向性过滤属于策略 alpha，标的适配交给 stock_filter 筛选层。trend 策略在 `_trend_filters_ok()` 中自带趋势过滤：
  1. **均线趋势**：EMA(ema_fast) > EMA(ema_slow)，只做多头排列。
  2. **MACD 多头**：macd(DIF) > 0（零轴上方）且 macd > macdsignal（金叉状态），且 DIF 持续上行 `macd_momentum_bars` 根、macdhist 柱持续放大（动量增强）。指标属性命名对齐 freqtrade：macd / macdsignal / macdhist / adx / plus_di / minus_di。
  3. **波动率**：ATR/收盘价 > min_volatility_ratio，过滤横盘假突破。
  4. **ADX 趋势强度**：ADX ≥ adx_min（低于阈值视为横盘震荡直接不开仓，>25 强趋势），且 +DI > −DI（多头方向）。
- **亏损侧放宽 + 摊低加仓（低价标的适用）**：
  - 最差止损地板：`loss_floor = 持仓均价 × (1 − max_loss_stop_ratio)`（默认 30%），初始止损即地板；启用时追踪止损只在盈利侧（candidate > 均价）ratchet，收盘 ≤ 地板才触发止损（原因标注 `max_loss_stop`）。
  - 摊低加仓：首次收盘亏损 ≥ `average_down_drop`（默认 10%）时，按当前持仓 × `average_down_ratio`（默认 1/4）挂次日限价单，每笔交易仅一次；加仓成交后地板随新加权均价下移，盈利侧追踪止损不受影响。
- **ATR 固定风险仓位**：`size = int(总资产 × max_risk_ratio / (ATR × trail_atr_multiple))`，单笔风险不超过总资金的 `max_risk_ratio`。
- **纯动态追踪止损（默认唯一出场，无固定止盈）**：
  - 基础追踪：`stop = 持仓以来最高价 - trail_atr_multiple × ATR`，只上不下（ratchet）。
  - 动态止盈：浮盈（最高价 − 入场价）达到 `trail_tighten_profit_multiple × ATR` 后，止损倍数收紧为 `trail_tight_atr_multiple`，锁定利润但不封顶上行，全程跟随趋势吃满波段。
  - 固定止盈 `take_profit_atr_multiple` 默认 None 关闭，仅显式配置时启用（优先级高于追踪止损）。
- **全局风控保护（对齐 freqtrade Protections，参数名同名/同口径，默认全关）**：`cooldown_period_candles`（CooldownPeriod，卖出后 N 根 K 线冷却）、`stoploss_guard_trade_limit`/`stoploss_guard_lookback_period_candles`/`stoploss_guard_stop_duration_candles`（StoplossGuard，回看窗口内止损达上限即暂停开仓）、`max_allowed_drawdown`（MaxDrawdown，净值回撤超阈值期间禁止开仓）；只拦开仓、不拦平仓，仅基于已成交事实，无方向性判断。
- **策略专属信号止损**：子类 `populate_exit_trend()` 中定义（如动量转负、均线死叉等）。
- **统一输出接口**：`get_equity_dataframe()`、`get_trade_dataframe()`、`get_action_dataframe()`。

**策略清单（专属开仓信号 / 专属平仓条件）**：

| 策略 | 入场过滤 | 专属开仓信号 | 专属平仓条件（叠加追踪止损） |
|------|------|----------|------------------------------|
| trend | 策略专属趋势过滤（EMA/MACD/波动率/ADX） | 过滤通过 **且** Momentum>0 | Momentum < 0 |
| range | 无 | close ≤ bb_lowerband **或** 跌幅 > drop_ratio | close > bb_upperband |
| breakout | 无 | close > donchian_upper（唐奇安上轨突破） | close < donchian_lower（跌破下轨） |

> 3 个策略均继承 BaseStrategy，追踪止损、动态止盈、ATR 仓位与日志接口由基类统一提供，子类只需实现 `populate_indicators` / `populate_entry_trend` / `populate_exit_trend` 三个钩子（命名对齐 freqtrade）。基类不设方向性入场门控；趋势过滤参数（ema_*/macd_*/min_volatility_ratio/adx_*）为 trend 专属，range/breakout 不持有。每只标的由 `stock_filter.classify_regime` 打 regime 标签（trend/range/breakout），路由到对应策略（param_optimize 经 `routed_strategies`、main.py 经 `strategy_for_code`、live_trading 经 `routed_strategies` 收窄共识策略集）。

### 4.2 数据层 / 筛选层 / 公共工具层

**dataprovider.py（数据层，`DataProvider` 类）**：只负责 Tushare 拉取、增量更新、CSV 缓存、字段标准化（`datetime/open/high/low/close/volume/preclose/amount/turn/pctChg`，前复权），不含任何交易逻辑；AStockData 继承 bt.feeds.PandasData。板块指数行情：`fetch_index(index_code)` / `load_cached_index(index_code)` 走 Tushare `index_daily`（仅保证 close/pre_close/pct_chg/vol/amount，主题指数常缺 open/high/low），缓存 `cache/index_cache/{code}.csv`，与个股同策略（当日缓存命中不调 API，否则增量追加）。

**stock_filter.py（股票池前置筛选层）**：只读本地缓存，输出 `output/stock_filter.csv`。

- Pairlist Filters 质量门（常驻，概念对齐 freqtrade；阈值 config `stock_filter.pairlist`）：
  - `age_filter.min_days_listed`（AgeFilter，窗口内交易日数下限）
  - `price_filter.low_price`（PriceFilter，均价下限）
  - `volume_filter.lookback_days` / `volume_filter.min_avg_amount_yi`（VolumeFilter，0=全窗口；日均成交额，亿元）
  - `turnover_filter.min_avg_turn`（A 股扩展：日均换手率，默认 0.2%）
  - `turnover_filter.megacap_min_circ_mv_yi` / `megacap_min_avg_turn`（大盘股降档：流通市值≥1000 亿时换手率下限降至 0.1%；市值取 daily_basic 官方 `circ_mv` 近 60 日均值（万元），旧缓存无列时回退 amount/(turn/100) 推导；阈值设 0 关闭）
  - `liquidity_filter.max_zero_volume_ratio` / `max_gap_days`（A 股扩展：零成交占比 / 最长停牌缺口）
  - 常驻名称规则：ST/*ST/退 名称剔除（自定义过滤器，freqtrade 无内建对应）
- trend 趋势门（仅 --screen）：ADX 均值/强趋势占比/多头排列占比/均线年交叉/Kaufman 效率比/价格振幅/最长多头连涨；阈值 config `stock_filter.trend`
- regime 分类（常驻）：breakout（振幅≥0.8 且 ADX≥22）> trend（ADX≥22、效率≥0.04、连涨≥150）> range；阈值 config `stock_filter.regime`
- **regime 写回 config**：筛选后 `write_regime_to_config(filter_df)` 把每只标的 regime 文本级写入 config.yaml `stock_list` 对应条目（regex 块内插入/更新 `regime:` 行，保留注释，禁止 yaml.dump 全量重写）；stock_filter.py CLI 与 skyquant.py all 筛选步骤均执行
- 消费接口：`load_regime_map()` / `routed_strategies(code, map, active)` / `strategy_for_code(code, map, pool, default)`；筛选报告缺失时安全降级（不剔除、不路由）

**comm.py（公共工具层）**：路径常量唯一来源（PROJECT_ROOT/CACHE_DIR/STOCK_CACHE_DIR/OUTPUT_DIR/EQUITY_DIR/PLOT_DIR/LOG_FILE/CONFIG_PATH，Path 锚定不依赖 CWD）、`setup_logging`、`apply_blacklist`、`build_commission`、`apply_broker_settings`（setcash + 佣金 + 可选 slippage_perc）；AStockCommission 费率：买入=佣金+过户费，卖出=佣金+过户费+印花税。

### 4.3 main\.py 回测引擎

负责：加载配置、遍历标的、执行回测、收集净值与交易、调用指标、生成自包含 HTML 报表；隔离 Cerebro 细节。

**策略选择**：`--strategy` 显式指定时全部标的用该策略；默认 None 时经 `strategy_for_code` 按 regime 自动路由（regime 标签 → config 已配策略 → 默认 trend）。

**统一 broker 装配**：`comm.apply_broker_settings(broker, cfg, initial_capital, build_commission(cfg))`，回测/信号/复盘/寻优子进程同一资金、佣金、滑点口径；config `stock_blacklist` 在标的遍历时过滤。

**所有策略强制统一接口**：

- `get_equity_dataframe()` 输出每日净值

- `get_trade_dataframe()` 输出每笔交易

- `get_action_dataframe()` 输出决策时信号（含未平仓 BUY）

- `notify_trade()` 捕获平仓记录

**Cerebro analyzer 注册**（main.py 专有；其他模块复用同一 Cerebro 模式但不挂 analyzer）：

```python
cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="tradeanalyzer")
cerebro.addanalyzer(bt.analyzers.SQN, _name="sqn")
```

回测后通过 `getattr(strategy_instance.analyzers, name).get_analysis()` 提取结果，传入 `render_report` 的 `analyzer_results` 参数。

**命令行参数**：`--force_refresh`、`--strategy`（默认自动路由）、`--stock-list`（`--interactive` 已移除，HTML 报表与 K 线图默认生成）。

### 4.4 report\.py 指标体系（标准量化定义）

|指标|计算规则|
|---|---|
|总收益率|期末净值 / 初始资金 − 1|
|年化收益率|`(1 + total_return) ** (365/days) - 1`|
|最大回撤|`(equity - cummax) / cummax` 的最小值|
|夏普比率|sqrt(252) 年化，无风险利率2%，超额收益均值/标准差|
|卡玛比率|年化收益 / abs(最大回撤)|
|胜率|盈利交易数 / 总交易数|
|盈亏比|总盈利金额 / 总亏损金额|
|Sortino|sqrt(252)×日均收益 / 负收益日标准差（仅下行波动）|
|期望值/比率|win_rate×均盈 − loss_rate×均亏；比率 = 期望值/均亏（freqtrade 口径）|
|最佳/最差交易|单笔净盈亏极值|
|持仓时长|入场→平仓日历日均值（全/盈/亏分组）|
|连胜/连亏|按平仓顺序的最长连续盈利/亏损笔数|
|日度统计|best/worst_day、盈利/亏损/平盘交易日数|
|market change / alpha|Buy & Hold 收益（需传入收盘价序列）；alpha = 策略总收益 − B&H|
|总交易次数|全部平仓交易统计|

`calc_equity_metrics(equity_df)` 只依赖净值曲线（寻优 worker 用）；`calc_metrics(equity_df, trades_df, price_series=None)` 追加交易类/日度/B&H 基准指标（main.py 报表用）。回测与寻优指标同源同口径。`summarize_exit_reasons(trades_df)` 将原始 exit_reason 归一为 stop_loss/trailing_stop/take_profit/exit_signal 四类并聚合（freqtrade EXIT REASON STATS）。

### 4.5 report\.py 可视化规范

每标的每策略输出两个 HTML 文件至 `output/plots/`：

1. `{code}_{strategy_id}_report.html` — 自包含 HTML 回测报表（plotly.js 内联，可离线打开），区块顺序：头部 → 板块指数 → 8 张 KPI 卡片 → Equity Curve & Drawdown → Strategy Summary → Exit Reason → Monthly Returns → Signals & Fills → Analyzer → K 线图链接：
   - 头部：标的名/代码、策略 id（格式 `名称 (代码) — Strategy: 策略`，不含板块；板块在「板块指数」区块展示）、回测区间、初始资金、最终净值、总收益率
   - 板块指数 (Sector Index) 区块（位于最前、KPI 卡片之上）：板块/指数/代码/最新日期/最新点位/当日涨跌/回测窗口涨跌/近 20 交易日涨跌/标的窗口涨跌/相对强弱（标的−板块，涨红跌绿）；标的未配置 sector_index 或指数数据不可用时该区块跳过或降级为提示
   - 8 张 KPI 卡片：年化收益、最大回撤、Sharpe、Sortino、Calmar、胜率、盈亏比、总交易数
   - 净值曲线 \+ 回撤联动图（Plotly 暗色主题，与交互 K 线图同一配色体系：#0F1419/#141C28 背景、#4FC3F7 净值线、#F38181 回撤填充；共享 x 轴、x-unified hover、1M/3M/6M/1Y/All 按钮、周末 rangebreak）。净值行有琥珀色虚线 **Initial Capital 初始资金水平线**（含金额标注）；**净值 trace 不用 fill-to-zero**（该填充会强制 y 轴包含 0、曲线被压扁），y 轴紧贴净值+本金数据自动取范围使曲线撑满窗口；hline 形状不参与 autorange，本金线另用一条透明 trace 锚定范围。回撤行仍 fill-to-zero（相对 0 填充正确）
   - Strategy Summary 表：Market Regime 行 + 仅列 KPI 卡片未展示的指标（胜负平笔数、期望值、最佳/最差交易、持仓时长、连胜连亏、日度统计、B&H 与 alpha；年化/回撤/Sharpe/Sortino/Calmar/胜率/盈亏比/总交易数不在此重复）
   - Exit Reason Stats 表：四类平仓原因的笔数/胜率/均持时/盈亏聚合
   - Monthly Returns 月度收益热力表（年×12 月，正绿负红）
   - Signals &amp; Fills 统一表（HTML table，**按信号日倒序，最新在最上**）：每个信号一行，同时列触发侧（信号日/触发收盘价/拟下单手数/reason）与成交侧（状态/成交日/成交价/成交量/持仓均价/净盈亏），BUY 绿 SELL 红，含未平仓 BUY
   - Analyzer 字段表（递归压平 5 个 analyzer 的 namedtuple/dict/list）
   - Plotly K 线交互图相对链接

2. `{code}_{strategy_id}_interactive.html` — freqtrade 风格 Plotly 交互图（plotly.js 内联，可离线打开）：
   - K 线主图：红涨绿跌实心蜡烛；策略指标覆盖层（trend: EMA20/60；range: BB upper/mid/lower；breakout: Donchian upper/lower，legendgroup=`indicators`）；板块指数收盘线以副轴（secondary_y）叠加于主图（灰色细线，按股票交易日 reindex+ffill 对齐，指数数据缺失时不叠加）
   - **实际成交标记（legendgroup=`fills`，大实心三角+白色描边，最显著）**：亮蓝上三角=FILLED entry 成交买入，深绿/红下三角=FILLED exit 成交卖出（盈/亏，hover 显示成交价、净盈亏、exit 类别），琥珀三角=FILLED open position 持仓中；entry→exit 虚线连接每笔已平仓交易
   - **触发信号标记（legendgroup=`signals`，小空心三角、半透明，来自 action_log 全部信号含 EXPIRED 未成交）**：BUY 信号在 K 线低点下方（filled 蓝/expired 灰/pending 紫），SELL 信号在高点上方（filled 橙/expired 灰/pending 紫）；hover 显示信号日、触发收盘价、拟下单手数、成交回填信息、reason。每个 side×status 组合是独立 trace，图例点击可单独开关，便于分析信号组合与成交漏单
   - **技术交叉信号（legendgroup=`tech`，纯展示不回灌策略）**：仅 **MACD 金叉（绿圆点，K 线低点下方 1%）/ MACD 死叉（橙叉，高点上方 1%）**，标注在 K 线主图（row 1）。EMA 金叉/死叉不标注——EMA 线本身已直观呈现多空排列，额外标记冗余
   - 子图：成交量（红涨绿跌柱）、ATR14、**MACD（所有策略；trend 用指标线 macd/macdsignal/macdhist，range/breakout 从收盘价按 12/26/9 现算，仅绘图）**；trend 再追加 ADX（plus_di/minus_di/adx_min 阈值）
   - 图例分四组（**指标**=EMA/BB/Donchian/板块指数/EMA ref，全为线；**成交**=连线·盈亏线 + 买卖三角/持仓中点；**触发信号**=side×status 空心三角点；**技术交叉**=MACD 金死叉点），水平排列于**图表上方 margin 区**（`yref=container`，不遮挡 K 线）。**图例排列原则：线状 trace 全部集中在前、点状 trace 集中在后，功能组内也是线先点后**——通过控制 add_trace 顺序实现（Plotly 图例按 trace 添加顺序渲染）；短中文名，单击开关、双击隔离；1M/3M/6M/1Y/All 按钮在其下方
   - **freqtrade 风格十字线**（x/y axis `showspikes + spikemode=across + spikesnap=cursor`）：鼠标移动时全高点线贯穿各子图定位，unified hover tooltip 用半透明深色背景 + 小字号（10px）减轻遮挡
   - 周末跳过、scrollZoom、暗色主题

> 旧版 PNG 产物（`_equity_dd.png` / `_win_pie.png`）与 `plot_all` / `plot_equity_drawdown` / `plot_win_pie` / `_setup_chinese_font` 已全部移除。

### 4\.6 opt\_pipeline 参数优化规范

- param\_optimize：全网格暴力搜索。目标标的解析优先级 `--stock-list` > `--all-stocks` > 回归标的集（默认），由 `common.resolve_target_codes` 统一实现（末尾应用 `stock_blacklist`）；不在 config.yaml `stock_list` 中的代码直接报错。每标的按 `routed_strategies` 只寻优 regime 路由到的策略；网格结果输出多指标列（final_capital/profit/profit_rate/sharpe_ratio/max_drawdown/calmar_ratio），按 config `opt_pipeline.optimize_metric`（profit_rate/sharpe/calmar）对应的 GRID 目标列降序。

- out\_sample\_verify：剔除训练过拟合（训练/测试拆分）。按 `opt_pipeline.out_sample_train_end` 切分数据为训练段 / 测试段，分别回测，若训练收益率 − 测试收益率 > `opt_pipeline.out_sample_overfit_threshold` 则判为过拟合剔除。当训练段或测试段 K 线数 < 60（SMA60 最小周期下限）时跳过该标的并打印 `[ERROR]` 提示与改进方法。

- rolling\_window\_verify：多窗口稳定性筛选。按 `opt_pipeline.rolling_start_year` 起、`rolling_train_years` + `rolling_test_years` 长度滚动生成多个年度对齐窗口，对每个测试段回测并聚合平均 profit_rate / sharpe / calmar / drawdown（输出列 avg_test_profit / avg_test_sharpe / avg_test_calmar / avg_test_drawdown / valid），平均收益 > 0 视为稳定。窗口同时满足 `rolling_min_train_bars` / `rolling_min_test_bars` 才被采纳。若某标的在所有窗口中均不满足最低 K 线数，跳过并打印 `[ERROR]` 提示与改进方法；若全部标的被跳过、结果表为空，额外打印整体失败提示。

- aggregate\_best\_param：每标的每策略保留一组最优稳定参数；排序列由 `optimize_metric` 决定（profit_rate→avg_test_profit、sharpe→avg_test_sharpe、calmar→avg_test_calmar），仍以 avg_test_profit > 0 作为 recommend_use 门槛。

- write\_param\_to\_config：自动落地到 config\.yaml（仅更新传入标的的 `strategy_params` 条目；yaml.dump 整体重写，会清除 config 注释）。

**阶段脚本通用约定**：五个阶段脚本均支持 `--stock-list` 过滤；三个重计算阶段（param\_optimize / out\_sample\_verify / rolling\_window\_verify）另支持 `--maxcpu`（默认 0=全部 CPU），按标的建一次多进程 Pool，行情切片经 Pool initializer 每个 worker 只传一次，回测任务（strategy\_id + params）多进程并行（`--maxcpu 1` 走进程内串行，结果与多进程逐位一致）。阶段 CSV 统一经 `common.write_stage_csv` 写出——带 `--stock-list` 时按标的合并写（替换该标的旧行、保留其他标的行，即使该标的本次无合格行也会清除其旧行），不带时整体重写。

**滚动校验配置项**（config\.yaml 的 `opt_pipeline` 段，均为年度对齐窗口参数）：

| 键 | 默认值 | 含义 |
|------|--------|------|
| out\_sample\_train\_end | '2024-12-31' | 外样本训练/测试切分日期 |
| out\_sample\_overfit\_threshold | 0.15 | 训练−测试收益率差值阈值 |
| rolling\_start\_year | 2020 | 首个滚动窗口起点年份 |
| rolling\_train\_years | 4 | 训练窗口长度（年） |
| rolling\_test\_years | 1 | 测试窗口长度（年） |
| rolling\_min\_train\_bars | 200 | 训练段最低 K 线数 |
| rolling\_min\_test\_bars | 100 | 测试段最低 K 线数（>60，不可低于 SMA60 minperiod） |
| optimize\_metric | profit_rate | 寻优/聚合排序目标：profit_rate / sharpe / calmar |

**start\_date 下限约束**（end\_date=2026-09-21、默认滚动参数）：

- 至少 1 个有效滚动窗口（训练段 > 200 根）：start\_date ≤ 2024-03-04

- 推荐（训练段 242 根 + 2 个有效窗口）：start\_date = 2024-01-01

- 绝对底线（外样本训练段 ≥ 60 根，但无统计意义）：start\_date ≤ 2024-10-08

### 4.7 live_trading.py 实盘交易模块

实盘唯一入口（`LiveTrading` 类），每日收盘后运行：以 live_trades.csv 的**真实成交**为准，复盘真实交易与策略信号的一致性，并现算每只持仓标的的次日信号，产出自包含 HTML 实盘持仓报告。skyquant.py all 流水线收尾也调用本模块（透传完整目标集）。

**真实成交复盘（成交 VS 信号匹配）**：

- 按【股票代码 + 交易日】匹配策略信号（策略成交经 get_trade_dataframe 还原 BUY/SELL 两日）
- 统计：信号匹配率、实盘胜率、平均盈亏
- 输出 `output/live_trade_review.csv`（config `stock_blacklist` 同步过滤；broker 装配与正式回测同口径）

**持仓与次日信号生成**：

- 读取 live_trades.csv 计算当前持仓（BUY 加权累加 - SELL 扣减，净量 > 0 即为持仓）；**信号范围仅限当前真实持仓标的**
- 读取 output/stock_filter.csv：pairlist_passed=False 的标的跳过；按 regime 经 `routed_strategies` 收窄参与共识的策略集；报告缺失时安全降级为全部 active 策略
- 覆盖 data_provider.end_date 为当天日期（确保增量拉取覆盖今日行情；--force-refresh 可强制全量）
- 对每只持仓标的的每个（路由后的）策略运行回测，提取 action_log（决策时信号日志）
- 信号分类：最后一根 bar 触发买入/卖出 → BUY/SELL；已有持仓无新信号 → HOLD；无持仓无信号 → WAIT

**多策略共识**：防御性优先级 SELL > BUY > HOLD > WAIT（任一策略发出 SELL 即覆盖）

**操作建议**：

- 持仓 + SELL → 卖出
- 持仓 + BUY → 加仓（BUY_MORE）
- 持仓 + HOLD/WAIT → 持有
- 未持仓 + BUY → 买入（新开仓）
- 未持仓 + 其他 → 观望

**HTML 实盘持仓报告**（`output/plots/live_portfolio_report.html`）：账户 KPI（标的数/投入成本/市值/浮动盈亏/信号分布）、持仓总览与建议操作表、逐标的卡片（真实成交明细、策略最新信号、成交-信号匹配率、策略历史回测参考含 Sharpe/Calmar 中文评级、要点解释与下一步信号）。

**输出**：`output/live_trade_review.csv` + `output/live_signal_{YYYYMMDD}.csv` + `output/plots/live_portfolio_report.html` + 控制台摘要。

---

## 5\. config\.yaml 配置规范

全局唯一配置入口，所有参数不写死代码。

- global\_setting：资金、回测时间区间、`slippage_perc` 百分比滑点（0.0 关闭）

- stock\_blacklist：黑名单，覆盖 stock\_list 白名单（回测/信号/复盘/寻优统一过滤）

- commission\_config：完整A股交易费率

- stock\_filter：前置筛选阈值（stock_filter.pairlist 五组 Pairlist Filters / trend 趋势门 / regime 分类），详见 4.2 节

- opt\_pipeline：外样本校验与滚动窗口校验的可调参数（切分日期、过拟合阈值、窗口长度、最低 K 线数、`optimize_metric` 排序目标），详见 4.6 节

- tushare 密钥独立存放于用户目录 `~/.skyquant/tushare.yaml`（不随项目入库），config.yaml 中不包含 token

- stock\_list：回测标的池（白名单）

- strategy\_params：流水线自动更新的最优参数结果（yaml.dump 重写会清除注释，阈值默认值以 stock_filter.py / 策略类代码为准）

---

## 6\. 输出文件标准

所有输出路径固定、命名规则固定、用途固定。

- stock\_filter\.csv：股票池前置筛选报告（pairlist/trend 指标与通过标记 pairlist_passed/passed、regime 标签、失败原因），驱动策略路由

- metrics\_summary\.csv：汇总所有标的策略绩效

- param\_optimize\_result\.csv / out\_sample\_verify\_result\.csv / rolling\_verify\.csv / aggregate\_common\_param\.csv：寻优流水线各阶段产物（按标的合并写）

- equity\_curve/\*\.csv：每日净值序列

- plots/\*\_report\.html：自包含 HTML 回测报表（KPI/净值-回撤图/交易表/Analyzer）

- plots/\*\_interactive\.html：Plotly freqtrade 风格 K 线 \+ 成交量 \+ 指标子图 \+ 成交标记交互图

- plots/live\_portfolio\_report\.html：实盘持仓 HTML 复盘报告（账户 KPI、逐标的真实成交/最新信号/匹配率/回测参考/要点解释）

- live\_trade\_review\.csv：实盘成交 VS 策略信号匹配复盘

- live\_signal\_\*\.csv：实盘持仓信号报告（按日期生成，含每持仓标的每策略信号、共识、操作建议）

---

## 7\. 异常处理规范

- 流水线步骤失败 → 整体终止并记录日志

- 单标的数据缺失 → 跳过当前标的，不中断整体

- 无交易记录 → 指标正常兜底，不报错

- 绘图异常 → 静默跳过，不打断回测

---

## 8\. 依赖清单

```Plain Text
backtrader
pandas
numpy
pyyaml
plotly
tushare
scipy
```

---

## 9\. 风险提示

> 本章说明系统内建机制与回测方法论的风险边界。使用回测结果指导实盘前，必须理解以下各项。

### 9.1 亏损侧放宽机制的尾部风险

- **单笔最差亏损显著放大**：`max_loss_stop_ratio=0.30` 启用时，初始止损为均价 ×(1−30%) 的地板价。按 ATR 仓位公式，常规参数下仓位敞口约为账户 50%，触发地板的单笔亏损可达 **账户 −15% 左右**；历史上紧 ATR 止损下单笔亏损约为账户 −2.5%。
- **摊低加仓进一步加深尾部**：亏损 10% 时按当前持仓 1/4 加仓，敞口升至约 62%，若地板触发，单笔亏损可接近 **账户 −18%**。
- **机制假设风险**：摊低加仓本质是均值回归假设（跌了会反弹）。在单边阴跌行情中该假设失效——地板上移不回、加仓买在半山腰。该机制设计初衷是应对低价标的的高频假突破止损，不是普适的亏损补救手段。

### 9.2 撮合模型与实盘偏差

- **限价单未成交即放弃**：买入信号挂次日限价单（信号日收盘价），未成交自动放弃。回测中这是"可放弃的择时"，实盘中若信号持续、标的连续跳空，可能长期无法上车，实盘建仓节奏与回测不可比。
- **卖出为市价（次日开盘）**：跳空低开时实际卖出价劣于回测记录价；涨跌停、停牌日无法成交，回测未单独建模极端流动性缺失。
- **信号价 ≠ 成交价**：决策基于 T 日收盘，成交在 T+1。跳空幅度大的标的（如节后行情）实际成本可能显著偏离信号价，Action Log 的 Reason/触发价仅用于时机分析，盈亏以实际成交为准。

### 9.3 参数寻优与过拟合

- 网格寻优在历史数据上选择最优参数，外样本校验与滚动窗口校验只能**缓解**过拟合，不能消除。历史最优参数在未来继续有效的假设没有保证。
- 校验样本（尤其滚动窗口数量受数据长度限制）偏少时，"通过校验"的证据强度有限，应结合参数邻域稳定性（相邻参数表现是否接近）综合判断。

### 9.4 策略结构性约束

- **只做多、无对冲**：单边多头策略在系统性下跌中只能减仓避险或承受回撤，无盈利来源。
- **T+1 与涨跌停**：A股 T+1 制度下当日买入不可卖出；涨跌停板会阻塞成交，回测的价格连续性假设在极端行情下不成立。
- **标的价格区间偏依赖**：机制参数针对 20 元以下低价标的校准（ATR 占价比高、假突破频繁），直接套用到高价标的前需重新寻优验证。

### 9.5 回测方法论局限

- **历史不代表未来**：所有指标（年化、夏普、胜率、回撤）均为历史统计量，不构成收益承诺。
- **数据口径**：前复权价格、Tushare 数据质量（复权因子调整、停牌填充）直接影响结果；缓存数据未及时增量更新时信号日期可能滞后。
- **回归基线只保证行为一致性**：golden 基线验证"改动是否改变行为"，不验证"行为是否最优"。

---

## 10\. 迭代路线图

- **V1\.0（当前）**：完整流水线、指标、绘图、日志、实盘成交复盘、次日信号与 HTML 实盘持仓报告

- **V1\.1**：新增卡玛比率、最大连续亏损、波动率

- **V1\.2**：参数热力图、多策略对比图

- **V1\.3**：多进程并行回测加速

- **V1\.4**：简易Web可视化面板

> （注：部分内容可能由 AI 生成）
