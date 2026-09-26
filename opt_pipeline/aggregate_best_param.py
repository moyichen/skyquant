# Multi-symbol best parameter aggregation: for each symbol x strategy, take the parameter
# combination with the highest rolling-window average profit
import argparse

import pandas as pd
from common import AGGREGATE_CSV, ROLLING_CSV, extract_params, parse_code_list, read_stage_csv, write_stage_csv

# Non-strategy-parameter columns in the rolling verification result CSV
NON_PARAM_COLS = ["stock_code", "strategy", "avg_test_profit", "valid"]
# When results are empty, keep a fixed header so the downstream write_param_to_config can process it
EMPTY_COLUMNS = ["stock_code", "strategy", "avg_profit", "recommend_use"]


def main():
    parser = argparse.ArgumentParser(description="Aggregate best parameters per symbol x strategy")
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to aggregate (default: all rows in the rolling CSV)",
    )
    args = parser.parse_args()

    df = read_stage_csv(ROLLING_CSV)
    if args.stock_list:
        wanted = set(parse_code_list(args.stock_list))
        df = df[df["stock_code"].astype(str).isin(wanted)]
    touched_codes = parse_code_list(args.stock_list) if args.stock_list else None

    aggregate_rows = []
    if df.empty:
        print("Rolling verification results are empty, no parameters to aggregate")
    else:
        for (code, strategy), group in df.groupby(["stock_code", "strategy"]):
            best_row = group.loc[group["avg_test_profit"].idxmax()]
            best_param = extract_params(best_row, NON_PARAM_COLS)
            profit = best_row["avg_test_profit"]
            aggregate_rows.append(
                {
                    "stock_code": code,
                    "strategy": strategy,
                    **best_param,
                    "avg_profit": round(profit, 4),
                    "recommend_use": bool(profit > 0),
                }
            )

    agg_df = (
        pd.DataFrame(aggregate_rows)
        if aggregate_rows
        else pd.DataFrame(columns=EMPTY_COLUMNS)
    )
    write_stage_csv(AGGREGATE_CSV, agg_df, touched_codes)
    print("Multi-symbol best parameter aggregation complete")


if __name__ == "__main__":
    main()
