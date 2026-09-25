# SkyQuant 项目上下文文档

> 本文件供 AI Agent 新会话快速了解项目背景，避免重复沟通。详细规范见 spec.md。

## 项目简介

SkyQuant 是一套 A 股日线量化策略回测流水线，支持全自动参数寻优、过拟合校验、策略回测、指标计算、可视化绘图、手工交易复盘，以及每日收盘后信号生成。

## 运行环境

- WSL Ubuntu 24.04，Python 3.12.3
- Ruff 0.16.8 安装在 ~/.local/bin/ruff（格式化工具，行宽 88）
- Tushare 凭据存放于 ~/.skyquant/tushare.yaml（不入库）
- VS Code Ruff 扩展配置在 .vscode/settings.json（formatOnSave + source.fixAll.ruff）

## 运行方式

```bash
# 全量流水线（8 步：行情拉取→寻优→校验→聚合→写配置→回测→复盘）
python run_all.py
python run_all.py --skip-data          # 缓存加速模式
python run_all.py --stock-list 000725,600519   # 仅运行指定股票（逗号分隔）

# 每日信号生成（收盘后运行，输出买卖信号与次日操作建议）
python daily_signal.py
python daily_signal.py --force_refresh  # 强制全量下载

# 单次回测
python main.py --strategy maatr_base
python main.py --stock-list 000725 --strategy maatr_base  # 仅回测指定股票
```

### `--stock-list` 参数说明

`run_all.py`、`main.py`、`param_optimize.py`、`manual_trade_review.py`、`daily_signal.py` 均支持 `--stock-list` 参数，格式为逗号分隔的股票代码列表：

- 不传时使用 config.yaml 中 `stock_list` 的全部标的
- 传 `--stock-list 000725,600519` 时仅处理指定股票
- `run_all.py` 会将该参数透传给主回测、参数寻优、手工复盘三步；流水线中间步骤（out_sample/rolling/aggregate/write_config）读取前一步 CSV 产出，天然被过滤，无需额外传参

## 文件清单

| 文件 | 职责 |
|------|------|
| run_all.py | 全流水线调度入口，日志管理，异常终止 |
| main.py | 回测主引擎：加载配置、遍历标的、执行回测、输出指标与图表 |
| daily_signal.py | 每日信号生成：运行策略、输出买卖/持有信号与操作建议 |
| data_source.py | Tushare 行情拉取、增量更新、本地缓存、AStockData feed |
| comm.py | A 股交易手续费模型（佣金、印花税、过户费） |
| metrics_utils.py | 量化指标计算：年化收益、最大回撤、夏普比率、胜率、盈亏比 |
| plot_utils.py | 可视化：自包含 HTML 回测报告（KPI/净值-回撤图/交易表/Analyzer）、btplotting K 线图 |
| manual_trade_review.py | 手工交易复盘：策略信号匹配、对比统计 |
| strategy/base.py | 策略基类：ATR 仓位管理、追踪止损、动态止盈、action_log 信号日志、统一输出接口 |
| strategy/__init__.py | STRATEGY_MAPPING 策略注册表、DEFAULT_STRATEGY_PARAMS 默认参数 |
| strategy/maatr_base.py | 均线+ATR 策略：均线多头+波动率过滤买入，继承基类追踪止损+动态止盈 |
| strategy/momentum.py | 动量策略：动量为正买入，继承基类追踪止损+动态止盈 |
| strategy/short_reversal.py | 短期反转策略：跌幅超阈值买入，继承基类追踪止损+动态止盈 |
| strategy/boll_ma.py | 布林带+均线策略：回踩下轨买入，继承基类追踪止损+动态止盈 |
| strategy/multi_factor.py | 多因子策略：均线多头+跌幅过滤买入，继承基类追踪止损+动态止盈 |
| opt_pipeline/common.py | 流水线共享：BacktestRunner、路径常量、参数提取工具 |
| opt_pipeline/param_optimize.py | 网格参数寻优（自建 multiprocessing.Pool 多进程） |
| opt_pipeline/out_sample_verify.py | 外样本校验（剔除过拟合） |
| opt_pipeline/rolling_window_verify.py | 滚动窗口稳定性校验 |
| opt_pipeline/aggregate_best_param.py | 最优参数聚合 |
| opt_pipeline/write_param_to_config.py | 参数写入 config.yaml |
| ruff.toml | Ruff 配置（target-version=py39, line-length=260） |

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

