# Multi-factor composite strategy
import backtrader as bt

from .base import BaseStrategy


class MultiFactorStrategy(BaseStrategy):
    params = (
        ("atr_mult", 1.7),
        # max_risk_ratio inherited from BaseStrategy
    )

    def _init_indicators(self):
        self.ma20 = bt.indicators.SMA(self.data.close, period=20)
        self.ma60 = bt.indicators.SMA(self.data.close, period=60)

    def _on_entry(self):
        # Dual MA bullish alignment and daily decline no more than 5%
        if self.ma20[0] > self.ma60[0] and self.data.pctChg[0] > -5:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # Close when price falls below stop or MA death cross
        if self.data.close[0] < self.stop_price or self.ma20[0] < self.ma60[0]:
            self._close_position()
