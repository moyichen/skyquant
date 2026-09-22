# comm.py
import backtrader as bt


class AStockCommission(bt.CommInfoBase):
    """
    A股自定义交易手续费模型
    继承 bt.CommInfoBase
    费率说明：
        买入：佣金 + 过户费
        卖出：佣金 + 过户费 + 印花税（仅卖出单边征收）
    参数：
        commission: 佣金率（百分比）
        stamp_duty: 印花税率，卖出才扣
        transfer_fee: 过户费率，买卖双向
    """

    params = (
        ("commission", 0.0003),
        ("stamp_duty", 0.001),
        ("transfer_fee", 0.00001),
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("percabs", True),
    )

    def _getcommission(self, size, price, pseudoexec):
        # size >0 买入；size <0 卖出
        trade_value = abs(size) * price
        comm_fee = trade_value * self.p.commission
        transfer_fee = trade_value * self.p.transfer_fee

        if size > 0:
            # 买入
            total_fee = comm_fee + transfer_fee
        else:
            # 卖出，增加印花税
            stamp_fee = trade_value * self.p.stamp_duty
            total_fee = comm_fee + transfer_fee + stamp_fee
        return total_fee
