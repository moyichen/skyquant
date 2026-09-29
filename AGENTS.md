# SkyQuant 项目上下文文档

> 本文件供 AI Agent 新会话快速了解项目背景，避免重复沟通。详细规范见 spec.md。

## 项目简介

SkyQuant 是一套 A 股日线量化策略回测流水线，支持全自动参数寻优、过拟合校验、策略回测、指标计算、可视化绘图，以及每日收盘后的实盘成交复盘与次日信号生成（live_trading.py）。

## 系统分层架构

核心思想：**分层解耦**。策略只负责「信号规则」，不做数据读取、标的过滤、指标汇报、绘图、参数遍历；通用能力下沉到底层模块。自上而下：

| 层 | 文件 | 职责 |
|----|------|------|
| 1. 配置层 | config.yaml + config_store.py + params/ | 固定配置 config.yaml：资金/区间/滑点、黑白名单、费率、筛选阈值、股票池、寻优目标；高频变动的策略参数独立为参数集 params/active.yaml（生效）· drafts/（手工草稿）· experiments/（opt 归档），由 config_store.py 统一加载（freqtrade 多 config 风格，backtest/live 可用 `--params` 临时覆盖） |
| 2. 数据层 | dataprovider.py | Tushare 拉取/增量更新/CSV 缓存/标准化 DataFrame；板块指数行情 fetch_index（index_daily，缓存 cache/index_cache/）；无任何交易逻辑（`DataProvider` 类） |
| 3. 股票池筛选层 | stock_filter.py | Pairlist Filters 质量门（age/price/volume/turnover/liquidity/name，常驻）+ trend 趋势门（--screen）+ regime 分类路由；策略外部选股 |
| 4. 策略插件层 | strategy/*.py | 每个策略一个文件，继承统一 BaseStrategy，只做指标与买卖信号；注册到 STRATEGY_MAPPING 即插拔 |
| 5. 回测引擎层 | main.py | 封装 Cerebro：数据/手续费/滑点/Analyzer，默认按 regime 自动路由策略，遍历标的收集结果 |
| 6. 指标报表层 | report.py | calc_metrics 指标计算 + 自包含 HTML 报告（Strategy Summary 展示 regime/板块，「板块指数」区块：最新点位/窗口涨跌/近20日/相对强弱）+ Plotly freqtrade 风格 K 线交互图（板块指数收盘线副轴叠加）；统一输出标准 |
| 7. 参数寻优层 | opt_pipeline/*.py | 网格→外样本→滚动→聚合→归档 experiments 并合并 active 参数集；多指标 dict 贯穿 worker/CSV，回测与寻优复用同一套策略代码 |
| 8. 一键入口 | skyquant.py | 数据→筛选→按标的寻优闭环→批量回测→复盘；统一日志 output/run.log |
| 9. 公共工具层 | comm.py | 手续费、路径常量单一来源、setup_logging、apply_blacklist、build_commission、apply_broker_settings |

关键原则：标的前置过滤 > 策略内过滤；策略纯插件化；参数全部外置；统一输出标准（任何策略指标/报表格式一致，可横向对比）。

**与 freqtrade 概念映射**（术语与职责对齐高星工程，便于跨工程理解）：

| freqtrade | SkyQuant | 说明 |
|-----------|----------|------|
| `populate_indicators()` | `populate_indicators()` | 策略专属指标初始化 |
| `populate_entry_trend()` / `populate_exit_trend()` | `populate_entry_trend()` / `populate_exit_trend()` | 入场/出场信号，含策略专属方向过滤（同名对齐） |
| 指标列 `macd`/`macdsignal`/`macdhist`、`adx`/`plus_di`/`minus_di`、`bb_*band` | trend/range 策略同名属性 | 指标变量名对齐 |
| Pairlist Filters：AgeFilter/PriceFilter/VolumeFilter（+turnover/liquidity A 股扩展） | stock_filter.py `stock_filter.pairlist` 五组过滤器 | 标的池过滤在策略外部 |
| Protections（CooldownPeriod/StoplossGuard/MaxDrawdown） | BaseStrategy `cooldown_period_candles`/`stoploss_guard_*_candles`/`max_allowed_drawdown` | 全局风控保护，只拦开仓，默认全关 |
| stoploss / trailing_stop | `max_loss_stop_ratio` / `trail_atr_multiple` | 止损与追踪止损 |
| trailing_stop_positive + trailing_only_offset_is_reached | `trail_tighten_profit_multiple` + `trail_tight_atr_multiple` | 浮盈达标后收紧追踪 |
| minimal_roi | `take_profit_atr_multiple`（ATR 距离制，默认关） | 固定止盈 |
| DataProvider | dataprovider.py（`DataProvider` 类） | 数据获取与缓存（同名对齐） |
| Hyperopt | opt_pipeline/（网格+外样本+滚动） | 参数寻优 |
| `can_short=False` | 全项目只做多 | A 股约束 |

## 运行环境

- WSL Ubuntu 24.04，Python 3.12.3
- Ruff 0.16.8 安装在 ~/.local/bin/ruff（格式化工具，行宽 260）
- Tushare 凭据存放于 ~/.skyquant/tushare.yaml（不入库）
- VS Code Ruff 扩展配置在 .vscode/settings.json（formatOnSave + source.fixAll.ruff）

## 运行方式

```bash
# 寻优流水线（默认只跑回归标的：行情拉取→股票池前置筛选→按标的循环 寻优→校验→聚合→归档/生效参数集→最后批量回测→复盘）
python3 skyquant.py all
python3 skyquant.py all --all-stocks         # 手动触发全量标的池
python3 skyquant.py all --skip-data          # 缓存加速模式
python3 skyquant.py all --stock-list 000725,600519   # 仅运行指定股票（逗号分隔）
python3 skyquant.py all --screen             # Pairlist Filters 质量门之上再加趋势门，只对 pairlist+trend 双通过的标的跑流水线
python3 skyquant.py all --all-stocks --screen  # 全量标的池 + 趋势性前置过滤

# 单独跑股票池前置筛选（只读本地缓存，输出 output/stock_filter.csv）
python stock_filter.py
python stock_filter.py --stock-list 000725,600519

# 实盘复盘与次日信号（收盘后运行：真实成交匹配 + 持仓信号 + HTML 实盘持仓报告）
python live_trading.py
python live_trading.py --force-refresh  # 强制全量下载
python live_trading.py --stock-list 000725,601633  # 仅复盘指定持仓

# 单次回测（不传 --strategy 时按 stock_filter.csv 的 regime 自动路由）
python main.py --strategy trend
python main.py --stock-list 000725 --strategy trend  # 仅回测指定股票
python main.py --stock-list 000725 --params params/experiments/xxxx_profit_rate.yaml  # 临时用历史参数组，不改 active

# 参数集管理（生效集 / 草稿 / opt 归档）
python3 skyquant.py params list
python3 skyquant.py params show params/experiments/20260928_203000_profit_rate.yaml
python3 skyquant.py params apply params/experiments/20260928_203000_profit_rate.yaml --codes 000725
python3 skyquant.py opt --no-apply   # 只归档参数组不生效，确认后再 params apply
```

### 标的集合与 `--stock-list` 参数说明

**默认标的集合**：`skyquant.py all` 与 `param_optimize.py` 默认只处理回归标的集 `REGRESSION_STOCKS`（当前仅 `["000725"]`，定义在 `opt_pipeline/common.py`，与 tests/regression 共用同一常量）——每次修改参数后的快速迭代门槛。全量标的池需显式 `--all-stocks` 手动触发。`main.py` 默认用 config.yaml 全部标的；`live_trading.py` 默认取 live_trades.csv 中的全部真实成交/持仓。

`--stock-list` 参数（逗号分隔的股票代码列表）：

- `skyquant.py all`/`param_optimize.py`：`--stock-list` > `--all-stocks` > 回归标的集（默认），互相冲突或代码不在 config.yaml `stock_list` 中会直接报错（`common.resolve_target_codes` 统一解析）
- `main.py`：不传时用 config.yaml 全部标的；`live_trading.py`：不传时取 live_trades.csv 全部成交
- 五个寻优阶段脚本（param_optimize/out_sample/rolling/aggregate/export_param_set）均支持 `--stock-list`；阶段 CSV 经 `common.write_stage_csv` 按标的合并写——重跑某标的只替换该标的的行，其余标的行保留；阶段五的参数集 apply 同样只合并涉及标的
- `skyquant.py all` 结构：批量拉数据 → **股票池前置筛选（常驻，见 stock_filter.py）** → 按标的循环跑寻优链 5 阶段（单标的闭环后再下一个）→ 最后批量回测 + 实盘复盘/信号（live_trading）
- `skyquant.py all --screen`：Pairlist Filters 质量门常驻；加 `--screen` 后在质量门通过者之上再做趋势性门控，只把 pairlist+trend 双通过的标的送入寻优；筛选报告写 `output/stock_filter.csv`（含 regime 标签，驱动策略路由）

### stock_filter.py — 股票池前置筛选层（数据层与策略层之间）

**分层定位**：策略只负责信号，标的筛选在策略外部。该层读本地缓存行情（只读不调 API），产出 `output/stock_filter.csv`，main.py / param_optimize.py / live_trading.py 通过 `load_regime_map` / `routed_strategies` / `strategy_for_code` 消费它；无报告时安全降级（不剔除、不路由，跑全部 active 策略）。

**两层门控 + 一个分类**（阈值全部在 config.yaml `stock_filter`）：

1. **Pairlist Filters 质量门（skyquant all 常驻；趋势策略也执行）**：概念对齐 freqtrade Pairlist Filters，剔除数据不足/停牌/流动性差/低价/ST 标的
2. **trend 趋势门（仅 `--screen` 强制）**：趋势性 7 阈值
3. **regime 分类（常驻，不剔除只贴标签）**：trend / range / breakout，决定该标的路由到哪个策略

Pairlist Filters（`DEFAULT_PAIRLIST_FILTERS`，config `stock_filter.pairlist` 五组覆盖）：

| freqtrade 概念 | 组/键 | 默认阈值 | 含义 |
|------|------|----------|------|
| AgeFilter | `age_filter.min_days_listed` | 120 | 窗口内交易日数下限（指标预热） |
| PriceFilter | `price_filter.low_price` | 1.0 | 均价下限（剔除仙股/退市风险价区） |
| VolumeFilter | `volume_filter.min_avg_amount_yi`（`lookback_days`，0=全窗口） | 0.5 | 日均成交额下限（亿元；Tushare amount 单位千元，/100_000） |
| A 股扩展 | `turnover_filter.min_avg_turn` | 0.2 | 日均换手率下限（%，0.2 即 0.2%；过低会误杀大盘股） |
| A 股扩展 | `turnover_filter.megacap_min_circ_mv_yi` / `megacap_min_avg_turn` | 1000 / 0.1 | 流通市值≥1000 亿的大盘股换手率下限降档至 0.1%（市值取 daily_basic 官方 `circ_mv` 近 60 日均值，单位万元/1e4 转亿；旧缓存无该列时回退 amount/(turn/100) 推导；0 关闭降档） |
| A 股扩展 | `liquidity_filter.max_zero_volume_ratio` | 0.01 | 零成交量 K 线占比上限（停牌） |
| A 股扩展 | `liquidity_filter.max_gap_days` | 20 | 相邻 K 线最大日历日缺口（长期停牌） |
| 自定义名称规则 | 常驻无配置 | ST/*ST/退 | 名称含 ST 或「退」直接剔除（freqtrade 无内建对应过滤器） |

trend 指标（config `stock_filter.trend`）：

| 指标 | 默认阈值 | 含义 |
|------|----------|------|
| `adx_mean` | ≥ 18 | Wilder ADX(14) 均值，趋势强度 |
| `trend_ratio` | ≥ 0.30 | ADX≥25 的强趋势交易日占比 |
| `bull_alignment` | ≥ 0.35 | EMA60 > EMA120（多头结构）占比 |
| `ma_crossings_year` | ≤ 6 | EMA60/EMA120 年交叉次数，越低越少 whipsaw |
| `efficiency` | ≥ 0.04 | Kaufman 效率比 = 净涨跌/路径长度 |
| `price_range` | ≥ 0.40 | (期间最高-最低)/均价 |
| `max_bull_streak` | ≥ 150 | 最长连续 EMA60>EMA120 天数 |

**CSV 列**：`stock_code, name, pairlist 各指标(bars/zero_volume_ratio/max_gap_days/avg_amount_yi/avg_turn/mean_close), pairlist_passed, pairlist_fail_reason, trend 各指标, passed(=pairlist AND trend), fail_reason, regime`。

**用法**：
- 独立：`python stock_filter.py`（全量）/ `--stock-list 000725,600519`
- 流水线：`python3 skyquant.py all`（Pairlist Filters 常驻）/ `--screen`（pairlist+trend）
- ADX 用 Wilder 平滑手动实现（无外部 ta 库依赖）

## 文件清单

| 文件 | 职责 |
|------|------|
| skyquant.py | 全流水线调度入口（`all` 子命令），日志管理，异常终止（行情→常驻 Pairlist Filters 筛选→按标的寻优 5 阶段→批量回测→实盘复盘/信号；--screen 追加趋势门） |
| stock_filter.py | 股票池前置筛选层：Pairlist Filters 质量门（Age/Price/Volume + turnover/liquidity/name A 股扩展）+ trend 趋势门 + regime 分类（trend/range/breakout 路由），输出 output/stock_filter.csv |
| main.py | 回测引擎层：加载配置、遍历标的（默认按 regime 自动路由策略）、执行回测、输出指标与图表；隔离 Cerebro 细节 |
| live_trading.py | 实盘交易模块（`LiveTrading`）：live_trades.csv 真实成交解析持仓/成本/浮盈、成交-信号匹配复盘、现算策略共识与次日操作建议（按 regime 收窄、Pairlist 门、黑名单），输出 live_trade_review.csv + live_signal_*.csv + plots/live_portfolio_report.html |
| dataprovider.py | 数据层（`DataProvider` 类）：Tushare 行情拉取、增量更新、本地缓存、AStockData feed（不含任何交易逻辑） |
| comm.py | 公共工具层：A 股手续费模型、路径常量单一来源（PROJECT_ROOT/CACHE/OUTPUT/PLOT/LOG/CONFIG）、setup_logging、apply_blacklist、build_commission、apply_broker_settings（资金+佣金+可选滑点） |
| report.py | 指标与报表层：calc_metrics/calc_equity_metrics（年化、回撤、夏普、Sortino、卡玛、胜率、盈亏比、期望值、B&H alpha，含 Sharpe/Calmar 中文分级评价）+ 自包含 HTML 回测报告 + Plotly freqtrade 风格 K 线交互图（合并原 metrics_utils.py + plot_utils.py，取代 btplotting） |
| strategy/base.py | 策略基类：ATR 仓位管理、追踪止损、动态止盈、全局风控保护（Cooldown/StoplossGuard/MaxDrawdown，对齐 freqtrade Protections）、action_log 信号日志、统一输出接口（不设方向性入场门控） |
| strategy/__init__.py | STRATEGY_MAPPING 策略注册表、DEFAULT_STRATEGY_PARAMS 默认参数 |
| strategy/trend.py | 趋势策略（合并 trend_follow+momentum）：趋势过滤（EMA/MACD/波动率/ADX）+ Momentum>0 开仓 |
| strategy/range.py | 震荡/均值回归策略（合并 boll_ma+short_reversal）：布林下轨或急跌超阈值开仓 |
| strategy/breakout.py | 突破策略（新增）：唐奇安 N 日新高突破开仓 |
| opt_pipeline/common.py | 流水线共享：BacktestRunner（返回多指标 dict）、REGRESSION_STOCKS 默认标的集、resolve_target_codes（含黑名单）/write_stage_csv、优化目标解析（profit_rate/sharpe/calmar）、regime 路由、参数提取工具 |
| opt_pipeline/param_optimize.py | 参数寻优双模式：grid 全量枚举 / tpe hyperopt 贝叶斯采样（`--mode`；chunk 子进程并行；按 regime 只跑路由策略；输出 6 指标列，两模式同 schema） |
| opt_pipeline/out_sample_verify.py | 外样本校验（剔除过拟合；train/test 双段多指标） |
| opt_pipeline/rolling_window_verify.py | 滚动窗口稳定性校验（平均收益/夏普/卡玛/回撤 5 列） |
| opt_pipeline/aggregate_best_param.py | 最优参数聚合（按 config optimize_metric 选排序列，avg_test_profit>0 门槛） |
| config_store.py | 参数集统一管理：load_config（config.yaml 深合并 active.yaml）/load_param_set/update_active_entry/export_experiment/apply_param_set/save_draft/list_param_sets，RLock + 原子写 |
| opt_pipeline/export_param_set.py | 阶段五：聚合结果归档 params/experiments/<时间戳>_<objective>.yaml（含 metrics 元数据），默认再合并进 params/active.yaml；`--no-apply` 只归档；不碰 config.yaml |
| ruff.toml | Ruff 配置（target-version=py39, line-length=260） |
| tests/regression/run_regression.py | 策略回归基线：固定标的×类默认参数×缓存数据，对比 golden.json，支持 --update-golden |
| tests/regression/golden.json | 回归基线指标（final_value/交易数/买卖次数等），确认改进后才更新 |

## 开发约定

- **Python 版本兼容**：项目需兼容 Python 3.10 以下旧版本，类型注解使用 Optional[X] 而非 X | None
- **代码语言**：代码注释、日志、print 输出统一使用英文
- **Spec 文档语言**：需求文档（spec.md）统一使用中文
- **格式化**：Ruff，行宽 260，配置在 `ruff.toml`（`target-version = "py39"` 防止 `Optional[X]` 被自动转为 `X | None`），VS Code 配置 formatOnSave
- **变量命名**：不使用缩写（如用 strategy 而非 strat）
- **文件创建**：不主动创建 README 或文档文件，除非用户明确要求
- **路径锚定**：使用 Path(__file__).parent.resolve() 锚定项目根目录，不依赖 CWD

## 关键设计决策

1. **action_log vs trade_log**：BaseStrategy 有两个日志。trade_log 只记录已平仓交易（用于回测指标）；action_log 记录决策时信号意图（用于每日信号生成，能捕获未平仓的买入信号）。两者独立，互不影响。

2. **多策略共识机制**：每日信号生成时，一只标的可能配置多个策略。共识优先级为 SELL > BUY > HOLD > WAIT（防御性，任一策略发出 SELL 即覆盖）。

3. **end_date 覆盖**：config.yaml 的 global_setting.end_date 是固定值。live_trading.py 运行时会覆盖 data_provider.end_date 为当天日期，确保增量拉取覆盖最新行情。

4. **Tushare 凭据隔离**：token 不放在 config.yaml，独立存放于 ~/.skyquant/tushare.yaml，不随项目入库。

5. **opt_pipeline 路径引导**：opt_pipeline/common.py 统一处理 sys.path 注入项目根目录，各阶段脚本只需 from common import ...。

6. **自建进程池网格寻优（不使用 optstrategy）**：三个重阶段（param_optimize/out_sample_verify/rolling_window_verify）均在父进程物化参数组合，通过模块顶层 worker（`_run_single_combo` 原语 + `_worker_run_*` 包装）+ `multiprocessing.Pool.map` 多进程并行，每标的建一次 Pool。不使用 `cerebro.optstrategy()` 的原因：optstrategy 会把惰性的 `itertools.product` 迭代器挂在 Cerebro 上，多进程时将 Cerebro 自身 pickle 给子进程；Python 3.14 起 `itertools.product`/`itertools.count` 不可 pickle（报 `cannot pickle 'itertools.product' object`）。**行情切片经 Pool `initializer/initargs` 每个 worker 只传一次**，job 只含 `(strategy_id, params)`（逐任务 pickle 58KB df 实测慢约 30%）。worker 必须是模块顶层函数（spawn 模式要求可 import），不能是闭包/lambda，临时调试脚本也必须带 `if __name__ == "__main__"` 守卫，否则 spawn 子进程重新导入主模块时会递归建池导致 worker 不断重生、空转烧 CPU。**沙箱注意**：TRAE macOS 沙箱把子进程压在单核分时（实测 4 进程纯 CPU 任务比串行慢 2.8 倍），沙箱内排查性能问题应加 `--maxcpu 1`；WSL/沙箱外多核正常（4 worker 约 2–3x）。

7. **行情数据加载用 PandasData 而非 GenericCSVData**：AStockData 继承 `bt.feeds.PandasData`，不使用 backtrader 内置的 `GenericCSVData`。原因：
   - **数据生命周期中段有 DataFrame 处理步骤**：`fetch_stock` 在加载后要做增量合并（`pd.concat → drop_duplicates → sort_values`）、start_date 过滤（`df[df["datetime"] >= start_dt]`）、format_df 列名规范化。这些必须以 DataFrame 形式处理，GenericCSVData 直接读文件的路径走不通。
   - **扩展列两边都得子类化，没省事**：项目 CSV 含 `preclose/amount/turn/pctChg` 4 个扩展列。GenericCSVData 默认只有 datetime/open/high/low/close/volume/openinterest 7 个 line，挂扩展列仍需 `lines += (...)` + `params += (...)` 子类化，与 PandasData 子类化代码量相当。
   - **列名匹配**：CSV 中 `pctChg` 是驼峰命名，PandasData 用 `-1` 自动按 line 名匹配 DataFrame 列，GenericCSVData 则需逐列显式配 `params=(("open", 1), ...)`，更繁琐。
   - **datetime 类型精度**：GenericCSVData 走字符串 `dtformat` 解析，而 `_filter_by_start_date` 需向量化比较 `df["datetime"] >= start_dt`，PandasData 路径天然支持 `parse_dates=["datetime"]`。
   - **适用边界**：GenericCSVData 仅适合纯静态、列名规范、无中间处理、只用标准 OHLCV 的场景；本项目命中 PandasData 全部适用条件（增量更新/合并/过滤、挂载扩展列、列名非标准）。

8. **分层解耦与策略插件化**：数据（dataprovider）、选股（stock_filter）、信号（strategy）、回测（main）、报表（report）职责严格分开。策略类禁止读写文件/拉数据/绘图，新增策略只需新增 strategy 下文件并注册 STRATEGY_MAPPING。标的前置过滤 > 策略内过滤——震荡/停牌/ST 标的在 stock_filter 层剔除或路由，不把全部标的硬塞给策略试错。

9. **回测与寻优同一指标口径**：worker `_run_single_combo` 不再用 FinalValueAnalyzer 只取终值，而是跑完整策略后从 `get_equity_dataframe()` 经 report.calc_equity_metrics 算 {profit_rate, sharpe, calmar, drawdown}，与 main.py 回测报表同源；broker 装配（佣金+滑点）经 comm_config dict 传子进程，保证两种路径口径一致。config `opt_pipeline.optimize_metric` 决定网格/聚合排序列（profit_rate/sharpe/calmar）。

10. **统一 broker 装配 + 黑白名单**：main/live_trading/worker 全部走 comm.build_commission + apply_broker_settings（资金+佣金+可选 slippage_perc），杜绝四处手写导致的口径漂移；标的集三道关：config `stock_list` 白名单 → `stock_blacklist` 黑名单覆盖 → stock_filter 质量/趋势门。

## 配置文件

- config.yaml：**固定配置**（分层：global_setting 资金/区间/`slippage_perc` 滑点、`stock_blacklist` 黑名单、commission_config 费率、stock_list 白名单（每条含 code/name/sector/sector_index/sector_index_name，stock_filter 筛选后自动写回 regime 字段）、stock_filter 三层筛选阈值、opt_pipeline 校验参数与 `optimize_metric` 优化目标 profit_rate/sharpe/calmar）。**不再包含 strategy_params**，可放心人工编辑保留注释
- params/：**策略参数集目录**（机器生成 YAML，统一 schema `meta + strategy_params`）：
  - `params/active.yaml` 生效参数集：回测/实盘默认读取；opt 阶段五与网页控制台「保存为生效」写入此处
  - `params/experiments/<YYYYMMDD_HHMMSS>_<objective>.yaml`：每次 opt 自动归档的完整参数组（含滚动 avg_profit 等 metrics 元数据），不可变历史，可随时 `skyquant params apply` 重新生效
  - `params/drafts/<name>.yaml`：网页控制台「另存草稿」的手工参数组，试跑满意后再生效
  - 合并单元为 `(code, strategy)` 完整参数 dict；apply 只替换目标单元；CLI `skyquant params list/show/apply`；回测/实盘可用 `--params <file>` 临时用某组参数而不改变 active
- 读取唯一入口：`config_store.load_config()`（config.yaml 深合并 active.yaml），DataProvider/main.py/live_trading/dashboard 全部经此；禁止业务代码再直接 yaml.safe_load(config.yaml) 取 strategy_params
- live_trades.csv：实盘真实成交记录（trade_date, stock_code, side, price, size；仅 BUY/SELL 成交，不含 PNL 列）
- ruff.toml：Ruff 格式化配置（target-version=py39, line-length=260）
- ~/.skyquant/tushare.yaml：Tushare API token

## Spec 文档

详细项目规范见 spec.md（中文），包含：系统概述、目录规范、流水线流程、模块规范、配置规范、输出标准、异常处理、依赖清单、迭代路线图。

## 文档定位与代码重建

**当前文档不能直接用来重新生成代码。** 它们描述的是"做什么"和"为什么"，下面补充了关键的"怎么做"（函数签名与算法逻辑），但完整实现仍需阅读源文件。

### strategy/base.py — BaseStrategy(bt.Strategy)

**参数**：
- `atr_period=14`（ATR 周期）
- `max_risk_ratio=0.02`（单笔最大风险占比）
- 基类无方向性入场门控（入场过滤归各策略自管，标的适配交给 stock_filter 层）；EMA/MACD/波动率/ADX 过滤为 trend 策略专属参数
- 亏损侧机制：`max_loss_stop_ratio=0.30`（最差止损地板=均价×(1-30%)，启用时追踪止损只在盈利侧生效）、`average_down_drop=0.10` / `average_down_ratio=0.25`（首次亏损达 10% 按当前持仓 1/4 摊低加仓，每笔仅一次）
- `take_profit_atr_multiple=None`（固定止盈距离 ATR 倍数；默认关闭=纯追踪止损，显式配置才启用）
- `trail_tighten_profit_multiple=None`（动态止盈激活门槛：浮盈达该 ATR 倍数后收紧止损；None 关闭）
- `trail_tight_atr_multiple=0.8`（动态止盈激活后的收紧追踪止损 ATR 倍数）

**核心算法**：

```
开仓门控(基类): 无——方向性过滤不做全局前提（freqtrade 式设计：全局层只管风控/执行；
              标的适配交给 stock_filter 层）。历史教训：四重过滤/EMA 前提做强 global
              门控都会损伤 range/breakout（且 trend 在震荡股上也被拖累）
趋势过滤(trend 专属): EMA(ema_fast)>EMA(ema_slow) 且 MACD多头 且 ATR/close > min_volatility_ratio
              且 ADX >= adx_min 且 plus_di > minus_di（trend._trend_filters_ok 内检查）
MACD 多头判定: macd(DIF) > 0（零轴上方）且 macd > macdsignal（金叉状态）
              且 DIF 持续上行 macd_momentum_bars 根、macdhist 柱持续放大（动量增强）
ATR 仓位公式:  size = int(总资产 * max_risk_ratio / (ATR * atr_multiple))
固定止盈价:    take_price = entry_price + ATR * take_profit_atr_multiple（该参数非 None 时，默认关闭）
追踪止损价:    stop = 持仓以来最高价 - trail_atr_multiple × ATR（只上不下 ratchet）
              （max_loss_stop_ratio 启用时只在盈利侧生效：candidate > 持仓均价才 ratchet）
最差止损地板:  loss_floor = 持仓均价 × (1 - max_loss_stop_ratio)，初始止损即地板；
              收盘 <= 地板才止损（max_loss_stop），摊低加仓成交后地板随新均价下移
动态止盈收紧:  浮盈(最高价 - entry_price) >= trail_tighten_profit_multiple × ATR 时，
              stop = max(stop, 最高价 - trail_tight_atr_multiple × ATR)
摊低加仓:      首次收盘亏损 >= average_down_drop 时，按当前持仓 × average_down_ratio
              挂次日限价单（每笔交易仅一次，未成交下一 bar 条件仍满足则重试）
```

**方法签名**：

```python
def _position_size(self, atr_multiple) -> int  # ATR 仓位计算（含 NaN 守卫）
def _open_position(self, atr_multiple)          # 买入 + 设初始止损/止盈 + 记录 entry_bar/entry_atr_multiple
def _close_position(self)                         # 卖出 + 重置全部持仓状态 + 记录 action_log
def _update_trailing_stop(self)                   # 追踪止损 + 动态止盈更新（next 调用 populate_exit_trend 前自动执行）
def _protections_allow_entry(self) -> bool        # 全局风控保护（Cooldown/StoplossGuard/MaxDrawdown，默认全关）
def next(self)                                    # 模板方法: 无仓->Protections 门控后 populate_entry_trend(); 有仓先查固定止盈(默认关), 再 _update_trailing_stop, 再 populate_exit_trend()
def populate_entry_trend(self)                    # 子类重写: 入场信号（含策略专属过滤，如 trend 的 _trend_filters_ok）
def populate_exit_trend(self)                     # 子类重写: 出场条件（检查 stop_price 或策略专属信号）
def stop(self)                                    # 回测结束: self.final_value = broker.getvalue()
def get_equity_dataframe() -> pd.DataFrame        # 每日净值
def get_trade_dataframe() -> pd.DataFrame         # 已平仓交易记录
def get_action_dataframe() -> pd.DataFrame        # 决策时信号日志（含未平仓）
```

> 注：`_macd_bullish` / `_trend_filters_ok` 为 trend 策略专属（定义在 strategy/trend.py），不在基类。

**日志格式**：
- `action_log`: `{date, side: "BUY"/"SELL", price, size}`
- `trade_log`: `{entry_date, exit_date, entry_price, exit_price, size, profit_loss, profit_loss_net, profit_rate}`
- `equity_log`: `{datetime, equity}`

**多层级平仓优先级**：固定止盈 > 追踪止损/动态止盈 > 子类信号止损。

### 策略子类 — 入场/出场条件

**入场过滤归属**（BaseStrategy 不设全局门控）：方向性过滤是策略 alpha 的一部分，由各子类 `populate_entry_trend` 自管——trend 自带趋势过滤（EMA 多头 + MACD 多头 + 波动率 + ADX/DI 方向），range/breakout 无过滤；标的与策略的适配由 stock_filter 的 regime 路由负责（参考 freqtrade：入场在策略内、标的池过滤在策略外、基类只管风控与执行）。

| 策略 | 关键参数（默认值） | 专属入场信号 | 出场条件 |
|------|-------------------|----------|----------|
| trend | `trail_atr_multiple=1.6`, `momentum_period=20`，另有趋势过滤参数 `ema_*`/`macd_*`/`adx_*`/`min_volatility_ratio` | 趋势过滤（EMA/MACD/波动率/ADX）通过 **且** Momentum>0 | 追踪止损+动态止盈 / 动量转负 |
| range | `trail_atr_multiple=2.0`, `bb_period=20`, `drop_ratio=0.18` | close <= bb_lowerband **或** 跌幅 > drop_ratio | 追踪止损+动态止盈 / 突破 bb_upperband |
| breakout | `trail_atr_multiple=2.0`, `breakout_period=20` | close > donchian_upper（唐奇安上轨突破） | 追踪止损+动态止盈 / 跌破 donchian_lower |

> 注：止盈止损/仓位参数定义在 BaseStrategy，3 个策略统一继承；趋势过滤参数（ema_*/macd_*/adx_*/min_volatility_ratio）仅 trend 持有。追踪止损与动态止盈由基类 `_update_trailing_stop` 自动处理。`take_profit_atr_multiple` 默认 None（纯追踪止损）。

