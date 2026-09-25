# 短期反转策略
# 开仓条件：当日跌幅超过阈值 fall_ratio，即 (昨收-今收)/昨收 > fall_ratio，博反弹
# 平仓条件（多层级，优先级从高到低）：
#   1. 固定止盈：profit_multiple 非 None 时，收盘价 >= 入场价 + profit_multiple × ATR
#   2. 追踪止损 + 动态止盈（基类自动）：跌破 stop_price（持仓最高价 - atr_mult × ATR，只上不下；
#      浮盈达 trail_profit_activate × ATR 后收紧为 trail_tight_multiple × ATR）
#   3. 价格继续下跌击穿追踪止损
from .base import BaseStrategy


class ShortReversalStrategy(BaseStrategy):
    params = (
        ("atr_mult", 2.0),
        ("fall_ratio", 0.18),
        # max_risk_ratio 继承自 BaseStrategy
    )

    def _on_entry(self):
        # 短期跌幅超阈值时开仓博反弹
        drop = (self.data.preclose[0] - self.data.close[0]) / self.data.preclose[0]
        if drop > self.p.fall_ratio:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # 跌破追踪止损时平仓
        if self.data.close[0] < self.stop_price:
            self._close_position()
