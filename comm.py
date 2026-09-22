# comm.py
import backtrader as bt


class AStockCommission(bt.CommInfoBase):
    """
    Custom A-share trading commission model
    Inherits from bt.CommInfoBase
    Fee description:
        Buy: commission + transfer fee
        Sell: commission + transfer fee + stamp duty (levied only on the sell side)
    Parameters:
        commission: commission rate (percentage)
        stamp_duty: stamp duty rate, charged only on sell
        transfer_fee: transfer fee rate, applied to both buy and sell
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
        # size > 0 means buy; size < 0 means sell
        trade_value = abs(size) * price
        comm_fee = trade_value * self.p.commission
        transfer_fee = trade_value * self.p.transfer_fee

        if size > 0:
            # Buy
            total_fee = comm_fee + transfer_fee
        else:
            # Sell, add stamp duty
            stamp_fee = trade_value * self.p.stamp_duty
            total_fee = comm_fee + transfer_fee + stamp_fee
        return total_fee
