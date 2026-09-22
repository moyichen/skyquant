import numpy as np
import pandas as pd


def calc_metrics(equity_df: pd.DataFrame, trades_df: pd.DataFrame, risk_free_rate=0.02):
    """
    equity_df: datetime,equity
    trades_df: entry_date,exit_date,profit_loss_net (net profit after fees)
    risk_free_rate: annualized risk-free rate 2%
    return dict: annualized return, max drawdown, Sharpe ratio, win rate, profit factor
    """
    equity = equity_df["equity"].values
    dates = pd.to_datetime(equity_df["datetime"])
    # Daily return rate
    equity_df["ret"] = equity_df["equity"].pct_change()
    daily_ret = equity_df["ret"].dropna()

    # 1. Max drawdown
    cum_max = equity_df["equity"].cummax()
    drawdown = (equity_df["equity"] - cum_max) / cum_max
    max_dd = drawdown.min()

    # 2. Annualized return
    days = (dates.iloc[-1] - dates.iloc[0]).days
    total_return = (equity[-1] / equity[0]) - 1
    annual_return = (1 + total_return) ** (365.0 / days) - 1 if days > 0 else 0

    # 3. Sharpe ratio
    daily_rf = (1 + risk_free_rate) ** (1 / 365) - 1
    excess_ret = daily_ret - daily_rf
    sharpe = (
        np.sqrt(252) * excess_ret.mean() / excess_ret.std()
        if excess_ret.std() != 0
        else 0
    )

    # Trade metrics
    if len(trades_df) == 0:
        win_rate = 0
        profit_factor = 0
    else:
        win_trades = trades_df[trades_df["profit_loss_net"] > 0]
        lose_trades = trades_df[trades_df["profit_loss_net"] <= 0]
        win_rate = len(win_trades) / len(trades_df)
        gross_profit = win_trades["profit_loss_net"].sum()
        gross_loss = abs(lose_trades["profit_loss_net"].sum())
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    return {
        "annual_return": round(annual_return, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe_ratio": round(sharpe, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "total_trades": len(trades_df),
    }
