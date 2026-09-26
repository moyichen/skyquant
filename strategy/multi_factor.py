# 多因子复合策略（继承 BaseStrategy）
# 开仓条件：全局三重过滤（均线多头 + MACD 多头 + 波动率达标，基类执行，其中均线条件
#           覆盖原 SMA(20)>SMA(60)）通过，且策略专属信号当日跌幅不超过 5%（pctChg > -5）
# 平仓条件：纯动态追踪止损（基类，含动态止盈收紧）；均线死叉（SMA20 < SMA60）趋势反转
import backtrader as bt

from .base import BaseStrategy


class MultiFactorStrategy(BaseStrategy):
    params = (
        ("trail_atr_multiple", 1.7),
        # 全局过滤与止盈止损参数继承自 BaseStrategy
    )

    def _init_indicators(self):
        # 均线指标保留用于出场判断（死叉）；开仓趋势判断由基类全局过滤承担
        self.ma20 = bt.indicators.SMA(self.data.close, period=20)
        self.ma60 = bt.indicators.SMA(self.data.close, period=60)

    def _on_entry(self):
        # 全局三重过滤已在基类通过（含均线多头），这里只判断跌幅过滤
        if self.data.pctChg[0] > -5:
            self._open_position(self.p.trail_atr_multiple)

    def _on_exit(self):
        # 跌破追踪止损 或 均线死叉 时平仓
        if self.data.close[0] <= self.stop_price or self.ma20[0] < self.ma60[0]:
            self._close_position()
