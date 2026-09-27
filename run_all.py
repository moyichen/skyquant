"""SkyQuant one-click pipeline entry point.

Layered flow:
  config -> data fetch/cache -> stock pool pre-filter (basic always; trend with
  --screen; regime labels drive strategy routing) -> per-symbol optimization
  chain (grid -> out-of-sample -> rolling -> aggregate -> write config) ->
  final batched backtest + manual trade review.
"""

import argparse
import logging
import subprocess
import sys

import yaml

from comm import CACHE_DIR, LOG_FILE, OUTPUT_DIR, PLOT_DIR, PROJECT_ROOT, setup_logging

OPT_DIR = PROJECT_ROOT / "opt_pipeline"
sys.path.insert(0, str(OPT_DIR))
from common import resolve_target_codes  # noqa: E402
from dataprovider import DataProvider  # noqa: E402
from stock_filter import FILTER_CSV, filter_stock_pool  # noqa: E402

setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)


def run_step(name, cwd, cmd):
    logger.info("=" * 70)
    logger.info(name)
    logger.info(f"Working directory: {cwd}")
    logger.info(f"Command: {' '.join(cmd)}")
    logger.info("=" * 70)
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=sys.stdout, stderr=sys.stderr, text=True)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Subprocess returned non-zero exit code: {proc.returncode}")
        logger.info(f"{name} completed\n")
    except Exception as e:
        logger.error(f"{name} failed! {e!s}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="SkyQuant one-click quant pipeline")
    parser.add_argument(
        "--skip-data",
        action="store_true",
        help="Skip market data fetching (use local cache)",
    )
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to run (e.g. 000725,600519). Defaults to regression stocks.",
    )
    parser.add_argument(
        "--all-stocks",
        action="store_true",
        help="Run all stocks in config.yaml (default: regression stocks only)",
    )
    parser.add_argument(
        "--screen",
        action="store_true",
        help="Additionally apply the trendability gate (stock_filter.py): only symbols "
        "passing BOTH the basic quality filter and the trend filter enter the pipeline. "
        "Without --screen only the basic quality filter is enforced.",
    )
    args = parser.parse_args()

    with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    target_codes = resolve_target_codes(args, cfg)

    codes_csv = ",".join(target_codes)

    logger.info("==== SkyQuant full pipeline started ====")
    logger.info(f"Project root: {PROJECT_ROOT}")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info(f"Target symbols ({len(target_codes)}): {codes_csv}")

    for d in (CACHE_DIR, OUTPUT_DIR, OPT_DIR, PLOT_DIR):
        d.mkdir(exist_ok=True)

    # ---- Step 1: market data fetch (batched, I/O bound) ----
    if not args.skip_data:
        run_step(
            "[Data] Fetch full market data",
            PROJECT_ROOT,
            [sys.executable, "main.py", "--force_refresh", "--stock-list", codes_csv],
        )
    else:
        logger.info("--skip-data enabled, skipping market data fetch, using local cache")

    # ---- Step 2: stock pool pre-filter layer ----
    # Always runs (cache-only): Pairlist Filters gate (age/price/volume/turnover/
    # liquidity/name, aligned with freqtrade) plus regime classification;
    # output/stock_filter.csv drives strategy routing in param_optimize.py and
    # main.py. --screen additionally enforces trendability.
    filter_df = filter_stock_pool(DataProvider(), target_codes, cfg.get("stock_filter", {}))
    filter_df.to_csv(FILTER_CSV, index=False)

    pairlist_failed = filter_df[~filter_df["pairlist_passed"]]
    if len(pairlist_failed) > 0:
        logger.info(f"Pairlist Filters rejected {len(pairlist_failed)} symbols:")
        for _, row in pairlist_failed.iterrows():
            logger.info(f"  {row['stock_code']} {row['name']}: {row['pairlist_fail_reason']}")
    surviving = filter_df[filter_df["pairlist_passed"]]

    if args.screen:
        passed = surviving[surviving["passed"]]
        trend_failed = surviving[~surviving["passed"]]
        logger.info(f"Trendability screening: {len(passed)}/{len(target_codes)} symbols passed pairlist + trend")
        for _, row in trend_failed.iterrows():
            logger.info(f"  trend-rejected {row['stock_code']} {row['name']}: {row['fail_reason']}")
        target_codes = passed["stock_code"].astype(str).tolist()
        if not target_codes:
            logger.error("No symbols passed the filters; aborting.")
            sys.exit(1)
    else:
        target_codes = surviving["stock_code"].astype(str).tolist()
        if not target_codes:
            logger.error("No symbols passed the Pairlist Filters; aborting.")
            sys.exit(1)

    logger.info("Regime routing:")
    for _, row in filter_df[filter_df["stock_code"].astype(str).isin(target_codes)].iterrows():
        logger.info(f"  {row['stock_code']} {row['name']}: {row['regime']}")
    logger.info(f"Filter report saved to {FILTER_CSV}")
    codes_csv = ",".join(target_codes)

    # ---- Steps 3-7: per-symbol optimization loop ----
    # Each symbol runs the full optimize -> verify -> aggregate -> write-config chain
    # before moving to the next symbol, so a per-symbol rerun never waits for the
    # whole pool and stage CSVs are merge-written per symbol. Strategy routing
    # (regime -> strategy) happens inside param_optimize.py via the filter report.
    OPT_STAGES = [
        ("Grid parameter optimization", "param_optimize.py"),
        ("Out-of-sample validation, filter overfitted parameters", "out_sample_verify.py"),
        ("Rolling window stability validation", "rolling_window_verify.py"),
        ("Aggregate optimal parameters", "aggregate_best_param.py"),
        ("Write optimal parameters to config.yaml", "write_param_to_config.py"),
    ]
    for index, code in enumerate(target_codes, start=1):
        for stage_name, script in OPT_STAGES:
            run_step(
                f"[Symbol {index}/{len(target_codes)}: {code}] {stage_name}",
                OPT_DIR,
                [sys.executable, script, "--stock-list", code],
            )

    # ---- Steps 8-9: final batched backtest & manual trade review ----
    # No --strategy: main.py auto-routes each symbol via the filter report.
    run_step(
        "[Final] Batch backtest with regime routing and updated parameters",
        PROJECT_ROOT,
        [sys.executable, "main.py", "--stock-list", codes_csv],
    )
    run_step(
        "[Final] Manual trade review",
        PROJECT_ROOT,
        [sys.executable, "manual_trade_review.py", "--stock-list", codes_csv],
    )

    logger.info("\nAll pipeline tasks completed!")
    logger.info("Output file locations:")
    logger.info("  - Stock filter report: output/stock_filter.csv")
    logger.info("  - Parameter optimization: output/param_optimize_result.csv")
    logger.info("  - Out-of-sample validation: output/out_sample_verify_result.csv")
    logger.info("  - Rolling validation: output/rolling_verify.csv")
    logger.info("  - Optimal parameters summary: output/aggregate_common_param.csv")
    logger.info("  - Equity curves: output/equity_curve/")
    logger.info("  - Plots: output/plots/")
    logger.info("  - Backtest metrics summary: output/metrics_summary.csv")
    logger.info("  - Manual trade review report: output/manual_review_result.csv")
    logger.info(f"  - Run log: {LOG_FILE}")


if __name__ == "__main__":
    main()
