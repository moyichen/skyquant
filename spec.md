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

一站式完成：行情拉取 → 缓存管理 → 网格参数寻优 → 过拟合剔除 → 滚动稳定性校验 → 最优参数聚合 → 自动写入配置 → 策略回测 → 指标计算 → 可视化绘图 → 日志留存 → 手工交易复盘。

### 1\.2 核心能力

- Tushare 日线行情自动拉取 \+ 增量更新 \+ 本地缓存

- 完整 A 股交易成本模型（佣金、印花税、过户费）

- 多策略、多标的网格参数遍历寻优

- 双层过拟合校验：外样本验证 \+ 滚动窗口稳定性验证

- 自动参数筛选、聚合、写入配置文件

- 专业量化指标体系：年化收益、最大回撤、夏普比率、胜率、盈亏比

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
├── run_all.py                 # 一键全流水线入口
├── main.py                    # 回测主引擎
├── daily_signal.py            # 每日信号生成
├── data_source.py             # 行情拉取与缓存
├── metrics_utils.py           # 量化指标计算核心
├── plot_utils.py              # 可视化绘图工具
├── manual_trade_review.py     # 手工交易复盘模块
├── config.yaml                # 全局配置文件
├── manual_trades.csv          # 实盘手工交易记录
├── cache/
│   └── stock_cache/           # 股票K线缓存CSV
├── output/
│   ├── run.log                # 全流程运行日志（自动生成）
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
│   ├── trend_follow.py
│   ├── momentum.py
│   ├── short_reversal.py
│   ├── boll_ma.py
│   └── multi_factor.py
└── opt_pipeline/
    ├── common.py                # 流水线共享基础设施
    ├── param_optimize.py
    ├── out_sample_verify.py
    ├── rolling_window_verify.py
    ├── aggregate_best_param.py
    └── write_param_to_config.py
