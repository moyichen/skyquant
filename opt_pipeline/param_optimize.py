# 网格参数优化：遍历各策略参数网格做全样本回测，仅保留盈利参数组合
import itertools

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
    runner = BacktestRunner()
    valid_codes = [item["code"] for item in runner.ds.cfg["stock_list"]]
    # 每个标的只读一次缓存，网格组合直接复用
    cache_map = {code: runner.ds.load_cached_data(code) for code in valid_codes}

    result_rows = []
    for strategy_id, grid in PARAM_GRID.items():
        keys = list(grid.keys())
        for param_tuple in itertools.product(*grid.values()):
            param_dict = dict(zip(keys, param_tuple))
            for code in valid_codes:
                df = cache_map[code]
                if df is None or df.empty:
                    print(f"标的 {code} 无缓存数据，跳过")
                    continue
                try:
                    final_value = runner.run(df, strategy_id, param_dict)
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
                except Exception as e:
                    print(f"异常 {code} {strategy_id} {param_dict}:{e}")

    res_df = pd.DataFrame(result_rows)
    res_df = res_df[res_df["profit_rate"] > 0]
    res_df.to_csv(PARAM_GRID_CSV, index=False, encoding="utf8")
    print(f"网格调参结果输出:{PARAM_GRID_CSV}")


if __name__ == "__main__":
    main()
