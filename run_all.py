import sys
import argparse
import subprocess
import logging
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
        logging.StreamHandler(sys.stdout)
    ]
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
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True
        )
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Subprocess returned non-zero exit code: {proc.returncode}")
        logger.info(f"{name} completed\n")
    except Exception as e:
        logger.error(f"{name} failed! {str(e)}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="SkyQuant one-click quant pipeline")
    parser.add_argument("--skip-data", action="store_true", help="Skip market data fetching (use local cache)")
    args = parser.parse_args()

    logger.info("==== SkyQuant full pipeline started ====")
    logger.info(f"Project root: {BASE_DIR}")
    logger.info(f"Log file: {LOG_FILE}")

    required_dirs = [BASE_DIR / "cache", BASE_DIR / "output", OPT_DIR, BASE_DIR/"output/plots"]
    for d in required_dirs:
        d.mkdir(exist_ok=True)

    STEPS = []
    if not args.skip_data:
        STEPS.append((
            "[1/8] Fetch full market data",
            BASE_DIR,
            [sys.executable, "main.py", "--force_refresh"]
        ))
    else:
        logger.info("👉 --skip-data enabled, skipping market data fetch, using local cache")

    STEPS += [
        ("[2/8] Grid parameter optimization", OPT_DIR, [sys.executable, "param_optimize.py"]),
        ("[3/8] Out-of-sample validation, filter overfitted parameters", OPT_DIR, [sys.executable, "out_sample_verify.py"]),
        ("[4/8] Rolling window stability validation", OPT_DIR, [sys.executable, "rolling_window_verify.py"]),
        ("[5/8] Aggregate optimal parameters", OPT_DIR, [sys.executable, "aggregate_best_param.py"]),
        ("[6/8] Write optimal parameters to config.yaml", OPT_DIR, [sys.executable, "write_param_to_config.py"]),
        ("[7/8] Batch backtest with updated parameters", BASE_DIR, [sys.executable, "main.py"]),
        ("[8/8] Manual trade review", BASE_DIR, [sys.executable, "manual_trade_review.py"]),
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
