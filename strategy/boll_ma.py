# 布林 + 均线共振策略
from .base import BaseStrategy
import backtrader as bt


class BollMAStrategy(BaseStrategy):
    params = (
        ("atr_mult", 1.6),
        ("boll_period", 20),
        # max_risk_ratio 继承自 BaseStrategy
    )

    def _init_indicators(self):
        self.boll = bt.indicators.BollingerBands(
            self.data.close, period=self.p.boll_period
        )
        self.ma = bt.indicators.SMA(self.data.close, period=60)

    def _on_entry(self):
        # 价格回踩布林下轨 且 站上60日均线
        if self.data.close[0] <= self.boll.bot[0] and self.data.close[0] > self.ma[0]:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # 跌破止损价 或 突破布林上轨 平仓
        if (
            self.data.close[0] < self.stop_price
            or self.data.close[0] > self.boll.top[0]
        ):
            self._close_position()
