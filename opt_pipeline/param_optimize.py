# Grid parameter optimization: use cerebro.optstrategy for parallel grid search,
# keeping only profitable parameter combinations
import argparse

import pandas as pd
from common import PARAM_GRID_CSV, BacktestRunner

PARAM_GRID = {
    "maatr_base": {
        "atr_multiple": [1.6, 1.8, 2.0],
        "max_risk_ratio": [0.015, 0.02, 0.025],
    },
    "momentum": {
        "atr_multiple": [1.4, 1.5, 1.7],
        "max_risk_ratio": [0.02, 0.025],
        "momentum_period": [18, 20, 22],
    },
    "short_reversal": {
        "atr_mult": [1.8, 2.0, 2.2],
        "max_risk_ratio": [0.02],
        "fall_ratio": [0.15, 0.18, 0.2],
    },
    "boll_ma": {
        "atr_mult": [1.5, 1.6, 1.8],
        "max_risk_ratio": [0.02],
        "boll_period": [18, 20, 22],
    },
    "multi_factor": {"atr_mult": [1.6, 1.7, 1.9], "max_risk_ratio": [0.018, 0.02]},
}


def main():
    parser = argparse.ArgumentParser(description="Grid parameter optimization")
    parser.add_argument(
        "--maxcpu",
        type=int,
        default=1,
        help="Number of CPUs for parallel optimization (0 or -1 for auto)",
    )
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to run. Defaults to all stocks in config.yaml.",
    )
    args = parser.parse_args()

    maxcpu = args.maxcpu
    if maxcpu <= 0:
        import multiprocessing

        maxcpu = multiprocessing.cpu_count()

    runner = BacktestRunner()
    valid_codes = [item["code"] for item in runner.ds.cfg["stock_list"]]
    if args.stock_list:
        wanted = {c.strip() for c in args.stock_list.split(",") if c.strip()}
        valid_codes = [c for c in valid_codes if c in wanted]
    # Each symbol's cache is read only once; grid combinations reuse it directly
    cache_map = {code: runner.ds.load_cached_data(code) for code in valid_codes}

    result_rows = []
    for code in valid_codes:
        df = cache_map[code]
        if df is None or df.empty:
            print(f"Symbol {code} has no cached data, skipping")
            continue
        for strategy_id, grid in PARAM_GRID.items():
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
    res_df.to_csv(PARAM_GRID_CSV, index=False, encoding="utf8")
    print(f"Grid optimization results written to: {PARAM_GRID_CSV}")


if __name__ == "__main__":
    main()
