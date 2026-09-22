# Oversold reversal strategy
from .base import BaseStrategy


class ShortReversalStrategy(BaseStrategy):
    params = (
        ("atr_mult", 2.0),
        ("fall_ratio", 0.18),
        # max_risk_ratio inherited from BaseStrategy
    )

    def _on_entry(self):
        # Buy on reversal when short-term drop exceeds threshold
        drop = (self.data.preclose[0] - self.data.close[0]) / self.data.preclose[0]
        if drop > self.p.fall_ratio:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # Close when price falls below stop price
        if self.data.close[0] < self.stop_price:
            self._close_position()