3. **end_date 覆盖**：config.yaml 的 global_setting.end_date 是固定值。daily_signal.py 运行时会覆盖 ds.end_date 为当天日期，确保增量拉取覆盖最新行情。

4. **Tushare 凭据隔离**：token 不放在 config.yaml，独立存放于 ~/.skyquant/tushare.yaml，不随项目入库。

5. **opt_pipeline 路径引导**：opt_pipeline/common.py 统一处理 sys.path 注入项目根目录，各阶段脚本只需 from common import ...。

6. **自建进程池网格寻优（不使用 optstrategy）**：参数寻优在父进程用 `itertools.product` 枚举参数组合，通过模块顶层 worker 函数 `_run_single_combo` + `multiprocessing.Pool.map` 实现多进程并行。不使用 `cerebro.optstrategy()` 的原因：optstrategy 会把惰性的 `itertools.product` 迭代器挂在 Cerebro 上，多进程时将 Cerebro 自身 pickle 给子进程；Python 3.14 起 `itertools.product`/`itertools.count` 不可 pickle（报 `cannot pickle 'itertools.product' object`）。worker 入参全部是普通可 pickle 对象（df/strategy_id/params/资金/费率 dict），Cerebro 在子进程内部用 `addstrategy` 构建，通过自定义 `FinalValueAnalyzer` 捕获最终资产值。worker 必须是模块顶层函数（spawn 模式要求可 import），不能是闭包/lambda。

7. **行情数据加载用 PandasData 而非 GenericCSVData**：AStockData 继承 `bt.feeds.PandasData`，不使用 backtrader 内置的 `GenericCSVData`。原因：
   - **数据生命周期中段有 DataFrame 处理步骤**：`fetch_stock` 在加载后要做增量合并（`pd.concat → drop_duplicates → sort_values`）、start_date 过滤（`df[df["datetime"] >= start_dt]`）、format_df 列名规范化。这些必须以 DataFrame 形式处理，GenericCSVData 直接读文件的路径走不通。
   - **扩展列两边都得子类化，没省事**：项目 CSV 含 `preclose/amount/turn/pctChg` 4 个扩展列。GenericCSVData 默认只有 datetime/open/high/low/close/volume/openinterest 7 个 line，挂扩展列仍需 `lines += (...)` + `params += (...)` 子类化，与 PandasData 子类化代码量相当。
   - **列名匹配**：CSV 中 `pctChg` 是驼峰命名，PandasData 用 `-1` 自动按 line 名匹配 DataFrame 列，GenericCSVData 则需逐列显式配 `params=(("open", 1), ...)`，更繁琐。
   - **datetime 类型精度**：GenericCSVData 走字符串 `dtformat` 解析，而 `_filter_by_start_date` 需向量化比较 `df["datetime"] >= start_dt`，PandasData 路径天然支持 `parse_dates=["datetime"]`。
   - **适用边界**：GenericCSVData 仅适合纯静态、列名规范、无中间处理、只用标准 OHLCV 的场景；本项目命中 PandasData 全部适用条件（增量更新/合并/过滤、挂载扩展列、列名非标准）。

## 配置文件

- config.yaml：全局配置（资金、时间区间、费率、标的池、策略参数、opt_pipeline 校验参数）
- manual_trades.csv：手工交易记录（trade_date, stock_code, side, price, size）
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
- `profit_multiple=2.0`（固定止盈距离 ATR 倍数，默认启用；显式传 None 关闭）
- `trail_profit_activate=None`（动态止盈激活阈值，浮盈达该 ATR 倍数后收紧止损；None 关闭）
- `trail_tight_multiple=0.8`（动态止盈激活后的收紧追踪止损 ATR 倍数）

**核心算法**：

