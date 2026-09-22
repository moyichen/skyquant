# Write the aggregated recommended parameters into config.yaml's strategy_params
import os

import yaml
from common import AGGREGATE_CSV, CONFIG_PATH, extract_params, read_stage_csv, to_native

# Non-strategy-parameter columns in the aggregate result CSV
NON_PARAM_COLS = ["stock_code", "strategy", "avg_profit", "recommend_use"]


def load_config():
    """Read config.yaml; provide a minimal skeleton when missing or empty to ensure strategy_params exists"""
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
    # Coerce keys to str (unquoted numeric codes in yaml are parsed as int) and write back to fix
    config["strategy_params"] = {
        str(k): v for k, v in config["strategy_params"].items()
    }

    for _, row in valid_df.iterrows():
        code = str(row["stock_code"])
        strategy_id = row["strategy"]
        # NaN filtering + period to int + numpy scalars to native types
        param = to_native(extract_params(row, NON_PARAM_COLS))
        config["strategy_params"].setdefault(code, {})
        config["strategy_params"][code][strategy_id] = param

    with open(CONFIG_PATH, "w", encoding="utf8") as fw:
        yaml.dump(config, fw, sort_keys=False, allow_unicode=True)

    print("Qualified stable parameters automatically written to config.yaml")
    for code, strategy_item in config["strategy_params"].items():
        for sname, p in strategy_item.items():
            print(f"Symbol {code} strategy {sname}:{p}")


if __name__ == "__main__":
    main()
