#!/usr/bin/env python3
"""SkyQuant unified command-line interface.

Usage:
    python3 skyquant dashboard [--port 8765] [--no-browser]
    python3 skyquant backtest [--stock-list 000725,601633] [--strategy trend] [--force-refresh]
    python3 skyquant opt [--stock-list 000725,601633] [--all-stocks] [--maxcpu 0]
                         [--mode grid|tpe] [--epochs N] [--seed N] [--no-apply]
    python3 skyquant fetch [--stock-list 000725,601633] [--force-refresh]
    python3 skyquant filter [--stock-list 000725,601633]
    python3 skyquant live [--stock-list 000725,601633] [--force-refresh]
    python3 skyquant all [--stock-list 000725,601633] [--all-stocks] [--skip-data] [--screen]
    python3 skyquant params list | show <file> | apply <file> [--codes ...] [--strategies ...]

Command independence:
    fetch   — data only: incremental update by default, --force-refresh for full
              re-download. Exits after all symbols are updated/skipped.
    opt     — full optimization pipeline on cached data: grid -> out-of-sample
              -> rolling -> aggregate -> export/apply param set. Exits when best
              params are archived to params/experiments and merged into
              params/active.yaml. No backtest or live review.
    backtest— strategy backtest only: reads cached data (or --force-refresh),
              writes equity curves + metrics_summary.csv, exits.
    filter  — stock pool pre-filter only: writes stock_filter.csv + regime to
              config.yaml, exits.
    live    — live trade review + next-day signals only, exits.
    params  — strategy param set management (active / drafts / experiments).
    all     — full pipeline: fetch -> filter -> opt(5 stages) -> backtest -> live.

Config layout (freqtrade-style):
    config.yaml holds fixed settings only; per-stock strategy params live in
    params/active.yaml (active set), params/drafts/ (manual groups) and
    params/experiments/ (opt-archived groups). backtest/live accept --params to
    temporarily run with any set without changing the active one.
"""

import argparse
import logging
import os
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
    if args.params:
        cmd += ["--params", args.params]
    if args.force_refresh:
        cmd += ["--force_refresh"]
    run_step("[Backtest] Run strategy backtest", PROJECT_ROOT, cmd)


def cmd_opt(args):
    """Full optimization pipeline on cached data: search (grid|tpe) -> out-of-sample ->
    rolling -> aggregate -> export/apply param set.

    Reads cached market data (run `fetch` first if data is stale), searches the
    strategy PARAM_GRID (exhaustively in grid mode, or TPE-sampled with
    --mode tpe / config opt_pipeline.opt_mode), validates via out-of-sample and
    rolling windows, aggregates the most stable optimal params, archives them as
    a timestamped param set under params/experiments and (unless --no-apply)
    merges the qualified entries into params/active.yaml. Exits when persisted —
    no backtest or live review.
    """
    opt_dir = PROJECT_ROOT / "opt_pipeline"
    stages = [
        ("Parameter optimization", "param_optimize.py"),
        ("Out-of-sample validation", "out_sample_verify.py"),
        ("Rolling window stability validation", "rolling_window_verify.py"),
        ("Aggregate optimal parameters", "aggregate_best_param.py"),
        ("Export/apply optimal param set", "export_param_set.py"),
    ]
    for stage_name, script in stages:
        cmd = [sys.executable, script]
        if args.stock_list:
            cmd += ["--stock-list", args.stock_list]
        elif args.all_stocks:
            cmd += ["--all-stocks"]
        if script == "param_optimize.py":
            if args.mode:
                cmd += ["--mode", args.mode]
            if args.epochs:
                cmd += ["--epochs", str(args.epochs)]
            if args.seed is not None:
                cmd += ["--seed", str(args.seed)]
        if script == "export_param_set.py" and args.no_apply:
            cmd += ["--no-apply"]
        if args.maxcpu and args.maxcpu != 0:
            cmd += ["--maxcpu", str(args.maxcpu)]
        run_step(f"[Opt] {stage_name}", opt_dir, cmd)