```
ATR 仓位公式:  size = int(总资产 * max_risk_ratio / (ATR * atr_mult))
固定止盈价:    take_price = entry_price + ATR * profit_multiple（profit_multiple 非 None 时）
追踪止损价:    stop = 持仓以来最高价 - atr_mult × ATR（只上不下 ratchet）
动态止盈收紧:  浮盈(最高价 - entry_price) >= trail_profit_activate × ATR 时，
              stop = max(stop, 最高价 - trail_tight_multiple × ATR)
```

**方法签名**：

```python
def _position_size(self, atr_mult) -> int        # ATR 仓位计算（含 NaN 守卫）
def _open_position(self, atr_mult)                # 买入 + 设初始止损/止盈 + 记录 entry_bar/entry_atr_mult
def _close_position(self)                         # 卖出 + 重置全部持仓状态 + 记录 action_log
def _update_trailing_stop(self)                   # 追踪止损 + 动态止盈更新（next 调用 _on_exit 前自动执行）
def next(self)                                    # 模板方法: 无仓->_on_entry(); 有仓先查固定止盈, 再 _update_trailing_stop, 再 _on_exit()
def _on_entry(self)                               # 子类重写: 入场条件
def _on_exit(self)                                # 子类重写: 出场条件（检查 stop_price 或策略专属信号）
def stop(self)                                    # 回测结束: self.final_value = broker.getvalue()
def get_equity_dataframe() -> pd.DataFrame        # 每日净值
def get_trade_dataframe() -> pd.DataFrame         # 已平仓交易记录
def get_action_dataframe() -> pd.DataFrame        # 决策时信号日志（含未平仓）
```

**日志格式**：
- `action_log`: `{date, side: "BUY"/"SELL", price, size}`
- `trade_log`: `{entry_date, exit_date, entry_price, exit_price, size, profit_loss, profit_loss_net, profit_rate}`
- `equity_log`: `{datetime, equity}`

**多层级平仓优先级**：固定止盈 > 追踪止损/动态止盈 > 子类信号止损。

### 策略子类 — 入场/出场条件

| 策略 | 关键参数（默认值） | 入场条件 | 出场条件 |
|------|-------------------|----------|----------|
| maatr_base | `atr_multiple=1.6`, `atr_min_rel=0.015`, `sma_fast=20`, `sma_slow=60` | SMA(fast) > SMA(slow) 且 ATR/close > atr_min_rel | 固定止盈 / 追踪止损+动态止盈 / 跌破止损 |
| momentum | `atr_multiple=1.5`, `momentum_period=20` | Momentum(period) > 0 | 固定止盈 / 追踪止损+动态止盈 / 动量转负 |
| short_reversal | `atr_mult=2.0`, `fall_ratio=0.18` | (preclose-close)/preclose > fall_ratio | 固定止盈 / 追踪止损+动态止盈 / 跌破止损 |
| boll_ma | `atr_mult=1.6`, `boll_period=20` | close <= 布林下轨 且 close > SMA(60) | 固定止盈 / 追踪止损+动态止盈 / 突破布林上轨 |
| multi_factor | `atr_mult=1.7` | SMA(20) > SMA(60) 且 pctChg > -5 | 固定止盈 / 追踪止损+动态止盈 / 均线死叉 |

> 注：`profit_multiple`、`trail_profit_activate`、`trail_tight_multiple` 三个止盈止损参数定义在 BaseStrategy，所有 5 个策略（含 maatr_base）统一继承。追踪止损与动态止盈由基类 `_update_trailing_stop` 自动处理，子类无需实现。

### maatr_base 策略详解（均线交叉 + ATR 追踪止损）

源文件：[strategy/maatr_base.py](strategy/maatr_base.py)。**继承 BaseStrategy**，只实现 `_init_indicators`（建快慢均线，ATR 由基类创建）、`_on_entry`（均线多头 + 波动率过滤，调用基类 `_open_position(atr_multiple)`）、`_on_exit`（仅检查基类维护的追踪止损）。追踪止损、动态止盈、仓位管理、固定止盈、日志接口全部复用基类。

**子类参数**（在基类参数之上新增/覆盖）：

