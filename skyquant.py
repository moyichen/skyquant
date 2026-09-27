#!/usr/bin/env python3
"""SkyQuant unified command-line interface.

Usage:
    python3 skyquant dashboard [--port 8765] [--no-browser]
    python3 skyquant backtest [--stock-list 000725,601633] [--strategy trend] [--force-refresh]
    python3 skyquant opt [--stock-list 000725,601633] [--all-stocks] [--maxcpu 0]
    python3 skyquant fetch [--stock-list 000725,601633] [--force-refresh]
    python3 skyquant filter [--stock-list 000725,601633]
    python3 skyquant live [--stock-list 000725,601633] [--force-refresh]
    python3 skyquant all [--stock-list 000725,601633] [--all-stocks] [--skip-data] [--screen]
"""

import argparse
import logging
import subprocess
import sys

from comm import LOG_FILE, PROJECT_ROOT, setup_logging

setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)


def run_step(name, cwd, cmd):
    """Run a subprocess step; abort on failure."""
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


def cmd_dashboard(args):
    """Start the local web dashboard."""
    from dashboard import main as dashboard_main
    sys.argv = ["dashboard.py"]
    if args.port != 8765:
        sys.argv += ["--port", str(args.port)]
    if args.no_browser:
        sys.argv += ["--no-browser"]
    dashboard_main()


def cmd_backtest(args):
    """Run backtest for stock list with regime routing."""
    cmd = [sys.executable, "main.py"]
    if args.stock_list:
        cmd += ["--stock-list", args.stock_list]
    if args.strategy:
        cmd += ["--strategy", args.strategy]
    if args.force_refresh:
        cmd += ["--force_refresh"]
    run_step("[Backtest] Run strategy backtest", PROJECT_ROOT, cmd)


def cmd_opt(args):
    """Run full optimization pipeline: grid -> out-of-sample -> rolling -> aggregate -> write."""
    opt_dir = PROJECT_ROOT / "opt_pipeline"
    stages = [
        ("Grid parameter optimization", "param_optimize.py"),
        ("Out-of-sample validation", "out_sample_verify.py"),
        ("Rolling window stability validation", "rolling_window_verify.py"),
        ("Aggregate optimal parameters", "aggregate_best_param.py"),
        ("Write optimal parameters to config.yaml", "write_param_to_config.py"),
    ]
    for stage_name, script in stages:
        cmd = [sys.executable, script]
        if args.stock_list:
            cmd += ["--stock-list", args.stock_list]
        elif args.all_stocks:
            cmd += ["--all-stocks"]
        if args.maxcpu and args.maxcpu != 0:
            cmd += ["--maxcpu", str(args.maxcpu)]
        run_step(f"[Opt] {stage_name}", opt_dir, cmd)


def cmd_fetch(args):
    """Fetch/update market data for stock list."""
    cmd = [sys.executable, "main.py", "--force_refresh"]
    if args.stock_list:
        cmd += ["--stock-list", args.stock_list]
    # main.py --force_refresh fetches data and runs backtest; for pure fetch we
    # still run main.py because DataProvider is embedded in the backtest flow.
    # A lightweight alternative is to call DataProvider directly.
    logger.info("Fetching market data via main.py --force_refresh ...")
    run_step("[Fetch] Update market data", PROJECT_ROOT, cmd)


def cmd_filter(args):
    """Run stock pool pre-filter (Pairlist Filters + regime routing)."""
    cmd = [sys.executable, "stock_filter.py"]
    if args.stock_list:
        cmd += ["--stock-list", args.stock_list]
    run_step("[Filter] Stock pool pre-filter", PROJECT_ROOT, cmd)


def cmd_live(args):
    """Run live trading review and next-day signals."""
    cmd = [sys.executable, "live_trading.py"]
    if args.stock_list:
        cmd += ["--stock-list", args.stock_list]
    if args.force_refresh:
        cmd += ["--force-refresh"]
    run_step("[Live] Trade review and signals", PROJECT_ROOT, cmd)


