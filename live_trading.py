"""Live trading journal: real fills review + next-day strategy signals.

Single entry for live (paper/real-money) trading after market close:
    python live_trading.py

Inputs:  live_trades.csv (real BUY/SELL fills) + cached Tushare bars.
Outputs:
    - output/live_trade_review.csv          real fills vs strategy signals matrix
    - output/live_signal_{YYYYMMDD}.csv     live strategy signals for holdings
    - output/plots/live_portfolio_report.html  self-contained HTML report
"""

import argparse
import datetime
import html
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import backtrader as bt
import pandas as pd

from comm import (
    OUTPUT_DIR,
    PLOT_DIR,
    PROJECT_ROOT,
    apply_broker_settings,
    build_commission,
    setup_logging,
)
from dataprovider import AStockData, DataProvider
from report import evaluate_calmar, evaluate_sharpe
from stock_filter import FILTER_CSV, load_regime_map, routed_strategies
from strategy import DEFAULT_STRATEGY_PARAMS, STRATEGY_MAPPING, filter_active_strategies

logger = logging.getLogger(__name__)

TRADE_CSV = PROJECT_ROOT / "live_trades.csv"
REQUIRED_COLS = ["trade_date", "stock_code", "side"]
LIVE_REVIEW_CSV = OUTPUT_DIR / "live_trade_review.csv"
LIVE_HTML_REPORT = PLOT_DIR / "live_portfolio_report.html"
METRICS_SUMMARY = OUTPUT_DIR / "metrics_summary.csv"

CONSENSUS_PRIORITY = {"SELL": 3, "BUY": 2, "HOLD": 1, "WAIT": 0}

SUGGESTED_ACTION_CN = {
    "HOLD": "持有",
    "SELL": "卖出",
    "BUY": "买入（新开仓）",
    "BUY_MORE": "加仓",
    "WAIT": "观望",
}


