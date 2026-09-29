# Parameter optimization stage with two search modes (config opt_pipeline.opt_mode,
# CLI --mode overrides):
#   grid — enumerate each strategy's PARAM_GRID exhaustively, keeping only
#          profitable parameter combinations.
#   tpe  — hyperopt TPE (freqtrade hyperopt-style): sample `hyperopt_epochs`
#          points from the SAME discrete PARAM_GRID space with a bayesian
#          surrogate; converges much faster on large spaces at the cost of
#          exhaustiveness. Reproducible via opt_pipeline.hyperopt_seed.
# Both modes emit the identical param_optimize_result.csv schema, so the
# downstream stages (out-sample / rolling / aggregate / export) are unchanged.
#
# Default target universe is REGRESSION_STOCKS (fast iteration gate after any
# strategy/param change); a full-pool run must be triggered explicitly with
# --all-stocks, or an explicit subset with --stock-list.
#
# Each symbol is routed to its matching strategy by the regime labels in
# output/stock_filter.csv (written by stock_filter.py):
#   trend    -> trend strategy    (uses global quadruple trend filter)
#   range    -> range strategy    (bypasses trend filter; mean-reversion entry)
#   breakout -> breakout strategy (bypasses trend filter; Donchian breakout)
# Without a filter report every active strategy is optimized (backward compatible).
#
# Ranking objective is config opt_pipeline.optimize_metric:
#   profit_rate (default) | sharpe | calmar
import argparse

import numpy as np
import pandas as pd
from common import (
    GRID_OBJECTIVE_COLUMN,
    PARAM_GRID_CSV,
    BacktestRunner,
    load_regime_map,
    resolve_hyperopt_cfg,
    resolve_maxcpu,
    resolve_opt_mode,
    resolve_optimize_metric,
    resolve_target_codes,
    routed_strategies,
    write_stage_csv,
)
from strategy import ACTIVE_STRATEGIES

# Protections (freqtrade-style, shared by all strategies): binary on/off dimensions so the
# optimizer decides per stock whether to enable. None = off (extract_params drops NaN on CSV
# round-trip, so None never reaches config; the base-class default None applies).
PROTECTIONS_GRID = {
    "cooldown_period_candles": [None, 5],   # CooldownPeriod.stop_duration_candles: no re-entry for N candles after a sell fill
    "stoploss_guard_trade_limit": [None, 3],  # StoplossGuard.trade_limit: pause after 3 stop-exits in lookback window
    "max_allowed_drawdown": [None, 0.2],    # MaxDrawdown.max_allowed_drawdown: block entries while equity drawdown > 20%
}

PARAM_GRID = {
    "trend": {
        # Merged trend_follow + momentum: trend structure (quadruple filter) + momentum confirmation
        "trail_atr_multiple": [1.4, 1.6, 1.8],
        "momentum_period": [18, 20, 22],
        "min_volatility_ratio": [0.008, 0.025],
        "max_risk_ratio": [0.02, 0.025],
        "trail_tighten_profit_multiple": [1.0, 1.5, 2.0],
        "trail_tight_atr_multiple": [0.8, 1.0],
        "macd_fast": [10, 12],
        "macd_slow": [21, 26],
        "macd_signal": [7, 9],
        "adx_min": [20, 25],
        "max_loss_stop_ratio": [0.2, 0.3],
        **PROTECTIONS_GRID,
    },
    "range": {
        # Merged boll_ma + short_reversal: mean-reversion entry (boll lower / sharp drop).
        # Bypasses the trend filter, so macd_*/adx_min/min_volatility_ratio are excluded
        # (they do not affect entry for this strategy).
        "trail_atr_multiple": [1.8, 2.0, 2.2],
        "bb_period": [18, 20, 22],
        "drop_ratio": [0.15, 0.18, 0.2],
        "max_risk_ratio": [0.02, 0.025],
        "trail_tighten_profit_multiple": [1.0, 1.5, 2.0],
        "trail_tight_atr_multiple": [0.8, 1.0],
        "max_loss_stop_ratio": [0.2, 0.3],
        **PROTECTIONS_GRID,
    },
    "breakout": {
        # Donchian channel breakout for high-volatility names.
        # Bypasses the trend filter.
        "trail_atr_multiple": [1.8, 2.0, 2.2],
        "breakout_period": [15, 20, 30],
        "max_risk_ratio": [0.02, 0.025],
        "trail_tighten_profit_multiple": [1.0, 1.5, 2.0],
        "trail_tight_atr_multiple": [0.8, 1.0],
        "max_loss_stop_ratio": [0.2, 0.3],
        **PROTECTIONS_GRID,
    },
}

