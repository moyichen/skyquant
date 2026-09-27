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

    子类只需重写三个钩子（对齐 freqtrade populate_* 术语）：
      - populate_indicators()：初始化策略专属指标（如均线、动量、布林带）
      - populate_entry_trend()：空仓时判断是否开仓（满足条件调用 self._open_position(atr_multiple)）
      - populate_exit_trend()：持仓时判断是否平仓（满足条件调用 self._close_position(reason)，
                    reason 描述卖出逻辑，写入 action_log 与 trade_log 供人工分析；
                    追踪止损与动态止盈由基类 next() 在调用 populate_exit_trend 前自动更新 stop_price）

    开仓门控（设计原则：基类不设方向性过滤）：
      入场判断完全由各策略 populate_entry_trend() 自管（freqtrade 分层：方向性过滤属于
      策略 alpha，标的适配交给 stock_filter Pairlist Filters 筛选层）。trend 策略自带 EMA/MACD/波动率/ADX 趋势过滤。
      基类全局层只挂风控保护（对齐 freqtrade Protections，默认全关）：
      - CooldownPeriod（cooldown_period_candles）：卖出成交后 N 根 K 线内禁止新开仓
      - StoplossGuard（stoploss_guard_*）：回看窗口内止损平仓达上限即暂停开仓
      - MaxDrawdown（max_allowed_drawdown）：净值自峰值回撤超阈值期间禁止新开仓

    开仓执行模型（买入限价单，卖出市价单）：
      - 买入：信号日收盘触发后，以触发收盘价为限价挂单，仅在【下一交易日】有效，
        次日盘中触及限价才成交（开盘价低于限价时以开盘价成交）；次日未成交则自动放弃。
        避免跳空高开放大买入成本（如节后首日高开）。
      - 卖出：不受限价约束，信号次日以市价成交，确保止损/退出一定执行。
      - 同一时间只允许一个在途订单；订单未了结前不再产生新信号。

    平仓条件（多层级，按优先级）：
      1. 纯动态追踪止损（默认唯一出场）：跌破 stop_price → 立即平仓
         - 亏损侧（max_loss_stop_ratio 默认 0.30 启用）：初始止损 = 均价 × (1-30%) 最差地板，
           追踪止损只在盈利侧生效；首次亏损达 average_down_drop(10%) 时按当前持仓 1/4 摊低加仓
           （每笔交易仅一次，加仓成交后地板随新均价下移）
         - 基础追踪（盈利侧）：stop = 持仓以来最高价 - trail_atr_multiple × ATR（只上不下）
         - 动态止盈：浮盈(最高价-入场价)/ATR >= trail_tighten_profit_multiple 后，
           止损倍数收紧为 trail_tight_atr_multiple，
           即 stop = 最高价 - trail_tight_atr_multiple × ATR
         - 无固定止盈（take_profit_atr_multiple 默认 None），全程跟随趋势吃满波段
      2. 子类信号止损：populate_exit_trend() 中检查 stop_price 或策略专属出场信号

    风险提示（使用回测结果前必须理解，详见 spec.md 第 9 章）：
      1. 尾部风险：max_loss_stop_ratio 启用时单笔最差亏损可达账户 -15% 左右
         （30% 价格跌幅 × 约 50% 仓位敞口）；摊低加仓后敞口更高。
      2. 摊低加仓是均值回归假设：单边阴跌中加仓买在半山腰，地板止损失效，
         该机制针对低价标的假突破设计，不是普适的亏损补救手段。
      3. 信号价 != 成交价：决策基于 T 日收盘，成交在 T+1；限价单未成交即放弃，
         实盘连续跳空时可能长期无法建仓，建仓节奏与回测不可比。
      4. 只做多、无对冲、T+1 与涨跌停约束：系统性下跌中无盈利来源，
         极端行情下价格连续性假设不成立。
      5. 过拟合：寻优最优参数只保证历史表现，外样本/滚动校验只能缓解不能消除。
    """

    params = (
        ("atr_period", 14),                        # ATR 计算周期
        ("max_risk_ratio", 0.02),                  # 单笔最大风险占总资金比例
        # ---- 亏损侧机制（低价标的：放宽最差止损 + 首次亏损摊低加仓）----
        ("max_loss_stop_ratio", 0.30),             # 最差止损：收盘价较持仓均价亏损达 30% 才止损（None=ATR 初始止损；启用时追踪止损只在盈利侧生效）
        ("average_down_drop", 0.10),               # 首次亏损达该比例时摊低加仓（None 关闭；每次交易只加一次）
        ("average_down_ratio", 0.25),              # 摊低加仓量 = 当前持仓 × 该比例
        # ---- 止盈止损参数 ----
        ("take_profit_atr_multiple", None),        # 固定止盈距离（ATR 倍数）；默认 None=纯追踪止损
        ("trail_tighten_profit_multiple", None),   # 动态止盈激活门槛：浮盈达该 ATR 倍数后收紧止损；None 关闭
        ("trail_tight_atr_multiple", 0.8),         # 动态止盈激活后的收紧追踪止损 ATR 倍数
        # ---- 全局风控保护（对齐 freqtrade Protections；默认全部关闭；参数名对齐 freqtrade *_candles 口径）----
        ("cooldown_period_candles", None),              # CooldownPeriod.stop_duration_candles：卖出成交后 N 根 K 线内禁止新开仓
        ("stoploss_guard_trade_limit", None),           # StoplossGuard.trade_limit：回看窗口内止损平仓达该次数即暂停开仓
        ("stoploss_guard_lookback_period_candles", 60), #   StoplossGuard.lookback_period_candles 回看窗口（K 线数）
        ("stoploss_guard_stop_duration_candles", 24),   #   StoplossGuard.stop_duration_candles 暂停时长（K 线数）
        ("max_allowed_drawdown", None),                 # MaxDrawdown.max_allowed_drawdown：净值自峰值回撤超该比例期间禁止新开仓
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
        # 持仓成本跟踪（按实际成交加权），用于买入均价与卖出盈亏
        self._hold_cost = 0.0
        self._hold_size = 0
        # 平仓原因（_close_position 写入，notify_trade 消费到 trade_log）
        self.exit_reason = None
        # 在途订单状态机：None 表示无在途订单；否则为
        # {"ref": order.ref, "side": "BUY"/"SELL", "action_index": 行索引, "atr_multiple": 买入止损倍数}
        self._pending_order = None
        # notify_order 收到的终态事件，在 next() 顶部统一处理（此时指标/bar 索引可用）
        self._order_events = []
        # 止损 / 止盈价
        self.stop_price = None
        self.take_price = None
        # 入场参考信息
        self.entry_price = None
        self.entry_bar = None
        self.entry_atr_multiple = None
        # 动态止盈是否已激活（收紧追踪止损），用于卖出原因标注
        self._trail_tightened = False
        # 亏损侧最差止损地板（max_loss_stop_ratio 启用时非 None；摊低加仓后随地板下移）
        self._loss_floor = None
        # 本次交易是否已执行过摊低加仓（每笔交易只加一次）
        self._averaged_down = False
        # 全局风控保护状态（freqtrade Protections 对齐）
        self._cooldown_until_candle = -1  # 冷却截止 K 线索引（含），卖出成交后设定
        self._stop_exit_bars = []        # 止损平仓成交的 K 线索引（StoplossGuard 回看用）
        self._guard_until_candle = -1    # StoplossGuard 暂停截止 K 线（含）
        self._equity_peak = None         # 净值峰值（MaxDrawdown 用）
        self._last_exit_reason = None    # 最近一笔平仓原因（notify_trade 留存，供 Protections 判定）
        # 子类专属指标
        self.populate_indicators()

    def populate_indicators(self):
        """子类重写：初始化策略专属指标（如均线、动量、布林带）"""

    # ===================== 订单与交易回调 =====================
    # 订单终态 -> action_log 状态文案
    _ORDER_FAIL_STATUS = {
        "Expired": "EXPIRED",    # 限价单次日未成交，自动放弃
        "Canceled": "CANCELED",
        "Margin": "MARGIN",
        "Rejected": "REJECTED",
    }

    def notify_order(self, order):
        """收集订单终态事件（成交/失败），在 next() 顶部统一处理"""
        if order.status in [order.Submitted, order.Accepted, order.Partial]:
            return
        exec_dt = bt.num2date(order.executed.dt).date() if getattr(order.executed, "dt", None) else None
        status_name = order.getstatusname(order.status)
        self._order_events.append(
            {
                "ref": order.ref,
                "status_name": status_name,
                "is_buy": order.isbuy(),
                "exec_price": order.executed.price,
                "exec_size": abs(int(order.executed.size)),
                "exec_dt": exec_dt,
            }
        )
        # 卖出成交：立即记录实际成交价。notify_trade（平仓交易记录）与本事件同批
        # 送达且先于 next() 的 _process_order_events，若不在回调里同步写回，
        # trade_log 会读到上一笔交易的陈旧 exit_price
        if not order.isbuy() and status_name == "Completed" and order.executed.price:
            self.exit_price = order.executed.price

    def notify_trade(self, trade):
        """持仓平仓时生成标准交易记录，并把净盈亏回填到最近一笔 SELL action 行"""
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
                    "exit_reason": self.exit_reason,
                }
            )
            # 回填净盈亏到对应的 SELL action 行（时序无关：倒序找第一笔未回填的 SELL）
            for row in reversed(self.action_log):
                if row["side"] == "SELL" and row.get("net_profit_loss") is None:
                    row["net_profit_loss"] = round(trade.pnlcomm, 2)
                    row["net_return"] = round(profit_rate, 4)
                    break
            self.entry_size = None
            self.exit_price = None
            # 留存本次平仓原因供 _process_order_events 的 Protections 判定（本回调先于其执行）
            self._last_exit_reason = self.exit_reason
            self.exit_reason = None
        except Exception as e:
            print(f"Trade record parsing error: {e}, trade={trade}")

    def _next_session_date(self):
        """返回下一交易日的日期（限价单有效期）；无后续 K 线时返回 None"""
        next_index = len(self)  # 当前 bar 是数组索引 len(self)-1，下一根为 len(self)
        dt_array = self.data.datetime.array
        if next_index >= len(dt_array):
            return None
        return bt.num2date(dt_array[next_index]).date()

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

    def _new_action_row(self, side, price, size, reason):
        """创建一行整合日志（信号触发信息 + 待回填的实际成交信息）"""
        return {
            "date": self.data.datetime.date(0),
            "side": side,
            "trigger_price": price,
            "size": size,
            "reason": reason,
            "status": "PENDING",
            "exec_date": None,
            "exec_price": None,
            "exec_size": None,
            "avg_cost": None,
            "net_profit_loss": None,
            "net_return": None,
        }

    def _open_position(self, atr_multiple, reason=""):
        """挂【次日限价买单】：以触发收盘价为限价，仅下一交易日有效，未成交自动放弃"""
        if self._pending_order is not None:
            return
        size = self._position_size(atr_multiple)
        if size <= 0:
            return
        next_session = self._next_session_date()
        if next_session is None:
            return  # 最后一个 bar：没有下一交易日可挂单
        trigger_price = self.data.close[0]
        order = self.buy(size=size, exectype=bt.Order.Limit, price=trigger_price, valid=next_session)
        action_index = len(self.action_log)
        self.action_log.append(self._new_action_row("BUY", trigger_price, size, reason))
        self._pending_order = {
            "ref": order.ref,
            "side": "BUY",
            "action_index": action_index,
            "atr_multiple": atr_multiple,
        }

    def _close_position(self, reason=""):
        """挂【次日市价卖单】（卖出不受限价约束）；实际成交与盈亏在成交后回填。

        reason 统一附加 _exit_context() 复盘上下文（盈亏标签/均价/峰值/回吐/持仓天数），
        同时写入 action_log 与 trade_log.exit_reason。
        """
        if self._pending_order is not None:
            return
        order = self.close()
        trigger_price = self.data.close[0]
        size = self.position.size
        reason = reason + self._exit_context()
        action_index = len(self.action_log)
        self.action_log.append(self._new_action_row("SELL", trigger_price, size, reason))
        self._pending_order = {"ref": order.ref, "side": "SELL", "action_index": action_index, "atr_multiple": None}
        self.exit_reason = reason

    def _process_order_events(self):
        """在 next() 顶部处理订单终态：成交则回填实际值并初始化/重置持仓状态"""
        events = self._order_events
        self._order_events = []
        for event in events:
            pending = self._pending_order
            if pending is None or pending["ref"] != event["ref"]:
                continue
            row = self.action_log[pending["action_index"]]
            if event["status_name"] == "Completed":
                row["status"] = "FILLED"
                row["exec_date"] = event["exec_dt"]
                row["exec_price"] = round(event["exec_price"], 4)
                row["exec_size"] = event["exec_size"]
                if event["is_buy"]:
                    if self._hold_size == 0:
                        self._init_entry_on_fill(event["exec_price"], event["exec_size"], pending["atr_multiple"])
                    else:
                        # 持仓期间的买入 = 摊低加仓成交
                        self._update_add_on_fill(event["exec_price"], event["exec_size"])
                    row["avg_cost"] = round(self._hold_cost / self._hold_size, 4) if self._hold_size else None
                else:
                    avg_cost = self._hold_cost / self._hold_size if self._hold_size > 0 else None
                    row["avg_cost"] = round(avg_cost, 4) if avg_cost else None
                    self.exit_price = event["exec_price"]
                    # Protections 状态：卖出成交记录冷却窗口与止损事件
                    bar_no = len(self) - 1
                    if self.p.cooldown_period_candles:
                        self._cooldown_until_candle = bar_no + int(self.p.cooldown_period_candles)
                    if self._last_exit_reason and "stop" in self._last_exit_reason:
                        self._stop_exit_bars.append(bar_no)
                    self._last_exit_reason = None
                    self._reset_position_state()
            else:
                # 限价单未成交放弃 / 被拒 / 保证金不足：买入未建仓；卖出失败则保留持仓等下根 bar 重试
                row["status"] = self._ORDER_FAIL_STATUS.get(event["status_name"], event["status_name"])
                if not event["is_buy"]:
                    self.exit_reason = None
            self._pending_order = None

    def _init_entry_on_fill(self, fill_price, fill_size, atr_multiple):
        """买单实际成交后初始化持仓状态（成交价、止损/止盈均以成交 bar 数据为准）"""
        self.entry_size = fill_size
        self._hold_cost += fill_price * fill_size
        self._hold_size += fill_size
        atr_val = self.atr[0]
        self.entry_price = fill_price
        self.entry_bar = len(self) - 1
        self.entry_atr_multiple = atr_multiple
        self._trail_tightened = False
        self._averaged_down = False
        if self.p.max_loss_stop_ratio:
            # 亏损侧放宽：初始止损 = 最差地板（均价 × (1-30%)），追踪止损只在盈利侧生效
            self._loss_floor = fill_price * (1 - self.p.max_loss_stop_ratio)
            self.stop_price = self._loss_floor
        elif atr_val is not None and not math.isnan(atr_val) and atr_val > 0:
            self._loss_floor = None
            self.stop_price = fill_price - atr_multiple * atr_val
        else:
            # 理论不可达（信号通过要求 ATR 有效），防御性兜底
            self._loss_floor = None
            self.stop_price = fill_price * 0.9
        if self.p.take_profit_atr_multiple and atr_val is not None and not math.isnan(atr_val) and atr_val > 0:
            self.take_price = fill_price + atr_val * self.p.take_profit_atr_multiple
        else:
            self.take_price = None

    def _update_add_on_fill(self, fill_price, fill_size):
        """摊低加仓成交：更新加权均价与总规模，并把亏损侧地板调整到新均价"""
        self._hold_cost += fill_price * fill_size
        self._hold_size += fill_size
        self.entry_size = (self.entry_size or 0) + fill_size
        self._averaged_down = True
        if self.p.max_loss_stop_ratio and self._hold_size > 0:
            new_floor = (self._hold_cost / self._hold_size) * (1 - self.p.max_loss_stop_ratio)
            # 止损仍停在旧地板上（尚未锁定盈利）时允许随地板下移，维持"最差 -30%"口径；
            # 若追踪止损已在地板上方（盈利锁定），保持不动
            if self._loss_floor is not None and self.stop_price <= self._loss_floor:
                self.stop_price = new_floor
            self._loss_floor = new_floor

    def _reset_position_state(self):
        """卖出成交后重置全部持仓状态"""
        self._hold_cost = 0.0
        self._hold_size = 0
        self.stop_price = None
        self.take_price = None
        self.entry_price = None
        self.entry_bar = None
        self.entry_atr_multiple = None
        self._trail_tightened = False
        self._loss_floor = None
        self._averaged_down = False

    def _trail_stop_reason(self):
        """生成追踪止损卖出原因（含是否已动态收紧），供子类 populate_exit_trend 使用"""
        # 止损仍停在最差地板上 = 亏损侧最大容忍度触发
        if self._loss_floor is not None and self.stop_price <= self._loss_floor:
            return (
                f"max_loss_stop: close {self.data.close[0]:.2f} <= floor {self.stop_price:.2f} "
                f"(worst -{self.p.max_loss_stop_ratio * 100:.0f}%)"
            )
        tag = "trail_stop_tightened" if self._trail_tightened else "trail_stop"
        return f"{tag}: close {self.data.close[0]:.2f} <= stop {self.stop_price:.2f}"

    def _exit_context(self):
        """平仓原因的复盘上下文：信号时盈亏标签 + 均价/峰值/回吐/持仓天数。

        返回形如 " | PROFIT +5.6%, avg_cost 4.18, peak 5.10, gaveback -9.4%, held 12d"
        的后缀字符串；摊低加仓过则附上初始入场价。由 _close_position 统一拼接，
        所有卖出路径（追踪止损/最差地板/固定止盈/子类信号）自动携带。
        """
        close = self.data.close[0]
        if self._hold_size <= 0 or self.entry_bar is None:
            return ""
        avg_cost = self._hold_cost / self._hold_size
        pnl_rate = close / avg_cost - 1
        tag = "PROFIT" if pnl_rate >= 0 else "LOSS"
        parts = [f"{tag} {pnl_rate:+.1%}", f"avg_cost {avg_cost:.2f}"]
        if self._averaged_down and self.entry_price is not None:
            parts.append(f"initial_entry {self.entry_price:.2f}")
        bar_count = (len(self) - 1) - self.entry_bar + 1
        high_series = self.data.high.get(size=bar_count)
        peak = max(high_series)
        parts.append(f"peak {peak:.2f}")
        if peak > 0:
            parts.append(f"gaveback {(close / peak - 1) * 100:+.1f}%")
        parts.append(f"held {(len(self) - 1) - self.entry_bar}d")
        return " | " + ", ".join(parts)

    # ===================== 主循环（模板方法） =====================
    def next(self):
        # 记录每日净值
        self.equity_log.append({"datetime": self.data.datetime.date(0), "equity": self.broker.getvalue()})
        # 先处理上一阶段挂单的成交/过期结果（成交时初始化或重置持仓状态）
        self._process_order_events()
        # 有在途订单（如等待次日成交的限价单）时，本 bar 不产生新决策
        if self._pending_order is not None:
            return
        if not self.position:
            # 基类不设方向性入场门控：入场过滤完全由各策略 populate_entry_trend 自管；
            # 仅挂全局风控保护（freqtrade Protections：冷却/止损守卫/最大回撤）
            if self._protections_allow_entry():
                self.populate_entry_trend()
            return
        # 固定止盈：显式配置（非 None）且收盘价触及时立即平仓（默认关闭，纯追踪止损）
        if self.take_price is not None and self.data.close[0] >= self.take_price:
            self._close_position(f"take_profit: close {self.data.close[0]:.2f} >= take_price {self.take_price:.2f}")
            return
        # 追踪止损 + 动态止盈：更新 stop_price（只上不下），供子类 populate_exit_trend 检查
        self._update_trailing_stop()
        self.populate_exit_trend()
        # 摊低加仓：出场优先；仍持仓且无在途订单时，首次亏损达阈值按比例加仓摊低成本
        if self._pending_order is None and self.position and self._loss_floor is not None \
                and self.p.average_down_drop is not None and not self._averaged_down and self._hold_size > 0:
            avg_cost = self._hold_cost / self._hold_size
            if self.data.close[0] <= avg_cost * (1 - self.p.average_down_drop):
                self._average_down(avg_cost)

    def _average_down(self, avg_cost):
        """首次亏损达 average_down_drop 时摊低加仓：按当前持仓 × average_down_ratio 挂次日限价单"""
        add_size = int(self.position.size * self.p.average_down_ratio)
        if add_size <= 0:
            return
        next_session = self._next_session_date()
        if next_session is None:
            return
        trigger_price = self.data.close[0]
        reason = (
            f"average_down: loss {abs(trigger_price / avg_cost - 1) * 100:.1f}% "
            f">= {self.p.average_down_drop * 100:.0f}%, add {self.p.average_down_ratio:.0%} position"
        )
        order = self.buy(size=add_size, exectype=bt.Order.Limit, price=trigger_price, valid=next_session)
        action_index = len(self.action_log)
        self.action_log.append(self._new_action_row("BUY", trigger_price, add_size, reason))
        self._pending_order = {"ref": order.ref, "side": "BUY", "action_index": action_index, "atr_multiple": None}

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
                if tight_stop > candidate_stop:
                    self._trail_tightened = True
                candidate_stop = max(candidate_stop, tight_stop)
        # 亏损侧放宽启用时：追踪止损只在盈利侧生效（candidate 高于持仓均价才 ratchet），
        # 否则紧 ATR 止损会先于最差地板触发，亏损侧放宽形同虚设
        if self._loss_floor is not None:
            avg_cost = self._hold_cost / self._hold_size if self._hold_size > 0 else self.entry_price
            if candidate_stop <= avg_cost:
                return
        # 只上不下（ratchet）
        if candidate_stop > self.stop_price:
            self.stop_price = candidate_stop

    def _protections_allow_entry(self):
        """全局风控保护（对齐 freqtrade Protections）：冷却期 / 止损守卫 / 最大回撤，任一触发则禁止新开仓。

        全部默认关闭；只拦开仓，不影响平仓。仅基于已成交事实（卖出成交 bar、止损事件、净值回撤），
        不含任何方向性判断。
        """
        bar_no = len(self) - 1
        # CooldownPeriod：卖出成交后 N 根 K 线冷却
        if bar_no <= self._cooldown_until_candle:
            return False
        # StoplossGuard：暂停期内直接拒绝
        if bar_no <= self._guard_until_candle:
            return False
        if self.p.stoploss_guard_trade_limit:
            cutoff = bar_no - int(self.p.stoploss_guard_lookback_period_candles)
            recent_stops = sum(1 for b in self._stop_exit_bars if b >= cutoff)
            if recent_stops >= int(self.p.stoploss_guard_trade_limit):
                self._stop_exit_bars = []  # 清零重新计数，避免暂停期内重复触发
                self._guard_until_candle = bar_no + int(self.p.stoploss_guard_stop_duration_candles)
                return False
        # MaxDrawdown：净值自峰值回撤超阈值期间禁止开仓（恢复后自动解禁）
        if self.p.max_allowed_drawdown:
            equity = self.broker.getvalue()
            self._equity_peak = equity if self._equity_peak is None else max(self._equity_peak, equity)
            if self._equity_peak > 0 and (self._equity_peak - equity) / self._equity_peak > self.p.max_allowed_drawdown:
                return False
        return True

    def populate_entry_trend(self):
        """子类重写：空仓时判断是否开仓；满足条件调用 self._open_position(atr_multiple)"""
        raise NotImplementedError

    def populate_exit_trend(self):
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