def cmd_fetch(args):
    """Fetch/update market data only (no backtest).

    Default = incremental update: for each symbol, if the cached CSV's latest
    date is already today (or a future trading day), skip the API call; else
    pull the delta from the cached latest date to config end_date and append.
    --force-refresh = full re-download from config start_date to end_date.

    Fetches both per-stock K-line + daily_basic (turnover/market-cap) and the
    sector index bars referenced by each stock's sector_index field.
    """
    import yaml
    from dataprovider import DataProvider

    with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    pool = cfg["stock_list"]
    if args.stock_list:
        wanted = {c.strip() for c in args.stock_list.split(",") if c.strip()}
        pool = [s for s in pool if s["code"] in wanted]
    if not pool:
        logger.error("No symbols to fetch; check --stock-list / config.yaml")
        sys.exit(1)

    dp = DataProvider()
    # config end_date may be stale (hardcoded to a past date); override to today
    # so incremental fetch actually pulls new bars. Tushare returns whatever is
    # available up to end_date — non-trading days simply yield no new rows.
    import datetime as _dt
    today_str = _dt.date.today().strftime("%Y%m%d")
    if dp.end_date < today_str:
        logger.info(f"[Fetch] extending end_date {dp.end_date} -> {today_str} (today)")
        dp.end_date = today_str
    fr = args.force_refresh
    mode = "force-refresh" if fr else "incremental"
    logger.info(f"[Fetch] mode={mode}, symbols={len(pool)}")

    fetched_indices = set()
    n_skipped = 0
    n_updated = 0
    n_failed = 0
    for i, s in enumerate(pool, start=1):
        code, name = s["code"], s["name"]
        cache_file = os.path.join(dp.cache_root, f"{code}.csv")
        # Incremental mode: check if cache is already up to date before API call
        if not fr and os.path.exists(cache_file):
            try:
                import pandas as pd
                df_local = pd.read_csv(cache_file, parse_dates=["datetime"])
                local_latest = df_local["datetime"].max().date()
                import datetime as _dt
                if local_latest >= _dt.date.today():
                    logger.info(f"  [{i}/{len(pool)}] {code} {name}: cache up to date ({local_latest}), skip")
                    n_skipped += 1
                    continue
            except Exception:
                pass  # fall through to actual fetch

        logger.info(f"  [{i}/{len(pool)}] {code} {name}: fetching ({mode})...")
        try:
            df = dp.fetch_stock(code, force_refresh=fr)
            if df is not None and not df.empty:
                n_updated += 1
                logger.info(f"    -> {len(df)} rows, latest={df['datetime'].max().date()}")
            else:
                n_failed += 1
                logger.warning(f"    -> empty data returned")
        except Exception as e:
            n_failed += 1
            logger.error(f"    -> fetch failed: {e}")

        # Also fetch the sector index for this stock (deduplicated)
        sector_index = s.get("sector_index")
        if sector_index and sector_index not in fetched_indices:
            try:
                idx_df = dp.fetch_index(sector_index, force_refresh=fr)
                if idx_df is not None:
                    fetched_indices.add(sector_index)
                    logger.info(f"    sector index {sector_index} ({s.get('sector_index_name', '')}): "
                                f"{len(idx_df)} rows")
            except Exception as e:
                logger.warning(f"    sector index {sector_index} fetch failed: {e}")

    logger.info(f"[Fetch] done: {n_updated} updated, {n_skipped} up-to-date, {n_failed} failed")


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
    if args.params:
        cmd += ["--params", args.params]
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

    # Step 1: fetch data (force-refresh to ensure full range for optimization)
    if not args.skip_data:
        fetch_args = argparse.Namespace(stock_list=codes_csv, force_refresh=True)
        cmd_fetch(fetch_args)
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
        ("Parameter optimization", "param_optimize.py"),
        ("Out-of-sample validation", "out_sample_verify.py"),
        ("Rolling window stability validation", "rolling_window_verify.py"),
        ("Aggregate optimal parameters", "aggregate_best_param.py"),
        ("Export/apply optimal param set", "export_param_set.py"),
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


