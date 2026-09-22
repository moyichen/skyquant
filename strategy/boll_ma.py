# Bollinger + MA resonance strategy
import backtrader as bt

from .base import BaseStrategy


class BollMAStrategy(BaseStrategy):
    params = (
        ("atr_mult", 1.6),
        ("boll_period", 20),
        # max_risk_ratio inherited from BaseStrategy
    )

    def _init_indicators(self):
        self.boll = bt.indicators.BollingerBands(
            self.data.close, period=self.p.boll_period
        )
        self.ma = bt.indicators.SMA(self.data.close, period=60)

    def _on_entry(self):
        # Price pulls back to Bollinger lower band and holds above 60-day MA
        if self.data.close[0] <= self.boll.bot[0] and self.data.close[0] > self.ma[0]:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # Close when price breaks below stop or above Bollinger upper band
        if (
            self.data.close[0] < self.stop_price
            or self.data.close[0] > self.boll.top[0]
        ):
            self._close_position()
