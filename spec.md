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

一站式完成：行情拉取 → 缓存管理 → 股票池前置筛选（质量门/趋势门/regime 分类路由）→ 网格参数寻优 → 过拟合剔除 → 滚动稳定性校验 → 最优参数聚合 → 自动写入配置 → 策略回测 → 指标计算 → 可视化绘图 → 日志留存 → 手工交易复盘。

系统按层解耦：配置层 config.yaml → 数据层 data_source.py → 筛选层 stock_filter.py → 策略插件层 strategy/ → 回测引擎层 main.py → 指标报表层 report.py → 寻优层 opt_pipeline/ → 入口 run_all.py；公共能力（手续费、路径、日志、broker 装配）收敛于 comm.py。策略只负责信号规则，不读文件、不拉数据、不筛选标的、不绘图；新增策略只需新增文件并注册 STRATEGY_MAPPING。

### 1\.2 核心能力

- Tushare 日线行情自动拉取 \+ 增量更新 \+ 本地缓存

- 完整 A 股交易成本模型（佣金、印花税、过户费）+ 可选百分比滑点（config 开关，回测/寻优同口径）

- 股票池前置筛选：basic 质量门（停牌/流动性/低价/ST，常驻）+ trend 趋势门（--screen）+ regime 自动路由（trend/range/breakout）

- 多策略、多标的网格参数遍历寻优（按 regime 只寻优路由策略；profit_rate/sharpe/calmar 三种优化目标可配）

- 双层过拟合校验：外样本验证 \+ 滚动窗口稳定性验证

- 自动参数筛选、聚合、写入配置文件

- 专业量化指标体系：年化收益、最大回撤、夏普比率、卡玛比率、胜率、盈亏比

- 自动可视化：自包含 HTML 回测报表（KPI 卡片、净值-回撤联动图、交易表、Analyzer 字段表）、btplotting K 线交互图

- 全局日志系统：控制台 \+ 文件双输出

- 手工实盘交易 VS 策略信号自动复盘匹配

- 一键全流程启动脚本，支持缓存加速模式

- 每日收盘后信号生成：自动运行策略，输出买卖/持有信号及次日操作建议

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
├── run_all.py                 # 一键全流水线入口（编排层）
├── main.py                    # 回测引擎层：Cerebro 封装/标的遍历/regime 自动路由
├── daily_signal.py            # 每日信号生成
├── data_source.py             # 数据层：行情拉取与缓存
├── stock_filter.py            # 股票池前置筛选层：basic 质量门 + trend 趋势门 + regime 分类
├── comm.py                    # 公共工具层：手续费/路径常量/日志/broker 装配/黑名单
├── report.py                  # 指标与报表层：指标计算 + HTML 报表 + K 线图
├── manual_trade_review.py     # 手工交易复盘模块
├── config.yaml                # 全局配置文件（全部可调参数外置）
├── manual_trades.csv          # 实盘手工交易记录
├── cache/
│   └── stock_cache/           # 股票K线缓存CSV
├── output/
│   ├── run.log                # 全流程运行日志（自动生成）
│   ├── stock_filter.csv       # 前置筛选报告（basic/trend/regime，驱动策略路由）
│   ├── equity_curve/         # 每标的每日净值序列
│   ├── plots/                 # 每标的 HTML 回测报表与 btplotting K 线图
│   ├── metrics_summary.csv    # 指标汇总表
│   ├── param_optimize_result.csv
│   ├── out_sample_verify_result.csv
│   ├── rolling_verify.csv
│   ├── aggregate_common_param.csv
│   ├── manual_review_result.csv
│   └── daily_signal_*.csv       # 每日信号报告（按日期生成）
├── strategy/
│   ├── __init__.py
│   ├── base.py
│   ├── trend.py            # 趋势策略（合并 trend_follow+momentum）
│   ├── range.py            # 震荡策略（合并 boll_ma+short_reversal）
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
   1. basic 质量门：K 线数/零成交量/缺口/成交额/换手率/均价/ST，未过者剔除
   2. trend 趋势门：仅 `--screen` 时强制（ADX/EMA/效率比等 7 项）
   3. regime 分类：trend/range/breakout 标签（不剔除，驱动策略路由）