### 标的分类与策略路由（regime → strategy）

`stock_filter.classify_regime` 基于趋势性指标给每只标的打 regime 标签，寻优/信号时路由到对应策略：

| regime | 量化特征 | 策略 |
|---|---|---|
| `trend` | ADX≥22 + 效率比≥0.04 + 最长多头连涨≥150天 | trend |
| `range` | ADX<22 或 效率比<0.035（震荡/低效率） | range |
| `breakout` | 价格振幅≥0.8 且 ADX≥22（高波动大振幅） | breakout |

**路由消费函数**（stock_filter.py）：`load_regime_map()` 读 CSV→{code: regime}（缺失返回 {}）；`routed_strategies(code, regime_map, active_ids)` 有 regime→[该策略]，无→active 全集（param_optimize / live_trading 用）；`strategy_for_code(code, regime_map, param_pool, default)` 决定单策略（main.py 自动路由：regime→config 已配策略→default）。`write_regime_to_config(filter_df)` 在 stock_filter.py CLI 与 skyquant.py all 筛选步骤后把 regime 文本级写回 config.yaml `stock_list` 各条目（保留注释，不用 yaml.dump）。

### trend 策略详解（趋势跟随 + 动量确认）

源文件：[strategy/trend.py](strategy/trend.py)。**合并 trend_follow + momentum**：趋势结构由策略专属过滤（EMA 多头 + MACD/波动率/ADX）提供，momentum 提供入场时机确认。开仓需同时满足"趋势过滤通过"和"Momentum(period) > 0"，比纯趋势跟随更保守、比纯动量更稳健。

