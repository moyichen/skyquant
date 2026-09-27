"""SkyQuant backtest main engine.

Loads config, iterates the stock pool, runs the selected strategy per stock,
and writes equity curves, plots and the metrics summary under output/.
"""

import argparse
import logging

import backtrader as bt
import pandas as pd

from comm import (
    EQUITY_DIR,
    OUTPUT_DIR,
    PLOT_DIR,
    PROJECT_ROOT,
    apply_broker_settings,
    build_commission,
    setup_logging,
)
from data_source import AStockData, DataSource
from report import calc_metrics, render_interactive_chart, render_report
from strategy import STRATEGY_DESCRIPTIONS, STRATEGY_MAPPING

# ========== Paths (anchored to project root, independent of CWD) ==========
METRICS_SUMMARY = OUTPUT_DIR / "metrics_summary.csv"
TRADE_CSV = PROJECT_ROOT / "manual_trades.csv"

DEFAULT_STRATEGY = "trend"

# Analyzers registered on every Cerebro; results surface in the HTML report.
ANALYZER_NAMES = ("returns", "sharpe", "drawdown", "tradeanalyzer", "sqn")

logger = logging.getLogger(__name__)


def prepare_output_dirs():
    """Create output directories (cache dir is handled by DataSource)."""
    for d in (OUTPUT_DIR, EQUITY_DIR, PLOT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def validate_manual_trades(valid_codes):
    """Check stock codes in manual_trades.csv against the configured stock pool."""
    df_trades = pd.read_csv(TRADE_CSV, dtype={"stock_code": str}, parse_dates=["trade_date"])
    invalid = [c for c in df_trades["stock_code"].unique() if c not in valid_codes]
    if invalid:
        raise ValueError(f"manual_trades.csv contains codes missing from stock pool: {invalid}")


def get_strategy_param(param_pool, code, strategy_id):
    strategy_cls = STRATEGY_MAPPING[strategy_id]
    params = param_pool.get(code, {}).get(strategy_id, {})
    return strategy_cls, params


def run_backtest(dataSource, comminfo, cfg, param_pool, code, strategy_id, force_refresh, stock_name=None):
    """Run one backtest for a single stock/strategy; return metrics dict or None."""
    global_setting = cfg["global_setting"]
    df_data = dataSource.fetch_stock(code, force_refresh)
    if df_data is None:
        logger.warning(f"Failed to fetch market data for {code}, skipping")
        return None

    cerebro = bt.Cerebro()
    strategy_cls, param_dict = get_strategy_param(param_pool, code, strategy_id)
    cerebro.addstrategy(strategy_cls, **param_dict)

    # A-share extended feed: standard OHLCV + preclose/amount/turn/pctChg columns
    data_feed = AStockData(
        dataname=df_data,
        datetime="datetime",
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        timeframe=bt.TimeFrame.Days,
    )
    cerebro.adddata(data_feed)

    # Unified broker wiring: cash + A-share commission + optional slippage
    apply_broker_settings(cerebro.broker, cfg, float(global_setting["initial_capital"]), comminfo)

    # Analyzers feed the HTML report (returns / sharpe / drawdown / trades / sqn)
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="tradeanalyzer")
    cerebro.addanalyzer(bt.analyzers.SQN, _name="sqn")

    strategy_instance = cerebro.run()[0]

    equity_df = strategy_instance.get_equity_dataframe()
    trades_df = strategy_instance.get_trade_dataframe()
    action_df = strategy_instance.get_action_dataframe()

    equity_path = EQUITY_DIR / f"{code}_{strategy_id}_equity.csv"
    equity_df.to_csv(equity_path, index=False, encoding="utf-8")

    metrics = calc_metrics(equity_df, trades_df)
    metrics["stock_code"] = code
    metrics["strategy"] = strategy_id

    # Extract analyzer results for the report; tolerate missing ones.
    analyzer_results = {}
    for name in ANALYZER_NAMES:
        analyzer_obj = getattr(strategy_instance.analyzers, name, None)
        if analyzer_obj is None:
            continue
        try:
            analyzer_results[name] = analyzer_obj.get_analysis()
        except Exception as e:
            logger.warning(f"Analyzer {name} for {code} failed: {e}")

    # Render btplotting K-line chart (best-effort; optional dependency)
    interactive_html = None
    try:
        interactive_html = render_interactive_chart(strategy_instance, PLOT_DIR, code, strategy_id)
        logger.info(f"Interactive chart saved to {interactive_html}")
    except Exception as e:
        logger.warning(f"Interactive chart unavailable for {code}: {e}")

    # Render self-contained HTML report (replaces the old PNG plot_all)
    final_value = cerebro.broker.getvalue()
    report_path = render_report(
        equity_df=equity_df,
        trades_df=trades_df,
        action_df=action_df,
        metrics=metrics,
        analyzer_results=analyzer_results,
        out_dir=PLOT_DIR,
        code=code,
        strategy_name=strategy_id,
        start_date=str(global_setting.get("start_date", "")),
        end_date=str(global_setting.get("end_date", "")),
        initial_capital=float(global_setting.get("initial_capital", 0)),
        final_value=float(final_value),
        interactive_html=interactive_html,
        stock_name=stock_name,
    )
    logger.info(f"HTML report saved to {report_path}")

    logger.info(f"{code} {strategy_id} final portfolio value: {final_value:.2f}")
    logger.info(f"Metrics: {metrics}")
    return metrics