# Grid actually optimized: PARAM_GRID filtered by strategy.ACTIVE_STRATEGIES
ACTIVE_PARAM_GRID = {sid: grid for sid, grid in PARAM_GRID.items() if ACTIVE_STRATEGIES is None or sid in ACTIVE_STRATEGIES}

# One-line description per strategy for the pre-run summary
STRATEGY_DESC = {
    "trend": "四重趋势过滤 + 动量确认 + ATR 移动止损（趋势市）",
    "range": "布林带均值回归 + 急跌反弹（震荡市，绕过趋势过滤）",
    "breakout": "唐奇安通道突破（高波动市，绕过趋势过滤）",
}

# Loss assigned to failed / non-finite evaluations in TPE mode (hyperopt
# minimizes; the value must stay finite and clearly worse than any real run)
TPE_BAD_LOSS = 1e3


def _tpe_search(runner, df, strategy_id, grid, epochs, seed, batch_size, objective_col, maxcpu):
    """Sample `epochs` points from the discrete `grid` with hyperopt TPE.

    freqtrade hyperopt-style batched ask/tell: each batch of `batch_size`
    candidates is suggested from the current Trials posterior, backtested in one
    go via BacktestRunner.run_combos (chunked subprocess execution, isolated +
    resumable), then written back so the next batch is model-guided. Loss is
    -objective (optimize_metric is a maximize metric); non-finite results map to
    TPE_BAD_LOSS. Repeated suggestions are deduped through a loss cache so each
    distinct point is backtested at most once. `seed` fixes the RNG so identical
    config + data reproduces identical candidates (and hits the chunk cache on
    rerun).

    Returns a list of (param_dict, metrics_dict), same shape as
    BacktestRunner.optimize, in first-evaluation order.
    """
    import warnings

    with warnings.catch_warnings():
        # hyperopt imports pkg_resources (deprecated shim on Python 3.14); the
        # warning is noise — suppress only during import.
        warnings.simplefilter("ignore")
        from hyperopt import (
            JOB_STATE_DONE,
            STATUS_OK,
            Domain,
            Trials,
            hp,
            space_eval,
            tpe,
        )

    space = {name: hp.choice(name, list(values)) for name, values in grid.items()}
    # fn is a placeholder for Domain construction; actual evaluation is batched
    # through BacktestRunner, not through hyperopt's sequential fmin.
    domain = Domain(lambda params: 0.0, space)
    trials = Trials()
    rng = np.random.default_rng(seed)
    cache = {}  # param signature -> (params, loss, metrics); dedupes repeated suggestions
    evaluated = 0
    batch_no = 0

    while evaluated < epochs:
        k = min(batch_size, epochs - evaluated)
        new_trials = tpe.suggest(trials.new_trial_ids(k), domain, trials, rng)

        params_list, sigs = [], []
        for t in new_trials:
            vals = {key: v[0] for key, v in t["misc"]["vals"].items()}
            params = space_eval(space, vals)
            params_list.append(params)
            sigs.append(tuple(sorted((key, repr(v)) for key, v in params.items())))

        # Backtest only points never evaluated before (within-run + cache)
        pending, batch_seen = [], set()
        for i, sig in enumerate(sigs):
            if sig not in cache and sig not in batch_seen:
                batch_seen.add(sig)
                pending.append(i)
        if pending:
            results = runner.run_combos(df, strategy_id, [params_list[i] for i in pending], maxcpu=maxcpu)
            for i, metrics in zip(pending, results):
                value = metrics.get(objective_col)
                loss = -float(value) if value is not None and np.isfinite(value) else TPE_BAD_LOSS
                cache[sigs[i]] = (params_list[i], loss, metrics)

        # Tell: write every suggested trial back (cache hits reuse the stored loss)
        for t, sig in zip(new_trials, sigs):
            t["result"] = {"loss": cache[sig][1], "status": STATUS_OK}
            t["state"] = JOB_STATE_DONE
        trials.insert_trial_docs(new_trials)
        trials.refresh()

        evaluated += k
        batch_no += 1
        best = -trials.best_trial["result"]["loss"]
        print(
            f"  [tpe] batch {batch_no}: {evaluated}/{epochs} evals ({len(pending)} new), "
            f"best {objective_col}={best:.4f}",
            flush=True,
        )

    return [(params, metrics) for params, _loss, metrics in cache.values()]