**子类参数**：

```
trail_atr_multiple=1.6        # 追踪止损 ATR 倍数（兼作仓位分母）
momentum_period=20            # 动量确认周期；>0 才开仓
max_risk_ratio=0.02
# 趋势过滤（策略专属；变量名对齐 freqtrade）
ema_fast=20 / ema_slow=60
macd_fast=12 / macd_slow=26 / macd_signal=9 / macd_momentum_bars=2
min_volatility_ratio=0.015
adx_period=14 / adx_min=20
# 指标属性：mom / macd / macdsignal / macdhist / adx / plus_di / minus_di
```

**入场逻辑**（`populate_entry_trend`）：

1. `_trend_filters_ok()`（EMA 多头 + MACD 多头 + 波动率 + ADX/方向）且 `self.mom[0] > 0` → 调用 `self._open_position(trail_atr_multiple)`，reason 记录 `trend: filters passed + momentum X > 0`

**出场逻辑**（`populate_exit_trend`）：

1. `close <= stop_price` → 追踪止损（含动态止盈收紧），reason 用 `_trail_stop_reason()`
2. `mom < 0` → 动量转负，reason `momentum_negative: momentum X < 0`

### strategy/__init__.py — 策略注册表

```python
STRATEGY_MAPPING = {
    "trend": TrendStrategy,
    "range": RangeStrategy,
    "breakout": BreakoutStrategy,
}

DEFAULT_STRATEGY_PARAMS = {
    "trend": {"trail_atr_multiple": 1.6, "momentum_period": 20, "max_risk_ratio": 0.02},
    "range": {"trail_atr_multiple": 2.0, "bb_period": 20, "drop_ratio": 0.18, "max_risk_ratio": 0.02},
    "breakout": {"trail_atr_multiple": 2.0, "breakout_period": 20, "max_risk_ratio": 0.02},
}
```

