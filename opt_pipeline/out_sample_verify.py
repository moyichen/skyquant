# Out-of-sample verification: backtest grid-selected parameters on train/test sets
# separately to filter out overfitted parameters
import pandas as pd
from common import (
    OUT_SAMPLE_CSV,
    PARAM_GRID_CSV,
    BacktestRunner,
    extract_params,
    read_stage_csv,
)

TRAIN_END = "2024-12-31"
# Non-strategy-parameter columns in the grid result CSV
NON_PARAM_COLS = ["stock_code", "strategy", "final_capital", "profit", "profit_rate"]
# A train/test profit-rate difference exceeding this threshold is flagged as overfit
OVERFIT_THRESHOLD = 0.15


def split_train_test(df):
    df_train = df[df["datetime"] <= TRAIN_END].copy().reset_index(drop=True)
    df_test = df[df["datetime"] > TRAIN_END].copy().reset_index(drop=True)
    return df_train, df_test


def main():
    runner = BacktestRunner()
    grid_df = read_stage_csv(PARAM_GRID_CSV)
    verify_result = []
    for _, row in grid_df.iterrows():
        code = row["stock_code"]
        strategy_id = row["strategy"]
        param = extract_params(row, NON_PARAM_COLS)

        cache_df = runner.ds.load_cached_data(code)
        if cache_df is None or cache_df.empty:
            print(f"Symbol {code} has no cached data, skipping")
            continue
        df_train, df_test = split_train_test(cache_df)
        if df_train.empty or df_test.empty:
            print(f"Symbol {code} has an empty train/test set, skipping")
            continue

        train_rate = runner.profit_rate(runner.run(df_train, strategy_id, param))
        test_rate = runner.profit_rate(runner.run(df_test, strategy_id, param))
        overfit_flag = 1 if (train_rate - test_rate) > OVERFIT_THRESHOLD else 0
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
    verify_df.to_csv(OUT_SAMPLE_CSV, index=False, encoding="utf8")
    print(
        f"Out-of-sample verification complete, overfitted parameters removed, output:{OUT_SAMPLE_CSV}"
    )


if __name__ == "__main__":
    main()
