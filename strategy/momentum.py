# Momentum trend strategy
import backtrader as bt

from .base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    params = (
        ("atr_multiple", 1.5),
        ("momentum_period", 20),
        # max_risk_ratio inherited from BaseStrategy
    )

    def _init_indicators(self):
        self.mom = bt.indicators.Momentum(
            self.data.close, period=self.p.momentum_period
        )

    def _on_entry(self):
        # Open long when momentum is positive
        if self.mom[0] > 0:
            self._open_position(self.p.atr_multiple)

    def _on_exit(self):
        # Close when price falls below stop or momentum turns negative
        if self.data.close[0] < self.stop_price or self.mom[0] < 0:
            self._close_position()