`DEFAULT_STRATEGY_PARAMS` 为各策略的默认参数，当生效参数集 params/active.yaml 中没有某只股票的优化参数时，`live_trading.py` 会回退使用这些默认参数。

### dataprovider.py

**AStockData(bt.feeds.PandasData)**：扩展 K 线 feed，挂载 `preclose`/`amount`/`turn`/`pctChg` 扩展列。

```python
class DataProvider:
    def __init__(self, config_path=None, cache_root=None, credentials_path=None)
    def load_config() -> dict
    def get_ts_code(stock_code) -> str           # 6开头->SH, 否则->SZ
    def full_download_save(stock_code) -> Optional[pd.DataFrame]
    def incremental_download(stock_code, start_dt, end_dt) -> Optional[pd.DataFrame]
    def fetch_stock(stock_code, force_refresh=False) -> Optional[pd.DataFrame]
    def load_cached_data(stock_code) -> Optional[pd.DataFrame]
    def format_df(df) -> pd.DataFrame
```

**数据列**：`datetime, open, high, low, close, volume, preclose, amount, turn, pctChg`（前复权 qfq）

**fetch_stock 缓存逻辑**：force_refresh=True->全量下载; 本地最新日期>=今天->返回缓存; 有缓存->增量拉取合并; 无缓存->首次全量

### comm.py — 公共工具层（手续费 + 路径常量 + broker 装配）

**AStockCommission(bt.CommInfoBase)** 参数：`commission=0.0003`, `stamp_duty=0.001`, `transfer_fee=0.00001`；费率：买入=佣金+过户费; 卖出=佣金+过户费+印花税。

```python
# 路径常量（全项目唯一来源，Path(__file__) 锚定，不依赖 CWD）
PROJECT_ROOT / CONFIG_PATH / LOG_FILE / CACHE_DIR / STOCK_CACHE_DIR / OUTPUT_DIR / EQUITY_DIR / PLOT_DIR

def setup_logging(log_file=None, level=logging.INFO)   # None=仅控制台；传 LOG_FILE 同时写文件
def apply_blacklist(codes, cfg) -> list                # 读 config stock_blacklist 过滤代码列表
def build_commission(cfg) -> AStockCommission          # 从 commission_config 统一构建手续费
def apply_broker_settings(broker, cfg, initial_capital, comminfo)
    # setcash + addcommissioninfo；global_setting.slippage_perc > 0 时 set_slippage_perc
```

main.py / live_trading.py / opt worker 全部经 `build_commission` + `apply_broker_settings` 装配，保证费率与滑点口径一致。

### report.py — 指标与报表层（合并 metrics_utils.py + plot_utils.py）

```python
def calc_equity_metrics(equity_df, risk_free_rate=0.02) -> dict
    # 仅用净值曲线：{total_return, annual_return, max_drawdown, sharpe_ratio, calmar_ratio}（worker 用）
def calc_metrics(equity_df, trades_df, risk_free_rate=0.02, price_series=None) -> dict
    # 净值指标 + freqtrade 对齐交易/日度指标（见下）
def normalize_exit_reason(reason) -> str     # 原始 reason -> stop_loss/trailing_stop/take_profit/exit_signal
def summarize_exit_reasons(trades_df) -> pd.DataFrame  # EXIT REASON STATS：类别×笔数/胜率/均持时/盈亏
def build_console_summary(metrics, trades_df) -> str    # freqtrade 风格多行控制台摘要（main.py 输出）
def render_report(equity_df, trades_df, action_df, metrics, analyzer_results,
                  out_dir, code, strategy_name, start_date, end_date,
                  initial_capital, final_value,
                  regime=None, sector_info=None, index_df=None, price_df=None,
                  interactive_html=None, stock_name=None) -> str  # 返回 HTML 路径
def render_interactive_chart(strategy, out_dir, code, strategy_name, price_df, trades_df,
                             stock_name=None, index_df=None, index_label=None) -> str
    # freqtrade 风格 Plotly 交互图路径（K线/成交量/ATR/MACD 全策略/ADX trend + 进出场标记；
    # index_df 存在时板块指数收盘线叠加到价格行副轴）
```

