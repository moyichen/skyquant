# Grid parameter optimization: enumerate each strategy's PARAM_GRID with a
# self-built multiprocessing Pool, keeping only profitable parameter combinations.
#
# Default target universe is REGRESSION_STOCKS (fast iteration gate after any
# strategy/param change); a full-pool run must be triggered explicitly with
# --all-stocks, or an explicit subset with --stock-list.
import argparse

import pandas as pd
from common import PARAM_GRID_CSV, BacktestRunner, resolve_maxcpu, resolve_target_codes, write_stage_csv
from strategy import ACTIVE_STRATEGIES

PARAM_GRID = {
    "trend_follow": {
        "trail_atr_multiple": [1.6, 1.8, 2.0],
        "min_volatility_ratio": [0.008, 0.015, 0.025],
        "max_risk_ratio": [0.015, 0.02, 0.025],
        "trail_tighten_profit_multiple": [1.0, 1.5, 2.0],
        "trail_tight_atr_multiple": [0.8, 1.0, 1.2],
        "macd_fast": [10, 12],
        "macd_slow": [21, 26],
        "macd_signal": [7, 9],
    },
    "momentum": {
        "trail_atr_multiple": [1.4, 1.5, 1.7],
        "max_risk_ratio": [0.02, 0.025],
        "momentum_period": [18, 20, 22],
        "trail_tighten_profit_multiple": [1.0, 1.5, 2.0],
        "trail_tight_atr_multiple": [0.8, 1.0, 1.2],
        "macd_fast": [10, 12],
        "macd_slow": [21, 26],
        "macd_signal": [7, 9],
    },
    "short_reversal": {
        "trail_atr_multiple": [1.8, 2.0, 2.2],
        "max_risk_ratio": [0.02],
        "drop_ratio": [0.15, 0.18, 0.2],
        "trail_tighten_profit_multiple": [1.0, 1.5, 2.0],
        "trail_tight_atr_multiple": [0.8, 1.0, 1.2],
        "macd_fast": [10, 12],
        "macd_slow": [21, 26],
        "macd_signal": [7, 9],
    },
    "boll_ma": {
        "trail_atr_multiple": [1.5, 1.6, 1.8],
        "max_risk_ratio": [0.02],
        "boll_period": [18, 20, 22],
        "trail_tighten_profit_multiple": [1.0, 1.5, 2.0],
        "trail_tight_atr_multiple": [0.8, 1.0, 1.2],
        "macd_fast": [10, 12],
        "macd_slow": [21, 26],
        "macd_signal": [7, 9],
    },
    "multi_factor": {
        "trail_atr_multiple": [1.6, 1.7, 1.9],
        "max_risk_ratio": [0.018, 0.02],
        "trail_tighten_profit_multiple": [1.0, 1.5, 2.0],
        "trail_tight_atr_multiple": [0.8, 1.0, 1.2],
        "macd_fast": [10, 12],
        "macd_slow": [21, 26],
        "macd_signal": [7, 9],
    },
}

# Grid actually optimized: PARAM_GRID filtered by strategy.ACTIVE_STRATEGIES
# (paused strategies keep their grid definitions above for easy re-enable).
ACTIVE_PARAM_GRID = {sid: grid for sid, grid in PARAM_GRID.items() if ACTIVE_STRATEGIES is None or sid in ACTIVE_STRATEGIES}


def main():
    parser = argparse.ArgumentParser(description="Grid parameter optimization")
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
    args = parser.parse_args()

    maxcpu = resolve_maxcpu(args.maxcpu)

    runner = BacktestRunner()
    valid_codes = resolve_target_codes(args, runner.ds.cfg)
    # Each symbol's cache is read only once; grid combinations reuse it directly
    cache_map = {code: runner.ds.load_cached_data(code) for code in valid_codes}

    result_rows = []
    for code in valid_codes:
        df = cache_map[code]
        if df is None or df.empty:
            print(f"Symbol {code} has no cached data, skipping")
            continue
        for strategy_id, grid in ACTIVE_PARAM_GRID.items():
            print(f"Symbol {code} strategy {strategy_id}: grid {len(grid)} params on {maxcpu} workers")
            try:
                results = runner.optimize(df, strategy_id, grid, maxcpu=maxcpu)
            except Exception as e:
                print(f"Exception {code} {strategy_id}: {e}")
                continue
            for param_dict, final_value in results:
                profit = final_value - runner.initial_capital
                profit_rate = profit / runner.initial_capital
                result_rows.append(
                    {
                        "stock_code": code,
                        "strategy": strategy_id,
                        **param_dict,
                        "final_capital": round(final_value, 2),
                        "profit": round(profit, 2),
                        "profit_rate": round(profit_rate, 4),
                    }
                )

    res_df = pd.DataFrame(result_rows)
    if not res_df.empty:
        res_df = res_df[res_df["profit_rate"] > 0]
    write_stage_csv(PARAM_GRID_CSV, res_df, valid_codes)
    print(f"Grid optimization results written to: {PARAM_GRID_CSV} ({len(res_df)} rows)")


if __name__ == "__main__":
    main()
