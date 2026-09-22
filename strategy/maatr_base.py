# MA ATR base trend strategy
import backtrader as bt

from .base import BaseStrategy


class MAATRBaseStrategy(BaseStrategy):
    params = (
        ("atr_multiple", 1.8),
        # max_risk_ratio inherited from BaseStrategy
    )

    def _init_indicators(self):
        self.ma_short = bt.indicators.SMA(self.data.close, period=20)
        self.ma_long = bt.indicators.SMA(self.data.close, period=60)

    def _on_entry(self):
        # Open long when short MA crosses above long MA
        if self.ma_short[0] > self.ma_long[0]:
            self._open_position(self.p.atr_multiple)

    def _on_exit(self):
        # Close when price falls below stop price
        if self.data.close[0] < self.stop_price:
            self._close_position()
