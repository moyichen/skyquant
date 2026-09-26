# 短期反转策略（继承 BaseStrategy）
# 开仓条件：全局三重过滤（均线多头 + MACD 多头 + 波动率达标，基类执行）通过，
#           且策略专属信号当日跌幅超阈值：(昨收-今收)/昨收 > drop_ratio（多头行情中的急跌博反弹）
# 平仓条件：纯动态追踪止损（基类，含动态止盈收紧）；收盘价跌破 stop_price
from .base import BaseStrategy


class ShortReversalStrategy(BaseStrategy):
    params = (
        ("trail_atr_multiple", 2.0),
        ("drop_ratio", 0.18),
        # 全局过滤与止盈止损参数继承自 BaseStrategy
    )

    def _on_entry(self):
        # 全局三重过滤已在基类通过，这里只判断策略专属急跌信号
        drop = (self.data.preclose[0] - self.data.close[0]) / self.data.preclose[0]
        if drop > self.p.drop_ratio:
            self._open_position(self.p.trail_atr_multiple)

    def _on_exit(self):
        # 跌破追踪止损时平仓
        if self.data.close[0] <= self.stop_price:
            self._close_position()
