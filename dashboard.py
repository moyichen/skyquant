"""SkyQuant 回测看板主页（本地 HTTP 服务）。

用法：
    python dashboard.py                # 启动看板（默认 0.0.0.0:8765，局域网可访问，自动开浏览器）
    python dashboard.py --port 9000
    python dashboard.py --host 127.0.0.1   # 仅本机访问
    python dashboard.py --no-browser

特性：
  - 按 config.yaml 的 sector 分组罗列全部标的；
  - 已生成报告的标的可点击进入，未生成的标的灰色不可点；
  - 同一标的的 trend/range/breakout 报告通过选项卡切换（回测报告 / 交互K线）；
  - 每次刷新（或每 10 秒轮询）实时扫描 output/plots，报告逐步生成、逐步可点；
  - 主页顶部展示 live_trades.csv 聚合的当前实盘持仓（含最新收盘价/市值/浮动盈亏）；
  - 「控制台」面板：网页端更新数据（fetch）、触发回测（backtest）、编辑策略参数
    （写入 params/active.yaml 生效参数集）、管理参数组（生效/另存草稿/删除，
    参数集目录 params/active.yaml · drafts/ · experiments/），任务状态与日志实时轮询。
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from comm import (PLOT_DIR, PROJECT_ROOT, apply_broker_settings,
                  build_commission, setup_logging, LOG_FILE)
from live_trading import compute_holdings
from strategy import STRATEGY_MAPPING, BaseStrategy

setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)

# 展示口径固定为全量已注册策略（与 strategy.ACTIVE_STRATEGIES 顺序一致）
STRATEGY_IDS = ["trend", "range", "breakout"]
STRATEGY_CN = {"trend": "趋势", "range": "震荡", "breakout": "突破"}
REGIME_CN = {"trend": "趋势", "range": "震荡", "breakout": "突破",
             "unclassified": "未分类"}

STOCK_CACHE_DIR = PROJECT_ROOT / "cache" / "stock_cache"
LIVE_TRADES_CSV = PROJECT_ROOT / "live_trades.csv"
EQUITY_DIR = PROJECT_ROOT / "output" / "equity_curve"
LIVE_REPORT_HTML = PLOT_DIR / "live_portfolio_report.html"

UP_RED = "#F04848"
DOWN_GREEN = "#0EAE7C"


# ===================== 网页端任务控制台 =====================
# 按钮与 skyquant.py 子命令一一对应，保持各命令独立性：fetch 只拉数据、
# backtest 只跑回测。全局同一时刻只允许一个任务运行，避免并行读写缓存。
TASK_DEFS = {
    "fetch": {"label": "更新数据"},
    "backtest": {"label": "触发回测"},
}
TASK_HISTORY_MAX = 8
TASK_LOG_TAIL = 300  # 返回给前端的最大日志行数


class TaskManager:
    """后台任务管理器：subprocess 跑 skyquant.py 子命令，捕获输出供前端轮询。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = None  # dict，运行中或最近一次完成的任务
        self.recent = deque(maxlen=TASK_HISTORY_MAX)

    def start(self, name, stock_list=None, strategy=None, force_refresh=False):
        if name not in TASK_DEFS:
            return None, f"未知任务: {name}"
        if stock_list and not re.fullmatch(r"\d{6}(\s*,\s*\d{6})*", stock_list):
            return None, "标的代码格式错误（应为 6 位数字、逗号分隔）"
        if strategy and strategy not in STRATEGY_IDS:
            return None, f"未知策略: {strategy}"
        with self._lock:
            if self.current and self.current["status"] == "running":
                return None, f"「{self.current['label']}」正在运行中，请等待完成"
            cmd = [sys.executable, "-u", "skyquant.py", name]
            if stock_list:
                cmd += ["--stock-list", re.sub(r"\s+", "", stock_list)]
            if strategy and name == "backtest":
                cmd += ["--strategy", strategy]
            if force_refresh:
                cmd += ["--force-refresh"]
            task = {
                "id": uuid.uuid4().hex[:8],
                "name": name,
                "label": TASK_DEFS[name]["label"],
                "cmd": " ".join(cmd),
                "status": "running",
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None,
                "returncode": None,
                "pid": None,
                "log": deque(maxlen=2000),
            }
            if self.current:  # 上一个已完成的任务进入历史
                self.recent.appendleft(self.current)
            self.current = task
        threading.Thread(target=self._run, args=(task, cmd), daemon=True).start()
        return task, None

    def _run(self, task, cmd):
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            task["pid"] = proc.pid
            for line in proc.stdout:
                task["log"].append(line.rstrip("\n"))
            task["returncode"] = proc.wait()
        except Exception as e:
            task["log"].append(f"[dashboard] 任务启动/运行异常: {e}")
            task["returncode"] = -1
        task["status"] = "success" if task["returncode"] == 0 else "failed"
        task["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def snapshot(self):
        with self._lock:
            return {"current": self._pack(self.current),
                    "recent": [self._pack(t) for t in self.recent]}

    @staticmethod
    def _pack(t):
        if t is None:
            return None
        d = {k: v for k, v in t.items() if k != "log"}
        d["log"] = list(t["log"])[-TASK_LOG_TAIL:]
        return d


TASK_MANAGER = TaskManager()


# ---------- 策略参数读取 / 写回（保留 config.yaml 注释） ----------
_PARAM_SCHEMA_CACHE = None
_CONFIG_LOCK = threading.Lock()


# 参数中文名映射：页面标签「中文 + 英文」对照展示（英文参数名为准，中文助记）
PARAM_CN = {
    # BaseStrategy 通用（基础风控）
    "atr_period": "ATR 周期",
    "max_risk_ratio": "单笔风险占比",
    "max_loss_stop_ratio": "最差止损线",
    "average_down_drop": "摊低加仓触发跌幅",
    "average_down_ratio": "摊低加仓比例",
    "take_profit_atr_multiple": "固定止盈 ATR 倍数",
    "trail_tighten_profit_multiple": "动态止盈激活门槛",
    "trail_tight_atr_multiple": "收紧追踪止损倍数",
    "cooldown_period_candles": "冷却期 K 线数",
    "stoploss_guard_trade_limit": "止损保护次数上限",
    "stoploss_guard_lookback_period_candles": "止损保护回看窗口",
    "stoploss_guard_stop_duration_candles": "止损保护暂停时长",
    "max_allowed_drawdown": "最大回撤熔断",
    # trend 策略专属
    "trail_atr_multiple": "追踪止损 ATR 倍数",
    "momentum_period": "动量确认周期",
    "ema_fast": "快线 EMA 周期",
    "ema_slow": "慢线 EMA 周期",
    "macd_fast": "MACD 快线周期",
    "macd_slow": "MACD 慢线周期",
    "macd_signal": "MACD 信号周期",
    "macd_momentum_bars": "MACD 动量确认 K 线数",
    "min_volatility_ratio": "最小波动率过滤",
    "adx_period": "ADX 周期",
    "adx_min": "ADX 趋势强度下限",
    # range 策略专属
    "bb_period": "布林带周期",
    "drop_ratio": "急跌阈值",
    # breakout 策略专属
    "breakout_period": "唐奇安通道周期",
}


def _param_schema():
    """{sid: [{name, cn, default, group}]}：策略专属参数在前，BaseStrategy 通用参数在后。"""
    global _PARAM_SCHEMA_CACHE
    if _PARAM_SCHEMA_CACHE is None:
        base_names = {n for n, _ in BaseStrategy.params._getitems()}
        schema = {}
        for sid, cls in STRATEGY_MAPPING.items():
            items = list(cls.params._getitems())
            schema[sid] = (
                [{"name": n, "cn": PARAM_CN.get(n, ""), "default": d, "group": "strategy"} for n, d in items if n not in base_names]
                + [{"name": n, "cn": PARAM_CN.get(n, ""), "default": d, "group": "base"} for n, d in items if n in base_names]
            )
        _PARAM_SCHEMA_CACHE = schema
    return _PARAM_SCHEMA_CACHE


def _coerce_param(raw: str, default):
    """按默认值类型把表单字符串转回 Python 类型（默认 None 则尽力转数值）。"""
    if isinstance(default, bool):
        v = raw.lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"无法解析为布尔值: {raw!r}")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    if default is None:
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw
    return raw


def _apply_param_update(code: str, strategy: str, values: dict):
    """把 (code, strategy) 的参数修改写入 params/active.yaml 生效参数集。

    values: {参数名: 字符串}；空字符串 = 删除该覆盖项（回退策略类默认值）。
    参数集文件由 config_store 统一 yaml.safe_dump（无注释需要保留），
    config.yaml 固定配置不再被触碰。
    """
    import config_store as cs

    if strategy not in STRATEGY_IDS:
        raise ValueError(f"未知策略: {strategy}")
    schema = {p["name"]: p["default"] for p in _param_schema()[strategy]}
    cfg = cs.base_config()
    valid_codes = {str(s["code"]) for s in cfg.get("stock_list", [])}
    if code not in valid_codes:
        raise ValueError(f"标的 {code} 不在 stock_list 中")

    # 以 active.yaml 当前该单元为底（不存在={}），套用本次表单改动
    current = cs.load_param_set()["strategy_params"].get(code, {}).get(strategy, {})
    strat_p = dict(current)
    for name, raw in values.items():
        if name not in schema:
            raise ValueError(f"策略 {strategy} 无参数 {name}")
        raw = str(raw).strip()
        if raw == "":
            strat_p.pop(name, None)
        else:
            try:
                strat_p[name] = _coerce_param(raw, schema[name])
            except ValueError:
                raise ValueError(f"参数 {name} 的值无法解析: {raw!r}")

    with _CONFIG_LOCK:
        cs.update_active_entry(code, strategy, strat_p or None)