```
sma_fast=20, sma_slow=60      # 新增：快慢均线周期
atr_multiple=1.6              # 新增：追踪止损 ATR 倍数（兼作仓位分母）
atr_min_rel=0.015             # 新增：波动率过滤阈值
max_risk_ratio=0.015          # 覆盖基类默认 0.02
profit_multiple=None          # 覆盖基类默认 2.0，默认纯追踪止损
```

**指标**（`_init_indicators` 只建均线，ATR 由基类 `__init__` 创建）：

```
sma_fast = SMA(close, sma_fast)          # 默认 20
sma_slow = SMA(close, sma_slow)          # 默认 60
atr      = ATR(period=atr_period)        # 基类创建，默认 14
```

**入场逻辑**（`_on_entry`，基类 `next()` 在空仓时调用）：

1. 信号：`sma_fast[0] > sma_slow[0]` 且 `atr[0]/close[0] > atr_min_rel`——均线多头排列且波动率达标（过滤横盘假突破）
2. 触发后调用基类 `self._open_position(self.p.atr_multiple)`：
   - 仓位：`size = int(broker.getvalue() * max_risk_ratio / (atr[0] * atr_multiple))`
   - 初始止损：`stop_price = entry_price - atr_multiple * atr[0]`
   - 固定止盈（可选）：`take_price = entry_price + profit_multiple * atr[0]`

**出场逻辑**（全部由基类 `next()` 模板驱动，优先级从高到低）：

1. 固定止盈：基类检查 `close[0] >= take_price` → 平仓
2. 基类 `_update_trailing_stop()` 更新追踪止损 + 动态止盈：
   - `highest = max(持仓以来最高价)`
   - `candidate = highest - atr_multiple * atr[0]`（基础追踪）
   - 若 `(highest - entry_price)/atr[0] >= trail_profit_activate`：`candidate = max(candidate, highest - trail_tight_multiple * atr[0])`（动态收紧）
   - `stop_price = max(stop_price, candidate)`（只上不下）
3. `_on_exit` 检查 `close[0] <= stop_price` → 平仓

**关键设计约束**：

- **只做多、不做空**
- **追踪止损而非固定止损**：`stop_price` 随持仓最高价上移（ratchet），只上不下
- **动态止盈不封顶上行**：盈利达标后收紧止损倍数，但价格继续上涨时止损同步上移，不会截断大趋势
- **指标预热**：SMA(60) 需 60 根 K 线；ATR 预热期 `atr[0]` 为 NaN，基类 `_position_size` 已守卫

### strategy/__init__.py — 策略注册表

```python
STRATEGY_MAPPING = {
    "maatr_base": MAATRBaseStrategy,
    "momentum": MomentumStrategy,
    "short_reversal": ShortReversalStrategy,
    "boll_ma": BollMAStrategy,
    "multi_factor": MultiFactorStrategy,
}

DEFAULT_STRATEGY_PARAMS = {
    "maatr_base": {"atr_multiple": 1.6, "max_risk_ratio": 0.015},
    "momentum": {"atr_multiple": 1.5, "momentum_period": 20, "max_risk_ratio": 0.02},
    "short_reversal": {"atr_mult": 2.0, "fall_ratio": 0.18, "max_risk_ratio": 0.02},
    "boll_ma": {"atr_mult": 1.6, "boll_period": 20, "max_risk_ratio": 0.02},
    "multi_factor": {"atr_mult": 1.7, "max_risk_ratio": 0.02},
}
```

`DEFAULT_STRATEGY_PARAMS` 为各策略的默认参数，当 config.yaml 的 `strategy_params` 中没有某只股票的优化参数时，`daily_signal.py` 和 `manual_trade_review.py` 会回退使用这些默认参数。

### data_source.py

**AStockData(bt.feeds.PandasData)**：扩展 K 线 feed，挂载 `preclose`/`amount`/`turn`/`pctChg` 扩展列。

