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

# Default optimization universe == regression universe: after any strategy/param
# change the fast iteration loop optimizes only these symbols; a full-pool run
# must be triggered explicitly with --all-stocks. tests/regression reuses this list.
REGRESSION_STOCKS = ["000725", "000001", "600111", "002222", "601127"]


def parse_code_list(raw: str) -> list:
    """Parse a comma-separated --stock-list value into a list of code strings."""
    return [c.strip() for c in raw.split(",") if c.strip()]


def resolve_target_codes(args, cfg) -> list:
    """Resolve the target symbol set for a pipeline entry point.

    Priority: --stock-list (explicit subset) > --all-stocks (full config pool,
    manual trigger) > REGRESSION_STOCKS (fast iteration default). Unknown codes
    are rejected so a typo never silently optimizes an empty set.
    """
    pool_codes = [str(item["code"]) for item in cfg["stock_list"]]
    stock_list_arg = getattr(args, "stock_list", None)
    all_stocks = getattr(args, "all_stocks", False)
    if stock_list_arg and all_stocks:
        raise ValueError("--stock-list and --all-stocks are mutually exclusive")
    if stock_list_arg:
        codes = parse_code_list(stock_list_arg)
    elif all_stocks:
        codes = pool_codes
    else:
        codes = list(REGRESSION_STOCKS)
    unknown = [c for c in codes if c not in pool_codes]
    if unknown:
        raise ValueError(f"Stock codes not in config.yaml stock_list: {unknown}")
    return codes


def write_stage_csv(path: str, df: pd.DataFrame, touched_codes=None) -> None:
    """Write a pipeline stage CSV.

    touched_codes=None -> full rewrite (batch mode).
    Otherwise merge per symbol: replace all existing rows of the touched symbols
    with the new rows, keep every other symbol untouched, so a per-symbol rerun
    never clobbers the rest of the pool. Touched symbols are cleared even when df
    has no rows for them (e.g. every combo was filtered out), so stale rows never
    survive.
    """
    if touched_codes is None:
        old_df = pd.DataFrame()
    else:
        touched = {str(c) for c in touched_codes}
        old_df = read_stage_csv(path) if os.path.exists(path) else pd.DataFrame()
        if not old_df.empty:
            old_df = old_df[~old_df["stock_code"].astype(str).isin(touched)]
    merged = pd.concat([old_df, df], ignore_index=True) if not df.empty else old_df
    if merged.empty and len(df.columns) > 0:
        merged = pd.DataFrame(columns=list(df.columns))
    merged.to_csv(path, index=False, encoding="utf8")


class FinalValueAnalyzer(bt.Analyzer):
    """Capture final portfolio value for optimization mode."""

    def stop(self):
        self.final_value = self.strategy.broker.getvalue()

    def get_analysis(self):
        return self.final_value


def resolve_maxcpu(requested):
    """Resolve the worker count for a pipeline stage: positive value as-is,
    0/None/-1 -> all logical CPUs."""
    if requested and requested > 0:
        return requested
    return multiprocessing.cpu_count()