# ---------- 试跑回测（不落盘参数，仅生成预览图表） ----------
_PREVIEW_LOCK = threading.Lock()  # 同一时刻只跑一个预览回测，避免 Cerebro 冲突


def run_preview_backtest(code: str, strategy_id: str, params: dict) -> dict:
    """用自定义参数跑单标的回测，返回 KPI + 预览交互 K线地址。

    不写参数文件、不写 equity CSV、不写完整报告，只输出一份
    {code}_preview_{strategy}_interactive.html 到 output/plots，避免覆盖已保存结果。
    params: 已转好类型的策略参数字典（空=全用类默认值）。
    """
    import backtrader as bt
    import config_store as cs
    from dataprovider import AStockData, DataProvider
    from report import calc_metrics, render_interactive_chart

    cfg = cs.load_config()
    gs = cfg["global_setting"]
    stock_info = next((s for s in cfg["stock_list"] if str(s["code"]) == code), None)
    if not stock_info:
        raise ValueError(f"标的 {code} 不在 stock_list 中")

    dp = DataProvider()
    df = dp.fetch_stock(code, force_refresh=False)
    if df is None or df.empty:
        raise ValueError(f"无法获取 {code} 的行情数据，请先 fetch")

    # 板块指数叠加（尽力而为）
    index_df, index_label = None, None
    si = stock_info.get("sector_index")
    if si:
        try:
            index_df = dp.fetch_index(si, force_refresh=False)
            index_label = f"{stock_info.get('sector_index_name','')} ({si})".strip()
        except Exception:
            pass

    cerebro = bt.Cerebro()
    cerebro.addstrategy(STRATEGY_MAPPING[strategy_id], **params)

    feed = AStockData(
        dataname=df, datetime="datetime", open="open", high="high",
        low="low", close="close", volume="volume", timeframe=bt.TimeFrame.Days,
    )
    cerebro.adddata(feed)

    comminfo = build_commission(cfg)
    apply_broker_settings(cerebro.broker, cfg, float(gs["initial_capital"]), comminfo)

    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="tradeanalyzer")
    cerebro.addanalyzer(bt.analyzers.SQN, _name="sqn")

    inst = cerebro.run()[0]
    equity_df = inst.get_equity_dataframe()
    trades_df = inst.get_trade_dataframe()
    action_df = inst.get_action_dataframe()

    metrics = calc_metrics(equity_df, trades_df, price_series=df["close"])

    # SQN 来自 analyzer（calc_metrics 不产出）
    try:
        sqn_an = getattr(inst.analyzers, "sqn", None)
        if sqn_an is not None:
            sqn_val = sqn_an.get_analysis().get("sqn")
            metrics["sqn"] = round(float(sqn_val), 4) if sqn_val is not None else None
    except Exception:
        metrics["sqn"] = None

    # 清洗 NaN / inf，否则 json.dumps 输出 Infinity 导致前端 JSON.parse 失败
    import math
    for k, v in list(metrics.items()):
        if isinstance(v, float):
            if math.isnan(v):
                metrics[k] = None
            elif math.isinf(v):
                metrics[k] = "inf" if v > 0 else "-inf"

    # 预览图表用独立文件名，不覆盖已保存的 {code}_{strategy}_interactive.html
    chart_path = render_interactive_chart(
        inst, PLOT_DIR, f"{code}_preview", strategy_id, df, trades_df,
        stock_name=stock_info.get("name"), index_df=index_df, index_label=index_label,
        action_df=action_df,
    )
    return {
        "metrics": metrics,
        "chart_url": f"/plots/{Path(chart_path).name}",
        "n_trades": int(len(trades_df)) if trades_df is not None else 0,
        "final_value": float(cerebro.broker.getvalue()),
    }


# ===================== 状态采集 =====================
def _metrics_from_equity(code: str, strategy: str) -> dict | None:
    """从 equity curve CSV 实时计算核心指标（不依赖 metrics_summary.csv 快照）。"""
    path = EQUITY_DIR / f"{code}_{strategy}_equity.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        if df.empty or len(df) < 2:
            return None
        equity = df["equity"]
        total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)

        daily_ret = equity.pct_change().dropna()
        # Sharpe (annualized, risk-free 2%)
        daily_rf = (1 + 0.02) ** (1 / 365) - 1
        excess = daily_ret - daily_rf
        sharpe = float((252 ** 0.5) * excess.mean() / excess.std()) if excess.std() != 0 else 0.0

        # Max drawdown
        cummax = equity.cummax()
        drawdown = (equity - cummax) / cummax
        max_dd = float(drawdown.min())

        return {
            "total_return": round(total_return, 4),
            "sharpe": round(sharpe, 2),
            "max_drawdown": round(max_dd, 4),
        }
    except Exception as e:
        logger.warning(f"dashboard: metrics calc failed for {code}/{strategy}: {e}")
        return None


def _round(v, ndigits):
    try:
        if pd.isna(v):
            return None
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _last_close(code: str):
    """从本地行情缓存取最新收盘价 (close, date_str)，取不到返回 (None, None)。"""
    cache_file = STOCK_CACHE_DIR / f"{code}.csv"
    if not cache_file.exists():
        return None, None
    try:
        df = pd.read_csv(cache_file, usecols=["datetime", "close"])
        if df.empty:
            return None, None
        last = df.iloc[-1]
        date_str = str(pd.to_datetime(last["datetime"]).date())
        return float(last["close"]), date_str
    except Exception as e:
        logger.warning(f"dashboard: read cache {code} failed: {e}")
        return None, None


def _run_length(state: pd.Series, dates: pd.Series, warmup: int):
    """返回 (当前状态, 状态已持续交易日数, 状态起始日 str)。

    state: bool Series（True=多头/金叉）；warmup 之前为指标预热，不参与判定。
    持续天数 = 最新 bar 与最近一次状态翻转 bar 的索引差（翻转当日为 0）。
    窗口内从未翻转则以首个有效 bar 为起点。
    """
    valid = state.iloc[warmup:].astype(bool).reset_index(drop=True)
    vdates = pd.to_datetime(dates).iloc[warmup:].reset_index(drop=True)
    if valid.empty:
        return None
    current = bool(valid.iloc[-1])
    start_idx = 0
    for i in range(len(valid) - 1, 0, -1):
        if valid.iloc[i] != valid.iloc[i - 1]:
            start_idx = i
            break
    days = len(valid) - 1 - start_idx
    return current, int(days), vdates.iloc[start_idx].strftime("%Y-%m-%d")


def _latest_signals(code: str):
    """从本地行情缓存计算最新 MACD 金叉/死叉与 EMA20/60 多空排列及持续天数。

    口径与 report.py 交互K线、strategy/trend.py 完全一致：
      MACD: DIF=EMA12-EMA26，DEA=EMA9(DIF)，DIF 上穿 DEA=金叉
      EMA : EMA20 > EMA60 = 多头排列，反之为空头排列
    """
    cache_file = STOCK_CACHE_DIR / f"{code}.csv"
    if not cache_file.exists():
        return None
    try:
        df = pd.read_csv(cache_file, usecols=["datetime", "close"])
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        close = df["close"]
        if len(close) < 60:
            return None

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        ema60 = close.ewm(span=60, adjust=False).mean()

        dates = df["datetime"]
        out = {}
        macd = _run_length(dif > dea, dates, warmup=35)
        if macd is not None:
            bull, days, since = macd
            out["macd"] = {"bull": bull, "days": days, "since": since}
        ema = _run_length(ema20 > ema60, dates, warmup=60)
        if ema is not None:
            bull, days, since = ema
            out["ema"] = {"bull": bull, "days": days, "since": since}
        return out or None
    except Exception as e:
        logger.warning(f"dashboard: signal calc failed for {code}: {e}")
        return None