def cmd_all(args):
    """Full pipeline: fetch -> filter -> opt -> backtest -> live."""
    import yaml
    from opt_pipeline.common import resolve_target_codes
    from dataprovider import DataProvider
    from stock_filter import FILTER_CSV, filter_stock_pool, write_regime_to_config

    with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    target_codes = resolve_target_codes(args, cfg)
    codes_csv = ",".join(target_codes)

    logger.info("==== SkyQuant full pipeline started ====")
    logger.info(f"Project root: {PROJECT_ROOT}")
    logger.info(f"Target symbols ({len(target_codes)}): {codes_csv}")

    # Step 1: fetch data
    if not args.skip_data:
        run_step(
            "[Data] Fetch full market data",
            PROJECT_ROOT,
            [sys.executable, "main.py", "--force_refresh", "--stock-list", codes_csv],
        )
    else:
        logger.info("--skip-data enabled, skipping market data fetch")

    # Step 2: stock pool pre-filter
    filter_df = filter_stock_pool(DataProvider(), target_codes, cfg.get("stock_filter", {}))
    filter_df.to_csv(FILTER_CSV, index=False)
    write_regime_to_config(filter_df)

    pairlist_failed = filter_df[~filter_df["pairlist_passed"]]
    if len(pairlist_failed) > 0:
        logger.info(f"Pairlist Filters rejected {len(pairlist_failed)} symbols:")
        for _, row in pairlist_failed.iterrows():
            logger.info(f"  {row['stock_code']} {row['name']}: {row['pairlist_fail_reason']}")
    surviving = filter_df[filter_df["pairlist_passed"]]

    if args.screen:
        passed = surviving[surviving["passed"]]
        trend_failed = surviving[~surviving["passed"]]
        logger.info(f"Trendability screening: {len(passed)}/{len(target_codes)} passed")
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

    # Steps 3-7: per-symbol optimization loop
    OPT_STAGES = [
        ("Grid parameter optimization", "param_optimize.py"),
        ("Out-of-sample validation", "out_sample_verify.py"),
        ("Rolling window stability validation", "rolling_window_verify.py"),
        ("Aggregate optimal parameters", "aggregate_best_param.py"),
        ("Write optimal parameters to config.yaml", "write_param_to_config.py"),
    ]
    opt_dir = PROJECT_ROOT / "opt_pipeline"
    for index, code in enumerate(target_codes, start=1):
        for stage_name, script in OPT_STAGES:
            run_step(
                f"[Symbol {index}/{len(target_codes)}: {code}] {stage_name}",
                opt_dir,
                [sys.executable, script, "--stock-list", code],
            )

    # Steps 8-9: final backtest & live review
    run_step(
        "[Final] Batch backtest with regime routing",
        PROJECT_ROOT,
        [sys.executable, "main.py", "--stock-list", codes_csv],
    )
    run_step(
        "[Final] Live trade review and signals",
        PROJECT_ROOT,
        [sys.executable, "live_trading.py", "--stock-list", codes_csv],
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
    logger.info("  - Live trade review report: output/live_trade_review.csv")
    logger.info("  - Live portfolio HTML report: output/plots/live_portfolio_report.html")
    logger.info(f"  - Run log: {LOG_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="SkyQuant unified CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # dashboard
    p = subparsers.add_parser("dashboard", help="Start local web dashboard")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_dashboard)

    # backtest
    p = subparsers.add_parser("backtest", help="Run strategy backtest")
    p.add_argument("--stock-list", type=str, default=None)
    p.add_argument("--strategy", type=str, default=None,
                   choices=["trend", "range", "breakout"],
                   help="Force strategy for all stocks (default: auto-route by regime)")
    p.add_argument("--force-refresh", action="store_true", help="Force re-download market data")
    p.set_defaults(func=cmd_backtest)

    # opt
    p = subparsers.add_parser("opt", help="Run parameter optimization pipeline")
    p.add_argument("--stock-list", type=str, default=None)
    p.add_argument("--all-stocks", action="store_true")
    p.add_argument("--maxcpu", type=int, default=0)
    p.set_defaults(func=cmd_opt)

    # fetch
    p = subparsers.add_parser("fetch", help="Fetch/update market data")
    p.add_argument("--stock-list", type=str, default=None)
    p.add_argument("--force-refresh", action="store_true", help="Force full re-download")
    p.set_defaults(func=cmd_fetch)

    # filter
    p = subparsers.add_parser("filter", help="Run stock pool pre-filter")
    p.add_argument("--stock-list", type=str, default=None)
    p.set_defaults(func=cmd_filter)

    # live
    p = subparsers.add_parser("live", help="Live trading review and signals")
    p.add_argument("--stock-list", type=str, default=None)
    p.add_argument("--force-refresh", action="store_true")
    p.set_defaults(func=cmd_live)

    # all
    p = subparsers.add_parser("all", help="Run full pipeline (fetch->filter->opt->backtest->live)")
    p.add_argument("--skip-data", action="store_true")
    p.add_argument("--stock-list", type=str, default=None)
    p.add_argument("--all-stocks", action="store_true")
    p.add_argument("--screen", action="store_true",
                   help="Additionally apply trendability gate")
    p.set_defaults(func=cmd_all)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
