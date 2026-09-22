# 动量趋势策略
from .base import BaseStrategy
import backtrader as bt


class MomentumStrategy(BaseStrategy):
    params = (
        ("atr_multiple", 1.5),
        ("momentum_period", 20),
        # max_risk_ratio 继承自 BaseStrategy
    )

    def _init_indicators(self):
        self.mom = bt.indicators.Momentum(self.data.close, period=self.p.momentum_period)

    def _on_entry(self):
        # 动量为正开多
        if self.mom[0] > 0:
            self._open_position(self.p.atr_multiple)

    def _on_exit(self):
        # 跌破止损价 或 动量转负 平仓
        if self.data.close[0] < self.stop_price or self.mom[0] < 0:
            self._close_position()