```

---

## 3\. 流水线执行流程（寻优段按标的循环）

**run\_all\.py 执行顺序**

1. 全量行情拉取 / 跳过缓存（批量，I/O 密集）

2. 对每个目标标的依次执行完整寻优链（单标的闭环后再处理下一个标的）：
   1. 网格参数优化遍历
   2. 外样本校验（剔除训练集过拟合）
   3. 滚动窗口稳定性校验（防止偶然收益）
   4. 聚合该标的每策略最优稳定参数
   5. 自动写入 config\.yaml 策略参数（仅触及该标的的条目）

3. 正式回测、指标计算、绘图（全部标的批量）

4. 手工交易复盘匹配与统计（批量）

**目标标的集合**（寻优/回测/复盘共用同一集合）：

- 默认：回归标的集（`REGRESSION_STOCKS`，5 只，定义在 opt\_pipeline/common\.py）——每次修改参数后的快速迭代门槛
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

**日志规范**：

- 同时输出：控制台 \+ run\.log

- 每次运行覆盖日志

- 子进程报错立即终止流水线

### 4.1.1 strategy/ 策略模块规范

**统一机制（所有策略共享）**：

- **全局三重趋势过滤（开仓门控，基类强制执行，子类无法绕过）**：全部通过才调用子类 `_on_entry()` 判断专属信号。
  1. **均线趋势**：SMA(sma_fast) > SMA(sma_slow)，只做多头排列。
  2. **MACD 多头**：DIF > 0（零轴上方）且 DIF > DEA（金叉状态），且 DIF 持续上行 `macd_momentum_bars` 根、MACD 柱持续放大（动量增强）。
  3. **波动率**：ATR/收盘价 > min_volatility_ratio，过滤横盘假突破。
- **ATR 固定风险仓位**：`size = int(总资产 × max_risk_ratio / (ATR × trail_atr_multiple))`，单笔风险不超过总资金的 `max_risk_ratio`。
- **纯动态追踪止损（默认唯一出场，无固定止盈）**：
  - 基础追踪：`stop = 持仓以来最高价 - trail_atr_multiple × ATR`，只上不下（ratchet）。
  - 动态止盈：浮盈（最高价 − 入场价）达到 `trail_tighten_profit_multiple × ATR` 后，止损倍数收紧为 `trail_tight_atr_multiple`，锁定利润但不封顶上行，全程跟随趋势吃满波段。
  - 固定止盈 `take_profit_atr_multiple` 默认 None 关闭，仅显式配置时启用（优先级高于追踪止损）。
- **策略专属信号止损**：子类 `_on_exit()` 中定义（如动量转负、均线死叉等）。
- **统一输出接口**：`get_equity_dataframe()`、`get_trade_dataframe()`、`get_action_dataframe()`。

**策略清单（专属开仓信号 / 专属平仓条件）**：

| 策略 | 专属开仓信号（三重过滤通过后才判断） | 专属平仓条件（叠加追踪止损） |
|------|----------|------------------------------|
| trend_follow | 无（三重过滤通过即开仓，纯趋势跟随基线策略） | 无（仅追踪止损/动态止盈） |
| momentum | Momentum(period) > 0 | Momentum < 0 |
| short_reversal | (preclose−close)/preclose > drop_ratio | 无 |
| boll_ma | close ≤ 布林下轨 | close > 布林上轨 |
| multi_factor | pctChg > −5 | SMA(20) < SMA(60) |

> 全部 5 个策略（含 trend_follow）均继承 BaseStrategy，全局三重过滤、追踪止损、动态止盈、ATR 仓位与日志接口由基类统一提供，子类只需实现 `_init_indicators` / `_on_entry` / `_on_exit` 三个钩子。trend_follow 是所有策略的"最低要求基线"。

### 4.2 main\.py 回测主引擎

负责：加载配置、遍历标的、执行回测、收集净值与交易、调用指标、生成自包含 HTML 报表。

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

**命令行参数**：`--force_refresh`、`--strategy`、`--stock-list`（`--interactive` 已移除，HTML 报表与 K 线图默认生成）。

### 4.3 metrics\_utils\.py 指标体系（标准量化定义）

|指标|计算规则|
|---|---|
|年化收益率|基于回测首日/末日净值时间加权年化|
|最大回撤|历史最高净值至后续最低净值的最大跌幅|
|夏普比率|252日年化，无风险利率2%，超额收益均值/标准差|
|胜率|盈利交易数 / 总交易数|
|盈亏比|总盈利金额 / 总亏损金额|
|总交易次数|全部平仓交易统计|

### 4.4 plot\_utils\.py 可视化规范

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

### 4\.5 opt\_pipeline 参数优化规范

- param\_optimize：全网格暴力搜索。目标标的解析优先级 `--stock-list` > `--all-stocks` > 回归标的集（默认），由 `common.resolve_target_codes` 统一实现；不在 config.yaml `stock_list` 中的代码直接报错。

- out\_sample\_verify：剔除训练过拟合（训练/测试拆分）。按 `opt_pipeline.out_sample_train_end` 切分数据为训练段 / 测试段，分别回测，若训练收益率 − 测试收益率 > `opt_pipeline.out_sample_overfit_threshold` 则判为过拟合剔除。当训练段或测试段 K 线数 < 60（SMA60 最小周期下限）时跳过该标的并打印 `[ERROR]` 提示与改进方法。

- rolling\_window\_verify：多窗口稳定性筛选。按 `opt_pipeline.rolling_start_year` 起、`rolling_train_years` + `rolling_test_years` 长度滚动生成多个年度对齐窗口，对每个测试段回测并取平均收益率，平均收益 > 0 视为稳定。窗口同时满足 `rolling_min_train_bars` / `rolling_min_test_bars` 才被采纳。若某标的在所有窗口中均不满足最低 K 线数，跳过并打印 `[ERROR]` 提示与改进方法；若全部标的被跳过、结果表为空，额外打印整体失败提示。

- aggregate\_best\_param：每标的每策略保留一组最优稳定参数

- write\_param\_to\_config：自动落地到 config\.yaml（仅更新传入标的的 `strategy_params` 条目，config 其余内容原样保留）

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

**start\_date 下限约束**（end\_date=2026-09-21、默认滚动参数）：

- 至少 1 个有效滚动窗口（训练段 > 200 根）：start\_date ≤ 2024-03-04

- 推荐（训练段 242 根 + 2 个有效窗口）：start\_date = 2024-01-01

- 绝对底线（外样本训练段 ≥ 60 根，但无统计意义）：start\_date ≤ 2024-10-08

### 4.6 手工交易复盘模块

匹配规则：

- 按【股票代码 \+ 交易日】匹配策略信号

- 统计：信号匹配率、手工胜率、平均盈亏

- 输出每日对照复盘表

### 4.7 daily_signal.py 每日信号生成模块

每日收盘后独立运行，为 stock_list 中所有标的生成买卖信号。

**信号生成逻辑**：

- 读取 config.yaml 的 stock_list 与 strategy_params
- 读取 manual_trades.csv 计算当前持仓（BUY 累加 \- SELL 累加，净量 > 0 即为持仓）
- 覆盖 ds.end_date 为当天日期（确保增量拉取覆盖今日行情）
- 对每只标的的每个策略运行回测，提取 action_log（决策时信号日志）
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

- global\_setting：资金、回测时间区间

- commission\_config：完整A股交易费率

- opt\_pipeline：外样本校验与滚动窗口校验的可调参数（切分日期、过拟合阈值、窗口长度、最低 K 线数），详见 4.5 节

- tushare 密钥独立存放于用户目录 `~/.skyquant/tushare.yaml`（不随项目入库），config.yaml 中不包含 token

- stock\_list：回测标的池

- strategy\_params：流水线自动更新的最优参数结果

---

## 6\. 输出文件标准

所有输出路径固定、命名规则固定、用途固定。

- metrics\_summary\.csv：汇总所有标的策略绩效

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

## 9\. 迭代路线图

- **V1\.0（当前）**：完整流水线、指标、绘图、日志、复盘、每日信号生成

- **V1\.1**：新增卡玛比率、最大连续亏损、波动率

- **V1\.2**：参数热力图、多策略对比图

- **V1\.3**：多进程并行回测加速

- **V1\.4**：简易Web可视化面板

> （注：部分内容可能由 AI 生成）
