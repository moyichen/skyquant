# 均线 + ATR 策略（继承 BaseStrategy）
# 开仓条件：短均线 SMA(sma_fast) 在长均线 SMA(sma_slow) 上方（趋势向上）
#           且 ATR/收盘价 > atr_min_rel（波动率达标，过滤横盘假突破）
# 平仓条件（多层级，优先级从高到低，均由基类统一处理）：
#   1. 固定止盈：profit_multiple 非 None 时，收盘价 >= 入场价 + profit_multiple × ATR
#   2. 追踪止损 + 动态止盈：
#      - 基础追踪：stop = 持仓以来最高价 - atr_multiple × ATR（只上不下）
#      - 动态止盈：浮盈(最高价-入场价) >= trail_profit_activate × ATR 后，
#        止损倍数收紧为 trail_tight_multiple，即 stop = 最高价 - trail_tight_multiple × ATR
#   3. 收盘价跌破追踪止损 stop_price → 平仓离场
import backtrader as bt

from .base import BaseStrategy


class MAATRBaseStrategy(BaseStrategy):
    params = (
        ("sma_fast", 20),
        ("sma_slow", 60),
        ("atr_multiple", 1.6),      # 追踪止损 ATR 倍数
        ("atr_min_rel", 0.015),     # ATR/收盘价最小波动率阈值（过滤横盘）
        ("max_risk_ratio", 0.015),  # 覆盖基类默认 0.02
        ("profit_multiple", None),  # 覆盖基类默认 2.0；None=默认纯追踪止损，无固定止盈
        # atr_period / trail_profit_activate / trail_tight_multiple 继承自 BaseStrategy
    )

    def _init_indicators(self):
        # ATR 指标由基类创建，这里只建均线
        self.sma_fast = bt.ind.SMA(self.data.close, period=self.p.sma_fast)
        self.sma_slow = bt.ind.SMA(self.data.close, period=self.p.sma_slow)

    def _on_entry(self):
        # 均线多头排列 + 波动率达标时开仓
        atr_rel = self.atr[0] / self.data.close[0]
        if self.sma_fast[0] > self.sma_slow[0] and atr_rel > self.p.atr_min_rel:
            self._open_position(self.p.atr_multiple)

    def _on_exit(self):
        # 无策略专属出场信号，仅检查基类维护的追踪止损（含动态止盈收紧）
        if self.data.close[0] <= self.stop_price:
            self._close_position()
