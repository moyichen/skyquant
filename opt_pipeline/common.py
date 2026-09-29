# Shared infrastructure for each stage of the opt_pipeline:
# path bootstrap, standard backtest executor, CSV row param extraction, stage file paths
import itertools
import multiprocessing
import os
import pickle
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# When run as a script (python3 param_optimize.py), sys.path[0] is this directory,
# so the project root must be added manually. All stage scripts bootstrap from this
# module, and the sys.path hack is kept only here.
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backtrader as bt
import pandas as pd

from comm import AStockCommission
from dataprovider import AStockData, DataProvider
from report import calc_equity_metrics
from stock_filter import FILTER_CSV, load_regime_map, routed_strategies  # noqa: F401  (re-exported for stage scripts)
from strategy import ACTIVE_STRATEGIES, STRATEGY_MAPPING  # noqa: F401  (ACTIVE_STRATEGIES re-exported)

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
REGRESSION_STOCKS = ["000725"]

# ===================== Optimization objective schema =====================
# Metric columns produced by the grid worker for every parameter combination.
COMBO_METRIC_COLS = ["final_capital", "profit", "profit_rate", "sharpe_ratio", "max_drawdown", "calmar_ratio"]
# Per-combo columns produced by the rolling-window stage.
ROLLING_METRIC_COLS = ["avg_test_profit", "avg_test_sharpe", "avg_test_calmar", "avg_test_drawdown", "valid"]
# optimize_metric (config opt_pipeline) -> rolling CSV column used by aggregate
# to rank candidates. Profitability gate (avg_test_profit > 0) always applies.
OPTIMIZE_OBJECTIVE_COLUMN = {
    "profit_rate": "avg_test_profit",
    "sharpe": "avg_test_sharpe",
    "calmar": "avg_test_calmar",
}
# Same objective -> grid-stage CSV column (full-period metrics per combination)
GRID_OBJECTIVE_COLUMN = {
    "profit_rate": "profit_rate",
    "sharpe": "sharpe_ratio",
    "calmar": "calmar_ratio",
}
DEFAULT_OPTIMIZE_METRIC = "profit_rate"

# ===================== Optimization mode (grid | tpe) =====================
# freqtrade hyperopt-style: grid enumerates the full PARAM_GRID; tpe samples
# `hyperopt_epochs` points from the same discrete space via a TPE surrogate
# (bayesian), which converges much faster on large spaces at the cost of
# exhaustiveness. Both modes emit the same param_optimize_result.csv schema.
OPT_MODES = ("grid", "tpe")
DEFAULT_OPT_MODE = "grid"
DEFAULT_HYPEROPT_EPOCHS = 500
DEFAULT_HYPEROPT_SEED = 42
DEFAULT_HYPEROPT_BATCH_SIZE = 64


def resolve_optimize_metric(cfg) -> str:
    """Read and validate opt_pipeline.optimize_metric from config."""
    metric = str((cfg or {}).get("opt_pipeline", {}).get("optimize_metric", DEFAULT_OPTIMIZE_METRIC))
    if metric not in OPTIMIZE_OBJECTIVE_COLUMN:
        raise ValueError(f"Unknown optimize_metric '{metric}', choose from {list(OPTIMIZE_OBJECTIVE_COLUMN)}")
    return metric


def resolve_opt_mode(cfg, cli_mode=None) -> str:
    """Resolve optimization mode: CLI --mode > config opt_pipeline.opt_mode > grid."""
    mode = cli_mode or str((cfg or {}).get("opt_pipeline", {}).get("opt_mode", DEFAULT_OPT_MODE))
    if mode not in OPT_MODES:
        raise ValueError(f"Unknown opt_mode '{mode}', choose from {list(OPT_MODES)}")
    return mode