def _run_single_combo(task):
    """Primitive backtest worker: build a fresh Cerebro inside the child process
    and run one parameter combination on one market-data slice.

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


# ===================== Pool workers (module-level; spawn-safe) =====================
# The market data is identical for every job of one symbol, so it is passed once
# per worker via the Pool initializer instead of being pickled inside every task
# (measured ~30% faster on 4 cores than per-task payloads). Jobs are plain
# (strategy_id, params) tuples. _WORKER_STATE only exists inside child processes.
_WORKER_STATE = {}


def _init_combo_worker(df, initial_capital, comm_config):
    _WORKER_STATE["df"] = df
    _WORKER_STATE["initial_capital"] = initial_capital
    _WORKER_STATE["comm_config"] = comm_config


def _init_train_test_worker(df_train, df_test, initial_capital, comm_config):
    _WORKER_STATE["df_train"] = df_train
    _WORKER_STATE["df_test"] = df_test
    _WORKER_STATE["initial_capital"] = initial_capital
    _WORKER_STATE["comm_config"] = comm_config


def _init_rolling_worker(test_dfs, initial_capital, comm_config):
    _WORKER_STATE["test_dfs"] = test_dfs
    _WORKER_STATE["initial_capital"] = initial_capital
    _WORKER_STATE["comm_config"] = comm_config


def _worker_run_combo(job):
    strategy_id, params = job
    return _run_single_combo(
        (_WORKER_STATE["df"], strategy_id, params, _WORKER_STATE["initial_capital"], _WORKER_STATE["comm_config"])
    )


def _worker_run_train_test(job):
    strategy_id, params = job
    common = (strategy_id, params, _WORKER_STATE["initial_capital"], _WORKER_STATE["comm_config"])
    train_final = _run_single_combo((_WORKER_STATE["df_train"],) + common)
    test_final = _run_single_combo((_WORKER_STATE["df_test"],) + common)
    return train_final, test_final


def _worker_run_rolling(job):
    strategy_id, params = job
    common = (strategy_id, params, _WORKER_STATE["initial_capital"], _WORKER_STATE["comm_config"])
    return [_run_single_combo((df,) + common) for df in _WORKER_STATE["test_dfs"]]


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

    def _map_jobs(self, jobs, initializer, initargs, worker, serial_fn, maxcpu):
        """Dispatch jobs to a Pool when maxcpu>1, otherwise run serially in-process.

        initializer/initargs ship the (large, shared) market data once per worker;
        worker is the module-level child entry point; serial_fn is the direct
        primitive used for the in-process fallback (no worker state needed).
        pool.map preserves input order, so results zip back to jobs by position.
        """
        if maxcpu and maxcpu > 1:
            with multiprocessing.Pool(processes=maxcpu, initializer=initializer, initargs=initargs) as pool:
                return pool.map(worker, jobs)
        return [serial_fn(job) for job in jobs]

    def optimize(self, df: pd.DataFrame, strategy_id: str, param_grid: dict, maxcpu: int = 1) -> list[tuple[dict, float]]:
        """Enumerate the parameter grid in the parent and dispatch each combination
        to a multiprocessing Pool; return list of (param_dict, final_value).

        cerebro.optstrategy is deliberately not used: it attaches lazy
        itertools.product iterators to Cerebro and pickles the Cerebro itself to
        pool workers, which fails on Python 3.14 ("cannot pickle
        'itertools.product' object"). Instead the grid is materialized here as
        plain dicts and the module-level worker builds a fresh Cerebro (with
        addstrategy) inside each child process; df ships once per worker.

        Results are returned in itertools.product enumeration order.
        """
        keys = list(param_grid.keys())
        param_combos = [dict(zip(keys, combo)) for combo in itertools.product(*param_grid.values())]
        jobs = [(strategy_id, params) for params in param_combos]
        final_values = self._map_jobs(
            jobs,
            _init_combo_worker,
            (df, self.initial_capital, self.comm_config),
            _worker_run_combo,
            lambda job: _run_single_combo((df, job[0], job[1], self.initial_capital, self.comm_config)),
            maxcpu,
        )
        return list(zip(param_combos, final_values))

    def run_train_test_batch(self, df_train, df_test, jobs, maxcpu=1):
        """Run each (strategy_id, params) job twice (train slice, test slice).

        Returns a list of (train_final, test_final) aligned with jobs.
        """
        return self._map_jobs(
            jobs,
            _init_train_test_worker,
            (df_train, df_test, self.initial_capital, self.comm_config),
            _worker_run_train_test,
            lambda job: (
                _run_single_combo((df_train, job[0], job[1], self.initial_capital, self.comm_config)),
                _run_single_combo((df_test, job[0], job[1], self.initial_capital, self.comm_config)),
            ),
            maxcpu,
        )

    def run_rolling_batch(self, test_dfs, jobs, maxcpu=1):
        """Run each (strategy_id, params) job over every rolling test window.

        test_dfs is the shared list of window slices (identical for all jobs of
        one symbol). Returns a list aligned with jobs; each element is a list of
        final values in window order.
        """
        return self._map_jobs(
            jobs,
            _init_rolling_worker,
            (test_dfs, self.initial_capital, self.comm_config),
            _worker_run_rolling,
            lambda job: [_run_single_combo((df, job[0], job[1], self.initial_capital, self.comm_config)) for df in test_dfs],
            maxcpu,
        )


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
