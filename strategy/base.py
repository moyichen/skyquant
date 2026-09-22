# Strategy base class: encapsulates trade records, equity records, ATR risk position sizing shared by all strategies
import backtrader as bt
import pandas as pd


class BaseStrategy(bt.Strategy):
    """
    Base class for all strategies.

    Common logic (implemented here, subclasses need not repeat):
      - notify_order / notify_trade: capture actual execution price/volume and generate standard trade records
      - Daily equity recording
      - ATR-based fixed-risk position sizing and stop price setting
      - get_equity_dataframe / get_trade_dataframe unified output interface

    Subclasses only need to override three hooks:
      - _init_indicators(): initialize strategy-specific indicators
      - _on_entry(): decide whether to open when no position (call _open_position on signal)
      - _on_exit(): decide whether to close when in position (call _close_position on signal)
    """

    params = (
        ("atr_period", 14),  # ATR calculation period
        ("max_risk_ratio", 0.02),  # Max risk per trade as fraction of total capital
    )

    # ===================== Initialization =====================
    def __init__(self):
        # Common indicators
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_period)
        # Trade/equity record containers
        self.equity_log = []
        self.trade_log = []
        self.action_log = []
        # Actual execution info recorded by notify_order
        self.entry_size = None
        self.exit_price = None
        # Stop price
        self.stop_price = None
        # Subclass-specific indicators
        self._init_indicators()

    def _init_indicators(self):
        """Subclass override: initialize strategy-specific indicators (e.g. MA, momentum, Bollinger bands)"""

    # ===================== Order and trade callbacks =====================
    def notify_order(self, order):
        """Record actual execution: buy size / sell price, for notify_trade to generate trade records"""
        if order.status == order.Completed:
            if order.isbuy():
                self.entry_size = order.executed.size
            else:
                self.exit_price = order.executed.price

    def notify_trade(self, trade):
        """Generate standard trade record on position close"""
        if not trade.isclosed:
            return
        try:
            entry_dt = trade.open_datetime()
            exit_dt = trade.close_datetime()
            entry_value = trade.price * (self.entry_size or 0)
            profit_rate = trade.pnlcomm / entry_value if entry_value != 0 else 0
            self.trade_log.append(
                {
                    "entry_date": entry_dt.date(),
                    "exit_date": exit_dt.date(),
                    "entry_price": trade.price,
                    "exit_price": self.exit_price if self.exit_price is not None else trade.price,
                    "size": self.entry_size if self.entry_size is not None else 0,
                    "profit_loss": trade.pnl,
                    "profit_loss_net": trade.pnlcomm,
                    "profit_rate": profit_rate,
                }
            )
            self.entry_size = None
            self.exit_price = None
        except Exception as e:
            print(f"Trade record parsing error: {e}, trade={trade}")

    # ===================== Position sizing and risk control (utility methods called by subclasses) =====================
    def _position_size(self, atr_mult):
        """ATR-based fixed-risk position: size = total capital * max_risk_ratio / (ATR * atr_mult)"""
        risk_per_share = self.atr[0] * atr_mult
        if risk_per_share <= 0:
            return 0
        risk_cap = self.broker.getvalue() * self.p.max_risk_ratio
        return int(risk_cap / risk_per_share)

    def _set_stop(self, atr_mult):
        """Set stop price = close price - ATR * atr_mult"""
        self.stop_price = self.data.close[0] - self.atr[0] * atr_mult

    def _open_position(self, atr_mult):
        """Open position: calculate size -> buy -> set stop"""
        size = self._position_size(atr_mult)
        if size > 0:
            self.buy(size=size)
            self._set_stop(atr_mult)
            self.action_log.append(
                {
                    "date": self.data.datetime.date(0),
                    "side": "BUY",
                    "price": self.data.close[0],
                    "size": size,
                }
            )

    def _close_position(self):
        """Close position and reset stop price"""
        self.close()
        self.stop_price = None
        self.action_log.append(
            {
                "date": self.data.datetime.date(0),
                "side": "SELL",
                "price": self.data.close[0],
                "size": self.position.size,
            }
        )

    # ===================== Main loop (template method) =====================
    def next(self):
        # Record daily equity
        self.equity_log.append({"datetime": self.data.datetime.date(0), "equity": self.broker.getvalue()})
        if not self.position:
            self._on_entry()
        else:
            self._on_exit()

    def _on_entry(self):
        """Subclass override: decide whether to open when no position; call self._open_position(atr_mult) on signal"""
        raise NotImplementedError

    def _on_exit(self):
        """Subclass override: decide whether to close when in position; call self._close_position() on signal"""
        raise NotImplementedError

    # ===================== Unified output interface =====================
    def get_equity_dataframe(self):
        return pd.DataFrame(self.equity_log)

    def get_trade_dataframe(self):
        return pd.DataFrame(self.trade_log)

    def get_action_dataframe(self):
        return pd.DataFrame(self.action_log)
