# 超跌反转策略
from .base import BaseStrategy


class ShortReversalStrategy(BaseStrategy):
    params = (
        ("atr_mult", 2.0),
        ("fall_ratio", 0.18),
        # max_risk_ratio 继承自 BaseStrategy
    )

    def _on_entry(self):
        # 短期跌幅超阈值反转买入
        drop = (self.data.preclose[0] - self.data.close[0]) / self.data.preclose[0]
        if drop > self.p.fall_ratio:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # 跌破止损价平仓
        if self.data.close[0] < self.stop_price:
            self._close_position()