def main():
    parser = argparse.ArgumentParser(
        description="Parameter optimization (grid or TPE)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "mode:  grid — enumerate PARAM_GRID exhaustively (default, deterministic)\n"
            "       tpe  — hyperopt TPE bayesian sampling (freqtrade --epochs style)\n"
            "             epochs/seed/batch_size read from config opt_pipeline.*,\n"
            "             overridable via --epochs / --seed."
        ),
    )
    parser.add_argument(
        "--maxcpu",
        type=int,
        default=0,
        help="Number of CPUs for parallel optimization (0 or -1 for all CPUs)",
    )
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to run. Defaults to regression stocks.",
    )
    parser.add_argument(
        "--all-stocks",
        action="store_true",
        help="Optimize all stocks in config.yaml (default: regression stocks only)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=None,
        help="Optimization mode: grid or tpe (overrides config opt_pipeline.opt_mode)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=None,
        help="TPE total evaluation count (overrides config opt_pipeline.hyperopt_epochs)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="TPE random seed (overrides config opt_pipeline.hyperopt_seed)",
    )
    args = parser.parse_args()

    maxcpu = resolve_maxcpu(args.maxcpu)

    runner = BacktestRunner()
    cfg = runner.data_provider.cfg
    optimize_metric = resolve_optimize_metric(cfg)
    sort_col = GRID_OBJECTIVE_COLUMN[optimize_metric]
    mode = resolve_opt_mode(cfg, cli_mode=args.mode)
    hyperopt_cfg = resolve_hyperopt_cfg(cfg, cli_epochs=args.epochs, cli_seed=args.seed)
    valid_codes = resolve_target_codes(args, cfg)
    # Regime -> strategy routing table (output/stock_filter.csv); empty when the
    # filter layer has not run, in which case all active strategies are optimized.
    regime_map = load_regime_map()
    # Each symbol's cache is read only once; grid combinations reuse it directly
    cache_map = {code: runner.data_provider.load_cached_data(code) for code in valid_codes}

    result_rows = []

    # ---- Pre-run summary ----
    plan = []  # (code, strategy_id, grid, n_combos)
    total_combos = 0
    for code in valid_codes:
        df = cache_map[code]
        if df is None or df.empty:
            print(f"[skip] {code}: 无缓存数据")
            continue
        strategies = routed_strategies(code, regime_map, ACTIVE_STRATEGIES)
        for strategy_id in strategies:
            grid = ACTIVE_PARAM_GRID[strategy_id]
            n_combos = 1
            for v in grid.values():
                n_combos *= len(v)
            plan.append((code, strategy_id, grid, n_combos))
            total_combos += n_combos

    is_tpe = mode == "tpe"
    print("=" * 70)
    if is_tpe:
        print(f"TPE 寻优计划：{len(plan)} 个 标的×策略 组合，每组合 epochs={hyperopt_cfg['epochs']}")
        print(f"排序指标: {optimize_metric}  |  模式: {mode}  |  seed={hyperopt_cfg['seed']}  |  batch={hyperopt_cfg['batch_size']}")
    else:
        print(f"网格寻优计划：{len(plan)} 个 标的×策略 组合，共 {total_combos:,} 个参数组合")
        print(f"排序指标: {optimize_metric}  |  模式: {mode}  |  并发: {maxcpu}")
    print(f"分块: {BacktestRunner.JOBS_PER_CHUNK}/块, 最多 {BacktestRunner.MAX_CONCURRENT_CHUNKS} 并行块")
    print("-" * 70)
    for code, strategy_id, grid, n_combos in plan:
        dims = len(grid)
        desc = STRATEGY_DESC.get(strategy_id, "")
        combo_str = f"{hyperopt_cfg['epochs']:,} epochs" if is_tpe else f"{n_combos:>6,} 组合"
        print(f"  {code}  {strategy_id:8s}  {dims:2d}维 {combo_str}  | {desc}")
    print("-" * 70)
    if is_tpe:
        total_evals = len(plan) * hyperopt_cfg["epochs"]
        batches_per = -(-hyperopt_cfg["epochs"] // hyperopt_cfg["batch_size"])  # ceil
        print(f"总计: {total_evals:,} 回测评估 (每批 {hyperopt_cfg['batch_size']} → "
              f"{batches_per} 批/组合)")
    else:
        print(f"总计: {total_combos:,} 组合  (按 {BacktestRunner.JOBS_PER_CHUNK}/块 分块 → "
              f"{(total_combos + BacktestRunner.JOBS_PER_CHUNK - 1) // BacktestRunner.JOBS_PER_CHUNK} 块)")
    print("=" * 70)

    for code, strategy_id, grid, n_combos in plan:
        df = cache_map[code]
        if is_tpe:
            print(f"\n>>> {code} {strategy_id} (TPE {hyperopt_cfg['epochs']} epochs) ...")
            try:
                results = _tpe_search(
                    runner, df, strategy_id, grid,
                    epochs=hyperopt_cfg["epochs"],
                    seed=hyperopt_cfg["seed"],
                    batch_size=hyperopt_cfg["batch_size"],
                    objective_col=sort_col,
                    maxcpu=maxcpu,
                )
            except Exception as e:
                print(f"Exception {code} {strategy_id}: {e}")
                continue
        else:
            print(f"\n>>> {code} {strategy_id} ({n_combos:,} 组合) ...")
            try:
                results = runner.optimize(df, strategy_id, grid, maxcpu=maxcpu)
            except Exception as e:
                print(f"Exception {code} {strategy_id}: {e}")
                continue
        for param_dict, metrics in results:
            final_value = metrics["final_value"]
            profit = final_value - runner.initial_capital
            result_rows.append(
                {
                    "stock_code": code,
                    "strategy": strategy_id,
                    **param_dict,
                    "final_capital": round(final_value, 2),
                    "profit": round(profit, 2),
                    "profit_rate": round(metrics["profit_rate"], 4),
                    "sharpe_ratio": metrics["sharpe_ratio"],
                    "max_drawdown": metrics["max_drawdown"],
                    "calmar_ratio": metrics["calmar_ratio"],
                }
            )

    res_df = pd.DataFrame(result_rows)
    if not res_df.empty:
        # Profitability gate first, then rank by the configured objective (best on top)
        res_df = res_df[res_df["profit_rate"] > 0].sort_values(sort_col, ascending=False)
    write_stage_csv(PARAM_GRID_CSV, res_df, valid_codes)
    print(f"Parameter optimization results written to: {PARAM_GRID_CSV} ({len(res_df)} rows, objective={optimize_metric}, mode={mode})")


if __name__ == "__main__":
    main()