3. 对每个通过 basic（--screen 时 basic+trend）的标的依次执行完整寻优链（单标的闭环后再处理下一个标的；按 regime 只寻优路由到的策略）：
   1. 网格参数优化遍历（profit_rate/sharpe/calmar 多指标，按 optimize_metric 排序）
   2. 外样本校验（剔除训练集过拟合）
   3. 滚动窗口稳定性校验（防止偶然收益）
   4. 聚合该标的每策略最优稳定参数
   5. 自动写入 config\.yaml 策略参数（仅触及该标的的条目）

4. 正式回测（main.py 不带 --strategy，按 regime 自动路由）、指标计算、绘图（全部标的批量）

5. 手工交易复盘匹配与统计（批量）

**目标标的集合**（寻优/回测/复盘共用同一集合）：

- 默认：回归标的集（`REGRESSION_STOCKS = ["000725"]`，定义在 opt\_pipeline/common\.py，与 tests/regression 同源）——每次修改参数后的快速迭代门槛
- `--all-stocks`：手动触发全量标的池
- `--stock-list`：显式指定子集

阶段 CSV（param\_optimize\_result / out\_sample\_verify\_result / rolling\_verify / aggregate\_common\_param）按标的合并写：重跑某标的只替换该标的的行，其余标的行保留；不带 `--stock-list`（批量模式）时整体重写。

运行模式：

- `python run_all.py` 回归标的集流水线（默认）

- `python run_all.py --all-stocks` 完整全量流水线（手动触发）

- `python run_all.py --skip-data`缓存加速流水线

每日信号生成（独立运行，不在 run_all.py 流水线中）：

- `python daily_signal.py` 每日收盘后运行，输出买卖信号与次日操作建议

---

## 4\. 模块详细规范

### 4\.1 run\_all\.py 全局调度模块

**唯一职责**：流程调度、日志管理、异常终止。

**编排顺序**：行情拉取/缓存 → 股票池前置筛选（basic 常驻、--screen 追加 trend、regime 分类）→ 逐标的寻优 5 阶段闭环 → 批量回测（regime 自动路由）→ 手工复盘。

**日志规范**：

- 同时输出：控制台 \+ output/run\.log（comm.setup_logging 统一初始化）

- 每次运行覆盖日志

- 子进程报错立即终止流水线

### 4.1.1 strategy/ 策略模块规范

**统一机制（所有策略共享）**：

- **入场过滤（基类不设方向性门控，各策略自管）**：参考 freqtrade 分层——全局层只负责风控与执行（仓位/止损/摊低/执行模型），方向性过滤属于策略 alpha，标的适配交给 stock_filter 筛选层。trend 策略在 `_trend_filters_ok()` 中自带趋势过滤：
  1. **均线趋势**：EMA(sma_fast) > EMA(sma_slow)，只做多头排列。
  2. **MACD 多头**：DIF > 0（零轴上方）且 DIF > DEA（金叉状态），且 DIF 持续上行 `macd_momentum_bars` 根、MACD 柱持续放大（动量增强）。
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
- **全局风控保护（对齐 freqtrade Protections，默认全关）**：`cooldown_bars`（卖出后 N bar 冷却）、`stoploss_guard_trade_limit/lookback_bars/pause_bars`（回看窗口内止损达上限即暂停开仓）、`max_drawdown_limit`（净值回撤超阈值期间禁止开仓）；只拦开仓、不拦平仓，仅基于已成交事实，无方向性判断。
- **策略专属信号止损**：子类 `populate_exit()` 中定义（如动量转负、均线死叉等）。
- **统一输出接口**：`get_equity_dataframe()`、`get_trade_dataframe()`、`get_action_dataframe()`。

