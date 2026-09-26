import argparse
import logging
import subprocess
import sys
from pathlib import Path

import yaml

# ========== Logging Configuration ==========
BASE_DIR = Path(__file__).parent.resolve()
LOG_FILE = BASE_DIR / "output" / "run.log"
(LOG_FILE.parent).mkdir(parents=True, exist_ok=True)

# Log handlers: console + file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8", mode="w"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

OPT_DIR = BASE_DIR / "opt_pipeline"
sys.path.insert(0, str(OPT_DIR))
from common import resolve_target_codes  # noqa: E402
from stock_screening import screen_trendable_stocks  # noqa: E402


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
        help="Pre-filter the target pool by trendability (stock_screening.py) "
        "and only run the pipeline on passing symbols. Reads local cache only.",
    )
    args = parser.parse_args()

    with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    target_codes = resolve_target_codes(args, cfg)

    # ---- Pre-filter: trendability screening ----
    # The quadruple-filter trend strategy only suits stocks with medium-long
    # term bull trends; optimizing on box-oscillating names (e.g. 000725)
    # yields fragile fits. When --screen is set, replace the target pool with
    # symbols that pass the trendability thresholds in config.yaml.
    if args.screen:
        from data_source import DataSource

        thresholds = cfg.get("stock_screening", {})
        screen_df = screen_trendable_stocks(DataSource(), target_codes, thresholds)
        screen_df.to_csv(BASE_DIR / "output" / "stock_screening.csv", index=False)
        passed = screen_df[screen_df["passed"]]["stock_code"].astype(str).tolist()
        failed = screen_df[~screen_df["passed"]][["stock_code", "fail_reason"]]
        logger.info(f"Trendability screening: {len(passed)}/{len(target_codes)} symbols passed")
        if len(failed) > 0:
            logger.info("Rejected symbols:")
            for _, row in failed.iterrows():
                logger.info(f"  {row['stock_code']}: {row['fail_reason']}")
        if not passed:
            logger.error("No symbols passed trendability screening; aborting.")
            sys.exit(1)
        target_codes = passed

    codes_csv = ",".join(target_codes)

    logger.info("==== SkyQuant full pipeline started ====")
    logger.info(f"Project root: {BASE_DIR}")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info(f"Target symbols ({len(target_codes)}): {codes_csv}")

    required_dirs = [
        BASE_DIR / "cache",
        BASE_DIR / "output",
        OPT_DIR,
        BASE_DIR / "output/plots",
    ]
    for d in required_dirs:
        d.mkdir(exist_ok=True)

    # ---- Step 1: market data fetch (batched, I/O bound) ----
    if not args.skip_data:
        run_step(
            "[Data] Fetch full market data",
            BASE_DIR,
            [sys.executable, "main.py", "--force_refresh", "--stock-list", codes_csv],
        )
    else:
        logger.info("👉 --skip-data enabled, skipping market data fetch, using local cache")

    # ---- Steps 2-6: per-symbol optimization loop ----
    # Each symbol runs the full optimize -> verify -> aggregate -> write-config chain
    # before moving to the next symbol, so a per-symbol rerun never waits for the
    # whole pool and stage CSVs are merge-written per symbol.
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

    # ---- Steps 7-8: final batched backtest & manual trade review ----
    run_step(
        "[Final] Batch backtest with updated parameters",
        BASE_DIR,
        [sys.executable, "main.py", "--stock-list", codes_csv],
    )
    run_step(
        "[Final] Manual trade review",
        BASE_DIR,
        [sys.executable, "manual_trade_review.py", "--stock-list", codes_csv],
    )

    logger.info("\n✅ All pipeline tasks completed!")
    logger.info("Output file locations:")
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