```python
class DataSource:
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

### comm.py — AStockCommission(bt.CommInfoBase)

**参数**：`commission=0.0003`, `stamp_duty=0.001`, `transfer_fee=0.00001`

**费率**：买入=佣金+过户费; 卖出=佣金+过户费+印花税

### metrics_utils.py

```python
def calc_metrics(equity_df, trades_df, risk_free_rate=0.02) -> dict
```

**指标公式**：
- 年化收益：`(1 + total_return) ** (365/days) - 1`
- 最大回撤：`(equity - cummax) / cummax` 的最小值
- 夏普比率：`sqrt(252) * mean(excess_ret) / std(excess_ret)`
- 胜率：`profit_loss_net > 0 的交易数 / 总交易数`
- 盈亏比：`总盈利金额 / 总亏损金额`

### plot_utils.py

```python
def render_report(equity_df, trades_df, action_df, metrics, analyzer_results,
                  out_dir, code, strategy_name, start_date, end_date,
                  initial_capital, final_value,
                  interactive_html=None, stock_name=None) -> str  # 返回 HTML 路径
def render_interactive_chart(strategy, out_dir, code, strategy_name) -> str     # btplotting K 线图路径
```

**render_report 设计**：

- 使用 Bokeh `components()` + `INLINE` 资源输出自包含 HTML，可离线打开
- 内部模块：`_build_equity_plot`（净值图 + 联动 x 轴的回撤 varea 填充）、`_build_trade_table`（DataTable + `NumberFormatter`）、`_flatten_analyzer`（递归压平 namedtuple/dict/list 到 `(path, value)` 对）、`_build_action_table_html`、`_build_analyzer_table_html`
- 调用方（main.py）注册 5 个 analyzer：`Returns / SharpeRatio / DrawDown / TradeAnalyzer / SQN`，名称见 `ANALYZER_NAMES` 常量；`getattr(strategy_instance.analyzers, name).get_analysis()` 提取后传入 `analyzer_results`
- 产物：`output/plots/{code}_{strategy_name}_report.html`，相对链接到 `_{strategy_name}_interactive.html`（btplotting）
- **btplotting/Python 3.14 兼容层**（项目边界内，不改 site-packages）：
  - 惰性子类 `Py314CompatibleBacktraderPlotting(BacktraderPlotting)` 覆盖 `generate_bokeh_model()`，把第三方 `filter(None.__ne__, tab_panels)` 替换为 `[p for p in tab_panels if p is not None]`（Python 3.14 下 Bokeh Model 的 `None.__ne__` 返回 `NotImplemented`，bool 化报 `TypeError`）
  - 渲染前 `_replace_empty_sentinels()` 遍历所有 analyzer 的 `get_analysis()`，把 backtrader 的 MAXINT(2^63−1) 空集合哨兵（只做多策略 `TradeAnalyzer.len.{short,short.won,short.lost}.min` 必现）原地替换为 None，避免 btplotting Analyzers 表序列化出 JS 安全整数范围外的 `BokehUserWarning`；`render_report` 的 analyzer 表将 None/哨兵显示为 N/A
  - btplotting 内部调用已弃用的 `figure.circle/triangle(size=...)`，仅在 btplotting 调用边界 `catch_warnings` 抑制 `BokehDeprecationWarning`，不全局抑制
- 已删除：旧的 `plot_all` / `plot_equity_drawdown` / `plot_win_pie` / `_setup_chinese_font`（matplotlib PNG 路径全部移除）

### daily_signal.py

```python
def compute_holdings(trade_csv: Path) -> Dict[str, dict]        # BUY累加SELL累减, 返回{code: {size, avg_cost}}
def run_strategy_actions(ds, comminfo, cfg, code, strategy_id, param) -> Optional[pd.DataFrame]  # 返回action_log
def classify_signal(action_df, last_bar_date) -> dict            # 最后action日期==last_bar_date->该action; 否则持仓->HOLD/空仓->WAIT
def compute_consensus(actions: List[str]) -> str                # SELL > BUY > HOLD > WAIT
def derive_suggested_action(consensus: str, currently_held: bool) -> str  # 持仓+SELL->卖出, 持仓+BUY->加仓, 未持仓+BUY->买入
def build_report_rows(ds, comminfo, cfg, param_pool, stock_name_map, holdings, report_date, stock_list) -> List[dict]
def print_console_summary(df, holdings, report_date)            # 三段式: 持仓操作/关注列表/统计汇总
```

**命令行参数**：`--force_refresh`（强制全量下载）、`--stock-list`（逗号分隔股票代码，过滤 stock_list）

**默认参数回退**：当某只股票在 config.yaml 的 `strategy_params` 中没有优化后的参数时，使用 `strategy/__init__.py` 中的 `DEFAULT_STRATEGY_PARAMS` 作为回退，确保所有股票都能生成信号。

### main.py

```python
def run_backtest(dataSource, comminfo, global_setting, param_pool, code, strategy_id, force_refresh, stock_name=None) -> dict
def get_strategy_param(param_pool, code, strategy_id) -> (strategy_cls, params)
def validate_manual_trades(valid_codes)
```

**命令行参数**：`--force_refresh`（强制全量下载）、`--strategy`（策略 id，默认 maatr_base）、`--stock-list`（逗号分隔股票代码，过滤 stock_list）

> `--interactive` 已移除。每次回测默认生成自包含 HTML 报表与 btplotting K 线图，无需额外开关。

**Cerebro 配置模式**（main.py / daily_signal.py / manual_trade_review.py 共用）：

```python
cerebro = bt.Cerebro()
cerebro.addstrategy(STRATEGY_MAPPING[strategy_id], **param)
cerebro.adddata(AStockData(dataname=df, datetime="datetime", open="open", high="high", low="low", close="close", volume="volume"))
cerebro.broker.setcash(cfg["global_setting"]["initial_capital"])
cerebro.broker.addcommissioninfo(comminfo)
# main.py 专有：注册 analyzers 供 HTML 报表使用
#   Returns / SharpeRatio / DrawDown / TradeAnalyzer / SQN（_name 见 ANALYZER_NAMES）
strategy_instance = cerebro.run()[0]
```

**HTML 报表产物**（main.py `run_backtest` 内）：

- 每只标的生成 `output/plots/{code}_{strategy_id}_report.html`（自包含 Bokeh INLINE，可离线打开）
- 同目录生成 `output/plots/{code}_{strategy_id}_interactive.html`（btplotting K 线 + 指标 + 成交标记，报表通过相对链接跳转）
- 报表内容：头部（标的/策略/区间/初始资金/最终净值/总收益）、6 张 KPI 卡片（年化/最大回撤/夏普/胜率/盈亏比/交易数）、净值-回撤联动图（Bokeh，hover tooltip）、平仓交易 DataTable、action_log 信号表（含未平仓 BUY）、Analyzer 字段表（递归 flatten 5 个 analyzer）、K 线图链接

### run_all.py

```python
def run_step(name, cwd, cmd)    # subprocess.Popen 执行, 非零退出码->sys.exit(1)
```

**命令行参数**：
- `--skip-data`：跳过行情拉取，使用本地缓存
- `--stock-list 000725,600519`：仅运行指定股票，透传给 main.py / param_optimize.py / manual_trade_review.py

8 步：行情拉取 -> param_optimize.py -> out_sample_verify.py -> rolling_window_verify.py -> aggregate_best_param.py -> write_param_to_config.py -> main.py -> manual_trade_review.py

**--stock-list 透传机制**：run_all.py 将 `--stock-list` 透传给第 1、2、7、8 步；第 3-6 步读取前一步 CSV 产出，天然被过滤。

### opt_pipeline/common.py

```python
class FinalValueAnalyzer(bt.Analyzer):
    def stop(self): self.final_value = self.strategy.broker.getvalue()
    def get_analysis(self): return self.final_value