def main():
    strategy_lines = "\n".join(f"  - {sid}: {desc}" for sid, desc in STRATEGY_DESCRIPTIONS.items())
    parser = argparse.ArgumentParser(
        description="SkyQuant backtest main engine",
        epilog=f"Supported strategies:\n{strategy_lines}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--force_refresh",
        action="store_true",
        help="Force full re-download of market data, overwriting cache",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        choices=list(STRATEGY_MAPPING.keys()),
        help="Strategy id to backtest for every stock. Default: auto-route per stock "
        "(regime label from output/stock_filter.csv -> matching strategy).",
    )
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to run. Defaults to all stocks in config.yaml.",
    )
    args = parser.parse_args()

    setup_logging()
    prepare_output_dirs()

    # Load config and build runtime dependencies
    ds = DataSource()
    cfg = ds.cfg
    stock_list = cfg["stock_list"]
    if args.stock_list:
        wanted = {c.strip() for c in args.stock_list.split(",") if c.strip()}
        stock_list = [s for s in stock_list if s["code"] in wanted]
    # Config blacklist overrides the whitelist stock_list
    blacklist = {str(c) for c in (cfg.get("stock_blacklist") or [])}
    if blacklist:
        stock_list = [s for s in stock_list if str(s["code"]) not in blacklist]
    # Normalize keys to str (unquoted numeric codes in yaml are parsed as int)
    param_pool = {str(code): p for code, p in cfg["strategy_params"].items()}

    comminfo = build_commission(cfg)

    # Per-stock regime -> strategy routing table (written by stock_filter.py)
    from stock_filter import load_regime_map, strategy_for_code

    regime_map = load_regime_map()

    # Sanity-check manual trade records against the full configured pool rather
    # than the --stock-list filtered runtime set (non-fatal warning only)
    pool_codes = [item["code"] for item in cfg["stock_list"]]
    try:
        validate_manual_trades(pool_codes)
    except Exception as e:
        logger.warning(f"Manual trade validation warning: {e}")

    metric_rows = []
    for stock_info in stock_list:
        code, name = stock_info["code"], stock_info["name"]
        strategy_id = args.strategy or strategy_for_code(code, regime_map, param_pool, DEFAULT_STRATEGY)
        logger.info(f"==== Backtesting {name}({code}) strategy={strategy_id} ====")
        metrics = run_backtest(
            ds,
            comminfo,
            cfg,
            param_pool,
            code,
            strategy_id,
            args.force_refresh,
            stock_name=name,
        )
        if metrics is not None:
            metric_rows.append(metrics)

    if metric_rows:
        pd.DataFrame(metric_rows).to_csv(METRICS_SUMMARY, index=False, encoding="utf-8")
        logger.info(f"Metrics summary saved to {METRICS_SUMMARY}")


if __name__ == "__main__":
    main()
