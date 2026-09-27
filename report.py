"""Unified report layer for SkyQuant backtests.

Metrics and charting live in one module so the engine (main.py) and the
optimization workers share a single metrics definition.

Exports:
    calc_equity_metrics:       total/annualized return, max drawdown, Sharpe, Calmar
    calc_metrics:              equity metrics + trade stats (win rate, profit factor)
    render_interactive_chart:  freqtrade-style Plotly K-line chart (candles,
                               volume, indicators, trade entry/exit markers)
    render_report:             self-contained HTML backtest report (KPIs,
                               equity curve, drawdown, trades, analyzers)
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from backtrader.utils.py3 import MAXINT
from bokeh.embed import components
from bokeh.layouts import column
from bokeh.models import (
    ColumnDataSource,
    DatetimeTickFormatter,
    HoverTool,
    NumeralTickFormatter,
)
from bokeh.plotting import figure
from bokeh.resources import INLINE


# ===================== Metrics =====================
def calc_equity_metrics(equity_df: pd.DataFrame, risk_free_rate: float = 0.02) -> dict:
    """Performance metrics derived from the daily equity curve alone.

    Also used by the optimization pool workers (which have no closed-trade
    table), so keep this self-contained and cheap.
    Returns: total_return, annual_return, max_drawdown, sharpe_ratio, calmar.
    """
    equity = equity_df["equity"].values
    dates = pd.to_datetime(equity_df["datetime"])
    daily_ret = equity_df["equity"].pct_change().dropna()

    # Max drawdown
    cum_max = equity_df["equity"].cummax()
    drawdown = (equity_df["equity"] - cum_max) / cum_max
    max_dd = float(drawdown.min())

    # Annualized return
    days = (dates.iloc[-1] - dates.iloc[0]).days
    total_return = float(equity[-1] / equity[0] - 1)
    annual_return = (1 + total_return) ** (365.0 / days) - 1 if days > 0 else 0.0

    # Sharpe ratio
    daily_rf = (1 + risk_free_rate) ** (1 / 365) - 1
    excess_ret = daily_ret - daily_rf
    sharpe = float(np.sqrt(252) * excess_ret.mean() / excess_ret.std()) if excess_ret.std() != 0 else 0.0

    # Calmar ratio = annualized return / absolute max drawdown
    calmar = float(annual_return / abs(max_dd)) if max_dd < 0 else 0.0

    return {
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe_ratio": round(sharpe, 4),
        "calmar_ratio": round(calmar, 4),
    }


def normalize_exit_reason(reason: Any) -> str:
    """Map a raw strategy exit_reason string to a freqtrade-style exit category.

    Categories: stop_loss (worst-loss floor), trailing_stop (chandelier/tightened),
    take_profit, exit_signal (strategy-specific signal), other/left_open.
    """
    if reason is None or (isinstance(reason, float) and pd.isna(reason)):
        return "left_open"
    prefix = str(reason).split(":", 1)[0].strip()
    if prefix == "max_loss_stop":
        return "stop_loss"
    if prefix in ("trail_stop", "trail_stop_tightened"):
        return "trailing_stop"
    if prefix == "take_profit":
        return "take_profit"
    if prefix in ("momentum_negative", "boll_upper_break", "donchian_lower_break"):
        return "exit_signal"
    return "other"


def summarize_exit_reasons(trades_df: pd.DataFrame) -> pd.DataFrame:
    """freqtrade-style EXIT REASON STATS: per exit category positions, win rate,
    average holding duration, total net profit and summed return %."""
    if len(trades_df) == 0:
        return pd.DataFrame(columns=["exit_reason", "positions", "win_rate", "avg_duration_days", "profit_total", "profit_total_pct"])
    df = trades_df.copy()
    df["exit_category"] = df["exit_reason"].map(normalize_exit_reason)
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    df["duration_days"] = (df["exit_date"] - df["entry_date"]).dt.days
    rows = []
    for category, group in df.groupby("exit_category"):
        wins = int((group["profit_loss_net"] > 0).sum())
        rows.append(
            {
                "exit_reason": category,
                "positions": len(group),
                "win_rate": wins / len(group),
                "avg_duration_days": group["duration_days"].mean(),
                "profit_total": group["profit_loss_net"].sum(),
                "profit_total_pct": group["profit_rate"].sum(),
            }
        )
    order = ["stop_loss", "trailing_stop", "take_profit", "exit_signal", "other", "left_open"]
    rows.sort(key=lambda r: order.index(r["exit_reason"]) if r["exit_reason"] in order else len(order))
    return pd.DataFrame(rows)


def _monthly_returns(equity_df: pd.DataFrame) -> pd.DataFrame:
    """Monthly equity returns (%) in a year(rows) x month(cols) matrix."""
    df = equity_df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    monthly = df.set_index("datetime")["equity"].resample("ME").last().pct_change().dropna()
    if monthly.empty:
        return pd.DataFrame()
    table = pd.DataFrame({"year": monthly.index.year, "month": monthly.index.month, "ret": monthly.values})
    pivot = table.pivot_table(index="year", columns="month", values="ret")
    pivot = pivot.reindex(columns=range(1, 13))
    return pivot


def calc_metrics(equity_df: pd.DataFrame, trades_df: pd.DataFrame, risk_free_rate: float = 0.02, price_series: Optional[pd.Series] = None) -> dict:
    """
    equity_df: datetime,equity
    trades_df: entry_date,exit_date,profit_loss_net (net profit after fees)
    risk_free_rate: annualized risk-free rate 2%
    price_series: optional underlying close series for buy-and-hold benchmark
    return dict: freqtrade-aligned performance/trade/daily metrics
    """
    metrics = calc_equity_metrics(equity_df, risk_free_rate=risk_free_rate)

    daily_ret = equity_df["equity"].pct_change().dropna()

    # Sortino ratio (downside deviation only)
    downside = daily_ret[daily_ret < 0]
    sortino = float(np.sqrt(252) * daily_ret.mean() / downside.std()) if len(downside) > 0 and downside.std() != 0 else 0.0
    metrics["sortino_ratio"] = round(sortino, 4)

    # Daily equity stats (freqtrade daily stats)
    metrics["best_day"] = round(float(daily_ret.max()), 4) if len(daily_ret) else 0.0
    metrics["worst_day"] = round(float(daily_ret.min()), 4) if len(daily_ret) else 0.0
    metrics["winning_days"] = int((daily_ret > 0).sum())
    metrics["losing_days"] = int((daily_ret < 0).sum())
    metrics["zero_days"] = int((daily_ret == 0).sum())

    # Buy & hold benchmark (freqtrade market_change)
    if price_series is not None and len(price_series) >= 2:
        market_change = float(price_series.iloc[-1] / price_series.iloc[0] - 1)
        metrics["market_change"] = round(market_change, 4)
        metrics["alpha_vs_buyhold"] = round(metrics["total_return"] - market_change, 4)
    else:
        metrics["market_change"] = None
        metrics["alpha_vs_buyhold"] = None

    if len(trades_df) == 0:
        metrics.update(
            {
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "expectancy": 0.0,
                "expectancy_ratio": 0.0,
                "total_trades": 0,
                "n_wins": 0,
                "n_draws": 0,
                "n_losses": 0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "avg_duration_days": 0.0,
                "avg_win_duration_days": 0.0,
                "avg_loss_duration_days": 0.0,
                "max_win_streak": 0,
                "max_loss_streak": 0,
            }
        )
        return metrics

    pnl = trades_df["profit_loss_net"]
    win_trades = trades_df[pnl > 0]
    lose_trades = trades_df[pnl < 0]
    draw_trades = trades_df[pnl == 0]
    gross_profit = win_trades["profit_loss_net"].sum()
    gross_loss = abs(lose_trades["profit_loss_net"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    avg_win = win_trades["profit_loss_net"].mean() if len(win_trades) else 0.0
    avg_loss = abs(lose_trades["profit_loss_net"].mean()) if len(lose_trades) else 0.0
    win_rate = len(win_trades) / len(trades_df)
    loss_rate = len(lose_trades) / len(trades_df)
    expectancy = win_rate * avg_win - loss_rate * avg_loss
    expectancy_ratio = expectancy / avg_loss if avg_loss > 0 else 0.0

    # Holding durations (calendar days)
    entry_dt = pd.to_datetime(trades_df["entry_date"])
    exit_dt = pd.to_datetime(trades_df["exit_date"])
    durations = (exit_dt - entry_dt).dt.days
    win_durations = durations[pnl > 0]
    loss_durations = durations[pnl <= 0]

    # Longest winning / losing streak (freqtrade max_consecutive_*)
    max_win_streak = max_loss_streak = cur_win = cur_loss = 0
    for value in pnl:
        if value > 0:
            cur_win += 1
            cur_loss = 0
        elif value < 0:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = cur_loss = 0
        max_win_streak = max(max_win_streak, cur_win)
        max_loss_streak = max(max_loss_streak, cur_loss)

    metrics.update(
        {
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 4) if np.isfinite(profit_factor) else profit_factor,
            "expectancy": round(expectancy, 2),
            "expectancy_ratio": round(expectancy_ratio, 4),
            "total_trades": len(trades_df),
            "n_wins": len(win_trades),
            "n_draws": len(draw_trades),
            "n_losses": len(lose_trades),
            "best_trade": round(float(pnl.max()), 2),
            "worst_trade": round(float(pnl.min()), 2),
            "avg_duration_days": round(float(durations.mean()), 1),
            "avg_win_duration_days": round(float(win_durations.mean()), 1) if len(win_durations) else 0.0,
            "avg_loss_duration_days": round(float(loss_durations.mean()), 1) if len(loss_durations) else 0.0,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
        }
    )
    return metrics


# ===================== Value formatting helpers =====================
def _fmt_pct(value: Any, digits: int = 2) -> str:
    """Format a 0-1 ratio as percentage string; handles None / NaN / inf."""
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, float) and (value == float("inf") or value == float("-inf")):
        return "infinity"
    return f"{value * 100:.{digits}f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, float) and (value == float("inf") or value == float("-inf")):
        return "infinity"
    return f"{value:.{digits}f}"


def _fmt_money(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"¥{value:,.{digits}f}"


# ===================== Bokeh component builders =====================
def _build_equity_plot(equity_df: pd.DataFrame) -> Any:
    """Build a Bokeh column layout: equity curve above + drawdown below."""
    df = equity_df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["cum_max"] = df["equity"].cummax()
    df["drawdown_pct"] = (df["equity"] - df["cum_max"]) / df["cum_max"] * 100.0

    source = ColumnDataSource(df)

    # ---- Equity curve ----
    p_eq = figure(
        x_axis_type="datetime",
        height=350,
        sizing_mode="stretch_width",
        title="Equity Curve",
        tools="pan,wheel_zoom,box_zoom,reset,save",
        active_scroll="wheel_zoom",
        toolbar_location="above",
    )
    p_eq.toolbar.logo = None
    p_eq.line("datetime", "equity", source=source, line_width=2, color="#2E86AB")
    p_eq.yaxis.axis_label = "Equity (¥)"
    p_eq.yaxis.formatter = NumeralTickFormatter(format="0,0.00")
    p_eq.xaxis.formatter = DatetimeTickFormatter(days="%Y-%m-%d")
    p_eq.grid.grid_line_alpha = 0.3
    p_eq.add_tools(
        HoverTool(
            tooltips=[("Date", "@datetime{%F}"), ("Equity", "@equity{0,0.00}")],
            formatters={"@datetime": "datetime"},
        )
    )

    # ---- Drawdown (linked x-axis) ----
    p_dd = figure(
        x_axis_type="datetime",
        height=180,
        sizing_mode="stretch_width",
        title="Drawdown",
        tools="pan,wheel_zoom,box_zoom,reset,save",
        active_scroll="wheel_zoom",
        toolbar_location="above",
        x_range=p_eq.x_range,
    )
    p_dd.toolbar.logo = None
    p_dd.varea(
        "datetime",
        y1=0,
        y2="drawdown_pct",
        source=source,
        color="#A23B72",
        alpha=0.5,
    )
    p_dd.line("datetime", "drawdown_pct", source=source, line_width=1, color="#A23B72")
    p_dd.yaxis.axis_label = "Drawdown (%)"
    p_dd.yaxis.formatter = NumeralTickFormatter(format="0.0")
    p_dd.xaxis.formatter = DatetimeTickFormatter(days="%Y-%m-%d")
    p_dd.grid.grid_line_alpha = 0.3
    p_dd.add_tools(
        HoverTool(
            tooltips=[("Date", "@datetime{%F}"), ("Drawdown", "@drawdown_pct{0.00}%")],
            formatters={"@datetime": "datetime"},
        )
    )

    return column(p_eq, p_dd, sizing_mode="stretch_width")


# ===================== Analyzer flattening =====================
# backtrader uses MAXINT (2**63-1) as the "empty set" sentinel, e.g.
# TradeAnalyzer.len.{short,...}.min for groups with no trades
_ANALYZER_EMPTY_SENTINELS = (MAXINT,)


def _is_empty_sentinel(value: Any) -> bool:
    """Whether an analyzer scalar is backtrader's MAXINT empty-set placeholder."""
    return isinstance(value, int) and not isinstance(value, bool) and value in _ANALYZER_EMPTY_SENTINELS


