# 趋势策略（合并 trend_follow + momentum）
# 开仓条件：策略专属趋势过滤（EMA 多头 + MACD 多头 + 波动率达标 + ADX 趋势强度/方向）通过，
#           且 Momentum(momentum_period) > 0（动量确认趋势方向）
# 平仓条件：纯动态追踪止损（基类，含动态止盈收紧）；动量转负（mom < 0）趋势走坏
#
# 合并设计：趋势结构由 EMA 前提 + 三重过滤提供，momentum 提供入场时机确认；
# 两者合并后只在"多头趋势 + 动量为正"时开仓，比纯趋势跟随更保守、比纯动量更稳健。
import math

import backtrader as bt

from .base import BaseStrategy


class TrendStrategy(BaseStrategy):
    params = (
        ("trail_atr_multiple", 1.6),          # 追踪止损 ATR 倍数（兼作仓位分母）
        ("momentum_period", 20),              # 动量确认周期；>0 才开仓（趋势方向确认）
        ("max_risk_ratio", 0.02),             # 单笔最大风险占比
        # ---- 策略专属趋势过滤（EMA 多头 + MACD + 波动率 + ADX）----
        ("sma_fast", 20),                     # EMA 快线周期（多头排列前提）
        ("sma_slow", 60),                     # EMA 慢线周期
        ("macd_fast", 12),                    # MACD 快线 EMA 周期
        ("macd_slow", 26),                    # MACD 慢线 EMA 周期
        ("macd_signal", 9),                   # MACD 信号线（DEA）周期
        ("macd_momentum_bars", 2),            # MACD 多头动量确认：DIF/柱需连续放大的 bar 数
        ("min_volatility_ratio", 0.015),      # 波动率过滤：ATR/收盘价下限
        ("adx_period", 14),                   # ADX/DMI 计算周期
        ("adx_min", 20),                      # ADX 趋势强度下限（< 视为横盘震荡不开仓；>25 强趋势）
    )

    def populate_indicators(self):
        self.mom = bt.indicators.Momentum(self.data.close, period=self.p.momentum_period)
        self.sma_fast = bt.indicators.EMA(self.data.close, period=self.p.sma_fast)
        self.sma_slow = bt.indicators.EMA(self.data.close, period=self.p.sma_slow)
        self.macd = bt.indicators.MACDHisto(
            self.data.close,
            period_me1=self.p.macd_fast,
            period_me2=self.p.macd_slow,
            period_signal=self.p.macd_signal,
        )
        # ADX/DMI：区分趋势行情与震荡行情（adx 趋势强度，plusDI/minusDI 判定方向）
        self.dmi = bt.indicators.DMI(self.data, period=self.p.adx_period)

    def _trend_filters_ok(self):
        """策略专属趋势过滤：EMA 多头 + MACD 多头 + 波动率达标 + ADX 趋势强度/方向"""
        # 1. 均线趋势：快线在慢线上方（多头排列；预热期 NaN 比较为 False，天然不通过）
        if not (self.sma_fast[0] > self.sma_slow[0]):
            return False
        # 2. MACD 多头：零轴上方 + 金叉状态 + 动量增强
        if not self._macd_bullish():
            return False
        # 3. 波动率过滤：ATR/收盘价超过下限，过滤横盘假突破
        if not (self.atr[0] / self.data.close[0] > self.p.min_volatility_ratio):
            return False
        # 4. ADX 趋势强度：低于下限视为横盘震荡直接不开仓（预热期 NaN 天然不通过）
        adx_val = self.dmi.adx[0]
        if math.isnan(adx_val) or adx_val < self.p.adx_min:
            return False
        #    方向确认：+DI > -DI 才算多头趋势
        if not (self.dmi.plusDI[0] > self.dmi.minusDI[0]):
            return False
        return True

    def _macd_bullish(self):
        """MACD 多头判定：DIF>0 且金叉状态，且 DIF 与柱连续 macd_momentum_bars 根放大"""
        dif = self.macd.macd
        dea = self.macd.signal
        hist = self.macd.histo
        # 零轴上方：中期多头行情
        if not (dif[0] > 0):
            return False
        # 金叉状态：DIF 在 DEA 上方
        if not (dif[0] > dea[0]):
            return False
        # 动量增强：DIF 持续上行 且 MACD 柱持续放大（连续 N 根）
        for i in range(self.p.macd_momentum_bars):
            if not (dif[-i] > dif[-i - 1]):
                return False
            if not (hist[-i] > hist[-i - 1]):
                return False
        return True

    def populate_entry(self):
        # 趋势过滤（EMA/MACD/波动率/ADX）+ 动量确认趋势方向
        if self._trend_filters_ok() and self.mom[0] > 0:
            reason = f"trend: filters passed + momentum {self.mom[0]:.4f} > 0, close {self.data.close[0]:.2f}"
            self._open_position(self.p.trail_atr_multiple, reason=reason)

    def populate_exit(self):
        # 1. 追踪止损 / 动态止盈（stop_price 由基类更新）
        if self.data.close[0] <= self.stop_price:
            self._close_position(self._trail_stop_reason())
            return
        # 2. 动量转负：趋势走坏，主动离场
        if self.mom[0] < 0:
            reason = f"momentum_negative: momentum {self.mom[0]:.4f} < 0, close {self.data.close[0]:.2f}"
            self._close_position(reason)
