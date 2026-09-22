# 均线 ATR 基础趋势策略
from .base import BaseStrategy
import backtrader as bt


class MAATRBaseStrategy(BaseStrategy):
    params = (
        ("atr_multiple", 1.8),
        # max_risk_ratio 继承自 BaseStrategy
    )

    def _init_indicators(self):
        self.ma_short = bt.indicators.SMA(self.data.close, period=20)
        self.ma_long = bt.indicators.SMA(self.data.close, period=60)

    def _on_entry(self):
        # 短期均线上穿长期均线开多
        if self.ma_short[0] > self.ma_long[0]:
            self._open_position(self.p.atr_multiple)

    def _on_exit(self):
        # 跌破止损价平仓
        if self.data.close[0] < self.stop_price:
            self._close_position()
