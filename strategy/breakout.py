# 突破策略（高波动标的专用，新增）
# 开仓条件（基类无方向性门控，子类自管入场）：
#   收盘价突破过去 breakout_period 根 K 线的最高价（唐奇安通道上轨突破）
# 平仓条件：纯动态追踪止损（基类）；收盘价跌破过去 breakout_period 根最低价（反向突破）
#
# 适用标的：高波动、振幅大、趋势不持续但突破后有惯性的品种。
# 与 trend 策略区别：trend 靠均线/MACD/ADX 确认趋势方向后入场；
# breakout 靠价格创 N 日新高直接跟进，入场更早但假突破更多，靠紧止损控制。
# 指标命名采用 freqtrade 社区惯例：donchian_upper / donchian_lower。
import backtrader as bt

from .base import BaseStrategy


class BreakoutStrategy(BaseStrategy):
    params = (
        ("trail_atr_multiple", 2.0),          # 追踪止损 ATR 倍数（突破波动大，倍数放宽）
        ("breakout_period", 20),              # 唐奇安通道周期：突破过去 N 日最高/最低
        ("max_risk_ratio", 0.02),
        # 全局过滤与止盈止损参数继承自 BaseStrategy
    )

    def populate_indicators(self):
        # Donchian channel: highest high / lowest low over the lookback window
        self.donchian_upper = bt.indicators.Highest(self.data.high, period=self.p.breakout_period)
        self.donchian_lower = bt.indicators.Lowest(self.data.low, period=self.p.breakout_period)

    def populate_entry_trend(self):
        # 收盘价突破过去 N 日最高价 → 跟进买入
        if self.data.close[0] > self.donchian_upper[-1]:
            reason = (
                f"breakout: close {self.data.close[0]:.2f} > "
                f"donchian_upper {self.donchian_upper[-1]:.2f} (period {self.p.breakout_period})"
            )
            self._open_position(self.p.trail_atr_multiple, reason=reason)

    def populate_exit_trend(self):
        # 跌破追踪止损 或 跌破唐奇安下轨（突破失败/反转）
        if self.data.close[0] <= self.stop_price:
            self._close_position(self._trail_stop_reason())
        elif self.data.close[0] < self.donchian_lower[-1]:
            self._close_position(
                f"donchian_lower_break: close {self.data.close[0]:.2f} < "
                f"donchian_lower {self.donchian_lower[-1]:.2f}"
            )
