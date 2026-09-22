# 策略基类：封装所有策略共享的交易记录、净值记录、ATR风控仓位计算
import backtrader as bt
import pandas as pd


class BaseStrategy(bt.Strategy):
    """
    所有策略的基类。

    共性逻辑（由本类实现，子类无需重复）：
      - notify_order / notify_trade：捕获实际成交价量并生成标准交易记录
      - 每日净值记录
      - 基于 ATR 的固定风险仓位计算与止损价设置
      - get_equity_dataframe / get_trade_dataframe 统一输出接口

    子类只需覆盖三个钩子：
      - _init_indicators()：初始化策略专属指标
      - _on_entry()：无持仓时判断是否开仓（信号满足则调用 _open_position）
      - _on_exit()：持仓时判断是否平仓（满足则调用 _close_position）
    """

    params = (
        ("atr_period", 14),  # ATR 计算周期
        ("max_risk_ratio", 0.02),  # 单笔最大风险占总资金比例
    )

    # ===================== 初始化 =====================
    def __init__(self):
        # 通用指标
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_period)
        # 交易/净值记录容器
        self.equity_log = []
        self.trade_log = []
        # 由 notify_order 记录的实际成交信息
        self.entry_size = None
        self.exit_price = None
        # 止损价
        self.stop_price = None
        # 子类专属指标
        self._init_indicators()

    def _init_indicators(self):
        """子类覆盖：初始化策略专属指标（如均线、动量、布林带等）"""
        pass

    # ===================== 订单与成交回调 =====================
    def notify_order(self, order):
        """记录实际成交：买入数量 / 卖出价格，供 notify_trade 生成交易记录"""
        if order.status == order.Completed:
            if order.isbuy():
                self.entry_size = order.executed.size
            else:
                self.exit_price = order.executed.price

    def notify_trade(self, trade):
        """平仓时生成标准交易记录"""
        if not trade.isclosed:
            return
        try:
            entry_dt = trade.open_datetime()
            exit_dt = trade.close_datetime()
            entry_value = trade.price * (self.entry_size or 0)
            profit_rate = trade.pnlcomm / entry_value if entry_value != 0 else 0
            self.trade_log.append(
                {
                    "entry_date": entry_dt.date(),
                    "exit_date": exit_dt.date(),
                    "entry_price": trade.price,
                    "exit_price": self.exit_price
                    if self.exit_price is not None
                    else trade.price,
                    "size": self.entry_size if self.entry_size is not None else 0,
                    "profit_loss": trade.pnl,
                    "profit_loss_net": trade.pnlcomm,
                    "profit_rate": profit_rate,
                }
            )
            self.entry_size = None
            self.exit_price = None
        except Exception as e:
            print(f"交易记录解析异常: {e}, trade={trade}")

    # ===================== 仓位与风控（子类调用的工具方法） =====================
    def _position_size(self, atr_mult):
        """基于 ATR 的固定风险仓位：size = 总资金 * max_risk_ratio / (ATR * atr_mult)"""
        risk_per_share = self.atr[0] * atr_mult
        if risk_per_share <= 0:
            return 0
        risk_cap = self.broker.getvalue() * self.p.max_risk_ratio
        return int(risk_cap / risk_per_share)

    def _set_stop(self, atr_mult):
        """设置止损价 = 收盘价 - ATR * atr_mult"""
        self.stop_price = self.data.close[0] - self.atr[0] * atr_mult

    def _open_position(self, atr_mult):
        """开仓：计算仓位 → 买入 → 设置止损"""
        size = self._position_size(atr_mult)
        if size > 0:
            self.buy(size=size)
            self._set_stop(atr_mult)

    def _close_position(self):
        """平仓并重置止损价"""
        self.close()
        self.stop_price = None

    # ===================== 主循环（模板方法） =====================
    def next(self):
        # 记录每日净值
        self.equity_log.append(
            {"datetime": self.data.datetime.date(0), "equity": self.broker.getvalue()}
        )
        if not self.position:
            self._on_entry()
        else:
            self._on_exit()

    def _on_entry(self):
        """子类覆盖：无持仓时判断是否开仓，信号满足时调用 self._open_position(atr_mult)"""
        raise NotImplementedError

    def _on_exit(self):
        """子类覆盖：持仓时判断是否平仓，满足时调用 self._close_position()"""
        raise NotImplementedError

    # ===================== 统一输出接口 =====================
    def get_equity_dataframe(self):
        return pd.DataFrame(self.equity_log)

    def get_trade_dataframe(self):
        return pd.DataFrame(self.trade_log)
