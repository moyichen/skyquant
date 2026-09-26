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
      - _on_entry()：空仓时判断是否开仓（满足条件调用 self._open_position(atr_multiple)）
      - _on_exit()：持仓时判断是否平仓（满足条件调用 self._close_position()；
                    追踪止损与动态止盈由基类 next() 在调用 _on_exit 前自动更新 stop_price）

    开仓条件（全局三重趋势过滤，由基类统一执行，子类无法绕过）：
      1. 均线趋势：SMA(sma_fast) > SMA(sma_slow)，只做多头排列
      2. MACD 多头：DIF > 0（零轴上方）且 DIF > DEA（金叉状态）
         且 DIF 持续上行 macd_momentum_bars 根、MACD 柱持续放大（动量增强）
      3. 波动率过滤：ATR/收盘价 > min_volatility_ratio，过滤横盘假突破
      全部通过后才调用子类 _on_entry() 判断策略专属信号。

    平仓条件（多层级，按优先级）：
      1. 纯动态追踪止损（默认唯一出场）：跌破 stop_price → 立即平仓
         - 基础追踪：stop = 持仓以来最高价 - trail_atr_multiple × ATR（只上不下）
         - 动态止盈：浮盈(最高价-入场价)/ATR >= trail_tighten_profit_multiple 后，
           止损倍数收紧为 trail_tight_atr_multiple，
           即 stop = 最高价 - trail_tight_atr_multiple × ATR
         - 无固定止盈（take_profit_atr_multiple 默认 None），全程跟随趋势吃满波段
      2. 子类信号止损：_on_exit() 中检查 stop_price 或策略专属出场信号
    """

    params = (
        ("atr_period", 14),                        # ATR 计算周期
        ("max_risk_ratio", 0.02),                  # 单笔最大风险占总资金比例
        # ---- 全局三重趋势过滤参数 ----
        ("sma_fast", 20),                          # 均线趋势过滤：快线周期
        ("sma_slow", 60),                          # 均线趋势过滤：慢线周期
        ("macd_fast", 12),                         # MACD 快线 EMA 周期
        ("macd_slow", 26),                         # MACD 慢线 EMA 周期
        ("macd_signal", 9),                        # MACD 信号线（DEA）周期
        ("macd_momentum_bars", 2),                 # MACD 多头动量确认：DIF/柱需连续放大的 bar 数
        ("min_volatility_ratio", 0.015),           # 波动率过滤：ATR/收盘价下限
        # ---- 止盈止损参数 ----
        ("take_profit_atr_multiple", None),        # 固定止盈距离（ATR 倍数）；默认 None=纯追踪止损
        ("trail_tighten_profit_multiple", None),   # 动态止盈激活门槛：浮盈达该 ATR 倍数后收紧止损；None 关闭
        ("trail_tight_atr_multiple", 0.8),         # 动态止盈激活后的收紧追踪止损 ATR 倍数
    )

    # ===================== 初始化 =====================
    def __init__(self):
        # 通用指标
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_period)
        # 全局趋势过滤指标（所有策略共用）
        self.sma_fast = bt.indicators.SMA(self.data.close, period=self.p.sma_fast)
        self.sma_slow = bt.indicators.SMA(self.data.close, period=self.p.sma_slow)
        self.macd = bt.indicators.MACDHisto(
            self.data.close,
            period_me1=self.p.macd_fast,
            period_me2=self.p.macd_slow,
            period_signal=self.p.macd_signal,
        )
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
        self.entry_atr_multiple = None
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
    def _position_size(self, atr_multiple):
        """ATR 固定风险仓位：size = 总资金 × max_risk_ratio / (ATR × atr_multiple)"""
        atr_val = self.atr[0]
        # ATR 预热期可能为 NaN，防止 int(nan) 崩溃
        if atr_val is None or math.isnan(atr_val) or atr_val <= 0:
            return 0
        risk_per_share = atr_val * atr_multiple
        if risk_per_share <= 0:
            return 0
        risk_cap = self.broker.getvalue() * self.p.max_risk_ratio
        return int(risk_cap / risk_per_share)

    def _open_position(self, atr_multiple):
        """开仓：计算仓位 → 买入 → 设置初始止损与固定止盈 → 记录入场信息"""
        size = self._position_size(atr_multiple)
        if size > 0:
            self.buy(size=size)
            atr_val = self.atr[0]
            self.stop_price = self.data.close[0] - atr_val * atr_multiple
            self.entry_price = self.data.close[0]
            self.entry_bar = len(self) - 1
            self.entry_atr_multiple = atr_multiple
            # 固定止盈（take_profit_atr_multiple 非 None 时启用）
            if self.p.take_profit_atr_multiple:
                self.take_price = self.entry_price + atr_val * self.p.take_profit_atr_multiple
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
        self.entry_atr_multiple = None

    # ===================== 主循环（模板方法） =====================
    def next(self):
        # 记录每日净值
        self.equity_log.append({"datetime": self.data.datetime.date(0), "equity": self.broker.getvalue()})
        if not self.position:
            # 全局三重趋势过滤：全部通过才允许子类判断专属入场信号
            if self._entry_filters_ok():
                self._on_entry()
            return
        # 固定止盈：显式配置（非 None）且收盘价触及时立即平仓（默认关闭，纯追踪止损）
        if self.take_price is not None and self.data.close[0] >= self.take_price:
            self._close_position()
            return
        # 追踪止损 + 动态止盈：更新 stop_price（只上不下），供子类 _on_exit 检查
        self._update_trailing_stop()
        self._on_exit()

    def _entry_filters_ok(self):
        """全局三重趋势过滤：均线多头 + MACD 多头 + 波动率达标，全部通过才可开仓"""
        # 1. 均线趋势：快线在慢线上方（多头排列；预热期 NaN 比较为 False，天然不通过）
        if not (self.sma_fast[0] > self.sma_slow[0]):
            return False
        # 2. MACD 多头：零轴上方 + 金叉状态 + 动量增强
        if not self._macd_bullish():
            return False
        # 3. 波动率过滤：ATR/收盘价超过下限，过滤横盘假突破
        if not (self.atr[0] / self.data.close[0] > self.p.min_volatility_ratio):
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

    def _update_trailing_stop(self):
        """更新追踪止损：基础 chandelier 止损 + 盈利激活后的动态收紧"""
        if self.entry_bar is None or self.entry_atr_multiple is None:
            return
        atr_val = self.atr[0]
        if atr_val is None or math.isnan(atr_val) or atr_val <= 0:
            return
        # 持仓以来最高价
        bar_count = (len(self) - 1) - self.entry_bar + 1
        high_series = self.data.high.get(size=bar_count)
        highest_since_entry = max(high_series)
        # 基础追踪止损（宽松）
        candidate_stop = highest_since_entry - self.entry_atr_multiple * atr_val
        # 动态止盈：浮盈（以 ATR 倍数计）达门槛后收紧追踪止损倍数，锁定利润但不封顶上行
        if self.p.trail_tighten_profit_multiple is not None:
            profit_atr_multiple = (highest_since_entry - self.entry_price) / atr_val
            if profit_atr_multiple >= self.p.trail_tighten_profit_multiple:
                tight_stop = highest_since_entry - self.p.trail_tight_atr_multiple * atr_val
                candidate_stop = max(candidate_stop, tight_stop)
        # 只上不下（ratchet）
        if candidate_stop > self.stop_price:
            self.stop_price = candidate_stop

    def _on_entry(self):
        """子类重写：空仓时判断是否开仓；满足条件调用 self._open_position(atr_multiple)"""
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
