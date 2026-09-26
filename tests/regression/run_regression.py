#!/usr/bin/env python3
# Deterministic regression gate for strategy code changes.
#
# Runs every registered strategy over a FIXED stock set with the class-default
# params (DEFAULT_STRATEGY_PARAMS, NOT config-optimized params) and LOCAL CACHED
# data only (no network), then compares key metrics with golden.json.
#
# Usage:
#   python tests/regression/run_regression.py                 # check vs golden (exit 1 on ANY diff)
#   python tests/regression/run_regression.py --update-golden # accept current results as new golden
#   python tests/regression/run_regression.py --stock-list 000725
#
# Golden policy:
#   - Run this after every strategy change. SAME = no behavior drift.
#   - IMPROVED/CHANGED must be reviewed; only after confirming the change is an
#     intentional improvement run with --update-golden.
#   - DEGRADED must never be golden-updated; revert or fix first.
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import backtrader as bt
import pandas as pd

# Anchor project root (tests/regression -> repo root) and reuse production code
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from comm import AStockCommission  # noqa: E402
from data_source import AStockData, DataSource  # noqa: E402
from strategy import DEFAULT_STRATEGY_PARAMS, STRATEGY_MAPPING  # noqa: E402

sys.path.insert(0, str(ROOT / "opt_pipeline"))
from common import REGRESSION_STOCKS  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent / "golden.json"
# Absolute RMB tolerance for final portfolio value (deterministic same-machine
# runs are effectively bit-identical; this only absorbs cross-version float drift).
FINAL_VALUE_TOL = 0.01


def load_runtime_config():
    """Read capital / date window / commission from config.yaml global sections."""
    import yaml

    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return cfg["global_setting"], cfg["commission_config"]


def load_window_data(ds, code, global_setting):
    """Load cached data only, clipped to the fixed config date window."""
    df = ds.load_cached_data(code)
    if df is None:
        return None
    start_dt = pd.Timestamp(str(global_setting.get("start_date", "1900-01-01")))
    end_dt = pd.Timestamp(str(global_setting.get("end_date", "2999-12-31")))
    df = df[(df["datetime"] >= start_dt) & (df["datetime"] <= end_dt)].copy()
    return df.reset_index(drop=True)


def run_case(ds, comminfo, global_setting, code, strategy_id):
    """Run one stock/strategy backtest with class-default params; return metrics dict."""
    df = load_window_data(ds, code, global_setting)
    if df is None or df.empty:
        return None
    params = dict(DEFAULT_STRATEGY_PARAMS[strategy_id])

    cerebro = bt.Cerebro()
    cerebro.addstrategy(STRATEGY_MAPPING[strategy_id], **params)
    data_feed = AStockData(
        dataname=df,
        datetime="datetime",
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        timeframe=bt.TimeFrame.Days,
    )
    cerebro.adddata(data_feed)
    initial_capital = float(global_setting["initial_capital"])
    cerebro.broker.setcash(initial_capital)
    cerebro.broker.addcommissioninfo(comminfo)

    strategy_instance = cerebro.run()[0]

    trades_df = strategy_instance.get_trade_dataframe()
    action_df = strategy_instance.get_action_dataframe()
    final_value = float(strategy_instance.final_value)

    n_wins = int((trades_df["profit_loss_net"] > 0).sum()) if not trades_df.empty else 0
    n_buys = int((action_df["side"] == "BUY").sum()) if not action_df.empty else 0
    n_sells = int((action_df["side"] == "SELL").sum()) if not action_df.empty else 0

    return {
        "params": params,
        "bars": int(len(df)),
        "final_value": round(final_value, 4),
        "total_return": round(final_value / initial_capital - 1.0, 6),
        "n_trades": int(len(trades_df)),
        "n_wins": n_wins,
        "n_buys": n_buys,
        "n_sells": n_sells,
    }


def case_key(code, strategy_id):
    return "{}|{}".format(code, strategy_id)


