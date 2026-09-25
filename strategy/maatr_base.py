# MA ATR base trend strategy
import backtrader as bt

from .base import BaseStrategy


class MAATRBaseStrategy(BaseStrategy):
    params = (
        ("atr_multiple", 1.8),
        # max_risk_ratio inherited from BaseStrategy
    )

    def _init_indicators(self):
        self.sma_fast = bt.indicators.SMA(self.data.close, period=20)
        self.sma_slow = bt.indicators.SMA(self.data.close, period=60)
        self.trend_ok = self.sma_fast > self.sma_slow
        self.upper_band = self.data.close(-1) + self.p.atr_multiple * self.atr

    def _on_entry(self):
        # Open long when close breaks above the ATR channel with trend filter up
        price_break = self.data.close[0] > self.upper_band[0]
        if price_break and self.trend_ok[0]:
            self._open_position(self.p.atr_multiple)

    def _on_exit(self):
        # Close when price falls below the fixed ATR stop set at entry
        if self.data.close[0] <= self.stop_price:
            self._close_position()
