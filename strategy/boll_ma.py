# 布林带回归策略（继承 BaseStrategy）
# 开仓条件：全局三重过滤（均线多头 + MACD 多头 + 波动率达标，基类执行，其中均线条件
#           等价于原 close > SMA(60)）通过，且策略专属信号收盘价触及布林带下轨（均值回归）
# 平仓条件：纯动态追踪止损（基类，含动态止盈收紧）；收盘价突破布林带上轨（回归目标达成）
import backtrader as bt

from .base import BaseStrategy


class BollMAStrategy(BaseStrategy):
    params = (
        ("trail_atr_multiple", 1.6),
        ("boll_period", 20),
        # 全局过滤与止盈止损参数继承自 BaseStrategy
    )

    def _init_indicators(self):
        self.boll = bt.indicators.BollingerBands(
            self.data.close, period=self.p.boll_period
        )

    def _on_entry(self):
        # 全局三重过滤已在基类通过（含 close > SMA60），这里只判断布林下轨信号
        if self.data.close[0] <= self.boll.bot[0]:
            self._open_position(self.p.trail_atr_multiple)

    def _on_exit(self):
        # 跌破追踪止损 或 突破布林带上轨 时平仓
        if (
            self.data.close[0] <= self.stop_price
            or self.data.close[0] > self.boll.top[0]
        ):
            self._close_position()
