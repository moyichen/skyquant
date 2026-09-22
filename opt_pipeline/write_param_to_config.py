# 将聚合后推荐使用的参数写入 config.yaml 的 strategy_params
import os

import yaml
from common import AGGREGATE_CSV, CONFIG_PATH, extract_params, read_stage_csv, to_native

# 聚合结果 CSV 中非策略参数的列
NON_PARAM_COLS = ["stock_code", "strategy", "avg_profit", "recommend_use"]


def load_config():
    """读取 config.yaml；缺失或为空时给出最小骨架，保证 strategy_params 存在"""
    if not os.path.exists(CONFIG_PATH):
        return {
            "global_setting": {},
            "commission_config": {},
            "stock_list": [],
            "strategy_params": {},
        }
    with open(CONFIG_PATH, "r", encoding="utf8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("strategy_params", {})
    return cfg


def main():
    agg_df = read_stage_csv(AGGREGATE_CSV)
    valid_df = agg_df[agg_df["recommend_use"] == True] if not agg_df.empty else agg_df

    config = load_config()
    # 键统一转str（yaml中未加引号的数字代码会被解析为int），并回写修复
    config["strategy_params"] = {
        str(k): v for k, v in config["strategy_params"].items()
    }

    for _, row in valid_df.iterrows():
        code = str(row["stock_code"])
        strategy_id = row["strategy"]
        # NaN 过滤 + period 转 int + numpy 标量转原生类型
        param = to_native(extract_params(row, NON_PARAM_COLS))
        config["strategy_params"].setdefault(code, {})
        config["strategy_params"][code][strategy_id] = param

    with open(CONFIG_PATH, "w", encoding="utf8") as fw:
        yaml.dump(config, fw, sort_keys=False, allow_unicode=True)

    print("合格稳定参数自动写入config.yaml配置文件")
    for code, strategy_item in config["strategy_params"].items():
        for sname, p in strategy_item.items():
            print(f"标的{code} 策略{sname}:{p}")


if __name__ == "__main__":
    main()