**指标公式**：
- 年化收益：`(1 + total_return) ** (365/days) - 1`
- 最大回撤：`(equity - cummax) / cummax` 的最小值
- 夏普比率：`sqrt(252) * mean(excess_ret) / std(excess_ret)`
- 卡玛比率：`年化收益 / abs(最大回撤)`
- 胜率：`profit_loss_net > 0 的交易数 / 总交易数`
- 盈亏比：`总盈利金额 / 总亏损金额`
- Sortino：`sqrt(252) * mean(daily_ret) / std(负收益日)`（仅下行波动）
- 期望值：`win_rate×均盈 - loss_rate×均亏`；expectancy_ratio = 期望值/均亏（对齐 freqtrade）
- 连胜/连亏：按平仓顺序对 profit_loss_net 符号计数
- 持仓时长：entry_date→exit_date 日历日（全部/盈利单/亏损单分别均值）
- 日度统计：best/worst_day、winning/losing/zero_days
- market_change：传 price_series 时计算 Buy & Hold 收益，alpha_vs_buyhold = 策略总收益 − B&H

**Exit reason 归一化**：原始 reason 前缀映射——`max_loss_stop→stop_loss`、`trail_stop/trail_stop_tightened→trailing_stop`、`take_profit→take_profit`、策略子类信号（momentum_negative/boll_upper_break/donchian_lower_break）→`exit_signal`；缺省 other，空值 left_open。

**render_report 设计**：

- 使用 Plotly `fig.to_html(include_plotlyjs=True, full_html=False)` 输出自包含 HTML（plotly.js 内联），可离线打开；全项目已移除 Bokeh 依赖
- 内部模块：`_build_equity_chart_html`（Plotly 暗色主题净值+回撤双子图：#4FC3F7 净值线、#F5B041 虚线 Initial Capital 初始资金水平线、#F38181 回撤填充；净值 trace 禁止 fill-to-zero——会压扁曲线，y 轴紧贴数据，本金线用透明 trace 锚定 autorange；1M/3M/6M/1Y/All 按钮）、`_build_signals_fills_html`（信号-成交统一表）、`_flatten_analyzer`（递归压平 namedtuple/dict/list 到 `(path, value)` 对）、`_build_strategy_summary_html`（去重：KPI 卡片已列指标不重复）、`_build_sector_index_html`（区块置于报告最前、KPI 之上）、`_build_analyzer_table_html`
- 调用方（main.py）注册 5 个 analyzer：`Returns / SharpeRatio / DrawDown / TradeAnalyzer / SQN`，名称见 `ANALYZER_NAMES` 常量；`getattr(strategy_instance.analyzers, name).get_analysis()` 提取后传入 `analyzer_results`
- 产物：`output/plots/{code}_{strategy_name}_report.html`，相对链接到 `_{strategy_name}_interactive.html`（Plotly 交互图）
- **交互图已从 btplotting 迁移到 Plotly（freqtrade 风格）**：`render_interactive_chart(strategy, out_dir, code, strategy_name, price_df, trades_df, stock_name=None)`，自包含 HTML（plotly.js 内联）。行布局：K 线主图（红涨绿跌实心蜡烛 + 策略指标覆盖层 + 进出场标记）+ 成交量 + ATR + **MACD（所有策略；trend 用指标线 macd/macdsignal/macdhist，range/breakout 由 `_compute_macd_from_close` 按 12/26/9 从收盘价现算，仅用于绘图）**；trend 策略再追加 ADX（plus_di/minus_di/adx_min 阈值线）子图。指标值经 `_line_to_numpy(line, n)` 从 backtrader line buffer 按 K 线根数对齐提取。入场=青色上三角、盈利出场=绿下三角、亏损出场=红下三角、期末未平仓=琥珀三角；每笔交易 entry→exit 虚线连接（win/loss 分色），hover 显示日期/价格/手数/盈亏/exit 类别；1M/3M/6M/1Y/All 区间按钮，周末 rangebreak，scrollZoom
- 已删除：btplotting 兼容层（Py314CompatibleBacktraderPlotting / `_replace_empty_sentinels`）、旧的 `plot_all` / `plot_equity_drawdown` / `plot_win_pie` / `_setup_chinese_font`（matplotlib PNG 路径全部移除）

### live_trading.py（实盘交易：真实成交复盘 + 次日信号 + HTML 持仓报告）

模块级函数（信号链）：

```python
def compute_holdings(trade_csv: Path) -> Dict[str, dict]        # BUY加权累加SELL扣减, 返回{code: {size, avg_cost}}
def run_strategy_actions(data_provider, comminfo, cfg, code, strategy_id, param) -> Optional[pd.DataFrame]  # 返回action_log
def classify_signal(action_df, last_bar_date) -> dict            # 最后action日期==last_bar_date->该action; 否则持仓->HOLD/空仓->WAIT
def compute_consensus(actions: List[str]) -> str                # SELL > BUY > HOLD > WAIT
def derive_suggested_action(consensus: str, currently_held: bool) -> str  # 持仓+SELL->卖出, 持仓+BUY->加仓, 未持仓+BUY->买入
def build_report_rows(..., force_refresh=False) -> List[dict]   # 仅扫描 live_trades.csv 当前持仓标的
def print_console_summary(df, holdings, report_date)            # 三段式: 持仓操作/关注列表/统计汇总
```

`LiveTrading` 类（实盘复盘 + 报告）：

```python
class LiveTrading:
    def __init__(self, config_path="config.yaml", trade_csv="live_trades.csv", stock_list=None, params_path=None)
        # stock_list: 逗号分隔股票代码，过滤 live_trades.csv
        # params_path: 参数集 YAML（None=params/active.yaml），CLI 对应 --params
    def get_strategy_signal(self, code, strategy_id, param) -> Optional[pd.DataFrame]  # 从trade_log提取信号
    def match_live_trade(self) -> pd.DataFrame         # 按交易日匹配策略信号
    def summary_report(self, out_csv="output/live_trade_review.csv") -> pd.DataFrame  # 匹配率/胜率/盈亏
    def collect_portfolio_data(self, force_refresh=False, review_df=None) -> Optional[dict]
        # 持仓 + 现算信号 + 最新行情 + 匹配矩阵 + metrics_summary.csv 回测参考
    def render_live_html(self, data, out_html="output/plots/live_portfolio_report.html") -> str
        # 自包含 HTML：账户KPI/持仓总览/逐标的卡片（真实成交、最新信号、匹配率、回测参考、要点解释与下一步信号）
```

**命令行参数**：`--force-refresh`（强制全量下载）、`--stock-list`（逗号分隔股票代码，过滤 live_trades.csv 记录；默认全部真实成交）

**输出**：`output/live_trade_review.csv`（成交-信号匹配矩阵）+ `output/live_signal_{YYYYMMDD}.csv`（持仓信号）+ `output/plots/live_portfolio_report.html`（实盘持仓报告）+ 控制台摘要。

**默认参数回退**：当某只股票在生效参数集 params/active.yaml 中没有优化后的参数时，使用 `strategy/__init__.py` 中的 `DEFAULT_STRATEGY_PARAMS` 作为回退。

**筛选层联动**：读 `output/stock_filter.csv`——`pairlist_passed=False` 的标的直接跳过；按 regime 用 `routed_strategies` 收窄参与共识的策略集（000725 regime=breakout 时只跑 breakout，不再三策略共识）；报告不存在时安全降级为全部 active 策略。config `stock_blacklist` 同步过滤。

### main.py

```python
def run_backtest(data_provider, comminfo, cfg, param_pool, code, strategy_id, force_refresh,
                 stock_name=None, regime=None, sector_info=None) -> dict
def get_strategy_param(param_pool, code, strategy_id) -> (strategy_cls, params)
def validate_live_trades(valid_codes)
```

**命令行参数**：`--force_refresh`（强制全量下载）、`--strategy`（策略 id；**默认 None=按 regime 自动路由**：`strategy_for_code` 读 stock_filter.csv 的 regime 标签 → config 已配策略 → 默认 trend）、`--stock-list`（逗号分隔股票代码，过滤 stock_list）

> `--interactive` 已移除。每次回测默认生成自包含 HTML 报表与 Plotly K 线交互图，无需额外开关。

**Cerebro 配置模式**（main.py / live_trading.py 共用）：

```python
cerebro = bt.Cerebro()
cerebro.addstrategy(STRATEGY_MAPPING[strategy_id], **param)
cerebro.adddata(AStockData(dataname=df, datetime="datetime", open="open", high="high", low="low", close="close", volume="volume"))
apply_broker_settings(cerebro.broker, cfg, initial_capital, comminfo)  # 资金+佣金+可选滑点
# main.py 专有：注册 analyzers 供 HTML 报表使用
#   Returns / SharpeRatio / DrawDown / TradeAnalyzer / SQN（_name 见 ANALYZER_NAMES）
strategy_instance = cerebro.run()[0]
```

**HTML 报表产物**（main.py `run_backtest` 内）：

