"""Daily signal generation: run strategies on latest data and output buy/sell/hold signals.

Run after market close: python daily_signal.py
Outputs: output/daily_signal_{YYYYMMDD}.csv + console summary.
"""

import argparse
import datetime
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

import backtrader as bt
import pandas as pd

from comm import AStockCommission
from data_source import AStockData, DataSource
from strategy import STRATEGY_MAPPING

BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "output"
TRADE_CSV = BASE_DIR / "manual_trades.csv"

logger = logging.getLogger(__name__)

CONSENSUS_PRIORITY = {"SELL": 3, "BUY": 2, "HOLD": 1, "WAIT": 0}


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )


def compute_holdings(trade_csv: Path) -> Dict[str, dict]:
    """Parse manual_trades.csv and return current holdings.

    Returns {stock_code: {"size": int, "avg_cost": float}} for stocks with net positive size.
    """
    if not trade_csv.exists():
        return {}
    df = pd.read_csv(trade_csv, dtype={"stock_code": str}, parse_dates=["trade_date"])
    if df.empty:
        return {}
    holdings: Dict[str, dict] = {}
    for _, row in df.sort_values("trade_date").iterrows():
        code = str(row["stock_code"])
        side = str(row["side"]).upper()
        size = int(row["size"])
        price = float(row["price"])
        if side == "BUY":
            if code in holdings:
                old = holdings[code]
                total_size = old["size"] + size
                old["avg_cost"] = (
                    old["avg_cost"] * old["size"] + price * size
                ) / total_size
                old["size"] = total_size
            else:
                holdings[code] = {"size": size, "avg_cost": price}
        elif side == "SELL":
            if code in holdings:
                holdings[code]["size"] -= size
                if holdings[code]["size"] <= 0:
                    del holdings[code]
    return holdings


def run_strategy_actions(
    ds: DataSource,
    comminfo: AStockCommission,
    cfg: dict,
    code: str,
    strategy_id: str,
    param: dict,
) -> Optional[pd.DataFrame]:
    """Run a single strategy on a single symbol and return the action log DataFrame."""
    df = ds.load_cached_data(code)
    if df is None or len(df) == 0:
        df = ds.fetch_stock(code, force_refresh=False)
    if df is None or len(df) == 0:
        return None
    cerebro = bt.Cerebro()
    strategy_cls = STRATEGY_MAPPING[strategy_id]
    cerebro.addstrategy(strategy_cls, **param)
    feed = AStockData(
        dataname=df,
        datetime="datetime",
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
    )
    cerebro.adddata(feed)
    cerebro.broker.setcash(cfg["global_setting"]["initial_capital"])
    cerebro.broker.addcommissioninfo(comminfo)
    strategy_instance = cerebro.run()[0]
    return strategy_instance.get_action_dataframe()


def classify_signal(action_df: pd.DataFrame, last_bar_date) -> dict:
    """Classify the strategy signal based on the action log and last bar date."""
    if action_df is None or action_df.empty:
        return {
            "last_signal_date": None,
            "last_signal_side": None,
            "last_signal_price": None,
            "strategy_in_position": False,
            "strategy_action": "WAIT",
        }
    last_row = action_df.iloc[-1]
    num_buys = (action_df["side"] == "BUY").sum()
    num_sells = (action_df["side"] == "SELL").sum()
    in_position = num_buys > num_sells
    if last_row["date"] == last_bar_date:
        action = last_row["side"]
    else:
        action = "HOLD" if in_position else "WAIT"
    return {
        "last_signal_date": last_row["date"],
        "last_signal_side": last_row["side"],
        "last_signal_price": last_row["price"],
        "strategy_in_position": in_position,
        "strategy_action": action,
    }


def compute_consensus(actions: List[str]) -> str:
    """Aggregate per-strategy actions: SELL > BUY > HOLD > WAIT."""
    if not actions:
        return "WAIT"
    return max(actions, key=lambda a: CONSENSUS_PRIORITY.get(a, 0))


def derive_suggested_action(consensus: str, currently_held: bool) -> str:
    """Combine consensus with holdings to get suggested action."""
    if currently_held:
        if consensus == "SELL":
            return "SELL"
        elif consensus == "BUY":
            return "BUY_MORE"
        else:
            return "HOLD"
    else:
        if consensus == "BUY":
            return "BUY"
        else:
            return "WAIT"


