# 动量趋势策略
# 开仓条件：动量指标 Momentum(momentum_period) > 0，即当前价高于 N 日前价格，趋势向上
# 平仓条件（多层级，优先级从高到低）：
#   1. 固定止盈：profit_multiple 非 None 时，收盘价 >= 入场价 + profit_multiple × ATR
#   2. 追踪止损 + 动态止盈（基类自动）：跌破 stop_price（持仓最高价 - atr_multiple × ATR，只上不下；
#      浮盈达 trail_profit_activate × ATR 后收紧为 trail_tight_multiple × ATR）
#   3. 动量反转：Momentum < 0，趋势走坏
import backtrader as bt

from .base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    params = (
        ("atr_multiple", 1.5),
        ("momentum_period", 20),
        # max_risk_ratio inherited from BaseStrategy
    )

    def _init_indicators(self):
        self.mom = bt.indicators.Momentum(
            self.data.close, period=self.p.momentum_period
        )

    def _on_entry(self):
        # 动量为正时开多仓
        if self.mom[0] > 0:
            self._open_position(self.p.atr_multiple)

    def _on_exit(self):
        # 跌破追踪止损 或 动量转负 时平仓
        if self.data.close[0] < self.stop_price or self.mom[0] < 0:
            self._close_position()
