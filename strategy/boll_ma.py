# 布林带回归策略（继承 BaseStrategy）
# 开仓条件：全局四重过滤（均线多头 + MACD 多头 + 波动率达标 + ADX 趋势强度，基类执行，其中均线条件
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
        # 全局四重过滤已在基类通过（含 close > SMA60），这里只判断布林下轨信号
        if self.data.close[0] <= self.boll.bot[0]:
            reason = f"boll_lower_touch: close {self.data.close[0]:.2f} <= boll_bot {self.boll.bot[0]:.2f}"
            self._open_position(self.p.trail_atr_multiple, reason=reason)

    def _on_exit(self):
        # 跌破追踪止损 或 突破布林带上轨 时平仓（拆分判断以记录准确的卖出原因）
        if self.data.close[0] <= self.stop_price:
            self._close_position(self._trail_stop_reason())
        elif self.data.close[0] > self.boll.top[0]:
            self._close_position(f"boll_upper_break: close {self.data.close[0]:.2f} > boll_top {self.boll.top[0]:.2f}")
