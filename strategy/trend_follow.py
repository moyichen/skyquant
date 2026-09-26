# 纯趋势跟随策略（继承 BaseStrategy）
# 开仓条件：全局四重趋势过滤全部通过即开仓（无策略专属附加信号）——
#   1. 均线趋势：EMA(sma_fast) > EMA(sma_slow) 多头排列
#   2. MACD 多头：DIF > 0 且 DIF > DEA（金叉状态）且 DIF/柱持续放大（动量增强）
#   3. 波动率：ATR/收盘价 > min_volatility_ratio
#   四重过滤由基类 _entry_filters_ok 统一执行，本策略 _on_entry 无条件开仓
# 平仓条件：纯动态追踪止损（基类）——
#   - 基础追踪：stop = 持仓以来最高价 - trail_atr_multiple × ATR（只上不下）
#   - 动态止盈：浮盈/ATR >= trail_tighten_profit_multiple 后收紧为 trail_tight_atr_multiple × ATR
#   - 无固定止盈，全程跟随趋势吃满波段
from .base import BaseStrategy


class TrendFollowStrategy(BaseStrategy):
    params = (
        ("trail_atr_multiple", 1.6),          # 追踪止损 ATR 倍数（兼作仓位分母）
        ("max_risk_ratio", 0.015),            # 覆盖基类默认 0.02
        # sma_fast/sma_slow/macd_*/min_volatility_ratio/trail_tighten_profit_multiple/
        # trail_tight_atr_multiple 继承自 BaseStrategy
    )

    def _on_entry(self):
        # 全局四重过滤已在基类通过，这里无条件开仓（纯趋势跟随）
        reason = f"trend_follow: triple_filter_passed, close {self.data.close[0]:.2f}"
        self._open_position(self.p.trail_atr_multiple, reason=reason)

    def _on_exit(self):
        # 无策略专属出场信号，仅检查基类维护的追踪止损（含动态止盈收紧）
        if self.data.close[0] <= self.stop_price:
            self._close_position(self._trail_stop_reason())