def resolve_hyperopt_cfg(cfg, cli_epochs=None, cli_seed=None) -> dict:
    """Resolve TPE settings: CLI overrides config opt_pipeline.hyperopt_*."""
    section = (cfg or {}).get("opt_pipeline", {})
    return {
        "epochs": int(cli_epochs or section.get("hyperopt_epochs", DEFAULT_HYPEROPT_EPOCHS)),
        "seed": int(cli_seed if cli_seed is not None else section.get("hyperopt_seed", DEFAULT_HYPEROPT_SEED)),
        "batch_size": int(section.get("hyperopt_batch_size", DEFAULT_HYPEROPT_BATCH_SIZE)),
    }


def parse_code_list(raw: str) -> list:
    """Parse a comma-separated --stock-list value into a list of code strings."""
    return [c.strip() for c in raw.split(",") if c.strip()]


def resolve_target_codes(args, cfg) -> list:
    """Resolve the target symbol set for a pipeline entry point.

    Priority: --stock-list (explicit subset) > --all-stocks (full config pool,
    manual trigger) > REGRESSION_STOCKS (fast iteration default). Unknown codes
    are rejected so a typo never silently optimizes an empty set. The config
    stock_blacklist is applied last regardless of the resolution path.
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
    blacklist = {str(c) for c in (cfg.get("stock_blacklist") or [])}
    return [c for c in codes if c not in blacklist]


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


def resolve_maxcpu(requested):
    """Resolve the worker count for a pipeline stage: positive value as-is,
    0/None/-1 -> all logical CPUs."""
    if requested and requested > 0:
        return requested
    return multiprocessing.cpu_count()


def _run_single_combo(task):
    """Primitive backtest worker: build a fresh Cerebro inside the child process
    and run one parameter combination on one market-data slice.

    Returns a plain metrics dict {final_value, profit_rate, sharpe_ratio,
    max_drawdown, calmar_ratio}; a dict is used instead of a bare final value so
    the grid can rank by sharpe/calmar objectives, not total return only.

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
    cerebro.broker.addcommissioninfo(
        AStockCommission(
            commission=comm_config["commission"],
            stamp_duty=comm_config["stamp_duty"],
            transfer_fee=comm_config["transfer_fee"],
        )
    )
    slippage_perc = float(comm_config.get("slippage_perc", 0.0) or 0.0)
    if slippage_perc > 0:
        cerebro.broker.set_slippage_perc(perc=slippage_perc)
    strategy_instance = cerebro.run()[0]

    final_value = float(cerebro.broker.getvalue())
    equity_metrics = calc_equity_metrics(strategy_instance.get_equity_dataframe())
    return {
        "final_value": final_value,
        "profit_rate": (final_value - initial_capital) / initial_capital,
        "sharpe_ratio": equity_metrics["sharpe_ratio"],
        "max_drawdown": equity_metrics["max_drawdown"],
        "calmar_ratio": equity_metrics["calmar_ratio"],
    }


# ===================== Pool workers (module-level; fork-safe) =====================
# On macOS/Linux (fork): the initializer is called in the PARENT process before
# the Pool is created. Fork workers inherit _WORKER_STATE from the parent's
# address space — NO pipe communication for setup, avoiding the Python 3.14
# multiprocessing deadlock that occurs when 55k+ tasks are dispatched with a
# large initializer payload on the feeder pipe.
# On Windows (spawn): the initializer runs in each child via pipe as usual.
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
    train_result = _run_single_combo((_WORKER_STATE["df_train"],) + common)
    test_result = _run_single_combo((_WORKER_STATE["df_test"],) + common)
    return train_result, test_result


def _worker_run_rolling(job):
    strategy_id, params = job
    common = (strategy_id, params, _WORKER_STATE["initial_capital"], _WORKER_STATE["comm_config"])
    return [_run_single_combo((df,) + common) for df in _WORKER_STATE["test_dfs"]]