- 每只标的生成 `output/plots/{code}_{strategy_id}_report.html`（Plotly plotly.js 内联，自包含可离线打开；Bokeh 已移除）
- 同目录生成 `output/plots/{code}_{strategy_id}_interactive.html`（freqtrade 风格 Plotly 交互图：K 线 + 成交量 + ATR + MACD（全策略）+ ADX（trend）+ 板块指数副轴叠加 + **双层标记**——大实心三角=实际成交 fills（entry/exit 盈亏/持仓中），小空心三角=触发信号 signals（含 EXPIRED 未成交，按 side×status 分 trace），图例三组均可点击开关，报表通过相对链接跳转）
- 报表内容（freqtrade 对齐）：头部（标题格式 `名称 (代码) — Strategy: 策略`，不含板块；区间/初始资金/最终净值/总收益）、**板块指数区块（置于 KPI 卡片之上：最新点位/窗口涨跌/近20日/相对强弱）**、8 张 KPI 卡片（年化/最大回撤/Sharpe/Sortino/Calmar/胜率/盈亏比/交易数）、**Equity Curve & Drawdown（Plotly 暗色双子图，初始资金水平虚线，净值 y 轴紧贴数据，位于 Summary 之上）**、Strategy Summary 表（Market Regime + 期望值、最佳/最差交易、持仓时长、连胜连亏、日度统计、B&H 与 alpha——KPI 卡片已列项不重复）、Exit Reason Stats 表（按平仓类别聚合）、Monthly Returns 月度收益热力表（年×12 月，正负绿红）、Signals & Fills 信号-成交统一表（按信号日倒序，最新在最上；含未平仓 BUY）、Analyzer 字段表（递归 flatten 5 个 analyzer）、K 线图链接；控制台同步打印 freqtrade 风格多行摘要

### skyquant.py（统一 CLI）

```python
def run_step(name, cwd, cmd)    # subprocess.Popen 执行, 非零退出码->sys.exit(1)
```

**子命令**：
- `dashboard`：启动本地看板（默认 127.0.0.1:8765，--no-browser 可关自动打开）
- `backtest`：单次回测（透传 main.py，支持 --stock-list/--strategy/--force-refresh）
- `opt`：寻优流水线（param_optimize→out_sample→rolling→aggregate→export_param_set，支持 --stock-list/--all-stocks/--maxcpu/--no-apply；`--mode grid|tpe` 选择搜索算法，`--epochs/--seed` 覆盖 TPE 配置）
- `fetch`：拉取行情（透传 main.py --force_refresh）
- `filter`：股票池前置筛选（透传 stock_filter.py）
- `live`：实盘复盘与信号（透传 live_trading.py）
- `all`：全流水线（数据→筛选→按标的寻优闭环→批量回测→复盘）

**all 子命令参数**：
- `--skip-data`：跳过行情拉取，使用本地缓存
- `--stock-list 000725,600519`：仅运行指定股票（显式子集）
- `--all-stocks`：手动触发 config.yaml 全量标的池；不传任何集合参数时默认回归标的集 `REGRESSION_STOCKS`
- `--screen`：Pairlist Filters 质量门常驻；加该参数后追加 trend 趋势门（pairlist+trend 双通过才入流水线）

**执行结构（数据层 → 筛选层 → 按标的寻优闭环 → 批量收尾）**：

1. 行情拉取（批量，透传完整目标集；`--skip-data` 可跳过）
2. **股票池前置筛选（常驻）**：`stock_filter.filter_stock_pool(DataProvider(), target_codes)` 写 output/stock_filter.csv；Pairlist Filters 未过者剔除并打印原因；`--screen` 时再用 trend `passed` 收窄；regime 标签供后续路由
3. 逐标的循环：对每个 code 依次调用 5 个阶段脚本并透传 `--stock-list <code>`：param_optimize.py（内部按 regime 只跑路由策略）→ out_sample_verify.py → rolling_window_verify.py → aggregate_best_param.py → export_param_set.py；单标的 5 阶段闭环后再处理下一个标的
4. 收尾批量执行 main.py（不带 --strategy，按 regime 自动路由）与 live_trading.py（实盘成交复盘 + 次日信号 + HTML 报告），透传完整目标集——保证 metrics_summary / 复盘报告等汇总 CSV 不被单标的覆盖

**标的集解析**：`common.resolve_target_codes(args, cfg)`，优先级 `--stock-list` > `--all-stocks` > `REGRESSION_STOCKS`；`--stock-list` 与 `--all-stocks` 互斥，未知代码报错；最后统一应用 config `stock_blacklist`。

### opt_pipeline/common.py

```python
REGRESSION_STOCKS = ["000725"]  # 默认寻优集=回归集，tests/regression 复用（后续按需扩充）

def parse_code_list(raw) -> list                      # 逗号分隔代码 -> list[str]
def resolve_target_codes(args, cfg) -> list           # --stock-list > --all-stocks > REGRESSION_STOCKS；未知代码报错；末尾应用黑名单
def resolve_maxcpu(requested) -> int                  # 正数原样；0/None/-1 -> cpu_count()，三个重阶段共用
def write_stage_csv(path, df, touched_codes=None)     # 阶段 CSV 写出：None=整体重写；否则按标的合并写（替换 touched 旧行）

# ---- 优化目标（config opt_pipeline.optimize_metric：profit_rate | sharpe | calmar）----
COMBO_METRIC_COLS   = [final_capital, profit, profit_rate, sharpe_ratio, max_drawdown, calmar_ratio]
ROLLING_METRIC_COLS = [avg_test_profit, avg_test_sharpe, avg_test_calmar, avg_test_drawdown, valid]
GRID_OBJECTIVE_COLUMN / OPTIMIZE_OBJECTIVE_COLUMN      # 目标 -> 排序列映射（网格列 / 滚动聚合列）
def resolve_optimize_metric(cfg) -> str                # 读取并校验 optimize_metric

def _run_single_combo(task) -> dict
    # 回测原语（模块顶层，spawn 可 pickle）：入参 (df, strategy_id, params, initial_capital, comm_config)
    # comm_config 含 commission/stamp_duty/transfer_fee/slippage_perc（与主回测口径一致）
    # 子进程构建 Cerebro 跑完整策略，从 strategy.get_equity_dataframe() 经 calc_equity_metrics 计算：
    # -> {final_value, profit_rate, sharpe_ratio, max_drawdown, calmar_ratio}
    # 三个专用 worker（_worker_run_combo / _worker_run_train_test / _worker_run_rolling）
    # 均在子进程内调用本原语；行情数据经 Pool initializer 每 worker 只传一次（_WORKER_STATE）
    # 注意：FinalValueAnalyzer 已删除，指标统一由净值曲线计算（回测/寻优同一口径）

class BacktestRunner:
    def __init__(self, data_provider=None)
    def run(self, df, strategy_id, params) -> dict                              # 单回测，返回指标 dict
    def profit_rate(self, result) -> float                                      # 从 dict 取 profit_rate
    def optimize(df, strategy_id, param_grid, maxcpu=1) -> List[Tuple[dict, dict]]  # 寻优网格（initializer Pool）
    def run_train_test_batch(df_train, df_test, jobs, maxcpu=1)                 # 外样本：-> [(train_metrics, test_metrics)]
    def run_rolling_batch(test_dfs, jobs, maxcpu=1)                             # 滚动：-> 每 job 各窗口 metrics dict 列表
    def _map_jobs(jobs, initializer, initargs, worker, serial_fn, maxcpu)       # 统一分发：maxcpu>1 走 Pool，否则进程内串行（结果按位对齐）
```

**三个重阶段全部多进程并行**：param_optimize / out_sample_verify / rolling_window_verify 均有 `--maxcpu`（默认 0=cpu_count），每标的建一次 Pool：父进程物化 jobs 为普通 `(strategy_id, params)` tuple，行情切片（df / train+test / 各滚动窗口）经 `initializer/initargs` 每个 worker 只 pickle 一次（实测比逐任务传 df 快约 30%），`pool.map` 保序返回。`maxcpu=1` 走进程内串行原语，结果与 Pool 逐位一致（已验证 diff=0）。

**write_stage_csv 合并写语义**：阶段脚本带 `--stock-list` 单标的重跑时，只替换 touched_codes 的行（即使本次无合格行，旧行也会被清除，避免陈旧残留），其他标的行原样保留；文件不存在或旧表为空时安全降级（写表头）。不带 `--stock-list`（批量模式）时整体重写，与旧行为一致。

```python
def extract_params(row, exclude_cols) -> dict    # 从CSV行提取策略参数, period类型强制int
def to_native(params: dict) -> dict              # numpy标量转Python原生类型
def read_stage_csv(path) -> pd.DataFrame          # stock_code强制str
```

### opt_pipeline/param_optimize.py — 参数寻优（grid / tpe 双模式）

```python
PARAM_GRID = {strategy_id: {param_name: [values, ...]}}   # 网格/搜索空间定义

def main():
    parser.add_argument("--maxcpu", type=int, default=0)  # 0/-1 使用全部 CPU
    parser.add_argument("--stock-list", type=str, default=None)  # 逗号分隔股票代码
    parser.add_argument("--all-stocks", action="store_true")     # 全量标的池（默认回归标的集）
    parser.add_argument("--mode", choices=["grid", "tpe"], default=None)  # 覆盖 config opt_pipeline.opt_mode
    parser.add_argument("--epochs", type=int, default=None)  # 覆盖 config opt_pipeline.hyperopt_epochs
    parser.add_argument("--seed", type=int, default=None)    # 覆盖 config opt_pipeline.hyperopt_seed
    # resolve_target_codes 解析目标集 -> load_regime_map + routed_strategies 按 regime 收窄策略
    # grid: 遍历 PARAM_GRID 全量 -> runner.optimize(...)
    # tpe : _tpe_search() 按 epochs 采样同一离散空间（hp.choice 映射）
    # 两种模式同写 output/param_optimize_result.csv（schema 一致，下游四阶段无感）
    # 过滤 profit_rate > 0 -> 按 GRID_OBJECTIVE_COLUMN（optimize_metric 决定）降序
    # -> write_stage_csv 合并写 output/param_optimize_result.csv
```

