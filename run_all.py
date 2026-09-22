import argparse
import logging
import subprocess
import sys
from pathlib import Path

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
        help="Comma-separated stock codes to run (e.g. 000725,600519). Defaults to all stocks in config.yaml.",
    )
    args = parser.parse_args()

    stock_list_arg = []
    if args.stock_list:
        stock_list_arg = [c.strip() for c in args.stock_list.split(",") if c.strip()]

    logger.info("==== SkyQuant full pipeline started ====")
    logger.info(f"Project root: {BASE_DIR}")
    logger.info(f"Log file: {LOG_FILE}")
    if stock_list_arg:
        logger.info(f"Stock list (overridden): {stock_list_arg}")

    required_dirs = [
        BASE_DIR / "cache",
        BASE_DIR / "output",
        OPT_DIR,
        BASE_DIR / "output/plots",
    ]
    for d in required_dirs:
        d.mkdir(exist_ok=True)

    stock_flag = ["--stock-list", args.stock_list] if stock_list_arg else []

    STEPS = []
    if not args.skip_data:
        STEPS.append(
            (
                "[1/8] Fetch full market data",
                BASE_DIR,
                [sys.executable, "main.py", "--force_refresh", *stock_flag],
            )
        )
    else:
        logger.info("👉 --skip-data enabled, skipping market data fetch, using local cache")

    STEPS += [
        (
            "[2/8] Grid parameter optimization",
            OPT_DIR,
            [sys.executable, "param_optimize.py", *stock_flag],
        ),
        (
            "[3/8] Out-of-sample validation, filter overfitted parameters",
            OPT_DIR,
            [sys.executable, "out_sample_verify.py"],
        ),
        (
            "[4/8] Rolling window stability validation",
            OPT_DIR,
            [sys.executable, "rolling_window_verify.py"],
        ),
        (
            "[5/8] Aggregate optimal parameters",
            OPT_DIR,
            [sys.executable, "aggregate_best_param.py"],
        ),
        (
            "[6/8] Write optimal parameters to config.yaml",
            OPT_DIR,
            [sys.executable, "write_param_to_config.py"],
        ),
        (
            "[7/8] Batch backtest with updated parameters",
            BASE_DIR,
            [sys.executable, "main.py", *stock_flag],
        ),
        (
            "[8/8] Manual trade review",
            BASE_DIR,
            [sys.executable, "manual_trade_review.py", *stock_flag],
        ),
    ]

    for name, cwd, cmd in STEPS:
        run_step(name, cwd, cmd)

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