def _run_single_combo(task) -> float
    # 模块顶层 pool worker（spawn 要求可 pickle）；入参均为普通对象:
    # (df, strategy_id, params, initial_capital, comm_config)
    # 子进程内部 addstrategy 构建 Cerebro，用 FinalValueAnalyzer 取最终资产值

class BacktestRunner:
    def __init__(self, data_source=None)
    def run(self, df, strategy_id, params) -> float                              # 单回测，返回最终资产值
    def profit_rate(self, final_value) -> float
    def optimize(df, strategy_id, param_grid, maxcpu=1) -> List[Tuple[dict, float]]  # 自建进程池网格寻优
        # 父进程 itertools.product 枚举网格 -> multiprocessing.Pool.map(_run_single_combo, tasks)
        # maxcpu<=1 时进程内串行；返回 [(param_dict, final_value), ...]，顺序与 product 枚举一致
```

**optimize 内部流程**：父进程 `itertools.product(*grid.values())` 物化为 param dict 列表（不使用 `cerebro.optstrategy`）→ 组装普通 tuple 任务 → `Pool.map` 分发到顶层 worker（子进程内 `addstrategy` + `FinalValueAnalyzer`）→ `pool.map` 保序返回最终资产值，与参数组合按位置配对。注意 backtrader 的参数名是 `maxcpus`（复数），本模块自建 Pool 不经过该参数。

```python
def extract_params(row, exclude_cols) -> dict    # 从CSV行提取策略参数, period类型强制int
def to_native(params: dict) -> dict              # numpy标量转Python原生类型
def read_stage_csv(path) -> pd.DataFrame          # stock_code强制str
```

### opt_pipeline/param_optimize.py — 自建进程池网格寻优

```python
PARAM_GRID = {strategy_id: {param_name: [values, ...]}}   # 网格定义