def build_state() -> dict:
    """扫描配置/报告目录/持仓，生成看板渲染所需的全部状态（每次请求实时计算）。"""
    import config_store as cs

    cfg = cs.load_config()  # config.yaml 固定配置 + params/active.yaml 生效参数集
    stock_list = cfg.get("stock_list", [])
    # metrics 从 equity curve 实时计算，不再读 metrics_summary.csv 快照

    # code -> 名称（持仓里的标的可能已被移出 stock_list，仍要能显示名字）
    name_map = {str(s["code"]): s.get("name", "") for s in stock_list}

    sectors = {}  # 保持 config 中板块首次出现的顺序
    sector_meta = {}
    for s in stock_list:
        code = str(s["code"])
        sector = s.get("sector") or "其他"
        sector_meta.setdefault(sector, s.get("sector_index_name") or "")
        strategies = {}
        any_report = False
        for sid in STRATEGY_IDS:
            report_file = PLOT_DIR / f"{code}_{sid}_report.html"
            inter_file = PLOT_DIR / f"{code}_{sid}_interactive.html"
            has_report = report_file.exists()
            has_inter = inter_file.exists()
            if has_report:
                any_report = True
            entry = {
                "report": has_report,
                "interactive": has_inter,
                "mtime": datetime.fromtimestamp(report_file.stat().st_mtime).strftime("%m-%d %H:%M")
                if has_report else None,
                "metrics": _metrics_from_equity(code, sid),
            }
            strategies[sid] = entry
        sectors.setdefault(sector, []).append({
            "code": code,
            "name": s.get("name", ""),
            "regime": s.get("regime"),
            "sector_index_name": s.get("sector_index_name") or "",
            "available": any_report,
            "strategies": strategies,
            "signals": _latest_signals(code),
        })

    sector_groups = [
        {"sector": sec, "index_name": sector_meta.get(sec, ""), "stocks": stocks}
        for sec, stocks in sectors.items()
    ]

    # ---- 实盘持仓（live_trades.csv 聚合 + 最新收盘价）----
    raw_holdings = compute_holdings(LIVE_TRADES_CSV)
    holdings = []
    total_cost = total_mv = 0.0
    for code, h in raw_holdings.items():
        size, avg_cost = int(h["size"]), float(h["avg_cost"])
        last_close, last_date = _last_close(code)
        cost_value = avg_cost * size
        mv = last_close * size if last_close is not None else None
        pnl = (last_close - avg_cost) * size if last_close is not None else None
        pnl_pct = (last_close / avg_cost - 1) if last_close is not None else None
        total_cost += cost_value
        if mv is not None:
            total_mv += mv
        holdings.append({
            "code": code,
            "name": name_map.get(code, code),
            "size": size,
            "avg_cost": round(avg_cost, 4),
            "last_close": round(last_close, 2) if last_close is not None else None,
            "last_date": last_date,
            "market_value": round(mv, 2) if mv is not None else None,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
        })
    holdings.sort(key=lambda x: x["code"])

    ready_count = sum(1 for g in sector_groups for s in g["stocks"] if s["available"])
    total_count = sum(len(g["stocks"]) for g in sector_groups)

    # ---- 策略参数编辑数据（schema + 当前覆盖值） ----
    # strategy_params 中每个 code 的值是一个 {strategy: {param: value}} mapping
    param_overrides = cfg.get("strategy_params", {})
    param_schema = _param_schema()
    param_codes = [str(s["code"]) for s in stock_list]

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sectors": sector_groups,
        "holdings": holdings,
        "totals": {
            "cost": round(total_cost, 2),
            "market_value": round(total_mv, 2) if total_mv else None,
            "pnl": round(total_mv - total_cost, 2) if total_mv else None,
            "pnl_pct": round(total_mv / total_cost - 1, 4) if total_mv and total_cost else None,
        },
        "live_report": LIVE_REPORT_HTML.exists(),
        "counts": {"ready": ready_count, "total": total_count},
        "strategy_ids": STRATEGY_IDS,
        "strategy_cn": STRATEGY_CN,
        "regime_cn": REGIME_CN,
        "param_schema": param_schema,
        "param_overrides": param_overrides,
        "param_codes": param_codes,
        "param_sets": cs.list_param_sets(),
    }


