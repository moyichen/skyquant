# Rolling window stability check: validate parameter stability across multiple
# rolling train/test windows. All window parameters are configurable in
# config.yaml (opt_pipeline section); defaults preserve the original
# 4-year-train / 1-year-test behaviour starting from 2020.
import argparse

import pandas as pd
from common import (
    OUT_SAMPLE_CSV,
    ROLLING_CSV,
    BacktestRunner,
    extract_params,
    parse_code_list,
    read_stage_csv,
    resolve_maxcpu,
    write_stage_csv,
)

# Non-strategy-parameter columns in the out-of-sample result CSV
NON_PARAM_COLS = [
    "stock_code",
    "strategy",
    "train_profit_rate",
    "test_profit_rate",
    "overfit",
]


def rolling_slice(
    df,
    start_year=2020,
    train_years=4,
    test_years=1,
    min_train_bars=200,
    min_test_bars=100,
):
    """Generate rolling train/test market data slice pairs; windows with
    insufficient sample size are dropped.

    Windows are anchored to calendar years (start_year, start_year+1, ...) and
    slide forward by one year per iteration. A window is kept only when both
    the train slice and the test slice have enough bars to exceed the largest
    indicator warm-up in the strategy pool.
    """
    windows = []
    # 7 iterations cover ~5 years of overlap with 4-year train windows
    for offset in range(7):
        train_s = f"{start_year + offset}-01-01"
        train_e = f"{start_year + offset + train_years}-12-31"
        test_s = f"{start_year + offset + train_years + 1}-01-01"
        test_e = f"{start_year + offset + train_years + test_years}-12-31"
        df_train = df[(df["datetime"] >= train_s) & (df["datetime"] <= train_e)].reset_index(drop=True)
        df_test = df[(df["datetime"] >= test_s) & (df["datetime"] <= test_e)].reset_index(drop=True)
        if len(df_train) > min_train_bars and len(df_test) > min_test_bars:
            windows.append((df_train, df_test))
    return windows


def _print_no_windows_help(code, cfg, start_date, end_date):
    """Print actionable error and remediation when no rolling window qualifies."""
    print(
        f"[ERROR] Symbol {code} produced no valid rolling windows. "
        f"Current config: start_date={start_date}, end_date={end_date}, "
        f"rolling_start_year={cfg.get('rolling_start_year')}, "
        f"rolling_train_years={cfg.get('rolling_train_years')}, "
        f"rolling_test_years={cfg.get('rolling_test_years')}, "
        f"rolling_min_train_bars={cfg.get('rolling_min_train_bars')}, "
        f"rolling_min_test_bars={cfg.get('rolling_min_test_bars')}."
    )
    print(
        "Remediation: choose one of the following —\n"
        "  1) Move global_setting.start_date earlier so at least one train window "
        "has > rolling_min_train_bars bars (e.g. start_date <= 2024-03-04 with the "
        "default 4-year/1-year windows and end_date=2026-09-21).\n"
        "  2) Reduce rolling_train_years / rolling_test_years so windows fit the data span.\n"
        "  3) Lower rolling_min_train_bars / rolling_min_test_bars, but never below 60 "
        "(SMA60 minperiod); backtrader crashes on slices shorter than the indicator warm-up.\n"
        "  4) Move rolling_start_year later so the first test window overlaps the data."
    )


def main():
    parser = argparse.ArgumentParser(description="Rolling window stability verification")
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to verify (default: all rows in the out-of-sample CSV)",
    )
    parser.add_argument(
        "--maxcpu",
        type=int,
        default=0,
        help="Number of worker processes for rolling-window backtests (0 or -1 for all CPUs)",
    )
    args = parser.parse_args()
    maxcpu = resolve_maxcpu(args.maxcpu)

    runner = BacktestRunner()
    cfg = runner.ds.cfg.get("opt_pipeline", {})
    start_year = int(cfg.get("rolling_start_year", 2020))
    train_years = int(cfg.get("rolling_train_years", 4))
    test_years = int(cfg.get("rolling_test_years", 1))
    min_train_bars = int(cfg.get("rolling_min_train_bars", 200))
    min_test_bars = int(cfg.get("rolling_min_test_bars", 100))
    start_date = runner.ds.start_date
    end_date = runner.ds.end_date

    df_input = read_stage_csv(OUT_SAMPLE_CSV)
    if args.stock_list:
        wanted = set(parse_code_list(args.stock_list))
        df_input = df_input[df_input["stock_code"].astype(str).isin(wanted)]
    touched_codes = parse_code_list(args.stock_list) if args.stock_list else None

    result_list = []
    skipped_any = False
    # Group rows by symbol: rolling windows are identical per symbol, so build
    # them once and dispatch all (strategy, params) jobs x windows to one Pool.
    for code in df_input["stock_code"].astype(str).unique():
        group = df_input[df_input["stock_code"].astype(str) == code]
        full_df = runner.ds.load_cached_data(code)
        if full_df is None or full_df.empty:
            print(f"Symbol {code} has no cached data, skipping")
            skipped_any = True
            continue

        windows = rolling_slice(
            full_df,
            start_year=start_year,
            train_years=train_years,
            test_years=test_years,
            min_train_bars=min_train_bars,
            min_test_bars=min_test_bars,
        )
        if len(windows) == 0:
            _print_no_windows_help(code, cfg, start_date, end_date)
            skipped_any = True
            continue

        test_dfs = [test_df for _train_df, test_df in windows]
        jobs = [(row["strategy"], extract_params(row, NON_PARAM_COLS)) for _, row in group.iterrows()]
        print(f"Symbol {code}: rolling verify {len(jobs)} combos x {len(test_dfs)} windows on {maxcpu} workers")
        windows_finals = runner.run_rolling_batch(test_dfs, jobs, maxcpu=maxcpu)
        for (strategy_id, param), finals in zip(jobs, windows_finals):
            test_rate_list = [runner.profit_rate(final_value) for final_value in finals]
            avg_test = sum(test_rate_list) / len(test_rate_list)
            valid = 1 if avg_test > 0 else 0
            result_list.append(
                {
                    "stock_code": code,
                    "strategy": strategy_id,
                    **param,
                    "avg_test_profit": round(avg_test, 4),
                    "valid": valid,
                }
            )

    res_df = pd.DataFrame(result_list)
    if not res_df.empty:
        res_df = res_df[res_df["valid"] == 1]
    write_stage_csv(ROLLING_CSV, res_df, touched_codes)
    if res_df.empty and skipped_any:
        print("[ERROR] Rolling window verification produced no valid parameters. All symbols were skipped due to insufficient data. See remediation hints above and adjust config.yaml.")
    print("Rolling window stability check complete")


if __name__ == "__main__":
    main()