# ===================== Live signal helpers =====================
def compute_holdings(trade_csv: Path) -> Dict[str, dict]:
    """Parse live_trades.csv and return current holdings.

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
                old["avg_cost"] = (old["avg_cost"] * old["size"] + price * size) / total_size
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
    data_provider: DataProvider,
    comminfo,
    cfg: dict,
    code: str,
    strategy_id: str,
    param: dict,
) -> Optional[pd.DataFrame]:
    """Run a single strategy on a single symbol and return the action log DataFrame."""
    df = data_provider.load_cached_data(code)
    if df is None or len(df) == 0:
        df = data_provider.fetch_stock(code, force_refresh=False)
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
    apply_broker_settings(cerebro.broker, cfg, float(cfg["global_setting"]["initial_capital"]), comminfo)
    strategy_instance = cerebro.run()[0]
    return strategy_instance.get_action_dataframe()


def classify_signal(action_df: Optional[pd.DataFrame], last_bar_date) -> dict:
    """Classify the strategy signal based on the action log and last bar date."""
    if action_df is None or action_df.empty:
        return {
            "last_signal_date": None,
            "last_signal_side": None,
            "last_signal_price": None,
            "last_signal_reason": None,
            "last_signal_status": None,
            "last_signal_exec_date": None,
            "last_signal_exec_price": None,
            "strategy_in_position": False,
            "strategy_action": "WAIT",
        }
    last_row = action_df.iloc[-1]
    # Position only counts actually FILLED orders (limit buys may expire unfilled)
    num_buys = ((action_df["side"] == "BUY") & (action_df["status"] == "FILLED")).sum()
    num_sells = ((action_df["side"] == "SELL") & (action_df["status"] == "FILLED")).sum()
    in_position = num_buys > num_sells
    if last_row["date"] == last_bar_date:
        action = last_row["side"]
    else:
        action = "HOLD" if in_position else "WAIT"
    reason = last_row.get("reason")
    exec_date = last_row.get("exec_date")
    exec_price = last_row.get("exec_price")
    return {
        "last_signal_date": last_row["date"],
        "last_signal_side": last_row["side"],
        "last_signal_price": last_row["trigger_price"],
        "last_signal_reason": reason if pd.notna(reason) else None,
        "last_signal_status": last_row.get("status"),
        "last_signal_exec_date": exec_date if pd.notna(exec_date) else None,
        "last_signal_exec_price": exec_price if pd.notna(exec_price) else None,
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
    data_provider: DataProvider,
    comminfo,
    cfg: dict,
    param_pool: dict,
    holdings: Dict[str, dict],
    report_date: str,
    stock_list: List[dict],
    force_refresh: bool = False,
) -> List[dict]:
    """Run strategies for all stocks and collect report rows."""
    rows: List[dict] = []
    for stock_info in stock_list:
        code = str(stock_info["code"])
        name = stock_info.get("name", code)
        # Fetch latest data
        df = data_provider.fetch_stock(code, force_refresh=force_refresh)
        if df is None or len(df) == 0:
            logger.warning(f"No data for {code} ({name}), skipping")
            continue
        last_bar_date = df.iloc[-1]["datetime"].date() if "datetime" in df.columns else df.index[-1].date()
        held = code in holdings
        holding_info = holdings.get(code, {"size": 0, "avg_cost": 0.0})
        strategy_actions: List[str] = []
        for strategy_id, param in param_pool[code].items():
            try:
                action_df = run_strategy_actions(data_provider, comminfo, cfg, code, strategy_id, param)
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
                    "last_signal_reason": signal["last_signal_reason"],
                    "last_signal_status": signal["last_signal_status"],
                    "last_signal_exec_date": signal["last_signal_exec_date"],
                    "last_signal_exec_price": signal["last_signal_exec_price"],
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
                "last_signal_reason": None,
                "last_signal_status": None,
                "last_signal_exec_date": None,
                "last_signal_exec_price": None,
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


def print_console_summary(df: pd.DataFrame, report_date: str):
    """Print a three-section console summary."""
    consensus_rows = df[df["strategy_id"] == "CONSENSUS"].copy()
    print(f"\n{'=' * 60}")
    print(f"  Live Signal Report - {report_date}")
    print(f"{'=' * 60}")

    # Section 1: Held stocks
    held_rows = consensus_rows[consensus_rows["currently_held"] == True]  # noqa: E712
    print(f"\n--- Held Stocks ({len(held_rows)}) ---")
    if held_rows.empty:
        print("  (no holdings)")
    else:
        for _, r in held_rows.iterrows():
            print(f"  {r['stock_code']} {r['stock_name']}: {r['suggested_action']} (consensus={r['consensus']}, size={r['holding_size']}, avg_cost={r['avg_cost']})")

    # Section 2: Watchlist (BUY consensus, not held)
    watchlist = consensus_rows[(consensus_rows["suggested_action"] == "BUY") & (consensus_rows["currently_held"] == False)]  # noqa: E712
    print(f"\n--- Watchlist - Potential New Entries ({len(watchlist)}) ---")
    if watchlist.empty:
        print("  (none)")
    else:
        for _, r in watchlist.iterrows():
            print(f"  {r['stock_code']} {r['stock_name']}: BUY (consensus={r['consensus']})")

    # Section 3: Summary counts
    counts = consensus_rows["consensus"].value_counts()
    print("\n--- Summary ---")
    print(f"  Total stocks scanned: {len(consensus_rows)}")
    for action in ["SELL", "BUY", "HOLD", "WAIT"]:
        print(f"  {action}: {counts.get(action, 0)}")
    print(f"  Held: {len(held_rows)}, Not held: {len(consensus_rows) - len(held_rows)}")
    print(f"{'=' * 60}\n")


class LiveTrading:
    """Live trading journal: match real fills against strategy signals and
    render a real-portfolio report with the next-day signal for each holding."""

    def __init__(self, config_path: str = "config.yaml", trade_csv: Optional[str] = None, stock_list: Optional[str] = None):
        self.data_provider = DataProvider(config_path=config_path)
        self.cfg = self.data_provider.cfg
        if trade_csv is None:
            trade_csv = str(TRADE_CSV)
        self.trade_csv = trade_csv
        # dtype=str to prevent loss of leading zeros in stock codes (000725 -> 725)
        self.trade_df = pd.read_csv(trade_csv, parse_dates=["trade_date"], dtype={"stock_code": str})
        if stock_list:
            wanted = {c.strip() for c in stock_list.split(",") if c.strip()}
            self.trade_df = self.trade_df[self.trade_df["stock_code"].isin(wanted)]
        # Config blacklist overrides the whitelist stock_list
        blacklist = {str(c) for c in (self.cfg.get("stock_blacklist") or [])}
        if blacklist:
            self.trade_df = self.trade_df[~self.trade_df["stock_code"].isin(blacklist)]
        missing = [c for c in REQUIRED_COLS if c not in self.trade_df.columns]
        if missing:
            raise ValueError(f"live_trades.csv is missing required columns: {missing}, existing columns: {list(self.trade_df.columns)}, standard format is trade_date,stock_code,side,price,size")
        self.result_list = []
        self.comminfo = build_commission(self.cfg)
        # Default params for each strategy, used as fallback when a stock has no optimized config
        self.default_strategy_params = DEFAULT_STRATEGY_PARAMS

    def get_strategy_signal(self, code: str, strategy_id: str, param: dict) -> Optional[pd.DataFrame]:
        """Run a single strategy on a single symbol, extract daily buy/sell signals via the unified trade record interface"""
        df = self.data_provider.load_cached_data(code)
        if df is None or len(df) == 0:
            # Fallback to incremental fetching when no cache exists (with local cache acceleration)
            df = self.data_provider.fetch_stock(code, force_refresh=False)
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
        # Keep capital, commission and slippage consistent with the formal backtest
        apply_broker_settings(cerebro.broker, self.cfg, float(self.cfg["global_setting"]["initial_capital"]), self.comminfo)
        strategy_instance = cerebro.run()[0]
        trades_df = strategy_instance.get_trade_dataframe()
        # A closing trade corresponds to two signals: buy on entry_date / sell on exit_date
        sig = []
        if len(trades_df) > 0:
            for _, t in trades_df.iterrows():
                sig.append({"date": t["entry_date"], "side": "BUY", "price": t["entry_price"]})
                sig.append({"date": t["exit_date"], "side": "SELL", "price": t["exit_price"]})
        return pd.DataFrame(sig)

    def match_live_trade(self) -> pd.DataFrame:
        """Iterate over each real fill, match strategy signals for the same day, and produce comparative statistics"""
        # Convert keys to str uniformly (numeric codes without quotes in yaml will be parsed as int)
        strategy_params = {str(code): p for code, p in (self.cfg.get("strategy_params", {}) or {}).items()}
        for _, row in self.trade_df.iterrows():
            code = str(row["stock_code"])
            trade_date = row["trade_date"].date()
            side = row["side"] if "side" in row else None
            live_profit_loss = row["profit_loss"] if "profit_loss" in row else None
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
                note = "" if pd.notna(side) else "live side missing"
                if using_defaults:
                    note = "default params" if not note else f"{note}; default params"
                self.result_list.append(
                    {
                        "stock_code": code,
                        "trade_date": trade_date,
                        "strategy": strategy_id,
                        "live_side": side,
                        "strategy_signal": signal_side,
                        "match": match_flag,
                        "live_profit_loss": live_profit_loss,
                        "note": note,
                    }
                )
        return pd.DataFrame(self.result_list)

    def summary_report(self, out_csv: Optional[str] = None) -> pd.DataFrame:
        if out_csv is None:
            out_csv = str(LIVE_REVIEW_CSV)
        df = self.match_live_trade()
        df.to_csv(out_csv, index=False, encoding="utf8")
        print("===== Live Trades vs Strategy Signals Review =====")
        total_cnt = len(df)
        if total_cnt == 0:
            print("No review results (live trades have no corresponding strategy config or no market data), empty report output")
            return df
        match_cnt = int(df["match"].sum())
        match_rate = match_cnt / total_cnt
        # Win rate / profit-loss only counts live trades with profit_loss records (rows with open positions or missing profit_loss are excluded)
        profit_loss_df = df[df["live_profit_loss"].notna()]
        if len(profit_loss_df) > 0:
            win_rate = (profit_loss_df[profit_loss_df["live_profit_loss"] > 0].shape[0]) / len(profit_loss_df)
            avg_profit_loss = profit_loss_df["live_profit_loss"].mean()
            win_str, profit_loss_str = f"{win_rate:.2%}", f"{avg_profit_loss:.2f}"
        else:
            win_str, profit_loss_str = "N/A", "N/A"
        print(f"Total trades: {total_cnt}")
        print(f"Matches with strategy signals: {match_cnt}, match rate: {match_rate:.2%}")
        print(f"Live trade win rate: {win_str} (based on {len(profit_loss_df)} trades with profit-loss records)")
        print(f"Average profit-loss per trade: {profit_loss_str}")
        return df

    # ===================== Real-portfolio HTML report =====================
    def collect_portfolio_data(self, force_refresh: bool = False, review_df: Optional[pd.DataFrame] = None) -> Optional[dict]:
        """Assemble the real-portfolio dataset driven entirely by current files:
        holdings from live_trades.csv, latest quotes from cache, live strategy
        signals recomputed now, trade-vs-signal match matrix, and the latest
        backtest reference metrics from metrics_summary.csv."""
        holdings = compute_holdings(Path(self.trade_csv))
        # Honor the same stock_list / blacklist filter already applied to self.trade_df
        holdings = {code: info for code, info in holdings.items() if code in set(self.trade_df["stock_code"])}
        if not holdings:
            return None
        held_codes = list(holdings.keys())

        # Incremental refresh so the last bar covers the latest trading day
        self.data_provider.end_date = datetime.date.today().strftime("%Y%m%d")

        # Build the routed param pool (same gating as the pipeline backtest)
        optimized_params = {str(code): p for code, p in (self.cfg.get("strategy_params", {}) or {}).items()}
        regime_map = load_regime_map()
        pairlist_pass: dict = {}
        if Path(FILTER_CSV).exists():
            filter_df = pd.read_csv(FILTER_CSV, dtype={"stock_code": str})
            pairlist_pass = dict(zip(filter_df["stock_code"], filter_df["pairlist_passed"].astype(bool)))

        name_map = {str(s["code"]): s.get("name", str(s["code"])) for s in self.cfg["stock_list"]}
        stock_infos, param_pool = [], {}
        for code in held_codes:
            if not pairlist_pass.get(code, True):
                continue
            active_params = filter_active_strategies(optimized_params.get(code, DEFAULT_STRATEGY_PARAMS))
            routed_ids = routed_strategies(code, regime_map, list(active_params.keys()))
            param_pool[code] = {sid: p for sid, p in active_params.items() if sid in routed_ids}
            stock_infos.append({"code": code, "name": name_map.get(code, code)})

        report_date = datetime.date.today().strftime("%Y%m%d")
        rows = build_report_rows(
            self.data_provider,
            self.comminfo,
            self.cfg,
            param_pool,
            holdings,
            report_date,
            stock_infos,
            force_refresh=force_refresh,
        )
        signal_df = pd.DataFrame(rows)

        # Latest quote per holding (cache was refreshed inside build_report_rows)
        quotes: dict = {}
        for code in held_codes:
            df = self.data_provider.load_cached_data(code)
            if df is not None and len(df) > 0:
                last = df.iloc[-1]
                quotes[code] = {"date": pd.to_datetime(last["datetime"]).date(), "close": float(last["close"])}

        # Trade-vs-signal match matrix (reuse the one already produced for the CSV;
        # reset + recompute only when callers did not provide it, keeping repeats idempotent)
        if review_df is None:
            self.result_list = []
            review_df = self.match_live_trade()

        # Latest backtest reference metrics per code/strategy
        metrics_by_code: dict = {}
        if METRICS_SUMMARY.exists():
            metrics_df = pd.read_csv(METRICS_SUMMARY, dtype={"stock_code": str})
            for _, row in metrics_df[metrics_df["stock_code"].isin(held_codes)].iterrows():
                metrics_by_code.setdefault(row["stock_code"], {})[row["strategy"]] = row.to_dict()

        return {
            "report_date": report_date,
            "holdings": holdings,
            "quotes": quotes,
            "signal_df": signal_df,
            "review_df": review_df,
            "metrics_by_code": metrics_by_code,
            "name_map": name_map,
        }

    @staticmethod
    def _build_commentary(holding, quote, consensus_row, strat_rows, match_stats, backtest_metrics) -> list:
        """Auto-generated Chinese bullets explaining P&L, signal meaning,
        strategy-vs-holding alignment and backtest reference."""
        bullets: list = []
        size, cost = holding["size"], holding["avg_cost"]
        if quote:
            close = quote["close"]
            pnl = (close - cost) * size
            pct = close / cost - 1
            tone = "浮盈" if pnl >= 0 else "浮亏"
            bullets.append(
                f"截至 {quote['date']} 收盘 {close:.2f} 元：持仓 {size} 股，加权均价 {cost:.4f} 元，"
                f"市值 {close * size:,.0f} 元，{tone} {abs(pnl):,.0f} 元（{pct:+.1%}）。"
            )

        consensus = consensus_row["consensus"] if consensus_row is not None else "WAIT"
        if consensus == "SELL":
            bullets.append("⚠️ 策略共识为 SELL：已触发离场信号，建议优先执行卖出，不要与信号反向操作。")
        elif consensus == "BUY":
            bullets.append("策略共识为 BUY：策略仍给出买入信号，与持仓方向一致，如有加仓计划可按信号执行（BUY_MORE）。")
        elif consensus == "HOLD":
            bullets.append("策略共识为 HOLD：策略当前持仓且未发出卖出信号，与你的实盘方向一致，下一步继续持有即可。")
        elif consensus == "WAIT":
            last = strat_rows.iloc[-1] if strat_rows is not None and len(strat_rows) else None
            extra = ""
            if last is not None and pd.notna(last["last_signal_date"]):
                extra = f"（最近一次策略动作是 {last['last_signal_date']} 的 {last['last_signal_side']}，之后一直空仓观望）"
            bullets.append(
                "策略共识为 WAIT：策略本身当前空仓" + extra + "，你的这笔持仓属于策略外的主动交易。"
                "当前没有需要执行的策略信号，以持有观察为主，同时严格执行自己的止损纪律；待策略重新出现买入信号再考虑加仓。"
            )

        # Latest raw signal detail
        for _, row in (strat_rows if strat_rows is not None else pd.DataFrame()).iterrows():
            if pd.notna(row["last_signal_date"]):
                reason = str(row["last_signal_reason"]) if pd.notna(row["last_signal_reason"]) else ""
                status = str(row["last_signal_status"])
                exec_txt = ""
                if pd.notna(row["last_signal_exec_date"]):
                    exec_txt = f"，已于 {row['last_signal_exec_date']} 以 {row['last_signal_exec_price']} 元成交"
                bullets.append(
                    f"{row['strategy_id']} 策略最近信号：{row['last_signal_date']} {row['last_signal_side']} "
                    f"@ {row['last_signal_price']}（{status}{exec_txt}）。{('原因：' + reason) if reason else ''}"
                )

        # Trade-vs-signal alignment
        if match_stats:
            parts = [f"{strategy_id} {matched}/{total} 笔同日同向" for strategy_id, matched, total in match_stats]
            total_all = sum(total for _, _, total in match_stats)
            matched_all = sum(matched for _, matched, _ in match_stats)
            verdict = "买入时点与策略信号高度同步" if matched_all >= total_all / 2 else "多数买入并非策略信号触发，属于自主择时，需特别注意纪律风险"
            bullets.append(f"真实成交与策略信号匹配：{'；'.join(parts)}——{verdict}。")

        # Backtest reference
        if backtest_metrics:
            for strategy_id, m in backtest_metrics.items():
                bullets.append(
                    f"{strategy_id} 策略历史回测参考：总收益 {m['total_return']:+.1%}，最大回撤 {m['max_drawdown']:.1%}，"
                    f"Sharpe {m['sharpe_ratio']:.2f}（{evaluate_sharpe(m['sharpe_ratio']).split('：')[0]}），"
                    f"Calmar {m['calmar_ratio']:.2f}（{evaluate_calmar(m['calmar_ratio']).split('：')[0]}），"
                    f"相对买入持有 alpha {m.get('alpha_vs_buyhold', 0):+.1%}。"
                )
        return bullets

    def render_live_html(self, data: dict, out_html: Optional[str] = None) -> str:
        """Render the self-contained real-portfolio HTML report."""
        if out_html is None:
            out_html = str(LIVE_HTML_REPORT)
        os.makedirs(os.path.dirname(out_html), exist_ok=True)

        holdings, quotes = data["holdings"], data["quotes"]
        signal_df, review_df = data["signal_df"], data["review_df"]
        metrics_by_code, name_map = data["metrics_by_code"], data["name_map"]
        report_date = data["report_date"]

        def esc(value) -> str:
            return html.escape(str(value)) if value is not None and pd.notna(value) else ""

        def money(value) -> str:
            return f"{value:,.0f}"

        def pnl_class(value) -> str:
            return "positive" if value >= 0 else "negative"

        def action_badge(action) -> str:
            css = {"SELL": "badge-sell", "BUY": "badge-buy", "BUY_MORE": "badge-buy"}.get(action, "badge-hold")
            return f"<span class='badge {css}'>{esc(action)} · {SUGGESTED_ACTION_CN.get(action, esc(action))}</span>"

        # ---- Portfolio aggregates ----
        total_cost = total_value = 0.0
        per_code_rows = []
        detail_cards = []
        for code, holding in holdings.items():
            size, cost = holding["size"], holding["avg_cost"]
            quote = quotes.get(code)
            close = quote["close"] if quote else None
            value = close * size if close else 0.0
            cost_value = cost * size
            pnl = value - cost_value
            pnl_pct = pnl / cost_value if cost_value else 0.0
            total_cost += cost_value
            total_value += value

            code_signals = signal_df[signal_df["stock_code"] == code] if len(signal_df) else pd.DataFrame()
            consensus_row = code_signals[code_signals["strategy_id"] == "CONSENSUS"]
            consensus_row = consensus_row.iloc[0] if len(consensus_row) else None
            strat_rows = code_signals[code_signals["strategy_id"] != "CONSENSUS"]
            consensus = consensus_row["consensus"] if consensus_row is not None else "WAIT"
            suggested = consensus_row["suggested_action"] if consensus_row is not None else "WAIT"

            match_grp = review_df[review_df["stock_code"] == code] if len(review_df) else pd.DataFrame()
            match_stats = []
            if len(match_grp):
                for strategy_id, grp in match_grp.groupby("strategy"):
                    match_stats.append((strategy_id, int(grp["match"].sum()), len(grp)))

            backtest_metrics = metrics_by_code.get(code, {})
            bullets = self._build_commentary(holding, quote, consensus_row, strat_rows, match_stats, backtest_metrics)
            per_code_rows.append(
                f"<tr><td>{esc(code)}</td><td>{esc(name_map.get(code, code))}</td><td>{size}</td>"
                f"<td>{cost:.4f}</td><td>{close:.2f}<div class='sub'>{esc(quote['date']) if quote else 'N/A'}</div></td>"
                f"<td>{money(value)}</td><td>{money(cost_value)}</td>"
                f"<td class='{pnl_class(pnl)}'>{money(pnl)}<div class='sub'>{pnl_pct:+.1%}</div></td>"
                f"<td>{esc(consensus)}</td><td>{action_badge(suggested)}</td></tr>"
            )

            # --- Detail card: real fills ---
            code_trades = self.trade_df[self.trade_df["stock_code"] == code].sort_values("trade_date")
            fill_rows = "".join(
                f"<tr><td>{row['trade_date'].date()}</td>"
                f"<td class='side-{'buy' if str(row['side']).upper() == 'BUY' else 'sell'}'>{esc(str(row['side']).upper())}</td>"
                f"<td>{float(row['price']):.2f}</td><td>{int(row['size'])}</td>"
                f"<td>{money(float(row['price']) * int(row['size']))}</td></tr>"
                for _, row in code_trades.iterrows()
            )

            # --- Detail card: per-strategy live signal ---
            sig_rows = ""
            for _, row in (strat_rows if strat_rows is not None else pd.DataFrame()).iterrows():
                in_pos = "是" if bool(row["strategy_in_position"]) else "否"
                reason = esc(row["last_signal_reason"])
                sig_rows += (
                    f"<tr><td>{esc(row['strategy_id'])}</td><td>{esc(row['last_signal_date'])}</td>"
                    f"<td>{esc(row['last_signal_side'])}</td><td>{esc(row['last_signal_price'])}</td>"
                    f"<td>{esc(row['last_signal_status'])}</td><td>{esc(row['last_signal_exec_date'])}</td>"
                    f"<td>{esc(row['last_signal_exec_price'])}</td><td>{in_pos}</td><td>{esc(row['strategy_action'])}</td></tr>"
                    f"<tr class='reason-row'><td colspan='9'>{reason or '—'}</td></tr>"
                )

            # --- Detail card: match matrix ---
            match_rows = "".join(
                f"<tr><td>{esc(sid)}</td><td>{matched}/{total}</td><td>{matched / total:.0%}</td></tr>"
                for sid, matched, total in match_stats
            ) or "<tr><td colspan='3'>无配置策略</td></tr>"

            # --- Detail card: backtest reference ---
            bt_rows = ""
            for strategy_id, m in backtest_metrics.items():
                bt_rows += (
                    f"<tr><td>{esc(strategy_id)}</td><td>{m['total_return']:+.1%}</td>"
                    f"<td>{m['annual_return']:+.1%}</td><td>{m['max_drawdown']:.1%}</td>"
                    f"<td>{m['sharpe_ratio']:.2f}<div class='sub'>{esc(evaluate_sharpe(m['sharpe_ratio']).split('：')[0])}</div></td>"
                    f"<td>{m['calmar_ratio']:.2f}<div class='sub'>{esc(evaluate_calmar(m['calmar_ratio']).split('：')[0])}</div></td>"
                    f"<td>{m.get('market_change', 0):+.1%}</td><td class='{pnl_class(m.get('alpha_vs_buyhold', 0))}'>{m.get('alpha_vs_buyhold', 0):+.1%}</td></tr>"
                )
            if not bt_rows:
                bt_rows = "<tr><td colspan='8'>暂无最近回测结果（可运行 main.py 生成）</td></tr>"

            bullet_html = "".join(f"<li>{esc(b)}</li>" for b in bullets)
            detail_cards.append(
                f"""
    <div class="section detail-card">
      <div class="detail-head">
        <div class="detail-title">{esc(name_map.get(code, code))} ({esc(code)})</div>
        {action_badge(suggested)}
      </div>
      <div class="detail-grid">
        <div>
          <div class="block-title">真实成交明细（live_trades.csv）</div>
          <table class="data-table"><thead><tr><th>日期</th><th>方向</th><th>价格</th><th>数量</th><th>金额</th></tr></thead>
          <tbody>{fill_rows}</tbody></table>
        </div>
        <div>
          <div class="block-title">策略最新信号（现算）</div>
          <table class="data-table signal-table"><thead><tr><th>策略</th><th>信号日</th><th>方向</th><th>触发价</th><th>状态</th><th>成交日</th><th>成交价</th><th>策略持仓</th><th>当前动作</th></tr></thead>
          <tbody>{sig_rows or '<tr><td colspan=9>无信号</td></tr>'}</tbody></table>
          <div class="block-title" style="margin-top:16px">真实成交 vs 策略信号匹配</div>
          <table class="data-table"><thead><tr><th>策略</th><th>匹配笔数</th><th>匹配率</th></tr></thead><tbody>{match_rows}</tbody></table>
          <div class="block-title" style="margin-top:16px">策略历史回测参考</div>
          <table class="data-table"><thead><tr><th>策略</th><th>总收益</th><th>年化</th><th>最大回撤</th><th>Sharpe</th><th>Calmar</th><th>买入持有</th><th>Alpha</th></tr></thead>
          <tbody>{bt_rows}</tbody></table>
        </div>
      </div>
      <div class="block-title">要点解释与下一步信号</div>
      <ul class="commentary">{bullet_html}</ul>
    </div>"""
            )

        total_pnl = total_value - total_cost
        total_pct = total_pnl / total_cost if total_cost else 0.0
        consensus_counts = signal_df[signal_df["strategy_id"] == "CONSENSUS"]["consensus"].value_counts().to_dict() if len(signal_df) else {}
        signal_summary = " / ".join(f"{action}: {consensus_counts.get(action, 0)}" for action in ("SELL", "BUY", "HOLD", "WAIT"))

        latest_bar_dates = sorted({str(q["date"]) for q in quotes.values()})
        latest_bar_txt = latest_bar_dates[-1] if latest_bar_dates else "N/A"

        page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>SkyQuant 实盘持仓复盘报告 - {esc(report_date)}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ background: #0f1520; color: #d4d4d4; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 0; line-height: 1.6; }}
.container {{ max-width: 1280px; margin: 0 auto; padding: 30px 40px; }}
.header {{ border-bottom: 3px solid #2E86AB; padding-bottom: 20px; margin-bottom: 30px; }}
.header h1 {{ margin: 0 0 8px 0; font-size: 28px; color: #fff; }}
.header .meta {{ font-size: 13px; color: #888; margin-top: 6px; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; margin-bottom: 30px; }}
.kpi-card {{ background: #1a2332; border: 1px solid #2a3548; border-radius: 8px; padding: 16px 14px; text-align: center; }}
.kpi-label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
.kpi-value {{ font-size: 22px; font-weight: 600; margin-top: 6px; color: #fff; }}
.positive {{ color: #57CC99; }}
.negative {{ color: #F38181; }}
.sub {{ font-size: 11px; color: #999; font-weight: 400; margin-top: 2px; }}
.section {{ margin-bottom: 30px; }}
.section-title {{ font-size: 16px; font-weight: 600; color: #fff; border-bottom: 1px solid #2a3548; padding-bottom: 6px; margin-bottom: 12px; }}
.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.data-table th, .data-table td {{ padding: 8px 10px; border-bottom: 1px solid #2a3548; text-align: left; }}
.data-table th {{ background: #1a2332; color: #d4d4d4; font-weight: 600; white-space: nowrap; }}
.data-table tr:hover {{ background: #1a2332; }}
.signal-table .reason-row td {{ font-size: 11px; color: #8fa8c0; background: #121a27; padding: 6px 10px; }}
.side-buy {{ color: #57CC99; font-weight: 600; }}
.side-sell {{ color: #F38181; font-weight: 600; }}
.badge {{ display: inline-block; padding: 3px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; white-space: nowrap; }}
.badge-sell {{ background: rgba(243,129,129,.18); color: #F38181; }}
.badge-buy {{ background: rgba(87,204,153,.18); color: #57CC99; }}
.badge-hold {{ background: rgba(46,134,171,.2); color: #6db4d4; }}
.detail-card {{ background: #141c28; border: 1px solid #2a3548; border-radius: 10px; padding: 20px 24px; }}
.detail-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }}
.detail-title {{ font-size: 18px; font-weight: 700; color: #fff; }}
.detail-grid {{ display: grid; grid-template-columns: 360px 1fr; gap: 24px; }}
.block-title {{ font-size: 13px; font-weight: 600; color: #9fb3c8; margin-bottom: 8px; }}
.commentary {{ margin: 8px 0 0 0; padding-left: 20px; }}
.commentary li {{ margin-bottom: 8px; font-size: 13px; }}
footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #2a3548; font-size: 12px; color: #666; }}
</style></head>
<body><div class="container">
  <div class="header">
    <h1>SkyQuant 实盘持仓复盘报告</h1>
    <div class="meta">报告日期：{esc(report_date)} ｜ 行情截至：{esc(latest_bar_txt)} ｜ 信号为当日现算（consensus：SELL &gt; BUY &gt; HOLD &gt; WAIT）</div>
  </div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="kpi-label">持仓标的</div><div class="kpi-value">{len(holdings)}</div></div>
    <div class="kpi-card"><div class="kpi-label">投入成本</div><div class="kpi-value">{money(total_cost)}</div></div>
    <div class="kpi-card"><div class="kpi-label">当前市值</div><div class="kpi-value">{money(total_value)}</div></div>
    <div class="kpi-card"><div class="kpi-label">浮动盈亏</div><div class="kpi-value {pnl_class(total_pnl)}">{money(total_pnl)}<div class="sub">{total_pct:+.1%}</div></div></div>
    <div class="kpi-card"><div class="kpi-label">信号分布</div><div class="kpi-value" style="font-size:14px;padding-top:10px">{esc(signal_summary)}</div></div>
  </div>
  <div class="section">
    <div class="section-title">持仓总览与下一步操作</div>
    <table class="data-table"><thead><tr><th>代码</th><th>名称</th><th>股数</th><th>均价</th><th>最新收盘</th><th>市值</th><th>成本</th><th>浮动盈亏</th><th>策略共识</th><th>建议操作</th></tr></thead>
    <tbody>{''.join(per_code_rows)}</tbody></table>
  </div>
  <div class="section-title">逐标的成交、信号与要点</div>
  {''.join(detail_cards)}
  <footer>
    数据来源：live_trades.csv（真实成交）+ 本地缓存行情（Tushare）+ 策略信号现算 + metrics_summary.csv 最近一次回测。<br>
    本报告仅用于个人交易复盘与策略研究，不构成投资建议；策略历史回测结果不代表未来收益。
  </footer>
</div></body></html>"""

        with open(out_html, "w", encoding="utf-8") as f:
            f.write(page)
        return out_html


def main():
    parser = argparse.ArgumentParser(description="Live trading journal: real fills review + next-day signals")
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated stock codes to review. Defaults to all trades in live_trades.csv.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force full re-download of market data",
    )
    args = parser.parse_args()

    setup_logging()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    trader = LiveTrading(stock_list=args.stock_list)

    # 1) Real fills vs strategy signals matrix CSV
    review_df = trader.summary_report()

    # 2) Holdings + live signals + HTML portfolio report
    data = trader.collect_portfolio_data(force_refresh=args.force_refresh, review_df=review_df)
    if data is None:
        print("No open holdings parsed from live_trades.csv, signal report and HTML skipped")
        return

    signal_csv = OUTPUT_DIR / f"live_signal_{data['report_date']}.csv"
    data["signal_df"].to_csv(signal_csv, index=False, encoding="utf-8")
    print_console_summary(data["signal_df"], data["report_date"])
    logger.info(f"Live signal report saved to {signal_csv}")

    html_path = trader.render_live_html(data)
    logger.info(f"Live portfolio HTML report saved to {html_path}")


if __name__ == "__main__":
    main()