# ===================== 前端页面 =====================
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SkyQuant 回测看板</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f1419; color: #d4d4d4; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; font-size: 14px; }
  a { color: inherit; text-decoration: none; }

  /* ---------- 顶部 ---------- */
  header { padding: 18px 28px 14px; border-bottom: 1px solid #2a3548; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 22px; color: #fff; font-weight: 700; }
  header h1 .accent { color: #4FC3F7; }
  .header-meta { color: #6b7c93; font-size: 12px; }
  .header-spacer { flex: 1; }
  .header-link { color: #4FC3F7; font-size: 13px; border: 1px solid #2a3548; padding: 6px 14px; border-radius: 6px; }
  .header-link:hover { background: #1a2332; }

  /* ---------- 实盘持仓 ---------- */
  .holdings { padding: 16px 28px; background: #121a27; border-bottom: 1px solid #2a3548; }
  .section-label { font-size: 13px; font-weight: 600; color: #9fb3c8; margin-bottom: 10px; display: flex; align-items: center; gap: 10px; }
  .section-label .tag { font-size: 11px; font-weight: 400; color: #6b7c93; background: #1a2332; border: 1px solid #2a3548; padding: 1px 8px; border-radius: 10px; }
  .holding-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
  .holding-card { background: #1a2332; border: 1px solid #2a3548; border-radius: 8px; padding: 12px 14px; }
  .holding-card .hc-top { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
  .holding-card .hc-name { color: #fff; font-weight: 600; font-size: 15px; }
  .holding-card .hc-code { color: #6b7c93; font-size: 12px; }
  .holding-card .hc-row { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: #9fb3c8; line-height: 1.9; }
  .holding-card .hc-row > span:last-child { white-space: nowrap; text-align: right; }
  .holding-card .hc-row b { color: #d4d4d4; font-weight: 600; }
  .pnl-red { color: #F04848 !important; }
  .pnl-green { color: #0EAE7C !important; }
  .holding-summary { margin-top: 10px; display: flex; gap: 26px; flex-wrap: wrap; font-size: 13px; color: #9fb3c8; }
  .holding-summary b { font-size: 16px; margin-left: 6px; }
  .no-holdings { color: #6b7c93; font-size: 13px; padding: 4px 0; }

  /* ---------- 板块 / 标的卡片 ---------- */
  main { padding: 22px 28px 60px; }
  .sector-block { margin-bottom: 26px; }
  .sector-title { font-size: 16px; font-weight: 700; color: #fff; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; }
  .sector-title .idx-name { font-size: 12px; font-weight: 400; color: #6b7c93; }
  .sector-title .count { font-size: 12px; font-weight: 400; color: #6b7c93; }
  .stock-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(236px, 1fr)); gap: 12px; margin-top: 12px; }
  .stock-card { background: #1a2332; border: 1px solid #2a3548; border-radius: 10px; padding: 14px 16px; cursor: pointer; transition: border-color .15s, transform .15s; position: relative; }
  .stock-card:hover { border-color: #4FC3F7; transform: translateY(-2px); }
  .stock-card.disabled { opacity: .38; cursor: not-allowed; background: #141a24; }
  .stock-card.disabled:hover { border-color: #2a3548; transform: none; }
  .sc-top { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
  .sc-name { font-size: 16px; font-weight: 700; color: #fff; }
  .sc-code { font-size: 12px; color: #6b7c93; }
  .sc-tags { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .regime-tag { font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid #2E86AB; color: #7fc1e0; }
  .sig-tag { font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid; font-variant-numeric: tabular-nums; }
  .sig-bull { color: #EF5350; border-color: #7a3a3a; background: #2a1717; }
  .sig-bear { color: #26A69A; border-color: #2a6b62; background: #122622; }
  .holding-dot { font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid #8d6e00; color: #e8c45a; background: #241f10; }
  .sc-strats { display: flex; gap: 6px; flex-wrap: wrap; }
  .strat-badge { font-size: 11px; padding: 2px 9px; border-radius: 10px; background: #121a27; border: 1px solid #2a3548; color: #9fb3c8; }
  .sc-foot { margin-top: 10px; font-size: 11px; color: #6b7c93; }
  .sc-na { font-size: 12px; color: #5a6a80; }

  /* ---------- 详情视图 ---------- */
  #view-detail { display: none; height: calc(100vh - 0px); flex-direction: column; }
  .detail-bar { padding: 12px 24px; background: #121a27; border-bottom: 1px solid #2a3548; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  .back-btn { color: #4FC3F7; font-size: 13px; border: 1px solid #2a3548; background: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; }
  .back-btn:hover { background: #1a2332; }
  .detail-title { font-size: 18px; font-weight: 700; color: #fff; }
  .detail-title small { font-size: 12px; font-weight: 400; color: #6b7c93; margin-left: 8px; }
  .tab-row { display: flex; gap: 4px; flex-wrap: wrap; }
  .tab { padding: 6px 16px; font-size: 13px; border: 1px solid #2a3548; border-bottom: none; border-radius: 8px 8px 0 0; background: #141c28; color: #9fb3c8; cursor: pointer; }
  .tab.active { background: #1a2332; color: #fff; font-weight: 600; }
  .tab.disabled { opacity: .35; cursor: not-allowed; }
  .tab-sep { width: 1px; background: #2a3548; margin: 2px 8px; }
  .open-ext { margin-left: auto; color: #4FC3F7; font-size: 12px; border: 1px solid #2a3548; padding: 6px 12px; border-radius: 6px; }
  .open-ext:hover { background: #1a2332; }
  #detail-frame { flex: 1; width: 100%; border: none; background: #fff; }
  .detail-empty { flex: 1; display: flex; align-items: center; justify-content: center; color: #6b7c93; font-size: 14px; }

  /* ---------- 控制台 ---------- */
  .console-btn { color: #F5B041; font-size: 13px; border: 1px solid #2a3548; padding: 6px 14px; border-radius: 6px; background: none; cursor: pointer; font-family: inherit; }
  .console-btn:hover { background: #1a2332; }
  #console-panel { display: none; padding: 16px 28px 22px; background: #0c121c; border-bottom: 1px solid #2a3548; }
  #console-panel.open { display: block; }
  .console-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 12px; }
  .console-card { background: #1a2332; border: 1px solid #2a3548; border-radius: 8px; padding: 12px 14px; }
  .cc-title { font-size: 14px; font-weight: 600; color: #fff; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .cc-desc { font-size: 11px; font-weight: 400; color: #6b7c93; }
  .cc-opts { margin: 10px 0; display: flex; flex-direction: column; gap: 8px; font-size: 12px; color: #9fb3c8; }
  .cc-opts input[type=text], .cc-opts select { background: #0f1419; border: 1px solid #2a3548; border-radius: 5px; color: #d4d4d4; padding: 6px 8px; font-size: 12px; font-family: inherit; }
  .run-btn { background: #2E86AB; color: #fff; border: none; border-radius: 6px; padding: 7px 18px; font-size: 13px; cursor: pointer; font-family: inherit; }
  .run-btn:hover { background: #3a97bd; }
  .run-btn:disabled { opacity: .45; cursor: not-allowed; }
  #task-status { margin-top: 14px; font-size: 12px; color: #9fb3c8; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .status-badge { font-size: 11px; padding: 1px 9px; border-radius: 10px; border: 1px solid; white-space: nowrap; }
  .st-running { color: #4FC3F7; border-color: #2E86AB; animation: pulse 1.2s infinite; }
  @keyframes pulse { 50% { opacity: .5; } }
  .st-success { color: #0EAE7C; border-color: #1f6b58; }
  .st-failed { color: #F04848; border-color: #7a3a3a; }
  #task-log { display: none; margin-top: 10px; background: #080d14; border: 1px solid #2a3548; border-radius: 6px; padding: 10px 12px; font-size: 11.5px; line-height: 1.65; color: #8fa5bd; max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-all; font-family: "SF Mono", Menlo, Consolas, monospace; }
  .task-history { display: inline-flex; gap: 5px; align-items: center; flex-wrap: wrap; }
  /* 参数编辑 */
  #param-card { margin-top: 14px; }
  .param-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 10px 0 4px; }
  .param-bar select { background: #0f1419; border: 1px solid #2a3548; border-radius: 5px; color: #d4d4d4; padding: 6px 8px; font-size: 13px; font-family: inherit; }
  .param-tabs { display: flex; gap: 4px; }
  .param-tab { padding: 4px 12px; font-size: 12px; border: 1px solid #2a3548; border-radius: 6px; color: #9fb3c8; cursor: pointer; }
  .param-tab.active { background: #2E86AB; color: #fff; border-color: #2E86AB; }
  .param-group-label { font-size: 11px; color: #6b7c93; margin: 12px 0 6px; }
  .param-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 8px 14px; }
  .param-item { display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .param-item label { flex: 0 0 158px; line-height: 1.25; }
  .param-item label .cn { display: block; color: #c8d6e5; }
  .param-item label .en { display: block; font-size: 10px; color: #6b7c93; font-family: "SF Mono", Menlo, Consolas, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .param-item input { flex: 1; min-width: 0; background: #0f1419; border: 1px solid #2a3548; border-radius: 5px; color: #d4d4d4; padding: 4px 7px; font-size: 12px; font-family: inherit; }
  .param-item input.dirty { border-color: #F5B041; }
  .param-foot { margin-top: 14px; display: flex; align-items: center; gap: 12px; }
  #param-msg { font-size: 12px; color: #0EAE7C; }
  #param-msg.err { color: #F04848; }
  /* 试跑回测预览区：当前标的卡片 + KPI + K线 */
  #preview-section { margin-top: 16px; }
  .preview-stock { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 12px; background: #0f1622; border: 1px solid #2a3548; border-radius: 8px; margin-bottom: 12px; }
  .preview-stock .ps-name { font-size: 17px; font-weight: 700; color: #fff; }
  .preview-stock .ps-code { font-size: 12px; color: #6b7c93; }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; margin-bottom: 12px; }
  .kpi-card { background: #0f1622; border: 1px solid #2a3548; border-radius: 8px; padding: 10px 12px; }
  .kpi-card .k-label { font-size: 11px; color: #6b7c93; }
  .kpi-card .k-value { font-size: 18px; font-weight: 700; color: #fff; margin-top: 4px; font-variant-numeric: tabular-nums; }
  .kpi-card .k-value.pos { color: #F04848; }
  .kpi-card .k-value.neg { color: #0EAE7C; }
  .kpi-card .k-sub { font-size: 10px; color: #6b7c93; margin-top: 2px; }
  #preview-chart { width: 100%; min-height: 600px; border: 1px solid #2a3548; border-radius: 8px; background: #fff; display: block; }
  #preview-chart.empty { display: none; }
  .chart-ext { color: #4FC3F7; font-size: 11px; }
  /* 参数集管理表格 */
  .pset-group { font-size: 11px; color: #6b7c93; margin: 10px 0 4px; }
  .pset-row { display: flex; align-items: center; gap: 10px; padding: 6px 8px; border: 1px solid #2a3548; border-radius: 6px; margin-bottom: 6px; font-size: 12px; flex-wrap: wrap; }
  .pset-row.active-row { border-color: #2E86AB; background: #101c2a; }
  .pset-file { color: #9fb3c8; font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 11px; min-width: 210px; }
  .pset-meta { color: #6b7c93; font-size: 11px; }
  .pset-badge { font-size: 10px; padding: 0 7px; border-radius: 9px; border: 1px solid #2a3548; color: #6b7c93; white-space: nowrap; }
  .pset-badge.src-opt { color: #F5B041; border-color: #6b5a2a; }
  .pset-actions { margin-left: auto; display: flex; gap: 6px; align-items: center; }
  .mini-btn { font-size: 11px; padding: 3px 10px; border-radius: 5px; border: 1px solid #2a3548; background: #0f1622; color: #9fb3c8; cursor: pointer; font-family: inherit; }
  .mini-btn:hover { background: #1a2332; }
  .mini-btn.primary { color: #fff; background: #2E86AB; border-color: #2E86AB; }
  .mini-btn.danger { color: #F04848; }
  .preview-empty { color: #6b7c93; font-size: 13px; padding: 20px; text-align: center; border: 1px dashed #2a3548; border-radius: 8px; }
  .preview-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
</style>
</head>
<body>

<div id="view-home">
  <header>
    <h1>Sky<span class="accent">Quant</span> 回测看板</h1>
    <span class="header-meta" id="meta-line"></span>
    <span class="header-spacer"></span>
    <button class="console-btn" onclick="toggleConsole()">⚙ 控制台</button>
    <a class="header-link" id="live-link" href="/plots/live_portfolio_report.html" target="_blank" style="display:none">实盘组合报告 ↗</a>
  </header>

  <!-- ===== 网页端控制台：任务控制 + 参数调优 ===== -->
  <section id="console-panel">
    <!-- 任务控制：按钮映射 skyquant.py 子命令，后台 subprocess 执行 -->
    <div class="section-label">任务控制 <span class="tag">更新数据 / 触发回测（与 CLI 子命令一一对应）</span></div>
    <div class="console-grid">
      <!-- 更新数据卡片：fetch，默认增量，勾选后强制全量 -->
      <div class="console-card">
        <div class="cc-title">更新数据 <span class="cc-desc">skyquant fetch · 默认增量，已是最新自动跳过</span></div>
        <div class="cc-opts">
          <input type="text" id="fetch-codes" placeholder="标的代码（逗号分隔，留空=全部）">
          <label><input type="checkbox" id="fetch-force"> 强制全量重拉（--force-refresh）</label>
        </div>
        <button class="run-btn" data-task="fetch" onclick="startTask('fetch')">▶ 开始拉取</button>
      </div>
      <!-- 触发回测卡片：backtest，可选单策略或按市况路由 -->
      <div class="console-card">
        <div class="cc-title">触发回测 <span class="cc-desc">skyquant backtest · 读缓存数据生成报告</span></div>
        <div class="cc-opts">
          <input type="text" id="bt-codes" placeholder="标的代码（逗号分隔，留空=全部）">
          <select id="bt-strategy">
            <option value="">策略：按市况自动路由</option>
            <option value="trend">仅趋势</option>
            <option value="range">仅震荡</option>
            <option value="breakout">仅突破</option>
          </select>
          <label><input type="checkbox" id="bt-force"> 强制刷新数据（--force-refresh）</label>
        </div>
        <button class="run-btn" data-task="backtest" onclick="startTask('backtest')">▶ 开始回测</button>
      </div>
    </div>
    <!-- 任务状态徽标 + 实时日志（1 秒轮询） -->
    <div id="task-status"></div>
    <pre id="task-log"></pre>

    <!-- ===== 参数调优：选标的→改参数→试跑看 KPI/K线→满意再保存 ===== -->
    <div class="section-label" style="margin-top:18px">参数调优 <span class="tag">当前标的回测预览 · 不落盘，满意再保存</span></div>
    <div class="console-card" id="param-card">
      <div class="cc-title">参数编辑 <span class="cc-desc">选标的→选策略→改参数→试跑回测看效果→满意再保存</span></div>
      <!-- 标的下拉 + 策略选项卡 -->
      <div class="param-bar">
        <select id="param-stock" onchange="PARAM_STATE.code=this.value;onParamCodeChange()"></select>
        <div class="param-tabs" id="param-tabs"></div>
      </div>
      <!-- 参数表单：有 config 覆盖值显示值，无值显示默认值 placeholder -->
      <div id="param-form"></div>
      <!-- 操作区：试跑=只看效果；保存为生效=写 active.yaml；另存草稿=写 drafts 不生效 -->
      <div class="param-foot">
        <div class="preview-actions">
          <button class="run-btn" onclick="runPreview()" id="preview-btn">▶ 试跑回测</button>
          <button class="run-btn" onclick="saveParams()">保存为生效</button>
          <button class="run-btn" style="background:#6b5a2a" onclick="saveDraft()">另存草稿</button>
        </div>
        <span id="param-msg"></span>
      </div>
      <!-- 预览结果区：当前标的卡片 + KPI 指标 + 交互K线（试跑后才填充） -->
      <div id="preview-section">
        <div class="section-label">当前标的 <span class="tag" id="preview-tag">未试跑</span></div>
        <div class="preview-stock" id="preview-stock"></div>
        <div class="section-label">回测 KPI</div>
        <div class="kpi-grid" id="kpi-grid"></div>
        <div class="section-label">交互 K线 <a class="chart-ext" id="chart-ext" href="#" target="_blank" style="display:none">新标签页打开 ↗</a></div>
        <div class="preview-empty" id="preview-empty">尚未试跑回测，点击「试跑回测」查看当前参数效果</div>
        <iframe id="preview-chart" class="empty" src="about:blank"></iframe>
      </div>
    </div>

    <!-- 参数集管理：active 生效集 / drafts 手工草稿 / experiments opt 归档 -->
    <div class="section-label" style="margin-top:18px">参数集 <span class="tag">生效集 active.yaml · 草稿 drafts · opt 归档 experiments</span></div>
    <div class="console-card" id="paramsets-card">
      <div class="cc-title">参数集管理 <span class="cc-desc">多组参数共存：试跑满意可「另存草稿」，确认后「生效」；opt 每次自动归档到 experiments</span></div>
      <div id="paramsets-box"></div>
    </div>
  </section>

  <!-- 看板默认视图：实盘持仓 + 全标的卡片（控制台展开时隐藏） -->
  <section class="holdings" id="holdings-section"></section>
  <main id="sector-container"></main>
</div>

<div id="view-detail">
  <div class="detail-bar">
    <button class="back-btn" onclick="location.hash=''">← 返回看板</button>
    <div class="detail-title" id="detail-title"></div>
    <span class="tab-sep"></span>
    <div class="tab-row" id="strategy-tabs"></div>
    <span class="tab-sep"></span>
    <div class="tab-row" id="view-tabs"></div>
    <a class="open-ext" id="open-ext" href="#" target="_blank">新标签页打开 ↗</a>
  </div>
  <iframe id="detail-frame" src="about:blank"></iframe>
  <div class="detail-empty" id="detail-empty" style="display:none">该标的暂无可查看的报告</div>
</div>

<script>
let STATE = null;
const UP = "#F04848", DOWN = "#0EAE7C";

function pnlClass(v) { return v == null ? "" : (v >= 0 ? "pnl-red" : "pnl-green"); }
function fmtPct(v) { return v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(2) + "%"; }
function fmtMoney(v) { return v == null ? "—" : v.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function stockByCode(code) {
  for (const g of STATE.sectors) for (const s of g.stocks) if (s.code === code) return s;
  return null;
}
function holdingCodes() { return new Set(STATE.holdings.map(h => h.code)); }

/* ---------------- 主页：实盘持仓 ---------------- */
function renderHoldings() {
  const box = document.getElementById("holdings-section");
  const hs = STATE.holdings;
  let inner = `<div class="section-label">当前实盘持仓 <span class="tag">数据源 live_trades.csv</span></div>`;
  if (!hs.length) {
    inner += `<div class="no-holdings">当前无实盘持仓（live_trades.csv 无净持仓记录）</div>`;
    box.innerHTML = inner;
    return;
  }
  inner += `<div class="holding-grid">`;
  for (const h of hs) {
    inner += `
      <div class="holding-card">
        <div class="hc-top">
          <span class="hc-name">${esc(h.name)}</span>
          <span class="hc-code">${esc(h.code)}</span>
        </div>
        <div class="hc-row"><span>持仓 / 成本价</span><span><b>${h.size.toLocaleString()} 股</b> / ${h.avg_cost.toFixed(3)}</span></div>
        <div class="hc-row"><span>最新收盘${h.last_date ? " (" + esc(h.last_date) + ")" : ""}</span><span class="${pnlClass(h.pnl)}"><b>${h.last_close == null ? "—" : h.last_close.toFixed(2)}</b></span></div>
        <div class="hc-row"><span>持仓市值</span><span><b>${fmtMoney(h.market_value)}</b></span></div>
        <div class="hc-row"><span>浮动盈亏</span><span class="${pnlClass(h.pnl)}"><b>${h.pnl == null ? "—" : (h.pnl >= 0 ? "+" : "") + fmtMoney(h.pnl)}</b> (${fmtPct(h.pnl_pct)})</span></div>
      </div>`;
  }
  inner += `</div>`;
  const t = STATE.totals;
  inner += `<div class="holding-summary">
      <span>持仓成本 <b>${fmtMoney(t.cost)}</b></span>
      <span>总市值 <b>${fmtMoney(t.market_value)}</b></span>
      <span>总浮动盈亏 <b class="${pnlClass(t.pnl)}">${t.pnl == null ? "—" : (t.pnl >= 0 ? "+" : "") + fmtMoney(t.pnl)} (${fmtPct(t.pnl_pct)})</b></span>
    </div>`;
  box.innerHTML = inner;
}

/* ---------------- 主页：板块与标的 ---------------- */
function signalTags(s) {
  /* 最新 MACD 金叉/死叉 + EMA20/60 多空排列，均标注持续交易日数 */
  const sg = s.signals;
  if (!sg) return "";
  let html = "";
  if (sg.macd) {
    const m = sg.macd;
    const label = m.bull ? "金叉" : "死叉";
    const tip = `MACD${label} · 已持续 ${m.days} 个交易日（自 ${m.since}）`;
    html += `<span class="sig-tag ${m.bull ? "sig-bull" : "sig-bear"}" title="${tip}">${label}${m.days}d</span>`;
  }
  if (sg.ema) {
    const e = sg.ema;
    const label = e.bull ? "EMA多头" : "EMA空头";
    const tip = `${e.bull ? "EMA20>EMA60 多头排列" : "EMA20<EMA60 空头排列"} · 已持续 ${e.days} 个交易日（自 ${e.since}）`;
    html += `<span class="sig-tag ${e.bull ? "sig-bull" : "sig-bear"}" title="${tip}">${label}${e.days}d</span>`;
  }
  return html;
}

function renderSectors() {
  const held = holdingCodes();
  const container = document.getElementById("sector-container");
  let html = "";
  for (const g of STATE.sectors) {
    const ready = g.stocks.filter(s => s.available).length;
    html += `<section class="sector-block">
      <div class="sector-title">${esc(g.sector)}
        ${g.index_name ? `<span class="idx-name">板块指数：${esc(g.index_name)}</span>` : ""}
        <span class="count">${ready}/${g.stocks.length} 份报告</span>
      </div>
      <div class="stock-grid">`;
    for (const s of g.stocks) {
      if (!s.available) {
        html += `<div class="stock-card disabled" title="报告尚未生成">
            <div class="sc-top"><span class="sc-name">${esc(s.name)}</span><span class="sc-code">${esc(s.code)}</span></div>
            <div class="sc-tags">${s.regime ? `<span class="regime-tag">市况·${esc(STATE.regime_cn[s.regime] || s.regime)}</span>` : ""}${signalTags(s)}</div>
            <div class="sc-na">报告未生成</div>
          </div>`;
        continue;
      }
      let badges = "";
      for (const sid of STATE.strategy_ids) {
        const e = s.strategies[sid];
        if (e.report) {
          const m = e.metrics;
          const retCls = m && m.total_return != null ? (m.total_return >= 0 ? "pnl-red" : "pnl-green") : "";
          badges += `<span class="strat-badge">${STATE.strategy_cn[sid]}` +
            (m && m.total_return != null ? ` <span class="${retCls}">${fmtPct(m.total_return)}</span>` : "") + `</span>`;
        }
      }
      const firstReady = STATE.strategy_ids.find(sid => s.strategies[sid].report);
      html += `<div class="stock-card" onclick="location.hash='#/${s.code}/${firstReady}'">
          <div class="sc-top"><span class="sc-name">${esc(s.name)}</span><span class="sc-code">${esc(s.code)}</span></div>
          <div class="sc-tags">
            ${s.regime ? `<span class="regime-tag">市况·${esc(STATE.regime_cn[s.regime] || s.regime)}</span>` : ""}
            ${signalTags(s)}
            ${held.has(s.code) ? `<span class="holding-dot">实仓持有</span>` : ""}
          </div>
          <div class="sc-strats">${badges}</div>
          <div class="sc-foot">最近生成 ${esc(s.strategies[firstReady].mtime || "")}</div>
        </div>`;
    }
    html += `</div></section>`;
  }
  container.innerHTML = html;
}

function renderHeader() {
  document.getElementById("meta-line").textContent =
    `报告 ${STATE.counts.ready}/${STATE.counts.total} · 数据刷新 ${STATE.generated_at}（每 10 秒自动刷新状态）`;
  document.getElementById("live-link").style.display = STATE.live_report ? "" : "none";
}

/* ---------------- 详情视图：选项卡 + iframe ---------------- */
function parseHash() {
  // #/code/strategy/view  (strategy/view 可省略)
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (!parts.length) return null;
  return {code: parts[0], strategy: parts[1] || null, view: parts[2] || "report"};
}

function firstReadyStrategy(stock) {
  return STATE.strategy_ids.find(sid => stock.strategies[sid].report) || null;
}

function renderDetail() {
  const route = parseHash();
  const home = document.getElementById("view-home");
  const detail = document.getElementById("view-detail");
  if (!route) {
    home.style.display = "";
    detail.style.display = "none";
    document.getElementById("detail-frame").src = "about:blank";
    return;
  }
  const stock = stockByCode(route.code);
  if (!stock || !stock.available) { location.hash = ""; return; }

  let sid = route.strategy && stock.strategies[route.strategy] && stock.strategies[route.strategy].report
    ? route.strategy : firstReadyStrategy(stock);
  let view = route.view === "interactive" ? "interactive" : "report";
  const entry = stock.strategies[sid];
  if (view === "interactive" && !entry.interactive) view = "report";

  home.style.display = "none";
  detail.style.display = "flex";
  document.getElementById("detail-title").innerHTML =
    `${esc(stock.name)} <small>${esc(stock.code)}${stock.regime ? " · " + esc(STATE.regime_cn[stock.regime] || stock.regime) : ""}</small>`;

  // 策略选项卡（未生成报告的置灰不可点）
  let stHtml = "";
  for (const id of STATE.strategy_ids) {
    const e = stock.strategies[id];
    const cls = "tab" + (id === sid ? " active" : "") + (e.report ? "" : " disabled");
    const click = e.report ? `onclick="location.hash='#/${stock.code}/${id}/${view}'"` : "";
    stHtml += `<div class="${cls}" ${click}>${STATE.strategy_cn[id]}</div>`;
  }
  document.getElementById("strategy-tabs").innerHTML = stHtml;

  // 报告类型选项卡
  const vTab = (id, label, enabled) => {
    const cls = "tab" + (view === id ? " active" : "") + (enabled ? "" : " disabled");
    const click = enabled ? `onclick="location.hash='#/${stock.code}/${sid}/${id}'"` : "";
    return `<div class="${cls}" ${click}>${label}</div>`;
  };
  document.getElementById("view-tabs").innerHTML =
    vTab("report", "回测报告", entry.report) + vTab("interactive", "交互K线", entry.interactive);

  const file = view === "interactive" ? `${stock.code}_${sid}_interactive.html` : `${stock.code}_${sid}_report.html`;
  const src = "/plots/" + file;
  const frame = document.getElementById("detail-frame");
  const empty = document.getElementById("detail-empty");
  frame.style.display = ""; empty.style.display = "none";
  if (frame.dataset.src !== src) { frame.src = src; frame.dataset.src = src; }
  document.getElementById("open-ext").href = src;
}

function renderAll() {
  renderHeader();
  renderHoldings();
  renderSectors();
  renderDetail();
}

async function pollState() {
  try {
    const resp = await fetch("/api/state", {cache: "no-store"});
    const next = await resp.json();
    const prevRoute = location.hash;
    STATE = next;
    renderAll();
  } catch (e) { /* 轮询失败静默，下轮重试 */ }
}

/* ---------------- 控制台：任务触发 + 参数编辑 ---------------- */
let CONSOLE_OPEN = false;
let TASK_POLL_TIMER = null;
let PARAM_STATE = {code: null, strategy: "trend", overrides: {}};  // overrides: 本地未保存的改动

function toggleConsole() {
  CONSOLE_OPEN = !CONSOLE_OPEN;
  document.getElementById("console-panel").classList.toggle("open", CONSOLE_OPEN);
  // 控制台展开时只看当前编辑标的，隐藏下方持仓与全部标的卡片，避免来回切换
  document.getElementById("holdings-section").style.display = CONSOLE_OPEN ? "none" : "";
  document.getElementById("sector-container").style.display = CONSOLE_OPEN ? "none" : "";
  if (CONSOLE_OPEN) { pollTasks(); renderParamEditor(); }
}

async function startTask(name) {
  const prefix = name === "fetch" ? "fetch" : "bt";
  const body = {
    task: name,
    stock_list: document.getElementById(prefix + "-codes").value.trim(),
    force_refresh: document.getElementById(prefix + "-force").checked,
  };
  if (name === "backtest") body.strategy = document.getElementById("bt-strategy").value;
  const resp = await fetch("/api/task/start", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await resp.json();
  if (!resp.ok) { alert(j.error || "启动失败"); return; }
  pollTasks();
}

async function pollTasks() {
  if (TASK_POLL_TIMER) { clearTimeout(TASK_POLL_TIMER); TASK_POLL_TIMER = null; }
  let snap;
  try {
    snap = await (await fetch("/api/tasks", {cache: "no-store"})).json();
  } catch (e) { return; }
  renderTaskStatus(snap);
  const running = snap.current && snap.current.status === "running";
  if (running) TASK_POLL_TIMER = setTimeout(pollTasks, 1000);
}

function renderTaskStatus(snap) {
  const box = document.getElementById("task-status");
  const logBox = document.getElementById("task-log");
  const cur = snap.current;
  let html = "";
  if (cur) {
    const cls = cur.status === "running" ? "st-running" : (cur.status === "success" ? "st-success" : "st-failed");
    const txt = cur.status === "running" ? "运行中" : (cur.status === "success" ? "成功" : "失败");
    html += `<span class="status-badge ${cls}">${txt}</span>`;
    html += `<b>${esc(cur.label)}</b> <span style="color:#6b7c93">${esc(cur.cmd)}</span>`;
    html += `<span>启动 ${esc(cur.started_at)}${cur.finished_at ? " · 结束 " + esc(cur.finished_at) : ""}</span>`;
  } else {
    html = `<span style="color:#6b7c93">尚未运行任务</span>`;
  }
  if (snap.recent && snap.recent.length) {
    html += ` <span class="task-history">历史: `;
    for (const t of snap.recent.slice(0, 5)) {
      const cls = t.status === "success" ? "st-success" : "st-failed";
      html += `<span class="status-badge ${cls}" title="${esc(t.cmd)}">${esc(t.label)} ${t.started_at.slice(5, 16)}</span>`;
    }
    html += `</span>`;
  }
  box.innerHTML = html;
  if (cur && cur.log && cur.log.length) {
    logBox.style.display = "block";
    logBox.textContent = cur.log.join("\n");
    logBox.scrollTop = logBox.scrollHeight;
  } else {
    logBox.style.display = "none";
  }
}

/* ---- 参数编辑：标的/策略选择 + 表单渲染 ---- */
function currentOverrides(code, sid) {
  // 读取 active.yaml 中该标的该策略的已保存参数覆盖值
  const ov = (STATE.param_overrides || {})[code];
  return (ov && ov[sid]) || {};
}

function renderParamEditor() {
  // 渲染标的下拉、策略选项卡、参数表单、预览标的卡片
  if (!STATE || !STATE.param_codes || !STATE.param_codes.length) return;
  if (!PARAM_STATE.code || !STATE.param_codes.includes(PARAM_STATE.code)) PARAM_STATE.code = STATE.param_codes[0];
  const sel = document.getElementById("param-stock");
  sel.innerHTML = STATE.param_codes.map(c => {
    const st = stockByCode(c);
    const label = st ? `${st.name} ${c}` : c;
    return `<option value="${esc(c)}"${c === PARAM_STATE.code ? " selected" : ""}>${esc(label)}</option>`;
  }).join("");
  document.getElementById("param-tabs").innerHTML = STATE.strategy_ids.map(sid =>
    `<div class="param-tab${sid === PARAM_STATE.strategy ? " active" : ""}" onclick="switchParamStrategy('${sid}')">${esc(STATE.strategy_cn[sid])}</div>`).join("");
  renderParamForm();
  renderPreviewStock();
  renderParamSets();
}

function onParamCodeChange() {
  // 切换标的：清本地改动 + 清旧预览
  PARAM_STATE.overrides = {};
  resetPreview();
  renderParamEditor();
}

function switchParamStrategy(sid) {
  // 切换策略选项卡：清本地改动 + 清旧预览
  if (sid === PARAM_STATE.strategy) return;
  PARAM_STATE.strategy = sid;
  PARAM_STATE.overrides = {};
  resetPreview();
  renderParamEditor();
}

/* ---- 预览区：标的卡片 + KPI + K线 ---- */
function renderPreviewStock() {
  // 渲染当前编辑标的的名称/代码/市况/板块/策略标签
  const st = stockByCode(PARAM_STATE.code);
  const box = document.getElementById("preview-stock");
  if (!st) { box.innerHTML = ""; return; }
  const regime = st.regime ? `市况·${esc(STATE.regime_cn[st.regime] || st.regime)}` : "市况·未分类";
  box.innerHTML = `<span class="ps-name">${esc(st.name)}</span><span class="ps-code">${esc(st.code)}</span>` +
    `<span class="regime-tag">${regime}</span>` +
    (st.sector_index_name ? `<span class="ps-code">板块：${esc(st.sector_index_name)}</span>` : "") +
    `<span class="ps-code">策略：${esc(STATE.strategy_cn[PARAM_STATE.strategy])}</span>`;
}

function resetPreview() {
  // 清空 KPI 与 K线，恢复占位提示
  document.getElementById("preview-tag").textContent = "未试跑";
  document.getElementById("kpi-grid").innerHTML = "";
  document.getElementById("preview-empty").style.display = "";
  document.getElementById("chart-ext").style.display = "none";
  const f = document.getElementById("preview-chart");
  f.classList.add("empty"); f.src = "about:blank";
}

function fitChartHeight(f) {
  // K线自适应内容高度：消除 iframe 内部滚动条，随页面整体滚动查看
  const doc = f.contentDocument;
  if (!doc || !doc.body) return;
  const apply = () => {
    const h = Math.max(doc.documentElement.scrollHeight, doc.body.scrollHeight);
    if (h > 100) f.style.height = h + "px";
  };
  if (f._fitRO) f._fitRO.disconnect();
  f._fitRO = new ResizeObserver(apply);   // Plotly 异步渲染，监听 body 尺寸变化
  f._fitRO.observe(doc.body);
  apply();
}

function mergedParamValues() {
  // 已保存覆盖值 + 本地未保存改动（空字符串视为删除/回退默认）
  const saved = currentOverrides(PARAM_STATE.code, PARAM_STATE.strategy);
  const merged = {};
  for (const k in saved) merged[k] = String(saved[k]);
  for (const k in PARAM_STATE.overrides) {
    const v = PARAM_STATE.overrides[k];
    if (v === "") delete merged[k]; else merged[k] = v;
  }
  return merged;
}

async function runPreview() {
  // 用当前参数（已保存+未保存）调 /api/preview 试跑，成功后填充 KPI 与 K线；不写 config
  const btn = document.getElementById("preview-btn");
  const tag = document.getElementById("preview-tag");
  btn.disabled = true; tag.textContent = "试跑中…";
  try {
    const resp = await fetch("/api/preview", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        code: PARAM_STATE.code, strategy: PARAM_STATE.strategy,
        values: mergedParamValues(),
      }),
    });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.error || "试跑失败");
    renderKpi(j.metrics, j.n_trades, j.final_value);
    const f = document.getElementById("preview-chart");
    f.onload = () => fitChartHeight(f);      // 加载后自适应内容高度，避免 iframe 内滚动
    f.src = j.chart_url + "?t=" + Date.now();  // 文件名固定，加时间戳强制重载最新结果
    f.classList.remove("empty");
    const ext = document.getElementById("chart-ext");
    ext.href = j.chart_url; ext.style.display = "";
    document.getElementById("preview-empty").style.display = "none";
    tag.textContent = "已试跑（未保存）";
    tag.style.color = "#F5B041";
  } catch (e) {
    tag.textContent = "试跑失败";
    alert("试跑回测失败: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

function renderKpi(m, nTrades, finalValue) {
  // 把后端 metrics 渲染成 KPI 卡片（红涨绿跌，回撤固定绿色）
  if (!m) { document.getElementById("kpi-grid").innerHTML = ""; return; }
  const cards = [
    ["总收益", fmtPct(m.total_return), null, m.total_return],
    ["年化收益", fmtPct(m.annual_return), null, m.annual_return],
    ["最大回撤", fmtPct(m.max_drawdown), null, m.max_drawdown],
    ["Sharpe", m.sharpe_ratio?.toFixed(2) ?? "—", null, m.sharpe_ratio],
    ["Sortino", m.sortino_ratio?.toFixed(2) ?? "—", null, m.sortino_ratio],
    ["Calmar", m.calmar_ratio?.toFixed(2) ?? "—", null, m.calmar_ratio],
    ["胜率", m.total_trades ? (m.win_rate * 100).toFixed(1) + "%" : "—", `${nTrades} 笔交易`, null],
    ["盈亏比", m.profit_factor === "inf" ? "∞" : (m.profit_factor == null ? "—" : Number(m.profit_factor).toFixed(2)), null, null],
    ["SQN", m.sqn?.toFixed(2) ?? "—", null, m.sqn],
    ["期末净值", finalValue?.toLocaleString("zh-CN",{maximumFractionDigits:0}) ?? "—", null, null],
  ];
  let html = "";
  for (const [label, val, sub, raw] of cards) {
    let cls = "";
    // 回撤/亏损类为负是正常方向（绿跌红涨口径），其余越大越好
    if (raw != null && label !== "最大回撤") cls = raw >= 0 ? "pos" : "neg";
    else if (label === "最大回撤" && raw != null) cls = "neg";
    html += `<div class="kpi-card"><div class="k-label">${esc(label)}</div>` +
            `<div class="k-value ${cls}">${esc(val)}</div>` +
            (sub ? `<div class="k-sub">${esc(sub)}</div>` : "") + `</div>`;
  }
  document.getElementById("kpi-grid").innerHTML = html;
}

function renderParamForm() {
  // 按 schema 渲染参数输入框，分「策略参数」「基础风控」两组
  const code = PARAM_STATE.code, sid = PARAM_STATE.strategy;
  const schema = STATE.param_schema[sid] || [];
  const saved = currentOverrides(code, sid);
  const pend = PARAM_STATE.overrides;
  let html = "";
  let lastGroup = null;
  for (const item of schema) {
    if (item.group !== lastGroup) {
      if (lastGroup !== null) html += `</div>`;  // 关闭上一个分组 grid
      html += `<div class="param-group-label">${item.group === "strategy" ? "策略参数" : "基础风控（BaseStrategy）"}</div><div class="param-grid">`;
      lastGroup = item.group;
    }
    const name = item.name;
    // 取值优先级：本地未保存改动 > config 覆盖值 > 空（=默认）
    let val;
    if (name in pend) val = pend[name];
    else if (name in saved) val = String(saved[name]);
    else val = "";
    const dirty = (name in pend) && pend[name] !== (name in saved ? String(saved[name]) : "");
    const ph = item.default === null || item.default === undefined ? "默认: None" : `默认: ${item.default}`;
    // 标签双语对照：中文助记 + 英文参数名（与 config.yaml/backtrader 对齐）
    html += `<div class="param-item"><label title="${esc((item.cn ? item.cn + " · " : "") + name)}">` +
            `<span class="cn">${esc(item.cn || name)}</span><span class="en">${esc(name)}</span></label>` +
            `<input data-name="${esc(name)}" value="${esc(val)}" placeholder="${esc(ph)}" ` +
            `class="${dirty ? "dirty" : ""}" oninput="onParamInput(this)"></div>`;
  }
  if (lastGroup !== null) html += `</div>`;  // 关闭最后一个分组 grid
  document.getElementById("param-form").innerHTML = html;
  updateParamMsg();
}

function onParamInput(el) {
  // 输入回调：改回原值自动取消脏标记，否则记入未保存改动
  const name = el.dataset.name;
  const saved = currentOverrides(PARAM_STATE.code, PARAM_STATE.strategy);
  const orig = name in saved ? String(saved[name]) : "";
  if (el.value.trim() === orig) delete PARAM_STATE.overrides[name];  // 改回原值 → 不算修改
  else PARAM_STATE.overrides[name] = el.value.trim();
  el.classList.toggle("dirty", name in PARAM_STATE.overrides);
  updateParamMsg();
}

function updateParamMsg() {
  // 更新底部「N 项未保存」计数
  const n = Object.keys(PARAM_STATE.overrides).length;
  document.getElementById("param-msg").textContent = n ? `${n} 项未保存` : "";
  document.getElementById("param-msg").className = "";
}

async function saveParams() {
  // 把未保存改动写入生效参数集 params/active.yaml
  const msg = document.getElementById("param-msg");
  const code = PARAM_STATE.code, sid = PARAM_STATE.strategy;
  const values = PARAM_STATE.overrides;
  if (!Object.keys(values).length) { msg.textContent = "没有修改"; msg.className = "err"; return; }
  try {
    const resp = await fetch("/api/params/save", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code, strategy: sid, values}),
    });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.error || "保存失败");
    msg.textContent = "已保存到 params/active.yaml（生效）";
    msg.className = "";
    PARAM_STATE.overrides = {};
    await pollState();   // 重新拉 state（param_overrides / param_sets 已更新）
    renderParamForm();
  } catch (e) {
    msg.textContent = String(e.message || e);
    msg.className = "err";
  }
}

/* ---- 参数集管理：生效集 / 草稿 / opt 归档 ---- */
function renderParamSets() {
  // 把 STATE.param_sets 三组渲染成行：active 只展示；drafts/experiments 可生效/删除
  if (!STATE || !STATE.param_sets) return;
  const groups = [["active", "生效集（回测/实盘默认）"], ["drafts", "手工草稿"], ["experiments", "opt 自动归档"]];
  let html = "";
  for (const [g, cn] of groups) {
    const list = STATE.param_sets[g] || [];
    html += `<div class="pset-group">${cn}（${list.length}）</div>`;
    if (!list.length) html += `<div class="pset-meta" style="padding:2px 8px 8px">（空）</div>`;
    for (const s of list) {
      const m = s.meta || {};
      const badge = `<span class="pset-badge ${m.source === "opt" ? "src-opt" : ""}">${m.source === "opt" ? "opt 寻优" : "手工"}</span>`;
      const obj = m.objective ? `<span class="pset-meta">目标 ${esc(m.objective)}</span>` : "";
      let acts;
      if (g === "active") {
        acts = `<span class="pset-meta">${esc(m.applied_from ? "来自 " + m.applied_from : (m.note || ""))}</span>`;
      } else {
        acts = `<button class="mini-btn primary" onclick="applyParamSet('${esc(s.file)}')">生效</button>` +
               `<button class="mini-btn danger" onclick="deleteParamSet('${esc(s.file)}')">删除</button>`;
      }
      html += `<div class="pset-row ${g === "active" ? "active-row" : ""}">` +
              `<span class="pset-file">${esc(s.file)}</span>${badge}` +
              `<span class="pset-meta">${s.n_codes} 标的 / ${s.n_entries} 策略组</span>` +
              `<span class="pset-meta">${esc(m.created_at || "")}</span>${obj}` +
              `<span class="pset-actions">${acts}</span></div>`;
    }
  }
  document.getElementById("paramsets-box").innerHTML = html;
}

async function applyParamSet(file) {
  // 把草稿/归档参数集合并进 active.yaml（替换同 code×strategy 单元）
  if (!confirm(`把该参数集生效（合并进 active.yaml）？\n${file}`)) return;
  try {
    const resp = await fetch("/api/param-sets/apply", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({file}),
    });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.error || "生效失败");
    PARAM_STATE.overrides = {};
    await pollState();
    renderParamForm();
    alert(`已生效 ${j.applied.length} 个参数单元`);
  } catch (e) { alert("生效失败: " + (e.message || e)); }
}

async function deleteParamSet(file) {
  // 删除草稿/归档文件（active.yaml 不允许删除）
  if (!confirm(`删除参数集？\n${file}`)) return;
  try {
    const resp = await fetch("/api/param-sets/delete", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({file}),
    });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.error || "删除失败");
    await pollState();
  } catch (e) { alert("删除失败: " + (e.message || e)); }
}

async function saveDraft() {
  // 当前编辑（含未保存改动）+ active 全集另存为草稿，不动生效集
  const def = `${PARAM_STATE.code}_${PARAM_STATE.strategy}_${new Date().toISOString().slice(0, 10)}`;
  const name = prompt("草稿名称：", def);
  if (!name) return;
  try {
    const resp = await fetch("/api/param-sets/save-as", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name, code: PARAM_STATE.code, strategy: PARAM_STATE.strategy,
        values: mergedParamValues(),
      }),
    });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.error || "另存失败");
    await pollState();
    alert("已另存草稿（未生效），可在下方参数集列表点「生效」");
  } catch (e) { alert("另存失败: " + (e.message || e)); }
}

window.addEventListener("hashchange", renderDetail);
fetch("/api/state").then(r => r.json()).then(s => { STATE = s; renderAll(); });
setInterval(pollState, 10000);
</script>
</body>
</html>
"""


# ===================== HTTP 服务 =====================
class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True  # SO_REUSEADDR：重启不再撞上 TIME_WAIT
    daemon_threads = True       # 工作线程随主进程退出，避免关停时挂起


class DashboardHandler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            try:
                state = build_state()
                self._send(200, json.dumps(state, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                logger.exception("build_state failed")
                self._send(500, json.dumps({"error": str(e)}).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/tasks":
            self._send(200, json.dumps(TASK_MANAGER.snapshot(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path.startswith("/plots/"):
            self._serve_plot(path[len("/plots/"):])
            return
        self._send(404, b"Not Found", "text/plain; charset=utf-8")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            raise ValueError("请求体为空或过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
        except Exception:
            self._send(400, json.dumps({"error": "请求体不是合法 JSON"}).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/task/start":
            task, err = TASK_MANAGER.start(
                body.get("task"),
                stock_list=(body.get("stock_list") or "").strip() or None,
                strategy=(body.get("strategy") or "").strip() or None,
                force_refresh=bool(body.get("force_refresh")),
            )
            if err:
                self._send(409, json.dumps({"error": err}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            self._send(200, json.dumps({"ok": True, "task_id": task["id"]}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/params/save":
            try:
                _apply_param_update(
                    str(body.get("code", "")),
                    str(body.get("strategy", "")),
                    body.get("values") or {},
                )
            except (ValueError, RuntimeError) as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            except Exception as e:
                logger.exception("params save failed")
                self._send(500, json.dumps({"error": f"写入失败: {e}"}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/preview":
            # 试跑回测：用当前未保存参数跑单标的回测，返回 KPI + 预览K线
            code = str(body.get("code", ""))
            strategy = str(body.get("strategy", ""))
            values = body.get("values") or {}
            try:
                if strategy not in STRATEGY_IDS:
                    raise ValueError(f"未知策略: {strategy}")
                schema = {p["name"]: p["default"] for p in _param_schema()[strategy]}
                params = {}
                for name, raw in values.items():
                    if name not in schema:
                        continue
                    raw = str(raw).strip()
                    if raw == "":
                        continue  # 留空 = 用策略类默认值
                    params[name] = _coerce_param(raw, schema[name])
                with _PREVIEW_LOCK:
                    result = run_preview_backtest(code, strategy, params)
                self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                logger.exception("preview backtest failed")
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return

        # ---- 参数集管理：生效 / 另存草稿 / 删除 ----
        import config_store as cs

        if path == "/api/param-sets/apply":
            # 把指定参数集（experiments/drafts）合并进 active.yaml
            rel = str(body.get("file", ""))
            codes = body.get("codes") or None
            strategies = body.get("strategies") or None
            try:
                with _CONFIG_LOCK:
                    res = cs.apply_param_set(rel, codes=codes, strategies=strategies,
                                             note=f"dashboard apply @ {datetime.now():%Y-%m-%d %H:%M:%S}")
                self._send(200, json.dumps({"ok": True, "applied": res["applied"]}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return

        if path == "/api/param-sets/save-as":
            # 把 active 全集 + 当前编辑单元另存为草稿（不影响生效集）
            name = str(body.get("name", "")).strip()
            note = str(body.get("note", "")).strip()
            code = str(body.get("code", ""))
            strategy = str(body.get("strategy", ""))
            values = body.get("values") or {}
            if not name:
                self._send(400, json.dumps({"error": "草稿名称不能为空"}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            try:
                if strategy not in STRATEGY_IDS:
                    raise ValueError(f"未知策略: {strategy}")
                schema = {p["name"]: p["default"] for p in _param_schema()[strategy]}
                base = cs.load_param_set()["strategy_params"]
                sp = {c: {sid: dict(p) for sid, p in sids.items()} for c, sids in base.items()}
                unit = dict(sp.get(code, {}).get(strategy, {}))
                for pname, raw in values.items():
                    if pname not in schema:
                        continue
                    raw = str(raw).strip()
                    if raw == "":
                        unit.pop(pname, None)
                    else:
                        unit[pname] = _coerce_param(raw, schema[pname])
                sp.setdefault(code, {})[strategy] = unit
                path_out = cs.save_draft(name, sp, note=note or f"{code}/{strategy} 控制台另存")
                self._send(200, json.dumps({"ok": True, "file": str(path_out)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except (ValueError, RuntimeError) as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return

        if path == "/api/param-sets/delete":
            rel = str(body.get("file", ""))
            try:
                cs.delete_param_set(rel)
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return

        self._send(404, b"Not Found", "text/plain; charset=utf-8")

    def _serve_plot(self, rel_name: str):
        # 防目录穿越：resolve 后必须仍在 PLOT_DIR 内，且只允许 .html
        try:
            target = (PLOT_DIR / rel_name).resolve()
            target.relative_to(PLOT_DIR.resolve())
        except (ValueError, OSError):
            self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            return
        if not target.is_file() or target.suffix != ".html":
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return
        self._send(200, target.read_bytes(), "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        logger.debug("dashboard http: " + fmt, *args)


def _lan_ip() -> str:
    """获取本机局域网 IP（失败回退 127.0.0.1）。"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # 不实际发包，只用于选路
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _find_port_pid(host: str, port: int):
    """返回占用 host:port 的进程 PID（跨平台尽力实现，取不到返回 None）。"""
    import shutil
    import subprocess

    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        out = subprocess.run(
            [lsof, "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        pids = [p for p in out.splitlines() if p.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="SkyQuant 回测看板主页")
    parser.add_argument("--host", default="0.0.0.0",
                        help="监听地址（默认 0.0.0.0 允许局域网访问；127.0.0.1 仅本机）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    # 启动时先验证一次状态采集，配置/缓存异常能立刻暴露
    build_state()

    try:
        server = DashboardServer((args.host, args.port), DashboardHandler)
    except OSError as e:
        if e.errno in (48, 98):  # macOS 48 / Linux 98: address already in use
            pid = _find_port_pid(args.host, args.port)
            print(f"\n  端口 {args.port} 已被占用，无法启动看板。")
            if pid:
                print(f"  占用进程 PID: {pid}（可用 'kill {pid}' 停止它，或换一个端口）")
            print(f"  换端口启动: python3 dashboard.py --port {args.port + 1}\n")
            sys.exit(1)
        raise

    # host=0.0.0.0 表示监听所有网卡：浏览器打开用 localhost，
    # 同时打印局域网 IP 供手机/其他设备访问
    display_url = f"http://localhost:{args.port}/" if args.host in ("0.0.0.0", "::") else f"http://{args.host}:{args.port}/"
    logger.info(f"SkyQuant 看板已启动: {display_url} (bind {args.host})")
    print(f"\n  SkyQuant 回测看板: {display_url}")
    if args.host in ("0.0.0.0", "::"):
        print(f"  局域网访问: http://{_lan_ip()}:{args.port}/")
    print("  (Ctrl+C 停止；回测报告生成后刷新页面即可点击)\n")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(display_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
