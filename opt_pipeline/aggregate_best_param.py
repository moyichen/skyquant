# 多标的最优参数聚合：每个 标的×策略 取滚动窗口平均收益最高的参数组合
import pandas as pd
from common import AGGREGATE_CSV, ROLLING_CSV, extract_params, read_stage_csv

# 滚动校验结果 CSV 中非策略参数的列
NON_PARAM_COLS = ["stock_code", "strategy", "avg_test_profit", "valid"]
# 空结果时保留固定表头，供下游 write_param_to_config 正常处理
EMPTY_COLUMNS = ["stock_code", "strategy", "avg_profit", "recommend_use"]


def main():
    df = read_stage_csv(ROLLING_CSV)
    aggregate_rows = []
    if df.empty:
        print("滚动校验结果为空，无参数可聚合")
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
    agg_df.to_csv(AGGREGATE_CSV, index=False, encoding="utf8")
    print("多标的最优参数聚合完成")


if __name__ == "__main__":
    main()
