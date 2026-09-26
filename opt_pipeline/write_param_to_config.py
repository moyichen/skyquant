# Write the aggregated recommended parameters into config.yaml's strategy_params
import argparse
import os

import yaml
from common import AGGREGATE_CSV, CONFIG_PATH, extract_params, parse_code_list, read_stage_csv, to_native

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
    parser = argparse.ArgumentParser(description="Write aggregated recommended parameters into config.yaml")
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to write (default: all rows in the aggregate CSV). "
        "Only the given symbols' strategy_params entries are touched; all other config content is preserved.",
    )
    args = parser.parse_args()

    agg_df = read_stage_csv(AGGREGATE_CSV)
    if args.stock_list:
        wanted = set(parse_code_list(args.stock_list))
        agg_df = agg_df[agg_df["stock_code"].astype(str).isin(wanted)]
    valid_df = agg_df[agg_df["recommend_use"] == True] if not agg_df.empty else agg_df

    config = load_config()
    # Coerce keys to str (unquoted numeric codes in yaml are parsed as int) and write back to fix
    config["strategy_params"] = {
        str(k): v for k, v in config["strategy_params"].items()
    }

    written = []
    for _, row in valid_df.iterrows():
        code = str(row["stock_code"])
        strategy_id = row["strategy"]
        # NaN filtering + period to int + numpy scalars to native types
        param = to_native(extract_params(row, NON_PARAM_COLS))
        config["strategy_params"].setdefault(code, {})
        config["strategy_params"][code][strategy_id] = param
        written.append((code, strategy_id, param))

    with open(CONFIG_PATH, "w", encoding="utf8") as fw:
        yaml.dump(config, fw, sort_keys=False, allow_unicode=True)

    print(f"Qualified stable parameters automatically written to config.yaml ({len(written)} entries)")
    for code, sname, p in written:
        print(f"Symbol {code} strategy {sname}:{p}")


if __name__ == "__main__":
    main()