def build_report_rows(
    ds: DataSource,
    comminfo: AStockCommission,
    cfg: dict,
    param_pool: dict,
    stock_name_map: dict,
    holdings: Dict[str, dict],
    report_date: str,
) -> List[dict]:
    """Run strategies for all stocks and collect report rows."""
    rows: List[dict] = []
    for stock_info in cfg["stock_list"]:
        code = str(stock_info["code"])
        name = stock_info.get("name", code)
        if code not in param_pool:
            logger.warning(f"No strategy config for {code} ({name}), skipping")
            continue
        # Fetch latest data
        df = ds.fetch_stock(code, force_refresh=False)
        if df is None or len(df) == 0:
            logger.warning(f"No data for {code} ({name}), skipping")
            continue
        last_bar_date = (
            df.iloc[-1]["datetime"].date()
            if "datetime" in df.columns
            else df.index[-1].date()
        )
        held = code in holdings
        holding_info = holdings.get(code, {"size": 0, "avg_cost": 0.0})
        strategy_actions: List[str] = []
        for strategy_id, param in param_pool[code].items():
            try:
                action_df = run_strategy_actions(
                    ds, comminfo, cfg, code, strategy_id, param
                )
            except Exception as e:
                logger.error(f"Strategy {strategy_id} failed for {code}: {e}")
                action_df = None
            signal = classify_signal(action_df, last_bar_date)
            strategy_actions.append(signal["strategy_action"])
            rows.append(
                {
                    "report_date": report_date,
                    "stock_code": code,
                    "stock_name": name,
                    "strategy_id": strategy_id,
                    "last_bar_date": last_bar_date,
                    "last_signal_date": signal["last_signal_date"],
                    "last_signal_side": signal["last_signal_side"],
                    "last_signal_price": signal["last_signal_price"],
                    "strategy_in_position": signal["strategy_in_position"],
                    "strategy_action": signal["strategy_action"],
                    "currently_held": held,
                    "holding_size": holding_info["size"],
                    "avg_cost": round(holding_info["avg_cost"], 4),
                    "consensus": "",
                    "suggested_action": "",
                }
            )
        consensus = compute_consensus(strategy_actions)
        suggested = derive_suggested_action(consensus, held)
        rows.append(
            {
                "report_date": report_date,
                "stock_code": code,
                "stock_name": name,
                "strategy_id": "CONSENSUS",
                "last_bar_date": last_bar_date,
                "last_signal_date": None,
                "last_signal_side": None,
                "last_signal_price": None,
                "strategy_in_position": None,
                "strategy_action": None,
                "currently_held": held,
                "holding_size": holding_info["size"],
                "avg_cost": round(holding_info["avg_cost"], 4),
                "consensus": consensus,
                "suggested_action": suggested,
            }
        )
        logger.info(f"{code} ({name}): consensus={consensus}, suggested={suggested}")
    return rows


def print_console_summary(
    df: pd.DataFrame, holdings: Dict[str, dict], report_date: str
):
    """Print a three-section console summary."""
    consensus_rows = df[df["strategy_id"] == "CONSENSUS"].copy()
    print(f"\n{'=' * 60}")
    print(f"  Daily Signal Report - {report_date}")
    print(f"{'=' * 60}")

    # Section 1: Held stocks
    held_rows = consensus_rows[consensus_rows["currently_held"] == True]
    print(f"\n--- Held Stocks ({len(held_rows)}) ---")
    if held_rows.empty:
        print("  (no holdings)")
    else:
        for _, r in held_rows.iterrows():
            print(
                f"  {r['stock_code']} {r['stock_name']}: "
                f"{r['suggested_action']} (consensus={r['consensus']}, "
                f"size={r['holding_size']}, avg_cost={r['avg_cost']})"
            )

    # Section 2: Watchlist (BUY consensus, not held)
    watchlist = consensus_rows[
        (consensus_rows["suggested_action"] == "BUY")
        & (consensus_rows["currently_held"] == False)
    ]
    print(f"\n--- Watchlist - Potential New Entries ({len(watchlist)}) ---")
    if watchlist.empty:
        print("  (none)")
    else:
        for _, r in watchlist.iterrows():
            print(
                f"  {r['stock_code']} {r['stock_name']}: BUY (consensus={r['consensus']})"
            )

    # Section 3: Summary counts
    counts = consensus_rows["consensus"].value_counts()
    print(f"\n--- Summary ---")
    print(f"  Total stocks scanned: {len(consensus_rows)}")
    for action in ["SELL", "BUY", "HOLD", "WAIT"]:
        print(f"  {action}: {counts.get(action, 0)}")
    print(f"  Held: {len(held_rows)}, Not held: {len(consensus_rows) - len(held_rows)}")
    print(f"{'=' * 60}\n")


def main():
    parser = argparse.ArgumentParser(description="Daily signal generation")
    parser.add_argument(
        "--force_refresh",
        action="store_true",
        help="Force full re-download of market data",
    )
    args = parser.parse_args()

    setup_logging()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ds = DataSource()
    # Override end_date to today so incremental fetch covers the latest bar
    ds.end_date = datetime.date.today().strftime("%Y%m%d")
    cfg = ds.cfg

    comm_cfg = cfg["commission_config"]
    comminfo = AStockCommission(
        commission=comm_cfg["commission"],
        stamp_duty=comm_cfg["stamp_duty"],
        transfer_fee=comm_cfg["transfer_fee"],
    )

    # Normalize keys to str (unquoted numeric codes in yaml are parsed as int)
    param_pool = {str(code): p for code, p in cfg["strategy_params"].items()}
    stock_name_map = {
        str(s["code"]): s.get("name", s["code"]) for s in cfg["stock_list"]
    }

    holdings = compute_holdings(TRADE_CSV)
    logger.info(f"Holdings: {holdings}")

    report_date = datetime.date.today().strftime("%Y%m%d")

    rows = build_report_rows(
        ds, comminfo, cfg, param_pool, stock_name_map, holdings, report_date
    )

    if not rows:
        logger.warning("No signal rows generated")
        return

    df = pd.DataFrame(rows)
    out_path = OUTPUT_DIR / f"daily_signal_{report_date}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8")
    logger.info(f"Signal report saved to {out_path}")

    print_console_summary(df, holdings, report_date)


if __name__ == "__main__":
    main()
