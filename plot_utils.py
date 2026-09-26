"""Plotting and reporting utilities for SkyQuant backtests.

Exports:
    render_interactive_chart: btplotting-based K-line + indicators HTML chart
    render_report:            self-contained HTML backtest report (KPIs,
                              equity curve, drawdown, trades, analyzers)
"""

import os
import warnings
from typing import Any, Dict, List, Optional, Tuple

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


def _replace_empty_sentinels(obj: Any) -> None:
    """Recursively replace MAXINT placeholders inside analyzer results with None.

    Operates in place on dict-like analyzer outputs (AutoOrderedDict). btplotting
    renders analyzer tables from the same live objects; without this the sentinel
    reaches bokeh serialization as an integer beyond the JS safe range and emits
    "out of range integer may result in loss of precision".
    """
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            value = obj[key]
            if isinstance(value, dict):
                _replace_empty_sentinels(value)
            elif _is_empty_sentinel(value):
                obj[key] = None


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
      <div class="kpi-label">Sharpe Ratio</div>
      <div class="kpi-value">{_fmt_num(metrics["sharpe_ratio"])}</div>
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
        interactive_link = f'<div class="section"><div class="section-title">Interactive K-Line Chart</div><p><a href="{rel}" class="btn">Open btplotting K-Line chart &raquo;</a></p></div>'

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
.kpi-grid {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 14px; margin-bottom: 30px; }}
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
    <div class="section-title">Equity Curve &amp; Drawdown</div>
    {equity_div}
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


def _build_py314_compatible_plotter_cls():
    """Return a BacktraderPlotting subclass patched for Python 3.14+.

    Upstream generate_bokeh_model filters empty tabs with
    ``list(filter(None.__ne__, tab_panels))``. Since Python 3.14
    ``None.__ne__(obj)`` returns NotImplemented for any obj that defines a
    custom ``__eq__`` (Bokeh models do), ``filter`` evaluates
    ``bool(NotImplemented)`` and raises "NotImplemented should not be used
    in a boolean context". An identity check (``panel is not None``) gives
    the same filtering on every Python version. btplotting is an optional
    dependency, so the subclass is built lazily only when plotting.
    """
    from bokeh.models import Tabs
    from btplotting import BacktraderPlotting

    class Py314CompatibleBacktraderPlotting(BacktraderPlotting):
        def generate_bokeh_model(self, figid=0, use_tabs=True):
            figure_page = self.get_figurepage(figid)
            if use_tabs:
                if figure_page.strategy is not None:
                    tab_panels = self.generate_bokeh_model_tab_panels()
                else:
                    tab_panels = []
                for tab_builder in self.tabs:
                    tab = tab_builder(self, figure_page, None)
                    if tab.is_useable():
                        tab_panels.append(tab.get_tab_panel())
                all_tabs = [panel for panel in tab_panels if panel is not None]
                model = Tabs(tabs=all_tabs, sizing_mode="stretch_width")
            else:
                model = self.generate_bokeh_model_plots()
            figure_page.model = model
            return model

    return Py314CompatibleBacktraderPlotting


def render_interactive_chart(strategy, out_dir, code, strategy_name):
    """Render an interactive Bokeh HTML chart via btplotting (after cerebro.run).

    BacktraderPlotting is not a bt.Analyzer subclass (no _start hook), so it
    cannot be registered via cerebro.addanalyzer. Instead, instantiate it
    after the backtest and call plot(strategy) + show(). With
    output_mode="save" the show() call writes the HTML file without opening
    a browser. Returns the output path for logging.
    """
    try:
        plotter_cls = _build_py314_compatible_plotter_cls()
    except ImportError as exc:
        raise RuntimeError("btplotting is not installed; run `pip install btplotting` to enable interactive charts") from exc
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, f"{code}_{strategy_name}_interactive.html")
    # Extra kwargs are applied onto the scheme (see BacktraderPlotting.__init__):
    # style="candle" renders solid-filled candlesticks instead of a close-price line
    # (btplotting draws the candle body as a filled vbar, so bodies are solid).
    # Classic A-share colors: pure red for up bars, pure green for down bars.
    plotter = plotter_cls(
        filename=html_path,
        output_mode="save",
        style="candle",
        barup="#FF0000",
        bardown="#00A800",
        barup_wick="#FF0000",
        bardown_wick="#00A800",
        barup_outline="#FF0000",
        bardown_outline="#00A800",
    )
    # btplotting reads the live analyzer results for its Analyzers tab. Replace
    # backtrader's MAXINT empty-group sentinels up front so they are rendered as
    # blank cells instead of 2**63-1 integers that bokeh refuses to serialize
    for analyzer_name in strategy.analyzers.getnames():
        _replace_empty_sentinels(strategy.analyzers.getbyname(analyzer_name).get_analysis())
    # btplotting draws trade markers through the deprecated figure.circle/
    # triangle(size=...) glyph helpers (bokeh 3.4+ emits BokehDeprecationWarning).
    # Those calls live inside the third-party package, so silence them at this
    # boundary; the glyph methods still exist on the supported bokeh version.
    from bokeh.util.warnings import BokehDeprecationWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", BokehDeprecationWarning)
        plotter.plot(strategy)
        plotter.show()
    return html_path
