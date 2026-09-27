# 震荡/均值回归策略（合并 boll_ma + short_reversal）
# 开仓条件（基类无方向性门控，子类自管入场）：
#   收盘价触及布林带下轨 close <= bb_lowerband
#   OR 单日跌幅超阈值 (preclose-close)/preclose > drop_ratio（急跌博反弹）
# 平仓条件：纯动态追踪止损（基类）；收盘价突破布林带上轨（回归目标达成）
#
# 合并设计：布林下轨提供"跌到支撑"信号，急跌阈值提供"超卖反弹"信号；
# 两者都是均值回归入场逻辑，合并后任一触发即开仓。
# 指标/参数命名对齐 freqtrade：bb_period / bb_upperband / bb_middleband / bb_lowerband。
import backtrader as bt

from .base import BaseStrategy


class RangeStrategy(BaseStrategy):
    params = (
        ("trail_atr_multiple", 2.0),          # 追踪止损 ATR 倍数（震荡波动大，倍数放宽）
        ("bb_period", 20),                    # 布林带周期（freqtrade bb-period）
        ("drop_ratio", 0.18),                 # 急跌阈值：跌幅超过该比例视为超卖
        ("max_risk_ratio", 0.02),
        # 全局过滤与止盈止损参数继承自 BaseStrategy
    )

    def populate_indicators(self):
        boll = bt.indicators.BollingerBands(self.data.close, period=self.p.bb_period)
        self.bb_upperband = boll.top     # freqtrade: bb_upperband
        self.bb_middleband = boll.mid    # freqtrade: bb_middleband
        self.bb_lowerband = boll.bot     # freqtrade: bb_lowerband

    def populate_entry_trend(self):
        # 均值回归入场：触及布林下轨 或 急跌超阈值
        touch_lower = self.data.close[0] <= self.bb_lowerband[0]
        drop = (self.data.preclose[0] - self.data.close[0]) / self.data.preclose[0]
        sharp_drop = drop > self.p.drop_ratio
        if touch_lower or sharp_drop:
            parts = []
            if touch_lower:
                parts.append(f"boll_lower: close {self.data.close[0]:.2f} <= bb_lowerband {self.bb_lowerband[0]:.2f}")
            if sharp_drop:
                parts.append(f"sharp_drop: {drop:.4f} > {self.p.drop_ratio}")
            reason = "range: " + " | ".join(parts)
            self._open_position(self.p.trail_atr_multiple, reason=reason)

    def populate_exit_trend(self):
        # 跌破追踪止损 或 突破布林上轨（均值回归目标达成）
        if self.data.close[0] <= self.stop_price:
            self._close_position(self._trail_stop_reason())
        elif self.data.close[0] > self.bb_upperband[0]:
            self._close_position(f"boll_upper_break: close {self.data.close[0]:.2f} > bb_upperband {self.bb_upperband[0]:.2f}")
