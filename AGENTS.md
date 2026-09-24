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
| plot_utils.py | 可视化：净值回撤图、胜率饼图 |
| manual_trade_review.py | 手工交易复盘：策略信号匹配、对比统计 |
| strategy/base.py | 策略基类：ATR 仓位管理、止损、action_log 信号日志、统一输出接口 |
| strategy/__init__.py | STRATEGY_MAPPING 策略注册表、DEFAULT_STRATEGY_PARAMS 默认参数 |
| strategy/maatr_base.py | 均线+ATR 策略：短均线金叉长均线买入 |
| strategy/momentum.py | 动量策略：动量为正买入 |
| strategy/short_reversal.py | 短期反转策略：跌幅超阈值买入 |
| strategy/boll_ma.py | 布林带+均线策略：回踩下轨买入 |
| strategy/multi_factor.py | 多因子策略 |
| opt_pipeline/common.py | 流水线共享：BacktestRunner、路径常量、参数提取工具 |
| opt_pipeline/param_optimize.py | 网格参数寻优（optstrategy 多进程） |
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

6. **optstrategy 多进程寻优**：参数寻优使用 `cerebro.optstrategy()` + `cerebro.run(maxcpu=N)` 实现多进程并行。由于 optstrategy 返回 `OptReturn` 对象（非策略实例），通过自定义 `FinalValueAnalyzer` 捕获最终资产值。`stop()` 方法将 `final_value` 存入策略属性（仅单回测模式下直接访问，optstrategy 模式走 analyzer）。

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

**参数**：`atr_period=14`（ATR 周期），`max_risk_ratio=0.02`（单笔最大风险占比）

**核心算法**：

```
ATR 仓位公式: size = int(总资产 * max_risk_ratio / (ATR * atr_mult))
止损价公式:   stop_price = close - ATR * atr_mult
```

**方法签名**：

```python
def _position_size(self, atr_mult) -> int        # ATR 仓位计算
def _set_stop(self, atr_mult)                     # 设置止损价
def _open_position(self, atr_mult)                # 买入 + 设置止损 + 记录 action_log
def _close_position(self)                         # 卖出 + 重置止损 + 记录 action_log
def next(self)                                    # 模板方法: 无仓位->_on_entry(), 有仓位->_on_exit()
def _on_entry(self)                               # 子类重写: 入场条件
def _on_exit(self)                                # 子类重写: 出场条件
def stop(self)                                    # 回测结束: self.final_value = broker.getvalue()（供 optimize 模式）
def get_equity_dataframe() -> pd.DataFrame        # 每日净值
def get_trade_dataframe() -> pd.DataFrame         # 已平仓交易记录
def get_action_dataframe() -> pd.DataFrame        # 决策时信号日志（含未平仓）
```

**日志格式**：
- `action_log`: `{date, side: "BUY"/"SELL", price, size}`
- `trade_log`: `{entry_date, exit_date, entry_price, exit_price, size, profit_loss, profit_loss_net, profit_rate}`
- `equity_log`: `{datetime, equity}`

### 策略子类 — 入场/出场条件

| 策略 | 参数（默认值） | 入场条件 | 出场条件 |
|------|---------------|----------|----------|
| maatr_base | `atr_multiple=1.8` | SMA(20) > SMA(60) | close < stop_price |
| momentum | `atr_multiple=1.5`, `momentum_period=20` | Momentum(20) > 0 | close < stop_price 或 Momentum < 0 |
| short_reversal | `atr_mult=2.0`, `fall_ratio=0.18` | (preclose - close) / preclose > fall_ratio | close < stop_price |
| boll_ma | `atr_mult=1.6`, `boll_period=20` | close <= 布林下轨 且 close > SMA(60) | close < stop_price 或 close > 布林上轨 |
| multi_factor | `atr_mult=1.7` | SMA(20) > SMA(60) 且 pctChg > -5 | close < stop_price 或 SMA(20) < SMA(60) |

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
    "maatr_base": {"atr_multiple": 1.8, "max_risk_ratio": 0.02},
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
def run_backtest(dataSource, comminfo, global_setting, param_pool, code, strategy_id, force_refresh) -> dict
def get_strategy_param(param_pool, code, strategy_id) -> (strategy_cls, params)
def validate_manual_trades(valid_codes)
```

**命令行参数**：`--force_refresh`（强制全量下载）、`--strategy`（策略 id，默认 maatr_base）、`--stock-list`（逗号分隔股票代码，过滤 stock_list）

**Cerebro 配置模式**（main.py / daily_signal.py / manual_trade_review.py 共用）：

```python
cerebro = bt.Cerebro()
cerebro.addstrategy(STRATEGY_MAPPING[strategy_id], **param)
cerebro.adddata(AStockData(dataname=df, datetime="datetime", open="open", high="high", low="low", close="close", volume="volume"))
cerebro.broker.setcash(cfg["global_setting"]["initial_capital"])
cerebro.broker.addcommissioninfo(comminfo)
strategy_instance = cerebro.run()[0]
```

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

class BacktestRunner:
    def __init__(self, data_source=None)
    def run(self, df, strategy_id, params) -> float                              # 单回测，返回最终资产值
    def profit_rate(self, final_value) -> float
    def optimize(df, strategy_id, param_grid, maxcpu=1) -> List[Tuple[dict, float]]  # optstrategy 多进程寻优
        # cerebro.optstrategy(strategy_cls, **param_grid) + cerebro.run(maxcpu=N)
        # 返回 [(param_dict, final_value), ...]，用 FinalValueAnalyzer 取值
```

**optimize 内部流程**：`cerebro.optstrategy(cls, **grid)` → `cerebro.run(maxcpu=N)` 返回 `List[List[OptReturn]]` → 用 `strat.analyzers.final_value.get_analysis()` 取最终资产值 → 按 `itertools.product(*grid.values())` 顺序配对参数。

```python
def extract_params(row, exclude_cols) -> dict    # 从CSV行提取策略参数, period类型强制int
def to_native(params: dict) -> dict              # numpy标量转Python原生类型
def read_stage_csv(path) -> pd.DataFrame          # stock_code强制str
```

### opt_pipeline/param_optimize.py — optstrategy 多进程寻优

```python
PARAM_GRID = {strategy_id: {param_name: [values, ...]}}   # 网格定义

def main():
    parser.add_argument("--maxcpu", type=int, default=1)  # 0/-1 自动检测 CPU 数
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