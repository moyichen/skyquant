# 多因子合成策略
from .base import BaseStrategy
import backtrader as bt


class MultiFactorStrategy(BaseStrategy):
    params = (
        ("atr_mult", 1.7),
        # max_risk_ratio 继承自 BaseStrategy
    )

    def _init_indicators(self):
        self.ma20 = bt.indicators.SMA(self.data.close, period=20)
        self.ma60 = bt.indicators.SMA(self.data.close, period=60)

    def _on_entry(self):
        # 双均线多头排列 且 当日跌幅不超过5%
        if self.ma20[0] > self.ma60[0] and self.data.pctChg[0] > -5:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # 跌破止损价 或 均线死叉 平仓
        if self.data.close[0] < self.stop_price or self.ma20[0] < self.ma60[0]:
            self._close_position()