**策略清单（专属开仓信号 / 专属平仓条件）**：

| 策略 | 入场过滤 | 专属开仓信号 | 专属平仓条件（叠加追踪止损） |
|------|------|----------|------------------------------|
| trend | 策略专属趋势过滤（EMA/MACD/波动率/ADX） | 过滤通过 **且** Momentum>0 | Momentum < 0 |
| range | 无 | close ≤ 布林下轨 **或** 跌幅 > drop_ratio | close > 布林上轨 |
| breakout | 无 | close > 过去 N 日最高价（唐奇安突破） | close < 过去 N 日最低价（跌破下轨） |

> 3 个策略均继承 BaseStrategy，追踪止损、动态止盈、ATR 仓位与日志接口由基类统一提供，子类只需实现 `populate_indicators` / `populate_entry` / `populate_exit` 三个钩子。基类不设方向性入场门控；趋势过滤参数（sma_*/macd_*/min_volatility_ratio/adx_*）为 trend 专属，range/breakout 不持有。每只标的由 `stock_filter.classify_regime` 打 regime 标签（trend/range/breakout），路由到对应策略（param_optimize 经 `routed_strategies`、main.py 经 `strategy_for_code`、daily_signal 经 `routed_strategies` 收窄共识策略集）。

### 4.2 数据层 / 筛选层 / 公共工具层

**data_source.py（数据层）**：只负责 Tushare 拉取、增量更新、CSV 缓存、字段标准化（`datetime/open/high/low/close/volume/preclose/amount/turn/pctChg`，前复权），不含任何交易逻辑；AStockData 继承 bt.feeds.PandasData。

**stock_filter.py（股票池前置筛选层）**：只读本地缓存，输出 `output/stock_filter.csv`。

- basic 质量门（常驻）：min_bars / max_zero_volume_ratio / max_gap_days / min_avg_amount_yi / min_avg_turn / min_mean_close + ST/退市名称剔除；阈值 config `stock_filter.basic`
- trend 趋势门（仅 --screen）：ADX 均值/强趋势占比/多头排列占比/均线年交叉/Kaufman 效率比/价格振幅/最长多头连涨；阈值 config `stock_filter.trend`
- regime 分类（常驻）：breakout（振幅≥0.8 且 ADX≥22）> trend（ADX≥22、效率≥0.04、连涨≥150）> range；阈值 config `stock_filter.regime`
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
|总交易次数|全部平仓交易统计|

`calc_equity_metrics(equity_df)` 只依赖净值曲线（寻优 worker 用）；`calc_metrics(equity_df, trades_df)` 追加交易类指标（main.py 报表用）。回测与寻优指标同源同口径。

### 4.5 report\.py 可视化规范

每标的每策略输出两个 HTML 文件至 `output/plots/`：

1. `{code}_{strategy_id}_report.html` — 自包含 HTML 回测报表（Bokeh INLINE 资源，可离线打开），包含：
   - 头部：标的名/代码、策略 id、回测区间、初始资金、最终净值、总收益率
   - 6 张 KPI 卡片：年化收益、最大回撤、夏普比率、胜率、盈亏比、总交易数
   - 净值曲线 \+ 回撤联动图（Bokeh，共享 x 轴，hover tooltip）
   - 平仓交易 DataTable（Bokeh，含 NumberFormatter 金额/百分比格式）
   - action\_log 信号表（HTML table，BUY 绿/SELL 红，含未平仓 BUY）
   - Analyzer 字段表（递归压平 5 个 analyzer 的 namedtuple/dict/list）
   - btplotting K 线图相对链接

2. `{code}_{strategy_id}_interactive.html` — btplotting K 线 \+ 指标 \+ 成交标记交互图

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

### 4.7 手工交易复盘模块

匹配规则：

- 按【股票代码 \+ 交易日】匹配策略信号

- 统计：信号匹配率、手工胜率、平均盈亏

- 输出每日对照复盘表（config `stock_blacklist` 同步过滤；broker 装配与正式回测同口径）

