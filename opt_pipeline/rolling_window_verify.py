# Rolling window stability check: validate parameter stability across multiple
# 4-year train / 1-year test rolling windows
import pandas as pd
from common import (
    OUT_SAMPLE_CSV,
    ROLLING_CSV,
    BacktestRunner,
    extract_params,
    read_stage_csv,
)

# Non-strategy-parameter columns in the out-of-sample result CSV
NON_PARAM_COLS = [
    "stock_code",
    "strategy",
    "train_profit_rate",
    "test_profit_rate",
    "overfit",
]


def rolling_slice(df, start_year=2020, train_len=4, test_len=1):
    """Generate rolling train/test market data slice pairs; windows with insufficient
    sample size are dropped"""
    windows = []
    for offset in range(7):
        train_s = f"{start_year + offset}-01-01"
        train_e = f"{start_year + offset + train_len}-12-31"
        test_s = f"{start_year + offset + train_len + 1}-01-01"
        test_e = f"{start_year + offset + train_len + test_len}-12-31"
        df_train = df[
            (df["datetime"] >= train_s) & (df["datetime"] <= train_e)
        ].reset_index(drop=True)
        df_test = df[
            (df["datetime"] >= test_s) & (df["datetime"] <= test_e)
        ].reset_index(drop=True)
        if len(df_train) > 200 and len(df_test) > 100:
            windows.append((df_train, df_test))
    return windows


def main():
    runner = BacktestRunner()
    df_input = read_stage_csv(OUT_SAMPLE_CSV)
    result_list = []
    for _, row in df_input.iterrows():
        code = row["stock_code"]
        strategy_id = row["strategy"]
        param = extract_params(row, NON_PARAM_COLS)

        full_df = runner.ds.load_cached_data(code)
        if full_df is None or full_df.empty:
            print(f"Symbol {code} has no cached data, skipping")
            continue

        test_rate_list = []
        for _train_df, test_df in rolling_slice(full_df):
            test_rate = runner.profit_rate(runner.run(test_df, strategy_id, param))
            test_rate_list.append(test_rate)
        if len(test_rate_list) == 0:
            continue

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
    res_df.to_csv(ROLLING_CSV, index=False, encoding="utf8")
    print("Rolling window stability check complete")


if __name__ == "__main__":
    main()
