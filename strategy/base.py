# 策略基类：统一封装交易记录、净值记录、ATR 风险仓位管理、追踪止损与动态止盈
import math

import backtrader as bt
import pandas as pd


class BaseStrategy(bt.Strategy):
    """
    所有策略的基类。

    通用逻辑（子类无需重复实现）：
      - notify_order / notify_trade：记录实际成交价量，生成标准交易记录
      - 每日净值记录
      - ATR 固定风险仓位计算
      - 追踪止损（chandelier trailing stop，只上不下）
      - 动态止盈（浮盈达阈值后收紧追踪止损，锁定利润但不封顶上行）
      - get_equity_dataframe / get_trade_dataframe / get_action_dataframe 统一输出接口

    子类只需重写三个钩子：
      - _init_indicators()：初始化策略专属指标（如均线、动量、布林带）
      - _on_entry()：空仓时判断是否开仓（满足条件调用 self._open_position(atr_mult)）
      - _on_exit()：持仓时判断是否平仓（满足条件调用 self._close_position()；
                    追踪止损与动态止盈由基类 next() 在调用 _on_exit 前自动更新 stop_price）

    开仓条件：由子类 _on_entry() 定义，调用 _open_position(atr_mult) 触发买入。
    平仓条件（多层级，按优先级）：
      1. 固定止盈（profit_multiple 非 None 时）：收盘价 >= take_price → 立即平仓
      2. 追踪止损 + 动态止盈：基类自动更新 stop_price，跌破则平仓
         - 基础追踪：stop = 持仓以来最高价 - atr_mult × ATR（只上不下）
         - 动态止盈：浮盈(最高价-入场价) >= trail_profit_activate × ATR 后，
           止损倍数收紧为 trail_tight_multiple，即 stop = 最高价 - trail_tight_multiple × ATR
      3. 子类信号止损：_on_exit() 中检查 stop_price 或策略专属出场信号
    """

    params = (
        ("atr_period", 14),                  # ATR 计算周期
        ("max_risk_ratio", 0.02),            # 单笔最大风险占总资金比例
        ("profit_multiple", 2.0),            # 固定止盈距离（ATR 倍数）；None 关闭
        ("trail_profit_activate", None),     # 动态止盈激活阈值（浮盈达该 ATR 倍数后收紧止损）；None 关闭
        ("trail_tight_multiple", 0.8),       # 动态止盈激活后的收紧追踪止损 ATR 倍数
    )

    # ===================== 初始化 =====================
    def __init__(self):
        # 通用指标
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_period)
        # 交易 / 净值记录容器
        self.equity_log = []
        self.trade_log = []
        self.action_log = []
        # notify_order 记录的实际成交信息
        self.entry_size = None
        self.exit_price = None
        # 止损 / 止盈价
        self.stop_price = None
        self.take_price = None
        # 入场参考信息
        self.entry_price = None
        self.entry_bar = None
        self.entry_atr_mult = None
        # 子类专属指标
        self._init_indicators()

    def _init_indicators(self):
        """子类重写：初始化策略专属指标（如均线、动量、布林带）"""

    # ===================== 订单与交易回调 =====================
    def notify_order(self, order):
        """记录实际成交：买入数量 / 卖出价格，供 notify_trade 生成交易记录"""
        if order.status == order.Completed:
            if order.isbuy():
                self.entry_size = order.executed.size
            else:
                self.exit_price = order.executed.price

    def notify_trade(self, trade):
        """持仓平仓时生成标准交易记录"""
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
                    "exit_price": self.exit_price if self.exit_price is not None else trade.price,
                    "size": self.entry_size if self.entry_size is not None else 0,
                    "profit_loss": trade.pnl,
                    "profit_loss_net": trade.pnlcomm,
                    "profit_rate": profit_rate,
                }
            )
            self.entry_size = None
            self.exit_price = None
        except Exception as e:
            print(f"Trade record parsing error: {e}, trade={trade}")

    # ===================== 仓位与风控（子类调用的工具方法） =====================
    def _position_size(self, atr_mult):
        """ATR 固定风险仓位：size = 总资金 × max_risk_ratio / (ATR × atr_mult)"""
        atr_val = self.atr[0]
        # ATR 预热期可能为 NaN，防止 int(nan) 崩溃
        if atr_val is None or math.isnan(atr_val) or atr_val <= 0:
            return 0
        risk_per_share = atr_val * atr_mult
        if risk_per_share <= 0:
            return 0
        risk_cap = self.broker.getvalue() * self.p.max_risk_ratio
        return int(risk_cap / risk_per_share)

    def _open_position(self, atr_mult):
        """开仓：计算仓位 → 买入 → 设置初始止损与固定止盈 → 记录入场信息"""
        size = self._position_size(atr_mult)
        if size > 0:
            self.buy(size=size)
            atr_val = self.atr[0]
            self.stop_price = self.data.close[0] - atr_val * atr_mult
            self.entry_price = self.data.close[0]
            self.entry_bar = len(self) - 1
            self.entry_atr_mult = atr_mult
            # 固定止盈（profit_multiple 非 None 时启用）
            if self.p.profit_multiple:
                self.take_price = self.entry_price + atr_val * self.p.profit_multiple
            else:
                self.take_price = None
            self.action_log.append(
                {
                    "date": self.data.datetime.date(0),
                    "side": "BUY",
                    "price": self.data.close[0],
                    "size": size,
                }
            )

    def _close_position(self):
        """平仓并重置所有持仓状态"""
        self.close()
        self.action_log.append(
            {
                "date": self.data.datetime.date(0),
                "side": "SELL",
                "price": self.data.close[0],
                "size": self.position.size,
            }
        )
        self.stop_price = None
        self.take_price = None
        self.entry_price = None
        self.entry_bar = None
        self.entry_atr_mult = None

    # ===================== 主循环（模板方法） =====================
    def next(self):
        # 记录每日净值
        self.equity_log.append({"datetime": self.data.datetime.date(0), "equity": self.broker.getvalue()})
        if not self.position:
            self._on_entry()
            return
        # 固定止盈：profit_multiple 启用且价格触及，立即平仓（优先级最高）
        if self.take_price is not None and self.data.close[0] >= self.take_price:
            self._close_position()
            return
        # 追踪止损 + 动态止盈：更新 stop_price（只上不下），供子类 _on_exit 检查
        self._update_trailing_stop()
        self._on_exit()

    def _update_trailing_stop(self):
        """更新追踪止损：基础 chandelier 止损 + 盈利激活后的动态收紧"""
        if self.entry_bar is None or self.entry_atr_mult is None:
            return
        atr_val = self.atr[0]
        if atr_val is None or math.isnan(atr_val) or atr_val <= 0:
            return
        # 持仓以来最高价
        bar_count = (len(self) - 1) - self.entry_bar + 1
        high_series = self.data.high.get(size=bar_count)
        highest_since_entry = max(high_series)
        # 基础追踪止损（宽松）
        candidate_stop = highest_since_entry - self.entry_atr_mult * atr_val
        # 动态止盈：浮盈达阈值后收紧追踪止损倍数，锁定利润但不封顶上行
        if self.p.trail_profit_activate is not None:
            profit_atr = (highest_since_entry - self.entry_price) / atr_val
            if profit_atr >= self.p.trail_profit_activate:
                tight_stop = highest_since_entry - self.p.trail_tight_multiple * atr_val
                candidate_stop = max(candidate_stop, tight_stop)
        # 只上不下（ratchet）
        if candidate_stop > self.stop_price:
            self.stop_price = candidate_stop

    def _on_entry(self):
        """子类重写：空仓时判断是否开仓；满足条件调用 self._open_position(atr_mult)"""
        raise NotImplementedError

    def _on_exit(self):
        """子类重写：持仓时判断是否平仓；满足条件调用 self._close_position()"""
        raise NotImplementedError

    def stop(self):
        """回测结束时记录最终资产值，供参数寻优读取"""
        self.final_value = self.broker.getvalue()

    # ===================== 统一输出接口 =====================
    def get_equity_dataframe(self):
        return pd.DataFrame(self.equity_log)

    def get_trade_dataframe(self):
        return pd.DataFrame(self.trade_log)

    def get_action_dataframe(self):
        return pd.DataFrame(self.action_log)
