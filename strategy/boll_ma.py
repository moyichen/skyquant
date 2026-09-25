# 布林带 + 均线共振策略
# 开仓条件：收盘价触及布林带下轨（close <= boll.bot）且收盘价站上 60 日均线，博均值回归
# 平仓条件（多层级，优先级从高到低）：
#   1. 固定止盈：profit_multiple 非 None 时，收盘价 >= 入场价 + profit_multiple × ATR
#   2. 追踪止损 + 动态止盈（基类自动）：跌破 stop_price（持仓最高价 - atr_mult × ATR，只上不下；
#      浮盈达 trail_profit_activate × ATR 后收紧为 trail_tight_multiple × ATR）
#   3. 价格突破布林带上轨（close > boll.top），均值回归目标达成
import backtrader as bt

from .base import BaseStrategy


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
        # 价格回踩布林带下轨且站上 60 日均线时开仓
        if self.data.close[0] <= self.boll.bot[0] and self.data.close[0] > self.ma[0]:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # 跌破追踪止损 或 突破布林带上轨 时平仓
        if (
            self.data.close[0] < self.stop_price
            or self.data.close[0] > self.boll.top[0]
        ):
            self._close_position()
