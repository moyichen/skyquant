# Shared infrastructure for each stage of the opt_pipeline:
# path bootstrap, standard backtest executor, CSV row param extraction, stage file paths
import itertools
import multiprocessing
import os
import sys

# When run as a script (python3 param_optimize.py), sys.path[0] is this directory,
# so the project root must be added manually. All stage scripts bootstrap from this
# module, and the sys.path hack is kept only here.
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backtrader as bt
import pandas as pd

from comm import AStockCommission
from data_source import AStockData, DataSource
from strategy import STRATEGY_MAPPING

# ===================== Pipeline stage file paths (absolute paths, not dependent on cwd) =====================
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
PARAM_GRID_CSV = os.path.join(OUTPUT_DIR, "param_optimize_result.csv")
OUT_SAMPLE_CSV = os.path.join(OUTPUT_DIR, "out_sample_verify_result.csv")
ROLLING_CSV = os.path.join(OUTPUT_DIR, "rolling_verify.csv")
AGGREGATE_CSV = os.path.join(OUTPUT_DIR, "aggregate_common_param.csv")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")


class FinalValueAnalyzer(bt.Analyzer):
    """Capture final portfolio value for optimization mode."""

    def stop(self):
        self.final_value = self.strategy.broker.getvalue()

    def get_analysis(self):
        return self.final_value


def _run_single_combo(task):
    """Pool worker for grid optimization: build a fresh Cerebro inside the child
    process and run one parameter combination.

    Must be a module-level (picklable) function for spawn-based multiprocessing.
    All inputs are plain picklable objects, so the Cerebro itself is never sent
    across processes -- this replaces cerebro.optstrategy, which stores lazy
    itertools.product iterators on Cerebro and pickles Cerebro to pool workers;
    those iterators cannot be pickled on Python 3.14.
    """
    df, strategy_id, params, initial_capital, comm_config = task
    cerebro = bt.Cerebro()
    cerebro.addstrategy(STRATEGY_MAPPING[strategy_id], **params)
    cerebro.adddata(AStockData(dataname=df, datetime="datetime"))
    cerebro.broker.setcash(initial_capital)
    cerebro.broker.addcommissioninfo(AStockCommission(**comm_config))
    cerebro.addanalyzer(FinalValueAnalyzer, _name="final_value")
    strategy_instance = cerebro.run()[0]
    return strategy_instance.analyzers.final_value.get_analysis()


class BacktestRunner:
    """Standard backtest executor: unifies initial capital, A-share commission, and feed
    construction, reused by grid/out-of-sample/rolling verification."""

    def __init__(self, data_source: DataSource | None = None):
        self.ds = data_source or DataSource()
        cfg = self.ds.cfg
        self.initial_capital = cfg["global_setting"]["initial_capital"]
        comm_cfg = cfg["commission_config"]
        # Plain dict so it can be passed to pool workers (AStockCommission is
        # rebuilt inside the child instead of being pickled)
        self.comm_config = {
            "commission": comm_cfg["commission"],
            "stamp_duty": comm_cfg["stamp_duty"],
            "transfer_fee": comm_cfg["transfer_fee"],
        }
        self.comminfo = AStockCommission(**self.comm_config)

    def run(self, df: pd.DataFrame, strategy_id: str, params: dict) -> float:
        """Run a single strategy on the given market data slice and return the final asset value"""
        cerebro = bt.Cerebro()
        cerebro.addstrategy(STRATEGY_MAPPING[strategy_id], **params)
        cerebro.adddata(AStockData(dataname=df, datetime="datetime"))
        cerebro.broker.setcash(self.initial_capital)
        cerebro.broker.addcommissioninfo(self.comminfo)
        cerebro.run()
        return cerebro.broker.getvalue()

    def profit_rate(self, final_value: float) -> float:
        """Return the profit rate of the final asset value relative to the initial capital"""
        return (final_value - self.initial_capital) / self.initial_capital

    def optimize(self, df: pd.DataFrame, strategy_id: str, param_grid: dict, maxcpu: int = 1) -> list[tuple[dict, float]]:
        """Enumerate the parameter grid in the parent and dispatch each combination
        to a multiprocessing Pool; return list of (param_dict, final_value).

        cerebro.optstrategy is deliberately not used: it attaches lazy
        itertools.product iterators to Cerebro and pickles the Cerebro itself to
        pool workers, which fails on Python 3.14 ("cannot pickle
        'itertools.product' object"). Instead the grid is materialized here as
        plain dicts and the module-level _run_single_combo worker builds a fresh
        Cerebro (with addstrategy) inside each child process.

        Results are returned in itertools.product enumeration order.
        """
        keys = list(param_grid.keys())
        param_combos = [dict(zip(keys, combo)) for combo in itertools.product(*param_grid.values())]
        tasks = [(df, strategy_id, params, self.initial_capital, self.comm_config) for params in param_combos]
        if maxcpu and maxcpu > 1:
            with multiprocessing.Pool(processes=maxcpu) as pool:
                final_values = pool.map(_run_single_combo, tasks)
        else:
            final_values = [_run_single_combo(task) for task in tasks]
        return list(zip(param_combos, final_values))


def extract_params(row, exclude_cols) -> dict:
    """Extract strategy parameters from a row of a stage CSV:
    - Exclude non-parameter columns
    - Drop NaN values (different strategies have different parameter columns,
      missing columns are read out as NaN)
    - Force period-type parameters to int (a CSV column containing NaN becomes float as a whole)
    """
    param_cols = [col for col in row.index if col not in exclude_cols]
    params = {k: v for k, v in row[param_cols].to_dict().items() if pd.notna(v)}
    return {k: (int(v) if "period" in k else v) for k, v in params.items()}


def to_native(params: dict) -> dict:
    """Convert numpy scalars to native Python types (yaml.dump does not support
    np.float64 etc., which would raise a RepresenterError)"""
    return {k: (v.item() if hasattr(v, "item") else v) for k, v in params.items()}


def read_stage_csv(path: str) -> pd.DataFrame:
    """Read a pipeline stage output CSV:
    stock_code is forced to string to prevent leading zeros from being lost
    (000725 -> 725); an empty file returns an empty DataFrame"""
    try:
        return pd.read_csv(path, dtype={"stock_code": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
