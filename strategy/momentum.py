# 动量趋势策略（继承 BaseStrategy）
# 开仓条件：全局三重过滤（均线多头 + MACD 多头 + 波动率达标，基类执行）通过，
#           且策略专属信号 Momentum(momentum_period) > 0
# 平仓条件：纯动态追踪止损（基类，含动态止盈收紧）；专属信号 Momentum < 0 趋势走坏
import backtrader as bt

from .base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    params = (
        ("trail_atr_multiple", 1.5),
        ("momentum_period", 20),
        # 全局过滤与止盈止损参数继承自 BaseStrategy
    )

    def _init_indicators(self):
        self.mom = bt.indicators.Momentum(
            self.data.close, period=self.p.momentum_period
        )

    def _on_entry(self):
        # 全局三重过滤已在基类通过，这里只判断策略专属动量信号
        if self.mom[0] > 0:
            reason = f"momentum_positive: mom {self.mom[0]:.4f} > 0, close {self.data.close[0]:.2f}"
            self._open_position(self.p.trail_atr_multiple, reason=reason)

    def _on_exit(self):
        # 跌破追踪止损 或 动量转负 时平仓（拆分判断以记录准确的卖出原因）
        if self.data.close[0] <= self.stop_price:
            self._close_position(self._trail_stop_reason())
        elif self.mom[0] < 0:
            self._close_position(f"momentum_negative: momentum {self.mom[0]:.4f} < 0")
