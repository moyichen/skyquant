# Out-of-sample verification: backtest grid-selected parameters on train/test sets
# separately to filter out overfitted parameters
import argparse

import pandas as pd
from common import (
    OUT_SAMPLE_CSV,
    PARAM_GRID_CSV,
    BacktestRunner,
    extract_params,
    parse_code_list,
    read_stage_csv,
    resolve_maxcpu,
    write_stage_csv,
)

# Non-strategy-parameter columns in the grid result CSV
NON_PARAM_COLS = ["stock_code", "strategy", "final_capital", "profit", "profit_rate"]
# Minimum bars required in train/test slices (must exceed largest indicator minperiod,
# e.g. SMA60 -> minperiod 60; backtrader's vectorized mode crashes on shorter slices)
MIN_TRAIN_BARS = 60
MIN_TEST_BARS = 60


def _print_data_too_short_help(code, kind, bars, train_end, start_date, end_date):
    """Print actionable error and remediation when a slice has too few bars.

    kind: "train" or "test". Triggered when the slice is shorter than the
    largest indicator warm-up in the strategy pool, so backtrader would crash.
    """
    print(f"[ERROR] Symbol {code} {kind}-set has only {bars} bars, below the minimum {MIN_TRAIN_BARS if kind == 'train' else MIN_TEST_BARS}. Current config: start_date={start_date}, end_date={end_date}, out_sample_train_end={train_end}. Skipping this symbol.")
    print(
        "Remediation: choose one of the following —\n"
        "  1) Move global_setting.start_date earlier so the train set has >= 60 bars "
        "before out_sample_train_end (need start_date <= 2024-10-08 with end_date=2026-09-21).\n"
        "  2) Move opt_pipeline.out_sample_train_end later so more bars fall in the test set.\n"
        "  3) Accept that the data window is too short for out-of-sample verification."
    )


def split_train_test(df, train_end):
    df_train = df[df["datetime"] <= train_end].copy().reset_index(drop=True)
    df_test = df[df["datetime"] > train_end].copy().reset_index(drop=True)
    return df_train, df_test


def main():
    parser = argparse.ArgumentParser(description="Out-of-sample overfit verification")
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to verify (default: all rows in the grid CSV)",
    )
    parser.add_argument(
        "--maxcpu",
        type=int,
        default=0,
        help="Number of worker processes for train/test backtests (0 or -1 for all CPUs)",
    )
    args = parser.parse_args()
    maxcpu = resolve_maxcpu(args.maxcpu)

    runner = BacktestRunner()
    cfg = runner.ds.cfg.get("opt_pipeline", {})
    train_end = cfg.get("out_sample_train_end", "2024-12-31")
    overfit_threshold = float(cfg.get("out_sample_overfit_threshold", 0.15))
    start_date = runner.ds.start_date
    end_date = runner.ds.end_date

    grid_df = read_stage_csv(PARAM_GRID_CSV)
    if args.stock_list:
        wanted = set(parse_code_list(args.stock_list))
        grid_df = grid_df[grid_df["stock_code"].astype(str).isin(wanted)]
    # Per-symbol mode: replace rows for the requested symbols even when the grid
    # CSV has no rows for them (stale rows must not survive). Batch mode: rewrite.
    touched_codes = parse_code_list(args.stock_list) if args.stock_list else None

    verify_result = []
    skipped_any = False
    # Group rows by symbol: train/test slices are identical per symbol, so cache
    # and split once, then dispatch all (strategy, params) jobs to one Pool.
    for code in grid_df["stock_code"].astype(str).unique():
        group = grid_df[grid_df["stock_code"].astype(str) == code]
        cache_df = runner.ds.load_cached_data(code)
        if cache_df is None or cache_df.empty:
            print(f"Symbol {code} has no cached data, skipping")
            skipped_any = True
            continue
        df_train, df_test = split_train_test(cache_df, train_end)
        if len(df_train) < MIN_TRAIN_BARS:
            _print_data_too_short_help(code, "train", len(df_train), train_end, start_date, end_date)
            skipped_any = True
            continue
        if len(df_test) < MIN_TEST_BARS:
            _print_data_too_short_help(code, "test", len(df_test), train_end, start_date, end_date)
            skipped_any = True
            continue

        jobs = [(row["strategy"], extract_params(row, NON_PARAM_COLS)) for _, row in group.iterrows()]
        print(f"Symbol {code}: out-of-sample verify {len(jobs)} combos on {maxcpu} workers")
        pairs = runner.run_train_test_batch(df_train, df_test, jobs, maxcpu=maxcpu)
        for (strategy_id, param), (train_final, test_final) in zip(jobs, pairs):
            train_rate = runner.profit_rate(train_final)
            test_rate = runner.profit_rate(test_final)
            overfit_flag = 1 if (train_rate - test_rate) > overfit_threshold else 0
            verify_result.append(
                {
                    "stock_code": code,
                    "strategy": strategy_id,
                    **param,
                    "train_profit_rate": round(train_rate, 4),
                    "test_profit_rate": round(test_rate, 4),
                    "overfit": overfit_flag,
                }
            )

    verify_df = pd.DataFrame(verify_result)
    if not verify_df.empty:
        verify_df = verify_df[verify_df["overfit"] == 0]
    write_stage_csv(OUT_SAMPLE_CSV, verify_df, touched_codes)
    if verify_df.empty and skipped_any:
        print("[ERROR] Out-of-sample verification produced no valid parameters. All symbols were skipped due to insufficient data. See remediation hints above and adjust config.yaml.")
    print(f"Out-of-sample verification complete, overfitted parameters removed, output:{OUT_SAMPLE_CSV}")


if __name__ == "__main__":
    main()
