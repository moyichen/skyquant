import argparse

import backtrader as bt
import pandas as pd

from comm import AStockCommission
from data_source import AStockData, DataSource
from strategy import DEFAULT_STRATEGY_PARAMS, STRATEGY_MAPPING

REQUIRED_COLS = ["trade_date", "stock_code", "side"]


class ManualTradeReview:
    def __init__(self, config_path="config.yaml", trade_csv="manual_trades.csv", stock_list=None):
        self.ds = DataSource(config_path=config_path)
        self.cfg = self.ds.cfg
        # dtype=str to prevent loss of leading zeros in stock codes (000725 -> 725)
        self.trade_df = pd.read_csv(trade_csv, parse_dates=["trade_date"], dtype={"stock_code": str})
        if stock_list:
            wanted = {c.strip() for c in stock_list.split(",") if c.strip()}
            self.trade_df = self.trade_df[self.trade_df["stock_code"].isin(wanted)]
        missing = [c for c in REQUIRED_COLS if c not in self.trade_df.columns]
        if missing:
            raise ValueError(f"manual_trades.csv is missing required columns: {missing}, existing columns: {list(self.trade_df.columns)}, standard format is trade_date,stock_code,side,price,size")
        self.result_list = []
        comm_cfg = self.cfg["commission_config"]
        self.comminfo = AStockCommission(
            commission=comm_cfg["commission"],
            stamp_duty=comm_cfg["stamp_duty"],
            transfer_fee=comm_cfg["transfer_fee"],
        )
        # Default params for each strategy, used as fallback when a stock has no optimized config
        self.default_strategy_params = DEFAULT_STRATEGY_PARAMS

    def get_strategy_signal(self, code, strategy_id, param):
        """Run a single strategy on a single symbol, extract daily buy/sell signals via the unified trade record interface"""
        df = self.ds.load_cached_data(code)
        if df is None or len(df) == 0:
            # Fallback to incremental fetching when no cache exists (with local cache acceleration)
            df = self.ds.fetch_stock(code, force_refresh=False)
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
        # Keep capital and commission settings consistent with the formal backtest
        cerebro.broker.setcash(self.cfg["global_setting"]["initial_capital"])
        cerebro.broker.addcommissioninfo(self.comminfo)
        strategy_instance = cerebro.run()[0]
        trades_df = strategy_instance.get_trade_dataframe()
        # A closing trade corresponds to two signals: buy on entry_date / sell on exit_date
        sig = []
        if len(trades_df) > 0:
            for _, t in trades_df.iterrows():
                sig.append({"date": t["entry_date"], "side": "BUY", "price": t["entry_price"]})
                sig.append({"date": t["exit_date"], "side": "SELL", "price": t["exit_price"]})
        return pd.DataFrame(sig)

    def match_manual_trade(self):
        """Iterate over each manual trade, match strategy signals for the same period, and produce comparative statistics"""
        # Convert keys to str uniformly (numeric codes without quotes in yaml will be parsed as int)
        strategy_params = {str(code): p for code, p in (self.cfg.get("strategy_params", {}) or {}).items()}
        for _, row in self.trade_df.iterrows():
            code = str(row["stock_code"])
            trade_date = row["trade_date"].date()
            side = row["side"] if "side" in row else None
            manual_profit_loss = row["profit_loss"] if "profit_loss" in row else None
            # Use optimized params when available; otherwise fall back to strategy default params
            if code in strategy_params:
                stock_strategy_params = strategy_params[code]
                using_defaults = False
            else:
                stock_strategy_params = self.default_strategy_params
                using_defaults = True
            for strategy_id, param in stock_strategy_params.items():
                sig_df = self.get_strategy_signal(code, strategy_id, param)
                signal_side = None
                if sig_df is not None and len(sig_df) > 0:
                    sig_today = sig_df[sig_df["date"] == trade_date]
                    if len(sig_today) > 0:
                        signal_side = sig_today.iloc[0]["side"]
                match_flag = (signal_side == side) if pd.notna(side) else False
                note = "" if pd.notna(side) else "manual side missing"
                if using_defaults:
                    note = "default params" if not note else f"{note}; default params"
                self.result_list.append(
                    {
                        "stock_code": code,
                        "trade_date": trade_date,
                        "strategy": strategy_id,
                        "manual_side": side,
                        "strategy_signal": signal_side,
                        "match": match_flag,
                        "manual_profit_loss": manual_profit_loss,
                        "note": note,
                    }
                )
        return pd.DataFrame(self.result_list)

    def summary_report(self, out_csv="output/manual_review_result.csv"):
        df = self.match_manual_trade()
        df.to_csv(out_csv, index=False, encoding="utf8")
        print("===== Manual Trades vs Strategy Signals Review Report =====")
        total_cnt = len(df)
        if total_cnt == 0:
            print("No review results (manual trades have no corresponding strategy config or no market data), empty report output")
            return df
        match_cnt = int(df["match"].sum())
        match_rate = match_cnt / total_cnt
        # Win rate / profit-loss only counts manual trades with profit_loss records (rows with open positions or missing profit_loss are excluded)
        profit_loss_df = df[df["manual_profit_loss"].notna()]
        if len(profit_loss_df) > 0:
            win_rate = (profit_loss_df[profit_loss_df["manual_profit_loss"] > 0].shape[0]) / len(profit_loss_df)
            avg_profit_loss = profit_loss_df["manual_profit_loss"].mean()
            win_str, profit_loss_str = f"{win_rate:.2%}", f"{avg_profit_loss:.2f}"
        else:
            win_str, profit_loss_str = "N/A", "N/A"
        print(f"Total trades: {total_cnt}")
        print(f"Matches with strategy signals: {match_cnt}, match rate: {match_rate:.2%}")
        print(f"Manual trade win rate: {win_str} (based on {len(profit_loss_df)} trades with profit-loss records)")
        print(f"Average profit-loss per trade: {profit_loss_str}")
        return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manual trade review")
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to review. Defaults to all trades in manual_trades.csv.",
    )
    args = parser.parse_args()
    reviewer = ManualTradeReview(stock_list=args.stock_list)
    reviewer.summary_report()