def _flatten_analyzer(obj: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    """Recursively flatten analyzer result into a list of (path, value) pairs."""
    items: List[Tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            items.extend(_flatten_analyzer(v, prefix + str(k) + "."))
    elif hasattr(obj, "_fields"):  # namedtuple
        for f in obj._fields:
            v = getattr(obj, f)
            items.extend(_flatten_analyzer(v, prefix + f + "."))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            items.extend(_flatten_analyzer(v, prefix + str(i) + "."))
    else:
        path = prefix.rstrip(".")
        items.append((path, obj))
    return items


# ===================== HTML section builders =====================
def _build_signals_fills_html(action_df: Optional[pd.DataFrame]) -> str:
    """Build the unified Signals & Fills table from the integrated action log.

    One row per signal with both trigger-side info (signal date / trigger close /
    intended size / reason) and actual execution info (status / fill date / fill
    price / filled size / avg cost / net P&L), so timing analysis and real
    results can be read from a single table.
    """
    if action_df is None or len(action_df) == 0:
        return "<p class='empty'>No signals recorded.</p>"

    df = action_df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df["exec_date"] = pd.to_datetime(df["exec_date"]).dt.strftime("%Y-%m-%d")
    df["reason"] = df["reason"].fillna("")

    def _text(value, spec="{:.2f}"):
        return "" if pd.isna(value) else spec.format(value)

    def _int_text(value):
        return "" if pd.isna(value) else f"{int(value)}"

    def _money_text(value):
        return "" if pd.isna(value) else f"{value:+,.2f}"

    def _pct_text(value):
        return "" if pd.isna(value) else f"{value:+.2%}"

    rows = []
    for _, r in df.iterrows():
        side = str(r.get("side", ""))
        side_class = "side-buy" if side == "BUY" else "side-sell" if side == "SELL" else ""
        status = str(r.get("status", ""))
        status_class = {"FILLED": "status-filled", "EXPIRED": "status-expired", "PENDING": "status-pending"}.get(status, "status-failed")
        profit_txt = "" if pd.isna(r["net_profit_loss"]) else f"{_money_text(r['net_profit_loss'])} ({_pct_text(r['net_return'])})"
        profit_cell = f"<td>{profit_txt}</td>"
        rows.append(
            f"<tr>"
            f"<td>{r['date']}</td>"
            f"<td class='{side_class}'>{side}</td>"
            f"<td>{_text(r['trigger_price'])}</td>"
            f"<td>{_int_text(r['size'])}</td>"
            f"<td>{r['reason']}</td>"
            f"<td class='{status_class}'>{status}</td>"
            f"<td>{r['exec_date'] if not pd.isna(r['exec_date']) else ''}</td>"
            f"<td>{_text(r['exec_price'])}</td>"
            f"<td>{_int_text(r['exec_size'])}</td>"
            f"<td>{_text(r['avg_cost'])}</td>"
            f"{profit_cell}"
            f"</tr>"
        )
    headers = (
        "<th>Signal Date</th><th>Side</th><th>Trigger ¥</th><th>Size</th><th>Reason</th>"
        "<th>Status</th><th>Fill Date</th><th>Fill ¥</th><th>Filled</th><th>Avg Cost</th><th>Net P&L ¥ (Ret)</th>"
    )
    return f"<table class='data-table'><thead><tr>{headers}</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _build_analyzer_table_html(analyzer_results: Dict[str, Any]) -> str:
    """Build an HTML table that flattens every analyzer's get_analysis() output."""
    if not analyzer_results:
        return "<p class='empty'>No analyzers registered.</p>"

    rows = []
    for name, analysis in analyzer_results.items():
        flat = _flatten_analyzer(analysis)
        if not flat:
            rows.append(f"<tr><td>{name}</td><td>(empty)</td><td></td></tr>")
            continue
        for path, value in flat:
            # MAXINT marks empty groups (e.g. no short trades); show as N/A
            if value is None or _is_empty_sentinel(value):
                value = "N/A"
            rows.append(f"<tr><td>{name}</td><td>{path}</td><td>{value}</td></tr>")
    return "<table class='data-table'><thead><tr><th>Analyzer</th><th>Field</th><th>Value</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _build_exit_reason_table_html(exit_reason_df: pd.DataFrame) -> str:
    """freqtrade-style EXIT REASON STATS table."""
    if len(exit_reason_df) == 0:
        return "<p class='empty'>No closed trades.</p>"
    rows = []
    for _, r in exit_reason_df.iterrows():
        rows.append(
            "<tr>"
            f"<td>{r['exit_reason']}</td>"
            f"<td>{int(r['positions'])}</td>"
            f"<td>{_fmt_pct(r['win_rate'])}</td>"
            f"<td>{_fmt_num(r['avg_duration_days'], 1)}d</td>"
            f"<td class='{'positive' if r['profit_total'] >= 0 else 'negative'}'>{_fmt_money(r['profit_total'])}</td>"
            f"<td class='{'positive' if r['profit_total_pct'] >= 0 else 'negative'}'>{_fmt_pct(r['profit_total_pct'])}</td>"
            "</tr>"
        )
    return (
        "<table class='data-table'><thead><tr>"
        "<th>Exit Reason</th><th>Positions</th><th>Win Rate</th><th>Avg Duration</th>"
        "<th>Profit Total (net)</th><th>Profit Sum %</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _build_strategy_summary_html(metrics: dict) -> str:
    """freqtrade SUMMARY METRICS as a two-column key/value table."""
    def signed_pct(value, digits=2):
        if value is None or pd.isna(value):
            return "N/A"
        css = "positive" if value >= 0 else "negative"
        return f"<span class='{css}'>{_fmt_pct(value, digits)}</span>"

    def signed_money(value, digits=2):
        if value is None or pd.isna(value):
            return "N/A"
        css = "positive" if value >= 0 else "negative"
        return f"<span class='{css}'>{_fmt_money(value, digits)}</span>"

    rows = [
        ("Total / Win / Draw / Loss", f"{metrics['total_trades']} / {metrics['n_wins']} / {metrics['n_draws']} / {metrics['n_losses']}"),
        ("Win Rate", _fmt_pct(metrics["win_rate"])),
        ("Profit Factor", _fmt_num(metrics["profit_factor"])),
        ("Expectancy / Ratio", f"{signed_money(metrics['expectancy'])} / {_fmt_num(metrics['expectancy_ratio'])}"),
        ("Best / Worst Trade", f"{signed_money(metrics['best_trade'])} / {signed_money(metrics['worst_trade'])}"),
        ("Avg Duration (all / win / loss)", f"{metrics['avg_duration_days']}d / {metrics['avg_win_duration_days']}d / {metrics['avg_loss_duration_days']}d"),
        ("Max Consecutive Win / Loss", f"{metrics['max_win_streak']} / {metrics['max_loss_streak']}"),
        ("Best / Worst Day", f"{signed_pct(metrics['best_day'])} / {signed_pct(metrics['worst_day'])}"),
        ("Daily Win / Loss / Flat", f"{metrics['winning_days']} / {metrics['losing_days']} / {metrics['zero_days']}"),
        ("Sharpe / Sortino / Calmar", f"{_fmt_num(metrics['sharpe_ratio'])} / {_fmt_num(metrics['sortino_ratio'])} / {_fmt_num(metrics['calmar_ratio'])}"),
        ("Buy &amp; Hold (market change)", signed_pct(metrics.get("market_change"))),
        ("Alpha vs Buy &amp; Hold", signed_pct(metrics.get("alpha_vs_buyhold"))),
    ]
    body = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows)
    return "<table class='data-table summary-table'><tbody>" + body + "</tbody></table>"


def _build_monthly_returns_html(equity_df: pd.DataFrame) -> str:
    """Monthly returns heatmap table (year rows x 12 month columns)."""
    pivot = _monthly_returns(equity_df)
    if pivot.empty:
        return "<p class='empty'>Not enough data for monthly returns.</p>"
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    head = "<th>Year</th>" + "".join(f"<th>{m}</th>" for m in month_labels)
    body_rows = []
    for year, row in pivot.iterrows():
        cells = [f"<td class='year-cell'>{int(year)}</td>"]
        for month in range(1, 13):
            value = row.get(month)
            if pd.isna(value):
                cells.append("<td class='month-cell na'>&mdash;</td>")
            else:
                # Heatmap intensity: stronger color for larger |return|, capped at 15%
                intensity = min(abs(value) / 0.15, 1.0)
                alpha = 0.15 + 0.45 * intensity
                bg = f"rgba(87,204,153,{alpha:.2f})" if value >= 0 else f"rgba(243,129,129,{alpha:.2f})"
                cells.append(f"<td class='month-cell' style='background:{bg}'>{value * 100:.1f}%</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<table class='data-table monthly-table'><thead><tr>" + head + "</tr></thead><tbody>"
        + "".join(body_rows) + "</tbody></table>"
    )


def build_console_summary(metrics: dict, trades_df: pd.DataFrame) -> str:
    """freqtrade-style multi-line console summary."""
    def pct(value):
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            return "N/A" if value is None else "inf"
        return f"{value * 100:.2f}%"

    exit_df = summarize_exit_reasons(trades_df)
    exit_lines = "  (no closed trades)"
    if len(exit_df):
        exit_lines = "\n".join(
            f"    {r['exit_reason']:<16} {int(r['positions']):>3}  win {pct(r['win_rate']):>7}  "
            f"avg {r['avg_duration_days']:>5.1f}d  pnl {r['profit_total']:>10.2f}  ({pct(r['profit_total_pct'])})"
            for _, r in exit_df.iterrows()
        )

    market = metrics.get("market_change")
    market_line = f"\n  Buy & Hold:                 {pct(market):>9}    Alpha vs B&H: {pct(metrics.get('alpha_vs_buyhold'))}" if market is not None else ""

    return (
        "============ STRATEGY SUMMARY (freqtrade-style) ============\n"
        f"  Total / Win / Draw / Loss:  {metrics['total_trades']} / {metrics['n_wins']} / {metrics['n_draws']} / {metrics['n_losses']}\n"
        f"  Win Rate:                   {pct(metrics['win_rate']):>9}    Profit Factor: {_fmt_num(metrics['profit_factor'])}\n"
        f"  Expectancy / Ratio:         {metrics['expectancy']:>9.2f}    {_fmt_num(metrics['expectancy_ratio'])}\n"
        f"  Best / Worst Trade:         {metrics['best_trade']:>9.2f}    {metrics['worst_trade']:.2f}\n"
        f"  Avg Duration all/win/loss:  {metrics['avg_duration_days']:.1f}d / {metrics['avg_win_duration_days']:.1f}d / {metrics['avg_loss_duration_days']:.1f}d\n"
        f"  Max Consecutive W/L:        {metrics['max_win_streak']} / {metrics['max_loss_streak']}\n"
        f"  Best / Worst Day:           {pct(metrics['best_day']):>9}    {pct(metrics['worst_day'])}\n"
        f"  Days Win/Loss/Flat:         {metrics['winning_days']} / {metrics['losing_days']} / {metrics['zero_days']}\n"
        f"  Sharpe / Sortino / Calmar:  {_fmt_num(metrics['sharpe_ratio'])} / {_fmt_num(metrics['sortino_ratio'])} / {_fmt_num(metrics['calmar_ratio'])}"
        f"{market_line}\n"
        "  ---- Exit Reason Stats ----\n"
        f"{exit_lines}\n"
        "============================================================"
    )


# ===================== Public API =====================
def render_report(
    equity_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    action_df: Optional[pd.DataFrame],
    metrics: dict,
    analyzer_results: Dict[str, Any],
    out_dir,
    code: str,
    strategy_name: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
    final_value: float,
    interactive_html: Optional[str] = None,
    stock_name: Optional[str] = None,
) -> str:
    """Render a self-contained HTML backtest report.

    Output: {out_dir}/{code}_{strategy_name}_report.html
    Returns the output path.
    """
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{code}_{strategy_name}_report.html")

    # ---- Build Bokeh components ----
    equity_plot = _build_equity_plot(equity_df)
    script, divs = components({"equity_plot": equity_plot})

    # ---- freqtrade-aligned tables ----
    exit_reason_df = summarize_exit_reasons(trades_df)
    exit_reason_html = _build_exit_reason_table_html(exit_reason_df)
    strategy_summary_html = _build_strategy_summary_html(metrics)
    monthly_html = _build_monthly_returns_html(equity_df)

    # ---- Build HTML content sections ----
    name_display = f"{stock_name} " if stock_name else ""
    total_return = (final_value / initial_capital - 1) if initial_capital else 0.0
    total_return_color = "#57CC99" if total_return >= 0 else "#F38181"

    kpi_cards = f"""
    <div class="kpi-card">
      <div class="kpi-label">Annual Return</div>
      <div class="kpi-value {"positive" if metrics["annual_return"] >= 0 else "negative"}">{_fmt_pct(metrics["annual_return"])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Max Drawdown</div>
      <div class="kpi-value negative">{_fmt_pct(metrics["max_drawdown"])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Sharpe</div>
      <div class="kpi-value">{_fmt_num(metrics["sharpe_ratio"])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Sortino</div>
      <div class="kpi-value">{_fmt_num(metrics["sortino_ratio"])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Calmar</div>
      <div class="kpi-value">{_fmt_num(metrics["calmar_ratio"])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Win Rate</div>
      <div class="kpi-value">{_fmt_pct(metrics["win_rate"])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Profit Factor</div>
      <div class="kpi-value">{_fmt_num(metrics["profit_factor"])}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Total Trades</div>
      <div class="kpi-value">{int(metrics["total_trades"])}</div>
    </div>
    """

    signals_fills_html = _build_signals_fills_html(action_df)
    analyzer_html = _build_analyzer_table_html(analyzer_results)

    interactive_link = ""
    if interactive_html:
        rel = os.path.basename(interactive_html)
        interactive_link = f'<div class="section"><div class="section-title">Interactive K-Line Chart</div><p><a href="{rel}" class="btn">Open Plotly trades chart &raquo;</a></p></div>'

    equity_div = divs.get("equity_plot", "")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SkyQuant Report - {code} {strategy_name}</title>
{INLINE.render()}
<style>
* {{ box-sizing: border-box; }}
body {{
  background: #0f1419;
  color: #d4d4d4;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Roboto, Helvetica, Arial, sans-serif;
  margin: 0; padding: 0; line-height: 1.6;
}}
.container {{ max-width: 1280px; margin: 0 auto; padding: 30px 40px; }}
.header {{ border-bottom: 3px solid #2E86AB; padding-bottom: 20px; margin-bottom: 30px; }}
.header h1 {{ margin: 0 0 8px 0; font-size: 28px; color: #fff; }}
.header .stock-info {{ font-size: 20px; color: #2E86AB; font-weight: 500; }}
.header .meta {{ font-size: 13px; color: #888; margin-top: 6px; }}
.header .meta span {{ margin-right: 18px; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 30px; }}
.kpi-card {{ background: #1a2332; border: 1px solid #2a3548; border-radius: 8px; padding: 14px; text-align: center; }}
.kpi-label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
.kpi-value {{ font-size: 24px; font-weight: 600; margin-top: 6px; color: #fff; }}
.kpi-value.positive {{ color: #57CC99; }}
.kpi-value.negative {{ color: #F38181; }}
.section {{ margin-bottom: 30px; }}
.section-title {{ font-size: 16px; font-weight: 600; color: #fff; border-bottom: 1px solid #2a3548; padding-bottom: 6px; margin-bottom: 12px; }}
.empty {{ color: #888; font-style: italic; padding: 12px; }}
.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.data-table th, .data-table td {{ padding: 8px 10px; border-bottom: 1px solid #2a3548; text-align: left; }}
.data-table th {{ background: #1a2332; color: #d4d4d4; font-weight: 600; }}
.data-table tr:hover {{ background: #1a2332; }}
.side-buy {{ color: #57CC99; font-weight: 600; }}
.side-sell {{ color: #F38181; font-weight: 600; }}
.status-filled {{ color: #57CC99; }}
.status-expired {{ color: #f0ad4e; }}
.status-pending {{ color: #888; }}
.status-failed {{ color: #F38181; }}
.btn {{ display: inline-block; padding: 8px 16px; background: #2E86AB; color: #fff !important; text-decoration: none; border-radius: 4px; font-size: 13px; }}
.btn:hover {{ background: #1f6a8b; }}
.summary-table th {{ width: 32%; background: #141c28; color: #9fb3c8; font-weight: 500; }}
.monthly-table th, .monthly-table td {{ text-align: center; font-size: 12px; padding: 6px 4px; }}
.monthly-table .year-cell, .monthly-table th:first-child {{ text-align: left; font-weight: 600; background: #1a2332; }}
.monthly-table .month-cell.na {{ color: #555; }}
.bk-pane {{ background: transparent !important; }}
footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #2a3548; font-size: 12px; color: #666; }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>SkyQuant Backtest Report</h1>
    <div class="stock-info">{name_display}({code}) &mdash; Strategy: {strategy_name}</div>
    <div class="meta">
      <span>Period: {start_date} &rarr; {end_date}</span>
      <span>Initial Capital: {_fmt_money(initial_capital)}</span>
      <span>Final Value: {_fmt_money(final_value)}</span>
      <span>Total Return: <strong style="color: {total_return_color};">{_fmt_pct(total_return)}</strong></span>
    </div>
  </div>

  <div class="kpi-grid">{kpi_cards}</div>

  <div class="section">
    <div class="section-title">Strategy Summary</div>
    {strategy_summary_html}
  </div>

  <div class="section">
    <div class="section-title">Equity Curve &amp; Drawdown</div>
    {equity_div}
  </div>

  <div class="section">
    <div class="section-title">Exit Reason Stats</div>
    {exit_reason_html}
  </div>

  <div class="section">
    <div class="section-title">Monthly Returns</div>
    {monthly_html}
  </div>

  <div class="section">
    <div class="section-title">Signals &amp; Fills (trigger signal vs actual execution)</div>
    {signals_fills_html}
  </div>

  <div class="section">
    <div class="section-title">Analyzer Breakdown</div>
    {analyzer_html}
  </div>

  {interactive_link}

  <footer>Generated by SkyQuant &middot; Report path: {out_path}</footer>

</div>
{script}
</body>
</html>
"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def _line_to_numpy(line: Any, n: int) -> np.ndarray:
    """Extract a backtrader line buffer aligned to the first n data bars.

    Indicator/line buffers share the data length after cerebro.run(); shorter
    buffers (unexpected) are left-padded with NaN so x alignment stays correct.
    """
    values = np.asarray(line.array, dtype=float)
    if len(values) == n:
        return values
    if len(values) > n:
        return values[:n]
    return np.concatenate([np.full(n - len(values), np.nan), values])


def _compute_macd_from_close(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Compute MACD triple from a close-price series (freqtrade/talib convention).

    Returns (macd, macdsignal, macdhist) numpy arrays. Used for strategies that do
    not instantiate MACD (range/breakout) so every interactive chart has a MACD row.
    EMA uses adjust=False (recursive), matching talib.EMA semantics.
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    macdsignal = macd.ewm(span=signal, adjust=False).mean()
    macdhist = macd - macdsignal
    return macd.to_numpy(), macdsignal.to_numpy(), macdhist.to_numpy()


def render_interactive_chart(strategy, out_dir, code, strategy_name, price_df, trades_df, stock_name=None):
    """Render a freqtrade-style interactive Plotly chart (after cerebro.run).

    Rows: candlesticks + strategy overlay indicators + entry/exit markers and
    trade connectors; volume; ATR; MACD (all strategies, indicator lines when the
    strategy owns them else computed from close) and ADX row for the trend strategy.
    Self-contained HTML (plotly.js inlined, openable offline).
    Returns the output path.
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, f"{code}_{strategy_name}_interactive.html")

    df = price_df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    n = len(df)
    x = df["datetime"]

    # ---- MACD triple: freqtrade names macd / macdsignal / macdhist ----
    # trend instantiates the indicator; range/breakout get MACD computed from close
    # purely for chart display (does not affect strategy logic).
    if hasattr(strategy, "macd") and hasattr(strategy, "macdhist"):
        macd_vals = _line_to_numpy(strategy.macd, n)
        macdsignal_vals = _line_to_numpy(strategy.macdsignal, n)
        macdhist_vals = _line_to_numpy(strategy.macdhist, n)
    else:
        macd_vals, macdsignal_vals, macdhist_vals = _compute_macd_from_close(df["close"])
    has_dmi = hasattr(strategy, "adx")

    # ---- Subplot layout: MACD row exists for every strategy; ADX only for trend ----
    row_of = {"price": 1, "volume": 2, "atr": 3, "macd": 4}
    weights = [0.46, 0.12, 0.11, 0.12]
    if has_dmi:
        row_of["adx"] = len(row_of) + 1
        weights.append(0.12)
    total_weight = sum(weights)
    row_heights = [w / total_weight for w in weights]

    fig = make_subplots(
        rows=len(row_of),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
    )

    # ---- Candlesticks: A-share convention red up / green down ----
    up_color, down_color = "#F04848", "#0EAE7C"
    fig.add_trace(
        go.Candlestick(
            x=x,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="OHLC",
            increasing_line_color=up_color,
            increasing_fillcolor=up_color,
            decreasing_line_color=down_color,
            decreasing_fillcolor=down_color,
            whiskerwidth=0.6,
            hoverlabel=dict(namelength=-1),
        ),
        row=1,
        col=1,
    )

    # ---- Strategy overlay indicators ----
    def add_overlay(line, name, color, dash="solid", width=1.3):
        fig.add_trace(
            go.Scatter(x=x, y=_line_to_numpy(line, n), mode="lines", name=name,
                       line=dict(color=color, width=width, dash=dash)),
            row=1, col=1,
        )

    if hasattr(strategy, "ema_fast_line"):
        add_overlay(strategy.ema_fast_line, f"EMA{strategy.p.ema_fast}", "#4FC3F7")
        add_overlay(strategy.ema_slow_line, f"EMA{strategy.p.ema_slow}", "#F5B041")
    if hasattr(strategy, "bb_upperband"):
        add_overlay(strategy.bb_upperband, "BB upper", "#AB7DF6", width=1.1)
        add_overlay(strategy.bb_middleband, "BB mid", "#9AA7B8", dash="dash", width=1.0)
        add_overlay(strategy.bb_lowerband, "BB lower", "#AB7DF6", width=1.1)
    if hasattr(strategy, "donchian_upper"):
        add_overlay(strategy.donchian_upper, f"Donchian upper {strategy.p.breakout_period}", "#4FC3F7", width=1.1)
        add_overlay(strategy.donchian_lower, f"Donchian lower {strategy.p.breakout_period}", "#F5B041", width=1.1)

    # ---- Trade entry/exit markers (freqtrade plot-trades style) ----
    row_by_date = {d: i for i, d in enumerate(x.dt.date)}
    entry_x, entry_y, entry_text = [], [], []
    win_x, win_y, win_text = [], [], []
    loss_x, loss_y, loss_text = [], [], []
    win_conn_x, win_conn_y, loss_conn_x, loss_conn_y = [], [], [], []

    def connector_append(target_x, target_y, x0, y0, x1, y1):
        # None breaks the segment so one trace draws many disjoint lines
        target_x.extend([x0, x1, None])
        target_y.extend([y0, y1, None])

    if len(trades_df):
        for _, trade in trades_df.iterrows():
            entry_date = pd.to_datetime(trade["entry_date"]).date()
            exit_date = pd.to_datetime(trade["exit_date"]).date()
            entry_idx = row_by_date.get(entry_date)
            exit_idx = row_by_date.get(exit_date)
            if entry_idx is None or exit_idx is None:
                continue
            entry_low = df["low"].iloc[entry_idx]
            exit_high = df["high"].iloc[exit_idx]
            pnl_net = float(trade["profit_loss_net"])
            rate = float(trade["profit_rate"])
            category = normalize_exit_reason(trade.get("exit_reason"))
            win = pnl_net > 0
            entry_x.append(x.iloc[entry_idx])
            entry_y.append(entry_low * 0.985)
            entry_text.append(f"ENTRY {entry_date}<br>price {trade['entry_price']:.2f}  size {int(trade['size'])}")
            exit_x_val = x.iloc[exit_idx]
            exit_y_val = exit_high * 1.015
            hover = (
                f"EXIT {exit_date} [{category}]<br>price {trade['exit_price']:.2f}<br>"
                f"pnl {pnl_net:,.2f} ({rate * 100:.2f}%)"
            )
            if win:
                win_x.append(exit_x_val)
                win_y.append(exit_y_val)
                win_text.append(hover)
                connector_append(win_conn_x, win_conn_y, x.iloc[entry_idx], trade["entry_price"], exit_x_val, trade["exit_price"])
            else:
                loss_x.append(exit_x_val)
                loss_y.append(exit_y_val)
                loss_text.append(hover)
                connector_append(loss_conn_x, loss_conn_y, x.iloc[entry_idx], trade["entry_price"], exit_x_val, trade["exit_price"])

    fig.add_trace(go.Scatter(x=entry_x, y=entry_y, text=entry_text, hovertext=entry_text,
                             hoverinfo="text", mode="markers", name="entry",
                             marker=dict(symbol="triangle-up", size=12, color="#4FC3F7",
                                         line=dict(color="#0B2537", width=0.8))), row=1, col=1)
    fig.add_trace(go.Scatter(x=win_x, y=win_y, text=win_text, hovertext=win_text,
                             hoverinfo="text", mode="markers", name="exit win",
                             marker=dict(symbol="triangle-down", size=12, color="#57CC99",
                                         line=dict(color="#0B2B1F", width=0.8))), row=1, col=1)
    fig.add_trace(go.Scatter(x=loss_x, y=loss_y, hovertext=loss_text, hoverinfo="text",
                             mode="markers", name="exit loss",
                             marker=dict(symbol="triangle-down", size=12, color="#F38181",
                                         line=dict(color="#3A1414", width=0.8))), row=1, col=1)
    if win_conn_x:
        fig.add_trace(go.Scatter(x=win_conn_x, y=win_conn_y, mode="lines", name="win trade",
                                 line=dict(color="#57CC99", width=1, dash="dot"), hoverinfo="skip",
                                 opacity=0.55), row=1, col=1)
    if loss_conn_x:
        fig.add_trace(go.Scatter(x=loss_conn_x, y=loss_conn_y, mode="lines", name="loss trade",
                                 line=dict(color="#F38181", width=1, dash="dot"), hoverinfo="skip",
                                 opacity=0.55), row=1, col=1)

    # Open position at the end of the run (freqtrade plots an open-trade marker)
    if getattr(strategy, "_hold_size", 0) and strategy.entry_bar is not None and strategy.entry_bar < n:
        open_x = x.iloc[strategy.entry_bar]
        fig.add_trace(
            go.Scatter(x=[open_x], y=[df["low"].iloc[strategy.entry_bar] * 0.985],
                       hovertext=(f"OPEN POSITION<br>entry {open_x.date()}<br>"
                                  f"price {strategy.entry_price:.2f}  size {int(strategy._hold_size)}"),
                       hoverinfo="text", mode="markers", name="open position",
                       marker=dict(symbol="triangle-up", size=13, color="#F5B041",
                                   line=dict(color="#3A2C0B", width=0.8))),
            row=1, col=1,
        )

    # ---- Volume ----
    vol_colors = np.where(df["close"] >= df["open"], up_color, down_color)
    fig.add_trace(go.Bar(x=x, y=df["volume"], name="volume", marker_color=vol_colors,
                         opacity=0.55, showlegend=False), row=row_of["volume"], col=1)

    # ---- ATR (base strategy indicator, present for every strategy) ----
    fig.add_trace(go.Scatter(x=x, y=_line_to_numpy(strategy.atr, n), mode="lines", name=f"ATR{strategy.p.atr_period}",
                             line=dict(color="#AB7DF6", width=1.2)), row=row_of["atr"], col=1)

    # ---- MACD (all strategies): freqtrade names macd / macdsignal / macdhist ----
    fig.add_trace(go.Bar(x=x, y=macdhist_vals, name="macdhist",
                         marker_color=np.where(macdhist_vals >= 0, up_color, down_color),
                         opacity=0.55, showlegend=False), row=row_of["macd"], col=1)
    fig.add_trace(go.Scatter(x=x, y=macd_vals, mode="lines", name="macd (DIF)",
                             line=dict(color="#4FC3F7", width=1.2)), row=row_of["macd"], col=1)
    fig.add_trace(go.Scatter(x=x, y=macdsignal_vals, mode="lines", name="macdsignal (DEA)",
                             line=dict(color="#F5B041", width=1.2)), row=row_of["macd"], col=1)

    # ---- ADX / plus_di / minus_di with the adx_min threshold line (trend only) ----
    if has_dmi:
        adx_row = row_of["adx"]
        fig.add_trace(go.Scatter(x=x, y=_line_to_numpy(strategy.adx, n), mode="lines", name="ADX",
                                 line=dict(color="#F5B041", width=1.3)), row=adx_row, col=1)
        fig.add_trace(go.Scatter(x=x, y=_line_to_numpy(strategy.plus_di, n), mode="lines", name="+DI",
                                 line=dict(color="#57CC99", width=1.0)), row=adx_row, col=1)
        fig.add_trace(go.Scatter(x=x, y=_line_to_numpy(strategy.minus_di, n), mode="lines", name="-DI",
                                 line=dict(color="#F38181", width=1.0)), row=adx_row, col=1)
        fig.add_hline(y=float(strategy.p.adx_min), line_dash="dash", line_color="#9AA7B8",
                      line_width=0.8, opacity=0.7, row=adx_row, col=1,
                      annotation_text=f"adx_min {strategy.p.adx_min:g}", annotation_position="top left",
                      annotation_font_color="#9AA7B8", annotation_font_size=10)

    # ---- Styling (dark theme consistent with the HTML report) ----
    name_display = f"{stock_name} " if stock_name else ""
    fig.update_layout(
        title=dict(text=f"{name_display}({code}) · {strategy_name} · Interactive Trades Chart",
                   font=dict(size=18, color="#FFFFFF"), x=0.01, y=0.985, yanchor="top"),
        template="plotly_dark",
        paper_bgcolor="#0F1419",
        plot_bgcolor="#141C28",
        font=dict(color="#D4D4D4", size=11),
        height=900 + 170 + 170 * int(has_dmi),
        margin=dict(l=60, r=30, t=120, b=40),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#1A2332", font_size=11),
        legend=dict(orientation="h", yanchor="top", y=0.93, xanchor="left", x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        xaxis_rangeslider_visible=False,
        bargap=0.05,
    )
    for axis_idx in range(1, len(row_of) + 1):
        xaxis_name = "xaxis" if axis_idx == 1 else f"xaxis{axis_idx}"
        fig.update_layout({
            xaxis_name: dict(
                rangebreaks=[dict(bounds=["sat", "mon"])],
                showgrid=True, gridcolor="#2A3548",
                rangeslider=dict(visible=False),
                rangeselector=dict(
                    buttons=list([
                        dict(count=1, label="1M", step="month", stepmode="backward"),
                        dict(count=3, label="3M", step="month", stepmode="backward"),
                        dict(count=6, label="6M", step="month", stepmode="backward"),
                        dict(count=1, label="1Y", step="year", stepmode="backward"),
                        dict(step="all", label="All"),
                    ]),
                    bgcolor="#1A2332", activecolor="#2E86AB", font=dict(color="#D4D4D4"),
                ) if axis_idx == 1 else None,
            ),
        })
    for axis_idx in range(1, len(row_of) + 1):
        fig.update_yaxes(gridcolor="#2A3548", zerolinecolor="#2A3548", row=axis_idx, col=1)

    fig.write_html(
        html_path,
        include_plotlyjs=True,
        full_html=True,
        config={"scrollZoom": True, "displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]},
    )
    return html_path
