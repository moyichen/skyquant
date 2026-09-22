import os

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import pandas as pd


def _setup_chinese_font():
    """Dynamically select an installed CJK font to avoid hardcoding SimHei and triggering findfont warnings"""
    available = {f.name for f in fm.fontManager.ttflist}
    # Try CJK fonts by cross-platform priority
    candidates = [
        "SimHei",  # Windows
        "Microsoft YaHei",  # Windows
        "Heiti SC",  # macOS
        "PingFang SC",  # macOS
        "STHeiti",  # macOS
        "Arial Unicode MS",  # macOS/Office
        "Noto Sans CJK SC",  # Linux
        "WenQuanYi Zen Hei",  # Linux
    ]
    chosen = next((f for f in candidates if f in available), "DejaVu Sans")
    plt.rcParams["font.sans-serif"] = [chosen, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


_setup_chinese_font()


def plot_equity_drawdown(equity_df: pd.DataFrame, metrics: dict, save_path: str):
    """Equity curve + drawdown curve"""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    equity_df["datetime"] = pd.to_datetime(equity_df["datetime"])
    equity_df["cum_max"] = equity_df["equity"].cummax()
    equity_df["drawdown"] = (equity_df["equity"] - equity_df["cum_max"]) / equity_df[
        "cum_max"
    ]

    ax1.plot(
        equity_df["datetime"], equity_df["equity"], color="#2E86AB", label="Equity"
    )
    ax1.set_title(
        f"Equity Curve | Annual:{metrics['annual_return']:.2%} Sharpe:{metrics['sharpe_ratio']:.2f} Max Drawdown:{metrics['max_drawdown']:.2%}"
    )
    ax1.set_ylabel("Assets")
    ax1.legend()
    ax1.grid(alpha=0.3)

    ax2.plot(
        equity_df["datetime"], equity_df["drawdown"], color="#A23B72", label="Drawdown"
    )
    ax2.fill_between(
        equity_df["datetime"], equity_df["drawdown"], 0, color="#A23B72", alpha=0.2
    )
    ax2.set_ylabel("Drawdown")
    ax2.set_xlabel("Date")
    ax2.legend()
    ax2.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()


def plot_win_pie(metrics: dict, save_path: str):
    """Win rate pie chart"""
    win = metrics["win_rate"] * 100
    lose = 100 - win
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.pie(
        [win, lose],
        labels=[f"Profit {win:.1f}%", f"Loss {lose:.1f}%"],
        colors=["#57CC99", "#F38181"],
        autopct="%.1f%%",
    )
    ax.set_title(
        f"Trade Win Rate Pie | Total Trades:{metrics['total_trades']},Profit Factor:{metrics['profit_factor']:.2f}"
    )
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()


def plot_all(equity_df, trades_df, metrics, out_dir, code, strategy_name):
    os.makedirs(out_dir, exist_ok=True)
    eq_path = os.path.join(out_dir, f"{code}_{strategy_name}_equity_dd.png")
    pie_path = os.path.join(out_dir, f"{code}_{strategy_name}_win_pie.png")
    plot_equity_drawdown(equity_df, metrics, eq_path)
    plot_win_pie(metrics, pie_path)
    return eq_path, pie_path