class BacktestRunner:
    """Standard backtest executor: unifies initial capital, A-share commission, and feed
    construction, reused by grid/out-of-sample/rolling verification."""

    def __init__(self, data_provider: Optional[DataProvider] = None):
        self.data_provider = data_provider or DataProvider()
        cfg = self.data_provider.cfg
        self.initial_capital = cfg["global_setting"]["initial_capital"]
        comm_cfg = cfg["commission_config"]
        # Plain dict so it can be passed to pool workers (AStockCommission is
        # rebuilt inside the child instead of being pickled). slippage_perc is
        # carried here as well so workers honor global_setting.slippage_perc.
        self.comm_config = {
            "commission": comm_cfg["commission"],
            "stamp_duty": comm_cfg["stamp_duty"],
            "transfer_fee": comm_cfg["transfer_fee"],
            "slippage_perc": float(cfg.get("global_setting", {}).get("slippage_perc", 0.0) or 0.0),
        }
        self.comminfo = AStockCommission(
            commission=self.comm_config["commission"],
            stamp_duty=self.comm_config["stamp_duty"],
            transfer_fee=self.comm_config["transfer_fee"],
        )

    def run(self, df: pd.DataFrame, strategy_id: str, params: dict) -> dict:
        """Run a single strategy on the given market data slice; return metrics dict"""
        return _run_single_combo((df, strategy_id, params, self.initial_capital, self.comm_config))

    def profit_rate(self, result: dict) -> float:
        """Return the profit rate of a worker result dict (final vs initial capital)"""
        return result["profit_rate"]

    # 独立子进程分块执行器脚本（同目录）
    _CHUNK_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_chunk_runner.py")

    def _build_chunk_payload(self, kind, initargs, chunk_jobs):
        """按任务类型组装一个 chunk 的 pickle payload。"""
        payload = {"kind": kind, "jobs": chunk_jobs}
        if kind == "combo":
            payload.update(df=initargs[0], initial_capital=initargs[1], comm_config=initargs[2])
        elif kind == "train_test":
            payload.update(
                df_train=initargs[0], df_test=initargs[1],
                initial_capital=initargs[2], comm_config=initargs[3],
            )
        elif kind == "rolling":
            payload.update(
                test_dfs=initargs[0], initial_capital=initargs[1], comm_config=initargs[2],
            )
        else:
            raise ValueError(f"unknown chunk kind: {kind}")
        return payload

    def _run_chunk_subprocess(self, payload_path, output_path, log_path):
        """运行一个 chunk 子进程；非零退出时自动重试 1 次（8GB 内存机上
        jetsam 可能在导入/计算途中 SIGKILL 单个 worker，重试一次可扛过
        偶发内存绞杀；两次都失败才抛出带日志尾部的异常）。"""
        for attempt in (1, 2):
            for p in (output_path, output_path + ".tmp"):
                if os.path.exists(p):
                    os.remove(p)
            with open(log_path, "ab") as logf:
                if attempt == 2:
                    logf.write(b"\n[orchestrator] retrying chunk after failure\n")
                proc = subprocess.run(
                    [sys.executable, self._CHUNK_RUNNER, payload_path, output_path],
                    cwd=PROJECT_ROOT, stdout=logf, stderr=subprocess.STDOUT,
                )
            if proc.returncode == 0 and os.path.exists(output_path):
                with open(output_path, "rb") as f:
                    return pickle.load(f)
        with open(log_path, "r", errors="replace") as f:
            tail = "".join(f.readlines()[-30:])
        raise RuntimeError(f"chunk subprocess failed twice (exit {proc.returncode}):\n{tail}")

    # 8GB 内存机上 jetsam 会杀掉同时跑 4 个子进程 + orchestrator 的组合。
    # 限制最大并发为 2，保证 orchestrator + 2 个 chunk 子进程总内存可控。
    MAX_CONCURRENT_CHUNKS = 2
    # 每个 chunk 子进程处理的 job 数。太小 → 子进程启动开销占比高；
    # 太大 → 单个子进程长期运行累积内存、断点续跑粒度粗。1000 ≈ 12 分钟/块。
    JOBS_PER_CHUNK = 1000

    def _chunk_run_id(self, kind, jobs):
        """为一次 optimize 调用生成稳定的 run_id，用于持久化 chunk 目录。

        相同 kind + 相同 job 列表 → 相同 run_id → 断点续跑；
        不同参数/标的 → 不同 run_id → 全新目录全新跑。"""
        import hashlib
        first_job = jobs[0] if jobs else ()
        last_job = jobs[-1] if jobs else ()
        sig = f"{kind}|{len(jobs)}|{first_job}|{last_job}"
        return hashlib.md5(sig.encode("utf-8")).hexdigest()[:8]

    def _try_load_valid_chunk(self, output_path, expected_len):
        """检查 chunk 输出文件是否存在且结果条数与预期一致；是则返回结果列表，否则 None。"""
        if not os.path.exists(output_path):
            return None
        try:
            with open(output_path, "rb") as f:
                results = pickle.load(f)
            if isinstance(results, list) and len(results) == expected_len:
                # 校验：不能包含 __error__ 条目（说明上一次跑出错了）
                if not any(isinstance(r, dict) and "__error__" in r for r in results):
                    return results
        except Exception:
            pass
        return None

    def _map_jobs(self, jobs, initializer, initargs, worker, serial_fn, maxcpu):
        """串行回退（maxcpu<=1）或子进程分块并行（maxcpu>1）。

        不使用 multiprocessing.Pool：macOS + Python 3.14 下父进程已初始化
        tushare/pandas 等带后台线程的库后，Pool（spawn 引导管道 / fork 线程
        锁）在任务量达到数千时会死锁——worker 0% CPU、Pool 反复重启 worker、
        map 永久挂起。改为把任务切成多个小块（每块 JOBS_PER_CHUNK 个 job），
        每块由一个独立普通 `python3 _chunk_runner.py` 子进程串行执行、结果
        落 pickle 文件到持久化目录；编排器按 MAX_CONCURRENT_CHUNKS 并发拉起
        子进程并合并文件。

        断点续跑：chunk 目录按 run_id 持久化到 output/opt_chunks_<run_id>/，
        如果 orchestrator 被 jetsam 杀掉，重新运行相同优化时会跳过已完成的
        chunk（输出文件存在且条数正确），只跑缺失的部分。
        results 与 jobs 等长且按顺序对齐（chunk 有序、块内有序）。
        """
        if not (maxcpu and maxcpu > 1):
            return [serial_fn(job) for job in jobs]

        kind = {
            _init_combo_worker: "combo",
            _init_train_test_worker: "train_test",
            _init_rolling_worker: "rolling",
        }[initializer]

        # 分块：每块 JOBS_PER_CHUNK 个 job，取上限保证最后一块不会太碎
        chunk_size = self.JOBS_PER_CHUNK
        n_chunks = max(1, (len(jobs) + chunk_size - 1) // chunk_size)
        chunk_bounds = [i * len(jobs) // n_chunks for i in range(n_chunks + 1)]

        run_id = self._chunk_run_id(kind, jobs)
        chunk_dir = os.path.join(OUTPUT_DIR, f"opt_chunks_{run_id}")
        os.makedirs(chunk_dir, exist_ok=True)
        # 写一个 metadata 文件方便人工识别这个目录属于哪次优化
        meta_path = os.path.join(chunk_dir, "_meta.txt")
        if not os.path.exists(meta_path):
            with open(meta_path, "w") as f:
                f.write(f"kind={kind}\njobs={len(jobs)}\nchunks={n_chunks}\n"
                        f"chunk_size={chunk_size}\n")

        # 预建所有 chunk 的 payload 和 expected_len，并跳过已完成的
        chunk_specs = []
        skipped = 0
        for ci in range(n_chunks):
            chunk_jobs = jobs[chunk_bounds[ci]:chunk_bounds[ci + 1]]
            if not chunk_jobs:
                continue
            expected_len = len(chunk_jobs)
            payload_path = os.path.join(chunk_dir, f"chunk_{ci:04d}_in.pkl")
            output_path = os.path.join(chunk_dir, f"chunk_{ci:04d}_out.pkl")
            log_path = os.path.join(chunk_dir, f"chunk_{ci:04d}.log")

            # 断点续跑：输出已存在且条数正确 → 跳过
            cached = self._try_load_valid_chunk(output_path, expected_len)
            if cached is not None:
                skipped += 1
                chunk_specs.append((payload_path, output_path, log_path,
                                    expected_len, cached, True))  # done=True
                continue

            # 写 payload（覆盖旧的不完整 payload）
            payload = self._build_chunk_payload(kind, initargs, chunk_jobs)
            with open(payload_path, "wb") as f:
                pickle.dump(payload, f)
            chunk_specs.append((payload_path, output_path, log_path,
                                expected_len, None, False))

        if skipped > 0:
            print(f"[opt] resume: {skipped}/{len(chunk_specs)} chunks already "
                  f"done, skipping", flush=True)

        # 按 MAX_CONCURRENT_CHUNKS 并发拉起子进程
        pending = [(s[0], s[1], s[2], s[3]) for s in chunk_specs if not s[5]]
        max_concurrent = min(self.MAX_CONCURRENT_CHUNKS, len(pending)) if pending else 0

        results_by_idx = {}
        # 先收集已完成 chunk 的结果
        for ci, spec in enumerate(chunk_specs):
            if spec[5]:  # done
                results_by_idx[ci] = spec[4]

        if max_concurrent > 0:
            print(f"[opt] {len(pending)} chunks to run, {max_concurrent} "
                  f"concurrent", flush=True)
            done_count = skipped
            with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
                # 按波次提交：每波 max_concurrent 个 chunk
                for wave_start in range(0, len(pending), max_concurrent):
                    wave = pending[wave_start:wave_start + max_concurrent]
                    futures = {
                        pool.submit(self._run_chunk_subprocess, p, o, l): (p, o, l, exp)
                        for p, o, l, exp in wave
                    }
                    for fut in futures:
                        p, o, l, exp = futures[fut]
                        chunk_results = fut.result()
                        done_count += 1
                        # 找到这个 output_path 对应的 chunk index
                        for ci, spec in enumerate(chunk_specs):
                            if spec[1] == o:  # output_path match
                                results_by_idx[ci] = chunk_results
                                break
                        print(f"[opt] chunk {done_count}/{len(chunk_specs)} "
                              f"done ({len(chunk_results)} jobs)", flush=True)

        # 按顺序合并结果
        results = []
        for ci in range(len(chunk_specs)):
            if ci not in results_by_idx:
                raise RuntimeError(f"chunk {ci} result missing")
            results.extend(results_by_idx[ci])

        # 全部成功后清理持久化目录
        import shutil
        shutil.rmtree(chunk_dir, ignore_errors=True)

        if len(results) != len(jobs):
            raise RuntimeError(f"chunk results count mismatch: {len(results)} != {len(jobs)}")
        for r in results:
            if isinstance(r, dict) and "__error__" in r:
                raise RuntimeError(f"combo failed inside chunk:\n{r['__error__']}")
        return results

    def run_combos(self, df: pd.DataFrame, strategy_id: str, param_list: list, maxcpu: int = 1) -> list:
        """Backtest an explicit list of param dicts for one strategy on one data
        slice; return a list of metrics dicts aligned with param_list.

        Shared executor for grid enumeration (optimize) and TPE sampling
        (param_optimize._tpe_search)."""
        jobs = [(strategy_id, params) for params in param_list]
        return self._map_jobs(
            jobs,
            _init_combo_worker,
            (df, self.initial_capital, self.comm_config),
            _worker_run_combo,
            lambda job: _run_single_combo((df, job[0], job[1], self.initial_capital, self.comm_config)),
            maxcpu,
        )

    def optimize(self, df: pd.DataFrame, strategy_id: str, param_grid: dict, maxcpu: int = 1) -> list:
        """Enumerate the parameter grid in the parent and dispatch each combination
        to a multiprocessing Pool; return list of (param_dict, metrics_dict).

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
        results = self.run_combos(df, strategy_id, param_combos, maxcpu=maxcpu)
        return list(zip(param_combos, results))

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
    return {k: (int(v) if "period" in k or "bars" in k else v) for k, v in params.items()}


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