def run_all_cases(stock_list):
    """Execute all stock x strategy cases; return {case_key: metrics}."""
    global_setting, comm_cfg = load_runtime_config()
    ds = DataSource()
    comminfo = AStockCommission(
        commission=comm_cfg["commission"],
        stamp_duty=comm_cfg["stamp_duty"],
        transfer_fee=comm_cfg["transfer_fee"],
    )
    results = {}
    for code in stock_list:
        for strategy_id in STRATEGY_MAPPING:
            key = case_key(code, strategy_id)
            print("Running {} ...".format(key))
            metrics = run_case(ds, comminfo, global_setting, code, strategy_id)
            if metrics is None:
                print("  [WARN] no cached data in window, skipped")
                continue
            results[key] = metrics
    return results, global_setting


def classify_case(new_metrics, old_metrics):
    """Classify one case against golden: SAME / IMPROVED / DEGRADED / CHANGED / NEW."""
    if old_metrics is None:
        return "NEW", []

    notes = []
    structural_fields = ("bars", "n_trades", "n_wins", "n_buys", "n_sells")
    structural_changed = False
    for field in structural_fields:
        old_val = old_metrics.get(field)
        new_val = new_metrics.get(field)
        if old_val != new_val:
            structural_changed = True
            notes.append("{}: {} -> {}".format(field, old_val, new_val))

    # Params signature mismatch usually means class defaults changed intentionally
    if old_metrics.get("params") != new_metrics.get("params"):
        structural_changed = True
        notes.append("params signature changed")

    old_final = old_metrics.get("final_value", 0.0)
    new_final = new_metrics.get("final_value", 0.0)
    delta = new_final - old_final
    if abs(delta) > FINAL_VALUE_TOL:
        notes.append("final_value: {:.2f} -> {:.2f} ({:+.2f})".format(old_final, new_final, delta))

    if not notes:
        return "SAME", notes
    if structural_changed:
        return "CHANGED", notes
    return "IMPROVED" if delta > 0 else "DEGRADED", notes


def check_mode(results):
    """Compare results with golden; return process exit code."""
    if not GOLDEN_PATH.exists():
        print("[ERROR] golden.json not found. Run with --update-golden to create it.")
        return 2
    with open(GOLDEN_PATH, "r", encoding="utf-8") as fh:
        golden = json.load(fh)
    golden_cases = golden.get("cases", {})

    order = {"DEGRADED": 0, "CHANGED": 1, "IMPROVED": 2, "NEW": 3, "SAME": 4}
    rows = []
    has_diff = False
    for key in sorted(results):
        status, notes = classify_case(results[key], golden_cases.get(key))
        rows.append((key, status, notes))
        if status != "SAME":
            has_diff = True

    print("\n{:<32} {:<10} {}".format("CASE", "STATUS", "DETAIL"))
    print("-" * 90)
    for key, status, notes in sorted(rows, key=lambda r: (order[r[1]], r[0])):
        detail = "; ".join(notes) if notes else ""
        print("{:<32} {:<10} {}".format(key, status, detail))

    n_same = sum(1 for _, s, _ in rows if s == "SAME")
    print("\n{} / {} cases SAME".format(n_same, len(rows)))
    if has_diff:
        print("\nDifferences detected. Review the deltas above:")
        print("  - intentional improvement -> rerun with --update-golden")
        print("  - regression               -> revert/fix before committing")
        return 1
    print("All regression cases match golden.")
    return 0


def update_golden(results, global_setting, stock_list):
    """Write current results as the new golden baseline."""
    payload = {
        "meta": {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stocks": list(stock_list),
            "initial_capital": float(global_setting["initial_capital"]),
            "start_date": str(global_setting.get("start_date", "")),
            "end_date": str(global_setting.get("end_date", "")),
        },
        "cases": results,
    }
    with open(GOLDEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print("Golden updated: {} ({} cases)".format(GOLDEN_PATH, len(results)))


def main():
    parser = argparse.ArgumentParser(description="Strategy regression gate")
    parser.add_argument("--update-golden", action="store_true", help="overwrite golden.json with current results")
    parser.add_argument("--stock-list", type=str, default=None, help="comma-separated stock codes (default: pinned set)")
    args = parser.parse_args()

    stock_list = args.stock_list.split(",") if args.stock_list else REGRESSION_STOCKS
    results, global_setting = run_all_cases(stock_list)

    if args.update_golden:
        update_golden(results, global_setting, stock_list)
        return 0
    return check_mode(results)


if __name__ == "__main__":
    sys.exit(main())
