# 外样本校验：网格入选参数在训练集/测试集分别回测，剔除过拟合参数
import pandas as pd

from common import (BacktestRunner, extract_params, read_stage_csv,
                    PARAM_GRID_CSV, OUT_SAMPLE_CSV)

TRAIN_END = "2024-12-31"
# 网格结果 CSV 中非策略参数的列
NON_PARAM_COLS = ["stock_code", "strategy", "final_capital", "profit", "profit_rate"]
# 训练/测试收益率差异超过该阈值判定为过拟合
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
        strat_id = row["strategy"]
        param = extract_params(row, NON_PARAM_COLS)

        cache_df = runner.ds.load_cached_data(code)
        if cache_df is None or cache_df.empty:
            print(f"标的 {code} 无缓存数据，跳过")
            continue
        df_train, df_test = split_train_test(cache_df)
        if df_train.empty or df_test.empty:
            print(f"标的 {code} 训练/测试集为空，跳过")
            continue

        train_rate = runner.profit_rate(runner.run(df_train, strat_id, param))
        test_rate = runner.profit_rate(runner.run(df_test, strat_id, param))
        overfit_flag = 1 if (train_rate - test_rate) > OVERFIT_THRESHOLD else 0
        verify_result.append({
            "stock_code": code, "strategy": strat_id, **param,
            "train_profit_rate": round(train_rate, 4),
            "test_profit_rate": round(test_rate, 4),
            "overfit": overfit_flag
        })

    verify_df = pd.DataFrame(verify_result)
    if not verify_df.empty:
        verify_df = verify_df[verify_df["overfit"] == 0]
    verify_df.to_csv(OUT_SAMPLE_CSV, index=False, encoding="utf8")
    print(f"外样本校验完成，已剔除过拟合参数，输出:{OUT_SAMPLE_CSV}")


if __name__ == "__main__":
    main()