**TPE 模式（对标 freqtrade hyperopt）**：`_tpe_search()` 用 hyperopt `tpe.suggest` 批量 ask/tell——每批 `hyperopt_batch_size`（默认 64）个候选点经 `BacktestRunner.run_combos` 走既有 chunk 子进程回测（隔离+断点续跑），批间更新 Trials 后验；loss = `-optimize_metric`（非有限值罚 TPE_BAD_LOSS=1e3）；重复采样点经签名校验去重不重复回测；`hyperopt_seed` 固定后同配置同数据结果可复现。适用场景：空间大、粗筛方向；最终定参建议 grid 复核或加大 epochs。

**regime 路由**：若存在 output/stock_filter.csv，标的只寻优其 regime 对应的单一策略（000725=breakout 就只跑 breakout 的 216 组合，不再跑 trend/range）；无报告时跑全部 active 策略。

**运行方式**：
```bash
python opt_pipeline/param_optimize.py --maxcpu 1                          # 单进程（调试用），默认回归标的集
python opt_pipeline/param_optimize.py --maxcpu 4                          # 4 进程并行
python opt_pipeline/param_optimize.py --maxcpu 0                          # 自动检测 CPU 数
python opt_pipeline/param_optimize.py --all-stocks                        # 全量标的池（手动触发）
python opt_pipeline/param_optimize.py --maxcpu 4 --stock-list 000725,600519  # 仅寻优指定股票
python opt_pipeline/param_optimize.py --mode tpe --epochs 500             # TPE 采样 500 点（seed 默认取 config）
python3 skyquant.py opt --mode tpe --epochs 300 --stock-list 000725       # 统一入口（只影响阶段一）
```

**输出 CSV 列**：`stock_code, strategy, {各策略参数}, final_capital, profit, profit_rate, sharpe_ratio, max_drawdown, calmar_ratio`（COMBO_METRIC_COLS，schema 变更后需删除旧 CSV 重跑，避免合并写残留旧行；grid/tpe 两种模式 schema 一致可互换）

**网格参数完整说明**：

通用参数（所有策略共用，定义在 BaseStrategy）：

| 参数 | 含义 | 类默认值 |
|------|------|----------|
| `max_risk_ratio` | 单笔最大风险占总资金比例，用于 ATR 仓位公式 `size = 资金×max_risk_ratio/(ATR×trail_atr_multiple)` | 0.02 |
| `take_profit_atr_multiple` | 固定止盈距离（ATR 倍数）；收盘价 ≥ 入场价 + take_profit_atr_multiple×ATR 即平仓 | **None（默认纯追踪止损，无固定止盈）** |
| `max_loss_stop_ratio` | 亏损侧最差止损地板=持仓均价×(1-该比例)；启用时初始止损=地板、追踪止损只在盈利侧 ratchet（已纳入网格 [0.2, 0.3]） | 0.30 |
| `average_down_drop` / `average_down_ratio` | 首次亏损达 drop 时摊低加仓（按当前持仓 × ratio，次日限价单，每笔交易仅一次）；加仓成交后地板随新均价下移（未纳入网格） | 0.10 / 0.25 |
| `trail_tighten_profit_multiple` | 动态止盈激活门槛（浮盈达该 ATR 倍数后收紧止损）；None 关闭 | None |
| `trail_tight_atr_multiple` | 动态止盈激活后的收紧追踪止损 ATR 倍数（应小于 trail_atr_multiple） | 0.8 |
| `cooldown_period_candles` | **Protection·CooldownPeriod（`stop_duration_candles` 口径）**：卖出成交后 N 根 K 线内禁止新开仓（None 关闭，已入网格 [None,5]） | None |
| `stoploss_guard_trade_limit` / `stoploss_guard_lookback_period_candles` / `stoploss_guard_stop_duration_candles` | **Protection·StoplossGuard**：回看 lookback 根 K 线内止损平仓达 limit 次即暂停开仓 stop_duration 根 K 线（None 关闭；仅 trade_limit 入网格 [None,3]，lookback/stop_duration 固定） | None / 60 / 24 |
| `max_allowed_drawdown` | **Protection·MaxDrawdown（`max_allowed_drawdown` 同名）**：净值自峰值回撤超该比例期间禁止新开仓（None 关闭，已入网格 [None,0.2]） | None |

各策略专属参数：

| 参数 | 所属策略 | 含义 | 类默认值 |
|------|----------|------|----------|
| `trail_atr_multiple` | 全部 3 个策略 | 追踪止损 ATR 倍数（stop=最高价−trail_atr_multiple×ATR），兼作仓位分母 | trend 1.6 / range 2.0 / breakout 2.0 |
| `momentum_period` | trend | 动量确认周期，Momentum>0 才开仓 | 20 |
| `ema_fast` / `ema_slow` | trend | EMA 多头过滤：快/慢均线多头排列才允许开仓（freqtrade ema_*；未纳入网格） | 20 / 60 |
| `macd_fast` / `macd_slow` / `macd_signal` | trend | MACD 多头过滤的 EMA/信号线周期（指标属性 macd/macdsignal/macdhist） | 12 / 26 / 9 |
| `macd_momentum_bars` | trend | MACD 多头动量确认：DIF/柱需连续放大的 K 线数（未纳入网格） | 2 |
| `min_volatility_ratio` | trend | 波动率过滤：ATR/close 超过该下限才允许开仓 | 0.015 |
| `adx_period` / `adx_min` | trend | ADX 趋势强度过滤：ADX >= adx_min 才开仓（< 视为横盘震荡），且要求 plus_di > minus_di 多头方向（指标属性 adx/plus_di/minus_di；adx_period 未纳入网格） | 14 / 20 |
| `drop_ratio` | range | 单日跌幅阈值，(preclose−close)/preclose > drop_ratio 视为超卖 | 0.18 |
| `bb_period` | range | 布林带周期（freqtrade bb-period；指标属性 bb_upperband/bb_middleband/bb_lowerband） | 20 |
| `breakout_period` | breakout | 唐奇安通道周期（指标属性 donchian_upper/donchian_lower），突破过去 N 日最高/最低 | 20 |

> 全局共用为止盈止损/仓位类；趋势过滤参数（ema_*/macd_*/min_volatility_ratio/adx_*）为 trend 专属，range/breakout 不持有也不接受这些参数。

各策略完整网格取值（`PARAM_GRID`）：

| 策略 | 网格参数 → 取值 | 组合数 |
|------|------------------|--------|
| trend | 专属维度：`trail_atr_multiple` [1.4,1.6,1.8]；`momentum_period` [18,20,22]；`min_volatility_ratio` [0.008,0.025]；`max_risk_ratio` [0.02,0.025]；`trail_tighten_profit_multiple` [1.0,1.5,2.0]；`trail_tight_atr_multiple` [0.8,1.0]；`macd_fast` [10,12]；`macd_slow` [21,26]；`macd_signal` [7,9]；`adx_min` [20,25]；`max_loss_stop_ratio` [0.2,0.3]；+ PROTECTIONS_GRID ×8 | 55296 |
| range | 专属维度：`trail_atr_multiple` [1.8,2.0,2.2]；`bb_period` [18,20,22]；`drop_ratio` [0.15,0.18,0.2]；`max_risk_ratio` [0.02,0.025]；`trail_tighten_profit_multiple` [1.0,1.5,2.0]；`trail_tight_atr_multiple` [0.8,1.0]；`max_loss_stop_ratio` [0.2,0.3]（无 macd/adx/vol 参数，因无趋势过滤）；+ PROTECTIONS_GRID ×8 | 5184 |
| breakout | 专属维度：`trail_atr_multiple` [1.8,2.0,2.2]；`breakout_period` [15,20,30]；`max_risk_ratio` [0.02,0.025]；`trail_tighten_profit_multiple` [1.0,1.5,2.0]；`trail_tight_atr_multiple` [0.8,1.0]；`max_loss_stop_ratio` [0.2,0.3]；+ PROTECTIONS_GRID ×8 | 1728 |

> **PROTECTIONS_GRID**（三策略共享，param_optimize.py 顶部常量，参数名对齐 freqtrade）：`cooldown_period_candles` [None,5]；`stoploss_guard_trade_limit` [None,3]（lookback 60/stop_duration 24 固定）；`max_allowed_drawdown` [None,0.2]。二元开/关维度，让寻优按标的决定是否启用保护；None 组合经 CSV 往返被 extract_params 丢弃，不写 config（基类默认 None 生效）。
>
> 单只股票三策略合计 62208 个组合；仅保留 `profit_rate > 0` 的组合写入 CSV。

**注意事项**：
- **专属参数必须纳入网格**：`export_param_set.py` 只写网格产出的列，未进网格的参数（如曾遗漏的 `min_volatility_ratio`）会在导出参数集时丢失并静默回退类默认值。
- **固定止盈已从网格移除**：`take_profit_atr_multiple` 默认 None（纯追踪止损为强制默认行为），如需寻优固定止盈需显式在网格加回正值维度。
- `ema_fast`/`ema_slow`/`atr_period`/`macd_momentum_bars` 当前未纳入网格，按类默认值固定。
- 参数命名约定：追踪/止盈类倍数统一以 `*_atr_multiple` 结尾（不用缩写），追踪相关以 `trail_` 开头。

### opt_pipeline/out_sample_verify.py — 外样本校验（剔除训练集过拟合）

按 `opt_pipeline.out_sample_train_end` 将每只标的数据切成训练段 / 测试段，分别回测，若训练收益率 − 测试收益率 > `out_sample_overfit_threshold` 则判为过拟合剔除。

```python
def split_train_test(df, train_end) -> (df_train, df_test)
```

**配置项**（config.yaml 的 `opt_pipeline` 段）：

| 键 | 默认值 | 含义 |
|------|--------|------|
| out_sample_train_end | '2024-12-31' | 训练/测试切分日期 |
| out_sample_overfit_threshold | 0.15 | 训练−测试收益率差值阈值 |

**数据不足保护**：训练段或测试段 K 线数 < 60（SMA60 最小周期下限）时跳过该标的并打印 `[ERROR]` 提示，附 3 条改进方法（前移 start_date、后移 train_end、接受数据窗口过短）。

