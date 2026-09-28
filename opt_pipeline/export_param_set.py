# Pipeline stage 5: export the aggregated recommended parameters as a versioned
# param set under params/experiments (freqtrade hyperopt --export-config style),
# then (unless --no-apply) merge the qualified code x strategy units into
# params/active.yaml. config.yaml is never touched.
import argparse

# common 引导 sys.path 加入项目根后，才能 import config_store
from common import (
    AGGREGATE_CSV,
    DataProvider,
    extract_params,
    parse_code_list,
    read_stage_csv,
    resolve_optimize_metric,
    to_native,
)
import config_store as cs

# Non-strategy-parameter columns in the aggregate result CSV
NON_PARAM_COLS = ["stock_code", "strategy", "avg_profit", "recommend_use"]


def main():
    parser = argparse.ArgumentParser(
        description="Archive aggregated params to params/experiments and optionally apply to active.yaml"
    )
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to export (default: all rows in the aggregate CSV). "
        "Only the given symbols' entries are applied; all other active.yaml entries are preserved.",
    )
    parser.add_argument("--all-stocks", action="store_true",
                        help="Accepted for pipeline uniformity; all rows are already the default")
    parser.add_argument("--maxcpu", type=int, default=0,
                        help="Accepted for pipeline uniformity; this stage is single-process")
    parser.add_argument("--no-apply", action="store_true",
                        help="Only archive to params/experiments; do not merge into active.yaml")
    args = parser.parse_args()

    optimize_metric = resolve_optimize_metric(DataProvider().cfg)

    agg_df = read_stage_csv(AGGREGATE_CSV)
    if args.stock_list:
        wanted = set(parse_code_list(args.stock_list))
        agg_df = agg_df[agg_df["stock_code"].astype(str).isin(wanted)]
    valid_df = agg_df[agg_df["recommend_use"] == True] if not agg_df.empty else agg_df

    strategy_params, metrics, codes = {}, {}, []
    for _, row in valid_df.iterrows():
        code = str(row["stock_code"])
        strategy_id = str(row["strategy"])
        param = to_native(extract_params(row, NON_PARAM_COLS))
        strategy_params.setdefault(code, {})[strategy_id] = param
        metrics.setdefault(code, {})[strategy_id] = {"avg_profit": round(float(row["avg_profit"]), 4)}
        if code not in codes:
            codes.append(code)

    if not strategy_params:
        print("No qualified (recommend_use=True) parameters; nothing exported")
        return

    # 1) 归档：每次 opt 生成不可变历史参数组（多组参数共存，可随时回看/重新生效）
    exp_path = cs.export_experiment(
        strategy_params,
        meta={
            "source": "opt",
            "objective": optimize_metric,
            "codes": codes,
            "note": f"aggregated from {AGGREGATE_CSV.split('/')[-1]}",
            "metrics": metrics,
        },
    )
    print(f"Optimal param set archived: {exp_path} ({len(codes)} symbols)")

    # 2) 生效：默认把本次合格 entries 合并进 active.yaml（只动涉及的 code×strategy）
    if args.no_apply:
        print("--no-apply set; params/active.yaml left unchanged. "
              f"Apply later with: python3 skyquant.py params apply {exp_path}")
    else:
        res = cs.apply_param_set(
            exp_path,
            codes=set(codes),
            note=f"opt apply ({optimize_metric}) @ {cs._now()}",
        )
        print(f"Applied {len(res['applied'])} code x strategy units to params/active.yaml")
        for code, sid in res["applied"]:
            print(f"  {code} / {sid}")


if __name__ == "__main__":
    main()