def main():
    parser.add_argument("--maxcpu", type=int, default=0)  # 0/-1 使用全部 CPU
    parser.add_argument("--stock-list", type=str, default=None)  # 逗号分隔股票代码
    # 遍历 stock_list(可过滤) -> 遍历 PARAM_GRID -> runner.optimize(df, strategy_id, grid, maxcpu)
    # 过滤 profit_rate > 0 -> 写入 output/param_optimize_result.csv
```

**运行方式**：
```bash
python opt_pipeline/param_optimize.py --maxcpu 1                          # 单进程（调试用）
python opt_pipeline/param_optimize.py --maxcpu 4                          # 4 进程并行
python opt_pipeline/param_optimize.py --maxcpu 0                          # 自动检测 CPU 数
python opt_pipeline/param_optimize.py --maxcpu 4 --stock-list 000725,600519  # 仅寻优指定股票
```

**输出 CSV 列**：`stock_code, strategy, {各策略参数}, final_capital, profit, profit_rate`

**profit_multiple 网格维度**：所有 5 个策略的网格均含 `profit_multiple: [2.0, 3.0, 4.0]`；策略类默认值为 2.0（止盈默认启用，显式传 None 才关闭）。

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

**数据不足保护**：若某标的在所有 7 个滚动窗口中均不满足最低 K 线数（即 `rolling_slice` 返回空），跳过该标的并打印 `[ERROR]` 提示，附 4 条改进方法（前移 start_date、缩小 train/test_years、降低 min_bars 但不得低于 60、后移 start_year）。若全部标的被跳过、结果表为空，额外打印整体失败提示。

**start_date 下限**（end_date=2026-09-21、默认滚动参数）：
- 至少 1 个有效窗口（训练段 >200 根）：start_date ≤ 2024-03-04
- 推荐（训练段 242 根 + 2 个有效窗口）：start_date = 2024-01-01

### manual_trade_review.py

```python
class ManualTradeReview:
    def __init__(self, config_path="config.yaml", trade_csv="manual_trades.csv", stock_list=None)
        # stock_list: 逗号分隔股票代码，过滤 manual_trades.csv
    def get_strategy_signal(self, code, strategy_id, param) -> Optional[pd.DataFrame]  # 从trade_log提取信号
    def match_manual_trade(self) -> pd.DataFrame       # 按交易日匹配策略信号
    def summary_report(self, out_csv="output/manual_review_result.csv") -> pd.DataFrame  # 匹配率/胜率/盈亏
```

**命令行参数**：`--stock-list`（逗号分隔股票代码，过滤 manual_trades.csv 中对应记录）

### 新会话推荐工作流

1. 读 `agents.md` — 项目全貌、开发约定、关键决策、函数签名与算法逻辑
2. 读 `spec.md` — 详细需求规范、模块职责、输出标准
3. 读 `config.yaml` — 标的池、策略参数、费率配置
4. 读目标源文件 — 完整实现细节（agents.md 中的签名/算法为快速参考，完整逻辑仍需阅读源码）
5. 修改代码 — 遵循开发约定（英文注释、Optional[X]、Ruff 格式化）