def cmd_params(args):
    """Strategy param set management: list / show / apply."""
    import yaml

    import config_store as cs

    if args.params_action == "list":
        sets = cs.list_param_sets()
        groups = [("active", "生效参数集（回测/实盘默认）"),
                  ("drafts", "手工草稿（另存未生效）"),
                  ("experiments", "opt 自动归档")]
        for g, title in groups:
            print(f"\n== {title} ==")
            if not sets[g]:
                print("  (空)")
            for s in sets[g]:
                m = s["meta"] or {}
                extra = f"  objective={m['objective']}" if m.get("objective") else ""
                note = f"  {m['note']}" if m.get("note") else ""
                print(f"  {s['file']}  [{s['n_codes']}标的/{s['n_entries']}策略组]"
                      f"  src={m.get('source', '-')}  {m.get('created_at', '')}{extra}{note}")
        print()
        return

    if args.params_action == "show":
        if not args.file:
            logger.error("show 需要指定参数集文件，如 params/experiments/xxx.yaml")
            sys.exit(2)
        ps = cs.load_param_set(args.file)
        print(yaml.safe_dump(ps, allow_unicode=True, sort_keys=False), end="")
        return

    # apply
    if not args.file:
        logger.error("apply 需要指定参数集文件，如 params/experiments/xxx.yaml")
        sys.exit(2)
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
    strategies = [s.strip() for s in args.strategies.split(",")] if args.strategies else None
    res = cs.apply_param_set(args.file, codes=codes, strategies=strategies, note=args.note)
    if not res["applied"]:
        logger.warning("没有匹配的 code/strategy，未改动 active.yaml")
    else:
        logger.info(f"已生效 {len(res['applied'])} 个参数单元 -> {res['path']}")
        for code, sid in res["applied"]:
            logger.info(f"  {code} / {sid}")


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
    p.add_argument("--params", type=str, default=None,
                   help="Param set YAML to use temporarily instead of params/active.yaml")
    p.set_defaults(func=cmd_backtest)

    # opt
    p = subparsers.add_parser("opt", help="Run parameter optimization pipeline")
    p.add_argument("--stock-list", type=str, default=None)
    p.add_argument("--all-stocks", action="store_true")
    p.add_argument("--maxcpu", type=int, default=0)
    p.add_argument("--mode", type=str, default=None, choices=["grid", "tpe"],
                   help="Search mode: grid=exhaustive enumeration (default from config opt_pipeline.opt_mode), tpe=hyperopt TPE bayesian sampling")
    p.add_argument("--epochs", type=int, default=None,
                   help="TPE total evaluation count (overrides config opt_pipeline.hyperopt_epochs)")
    p.add_argument("--seed", type=int, default=None,
                   help="TPE random seed (overrides config opt_pipeline.hyperopt_seed)")
    p.add_argument("--no-apply", action="store_true",
                   help="Only archive the optimal set to params/experiments without merging into active.yaml")
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
    p.add_argument("--params", type=str, default=None,
                   help="Param set YAML to use temporarily instead of params/active.yaml")
    p.set_defaults(func=cmd_live)

    # all
    p = subparsers.add_parser("all", help="Run full pipeline (fetch->filter->opt->backtest->live)")
    p.add_argument("--skip-data", action="store_true")
    p.add_argument("--stock-list", type=str, default=None)
    p.add_argument("--all-stocks", action="store_true")
    p.add_argument("--screen", action="store_true",
                   help="Additionally apply trendability gate")
    p.set_defaults(func=cmd_all)

    # params — strategy param set management
    p = subparsers.add_parser("params", help="Manage strategy param sets (list/show/apply)")
    p.add_argument("params_action", choices=["list", "show", "apply"],
                   help="list all sets | show one set | merge one set into params/active.yaml")
    p.add_argument("file", nargs="?", default=None, help="Param set YAML (for show/apply)")
    p.add_argument("--codes", type=str, default=None, help="Comma-separated codes to apply")
    p.add_argument("--strategies", type=str, default=None, help="Comma-separated strategy ids to apply")
    p.add_argument("--note", type=str, default=None, help="Note recorded into active.yaml meta on apply")
    p.set_defaults(func=cmd_params)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