### 4.8 daily_signal.py 每日信号生成模块

每日收盘后独立运行，为 stock_list 中所有标的生成买卖信号。

**信号生成逻辑**：

- 读取 config.yaml 的 stock_list 与 strategy_params，应用 `stock_blacklist`
- 读取 output/stock_filter.csv：basic_passed=False 的标的跳过；按 regime 经 `routed_strategies` 收窄参与共识的策略集；报告缺失时安全降级为全部 active 策略
- 读取 manual_trades.csv 计算当前持仓（BUY 累加 \- SELL 累加，净量 > 0 即为持仓）
- 覆盖 ds.end_date 为当天日期（确保增量拉取覆盖今日行情）
- 对每只标的的每个（路由后的）策略运行回测，提取 action_log（决策时信号日志）
- 信号分类：最后一根 bar 触发买入/卖出 → BUY/SELL；已有持仓无新信号 → HOLD；无持仓无信号 → WAIT

**多策略共识**：

- 防御性优先级：SELL > BUY > HOLD > WAIT（任一策略发出 SELL 即覆盖）

**操作建议**：

- 持仓 \+ SELL → 卖出
- 持仓 \+ BUY → 加仓
- 持仓 \+ HOLD/WAIT → 持有
- 未持仓 \+ BUY → 买入
- 未持仓 \+ 其他 → 等待

**输出**：`output/daily_signal_{YYYYMMDD}.csv` \+ 控制台三段式摘要（持仓操作、关注列表、统计汇总）

---

## 5\. config\.yaml 配置规范

全局唯一配置入口，所有参数不写死代码。

- global\_setting：资金、回测时间区间、`slippage_perc` 百分比滑点（0.0 关闭）

- stock\_blacklist：黑名单，覆盖 stock\_list 白名单（回测/信号/复盘/寻优统一过滤）

- commission\_config：完整A股交易费率

- stock\_filter：前置筛选三层阈值（basic 质量门 / trend 趋势门 / regime 分类），详见 4.2 节

- opt\_pipeline：外样本校验与滚动窗口校验的可调参数（切分日期、过拟合阈值、窗口长度、最低 K 线数、`optimize_metric` 排序目标），详见 4.6 节

- tushare 密钥独立存放于用户目录 `~/.skyquant/tushare.yaml`（不随项目入库），config.yaml 中不包含 token

- stock\_list：回测标的池（白名单）

- strategy\_params：流水线自动更新的最优参数结果（yaml.dump 重写会清除注释，阈值默认值以 stock_filter.py / 策略类代码为准）

---

## 6\. 输出文件标准

所有输出路径固定、命名规则固定、用途固定。

- stock\_filter\.csv：股票池前置筛选报告（basic/trend 指标与通过标记、regime 标签、失败原因），驱动策略路由

- metrics\_summary\.csv：汇总所有标的策略绩效

- param\_optimize\_result\.csv / out\_sample\_verify\_result\.csv / rolling\_verify\.csv / aggregate\_common\_param\.csv：寻优流水线各阶段产物（按标的合并写）

- equity\_curve/\*\.csv：每日净值序列

- plots/\*\_report\.html：自包含 HTML 回测报表（KPI/净值-回撤图/交易表/Analyzer）

- plots/\*\_interactive\.html：btplotting K 线 \+ 指标 \+ 成交标记交互图

- manual\_review\_result\.csv：实盘复盘报告

- daily\_signal\_\*\.csv：每日信号报告（按日期生成，含每标的每策略信号、共识、操作建议）

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
bokeh
btplotting
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

- **V1\.0（当前）**：完整流水线、指标、绘图、日志、复盘、每日信号生成

- **V1\.1**：新增卡玛比率、最大连续亏损、波动率

- **V1\.2**：参数热力图、多策略对比图

- **V1\.3**：多进程并行回测加速

- **V1\.4**：简易Web可视化面板

> （注：部分内容可能由 AI 生成）