### opt_pipeline/rolling_window_verify.py — 滚动窗口稳定性校验

按 `rolling_start_year` 起、`rolling_train_years` + `rolling_test_years` 长度滚动生成多个年度对齐窗口，对每个测试段回测并取平均收益率，平均收益 > 0 视为稳定。窗口同时满足 `rolling_min_train_bars` / `rolling_min_test_bars` 才被采纳。

```python
def rolling_slice(df, start_year=2020, train_years=4, test_years=1,
                  min_train_bars=200, min_test_bars=100) -> List[Tuple[df_train, df_test]]
```

**配置项**（config.yaml 的 `opt_pipeline` 段）：

| 键 | 默认值 | 含义 |
|------|--------|------|
| rolling_start_year | 2020 | 首个窗口起点年份 |
| rolling_train_years | 4 | 训练窗口长度（年） |
| rolling_test_years | 1 | 测试窗口长度（年） |
| rolling_min_train_bars | 200 | 训练段最低 K 线数 |
| rolling_min_test_bars | 100 | 测试段最低 K 线数（>60，不可低于 SMA60 minperiod） |

**数据不足保护**：若某标的在所有 7 个滚动窗口中均不满足最低 K 线数（即 `rolling_slice` 返回空），跳过该标的并打印 `[ERROR]` 提示，附 4 条改进方法（前移 start_date、缩小 train/test_years、降低 rolling_min_*_bars 但不得低于 60、后移 start_year）。若全部标的被跳过、结果表为空，额外打印整体失败提示。

**start_date 下限**（end_date=2026-09-21、默认滚动参数）：
- 至少 1 个有效窗口（训练段 >200 根）：start_date ≤ 2024-03-04
- 推荐（训练段 242 根 + 2 个有效窗口）：start_date = 2024-01-01

## 策略修改检查清单与回归基线

每次修改 `strategy/`（含 BaseStrategy、任一子类、参数默认值、PARAM_GRID）后，**必须逐项过以下清单**。本清单沉淀自历史踩坑（迭代器 pickle、参数静默回退、盘中/收盘口径混用、固定止盈截断盈利等）。

### A. 逻辑正确性

1. **追踪止损只上不下（ratchet）**：`stop_price` 的所有写路径必须在 `BaseStrategy._update_trailing_stop` 内，且保留 `if candidate_stop > self.stop_price` 守卫；子类对 `stop_price` 只读，任何新出场逻辑不得直接下移止损。
2. **入场过滤归策略自管**：基类不设方向性门控，`next()` 无仓时直接调 `populate_entry_trend()`；策略专属过滤（如 trend 的 `_trend_filters_ok`）写在该策略文件内，不得上移到基类强加给所有策略（历史教训：四重过滤/EMA 前提做全局门控均损伤非趋势策略）。标的适配由 stock_filter 层负责，不靠策略内过滤补救。
3. **出场比较符统一**：所有子类 `populate_exit_trend` 对止损的比较统一用收盘价 `close[0] <= self.stop_price`（3 个策略均为 `<=`，含等于止损价的边界 K 线，边界明确不留歧义）。
4. **收盘价确认口径**：止损/止盈触发统一用当根**收盘价**，不得在策略里改用盘中 `low`/`high`——盘中插针口径与收盘价口径同参数结果不可比（历史教训）。
5. **ATR NaN 守卫**：任何新增使用 `self.atr[0]` 的计算必须防预热期 NaN（`math.isnan` 判空），否则 `int(nan)` 崩溃。
6. **平仓状态完整重置**：`_close_position` 必须重置 `stop_price/take_price/entry_price/entry_bar/entry_atr_multiple`，避免下一笔仓位状态残留。
7. **只做多、不做空**；无交易（0 trades）的策略先确认是策略自身过滤严格的预期结果，再排查 bug。

### B. 参数契约

8. **命名约定**：倍数类参数全称 `*_atr_multiple`（禁止 mult 缩写），追踪类以 `trail_` 开头；新参数名要自解释（如浮盈门槛用 `trail_tighten_profit_multiple`）。
9. **新参数三处同步**：策略 `params` → `DEFAULT_STRATEGY_PARAMS`（如需默认回退）→ `PARAM_GRID`（**必须进网格**，否则 `export_param_set` 不产出该列，参数集静默回退类默认值）；看板中文标签还需同步 dashboard.py 的 `PARAM_CN`。
10. **过滤参数归属**：公共风控/执行参数（atr_period/止损/止盈/摊低/Protections）定义在 BaseStrategy；方向性过滤参数（ema_*/macd_*/min_volatility_ratio/adx_*）属于 trend 专属，定义在 trend 子类，不上移基类；策略专属指标放子类 `populate_indicators`，命名对齐 freqtrade（macd/macdsignal/macdhist、adx/plus_di/minus_di、bb_*band）。
11. **纯追踪止损默认**：`take_profit_atr_multiple` 默认 None，PARAM_GRID 不含固定止盈维度；加回固定止盈需显式评估（历史数据证明它截断盈利、盈亏比恶化）。
12. **重命名时全量替换**：策略 id / 参数改名要同步 策略文件、`__init__.py`、`param_optimize.py`、dashboard.py `PARAM_CN`、AGENTS.md、spec.md（grep 旧名零残留），历史参数集 params/*.yaml 与 `output/*.csv` 旧产物下次保存/跑流水线自动重建。

### C. 注册表与接口

13. **注册表同步**：新增/改名策略更新 `STRATEGY_MAPPING`、`STRATEGY_DESCRIPTIONS`、`DEFAULT_STRATEGY_PARAMS` 及 `main.py` 的 `DEFAULT_STRATEGY`。
14. **三钩子 + 日志接口**：子类实现 `populate_indicators/populate_entry_trend/populate_exit_trend`；`get_equity_dataframe/get_trade_dataframe/get_action_dataframe` 必须可用（main/live_trading 均依赖）。
15. **多进程可 pickle**：新增 worker/任务入参必须是普通可 pickle 对象，函数定义在模块顶层（spawn 要求）；不得把 Cerebro/迭代器传入子进程。
16. **筛选层契约**：筛选阈值改 config.yaml `stock_filter` + stock_filter.py 的 DEFAULT_* 双处；`output/stock_filter.csv` 列名变更需同步全部消费方（main.py、live_trading.py、param_optimize.py 的 load_regime_map/routed_strategies/strategy_for_code）；报告缺失必须安全降级（不剔除、不路由）。优化目标列（profit_rate/sharpe/calmar）变更时同步 COMBO/ROLLING 列常量与 aggregate 排序列。

### D. 验证步骤（按顺序）

17. **3 策略冒烟 + 自动路由**：`for s in trend range breakout; do python3 main.py --stock-list 000725 --strategy $s; done`，再跑一次 `python3 main.py --stock-list 000725`（不带 --strategy，验证 stock_filter.csv regime 自动路由），全部正常产出净值与 HTML。
18. **回归基线（必跑）**：
    ```bash
    python tests/regression/run_regression.py                 # 对比 golden.json，有差异退出码非 0
    python tests/regression/run_regression.py --update-golden # 仅在确认是"有意改进"后更新基线
    ```
    - **SAME**：行为无漂移，通过；
    - **IMPROVED**（仅净值正向、结构指标不变）：人工确认改进成立后才允许 `--update-golden`；
    - **CHANGED**（交易笔数/买卖次数/bars/参数签名变化）或 **DEGRADED**（净值下降）：必须解释或回退，**禁止直接更新 golden 掩盖退化**。
19. **寻优路径**：改动参数后跑一次 `python opt_pipeline/param_optimize.py --stock-list 000725 --maxcpu 1`（WSL 正常环境可 --maxcpu 2 或 0；TRAE macOS 沙箱必须 `--maxcpu 1`），确认 regime 路由、CSV 新列名与多进程 spawn 路径正常；schema 变更时先删除旧阶段 CSV。
20. **全流水线**：`python3 skyquant.py all --stock-list 000725`（行情→常驻 Pairlist Filters 筛选→该标的寻优 5 阶段闭环→自动路由回测→复盘）全部通过；修改默认行为后另跑一次 `python3 skyquant.py all`（默认回归标的集）确认默认目标集解析正确。
21. **文档同步**：AGENTS.md（分层架构/参数表/策略表/网格表/本清单）与 spec.md 与代码一致。

### 回归基线说明（tests/regression/）

- `run_regression.py`：固定标的集（`REGRESSION_STOCKS = ["000725"]`，定义在 opt_pipeline/common.py，与参数寻优默认集同源；后续按需手动向该列表添加标的）× 全部注册策略，用**类默认参数**（`DEFAULT_STRATEGY_PARAMS`，不用 active 参数集寻优参数）+ **本地缓存数据**（无网络）+ config 固定日期窗口，保证确定性可复现。
- `golden.json`：每个 case 记录 `params 签名 / bars / final_value / total_return / n_trades / n_wins / n_buys / n_sells`；`final_value` 容差 0.01 元，结构指标精确匹配。
- 回归脚本回答的是"策略代码改动是否改变了行为、改变方向是改进还是退化"；参数寻优（PARAM_GRID）回答的是"哪组参数更优"，两者目的不同，不可互相替代。

### 新会话推荐工作流

1. 读 `agents.md` — 项目全貌、开发约定、关键决策、函数签名与算法逻辑
2. 读 `spec.md` — 详细需求规范、模块职责、输出标准
3. 读 `config.yaml`（固定配置：标的池、费率、阈值）+ `params/active.yaml`（生效策略参数）；读 `config_store.py` 了解参数集加载/合并/归档机制
4. 读目标源文件 — 完整实现细节（agents.md 中的签名/算法为快速参考，完整逻辑仍需阅读源码）
5. 修改代码 — 遵循开发约定（英文注释、Optional[X]、Ruff 格式化）