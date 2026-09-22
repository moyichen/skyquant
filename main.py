"""SkyQuant backtest main engine.

Loads config, iterates the stock pool, runs the selected strategy per stock,
and writes equity curves, plots and the metrics summary under output/.
"""

import argparse
import logging
import sys
from pathlib import Path

import backtrader as bt
import pandas as pd

from comm import AStockCommission
from data_source import AStockData, DataSource
from metrics_utils import calc_metrics
from plot_utils import plot_all
from strategy import STRATEGY_MAPPING

# ========== Paths (anchored to project root, independent of CWD) ==========
BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "output"
EQUITY_OUT = OUTPUT_DIR / "equity_curve"
PLOT_OUT = OUTPUT_DIR / "plots"
METRICS_SUMMARY = OUTPUT_DIR / "metrics_summary.csv"
TRADE_CSV = BASE_DIR / "manual_trades.csv"

DEFAULT_STRATEGY = "maatr_base"

logger = logging.getLogger(__name__)


def setup_logging():
    """Console logging only; run_all.py captures this output into run.log."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )


def prepare_output_dirs():
    """Create output directories (cache dir is handled by DataSource)."""
    for d in (OUTPUT_DIR, EQUITY_OUT, PLOT_OUT):
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


def run_backtest(dataSource, comminfo, global_setting, param_pool, code, strategy_id, force_refresh):
    """Run one backtest for a single stock/strategy; return metrics dict or None."""
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

    cerebro.broker.setcash(global_setting["initial_capital"])
    cerebro.broker.addcommissioninfo(comminfo)
    strategy_instance = cerebro.run()[0]

    equity_df = strategy_instance.get_equity_dataframe()
    trades_df = strategy_instance.get_trade_dataframe()

    equity_path = EQUITY_OUT / f"{code}_{strategy_id}_equity.csv"
    equity_df.to_csv(equity_path, index=False, encoding="utf-8")

    metrics = calc_metrics(equity_df, trades_df)
    metrics["stock_code"] = code
    metrics["strategy"] = strategy_id

    plot_all(equity_df, trades_df, metrics, PLOT_OUT, code, strategy_id)

    logger.info(f"{code} {strategy_id} final portfolio value: {cerebro.broker.getvalue():.2f}")
    logger.info(f"Metrics: {metrics}")
    return metrics


def main():
    parser = argparse.ArgumentParser(description="SkyQuant backtest main engine")
    parser.add_argument(
        "--force_refresh",
        action="store_true",
        help="Force full re-download of market data, overwriting cache",
    )
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY, help="Strategy id to backtest")
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
    global_setting = cfg["global_setting"]
    stock_list = cfg["stock_list"]
    if args.stock_list:
        wanted = {c.strip() for c in args.stock_list.split(",") if c.strip()}
        stock_list = [s for s in stock_list if s["code"] in wanted]
    valid_codes = [item["code"] for item in stock_list]
    # Normalize keys to str (unquoted numeric codes in yaml are parsed as int)
    param_pool = {str(code): p for code, p in cfg["strategy_params"].items()}

    comm_cfg = cfg["commission_config"]
    comminfo = AStockCommission(
        commission=comm_cfg["commission"],
        stamp_duty=comm_cfg["stamp_duty"],
        transfer_fee=comm_cfg["transfer_fee"],
    )

    # Sanity-check manual trade records (non-fatal warning only)
    try:
        validate_manual_trades(valid_codes)
    except Exception as e:
        logger.warning(f"Manual trade validation warning: {e}")

    metric_rows = []
    for stock_info in stock_list:
        code, name = stock_info["code"], stock_info["name"]
        logger.info(f"==== Backtesting {name}({code}) ====")
        metrics = run_backtest(
            ds,
            comminfo,
            global_setting,
            param_pool,
            code,
            args.strategy,
            args.force_refresh,
        )
        if metrics is not None:
            metric_rows.append(metrics)

    if metric_rows:
        pd.DataFrame(metric_rows).to_csv(METRICS_SUMMARY, index=False, encoding="utf-8")
        logger.info(f"Metrics summary saved to {METRICS_SUMMARY}")


if __name__ == "__main__":
    main()
