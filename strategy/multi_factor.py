# 多因子复合策略
# 开仓条件：20 日均线在 60 日均线上方（趋势向上）且当日跌幅不超过 5%（pctChg > -5），过滤暴跌日
# 平仓条件（多层级，优先级从高到低）：
#   1. 固定止盈：profit_multiple 非 None 时，收盘价 >= 入场价 + profit_multiple × ATR
#   2. 追踪止损 + 动态止盈（基类自动）：跌破 stop_price（持仓最高价 - atr_mult × ATR，只上不下；
#      浮盈达 trail_profit_activate × ATR 后收紧为 trail_tight_multiple × ATR）
#   3. 均线死叉：20 日均线跌破 60 日均线，趋势反转
import backtrader as bt

from .base import BaseStrategy


class MultiFactorStrategy(BaseStrategy):
    params = (
        ("atr_mult", 1.7),
        # max_risk_ratio 继承自 BaseStrategy
    )

    def _init_indicators(self):
        self.ma20 = bt.indicators.SMA(self.data.close, period=20)
        self.ma60 = bt.indicators.SMA(self.data.close, period=60)

    def _on_entry(self):
        # 均线多头排列 且 当日跌幅不超过 5% 时开仓
        if self.ma20[0] > self.ma60[0] and self.data.pctChg[0] > -5:
            self._open_position(self.p.atr_mult)

    def _on_exit(self):
        # 跌破追踪止损 或 均线死叉 时平仓
        if self.data.close[0] < self.stop_price or self.ma20[0] < self.ma60[0]:
            self._close_position()
