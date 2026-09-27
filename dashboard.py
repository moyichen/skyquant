"""SkyQuant 回测看板主页（本地 HTTP 服务）。

用法：
    python dashboard.py                # 启动看板（默认 127.0.0.1:8765，自动开浏览器）
    python dashboard.py --port 9000
    python dashboard.py --no-browser

特性：
  - 按 config.yaml 的 sector 分组罗列全部标的；
  - 已生成报告的标的可点击进入，未生成的标的灰色不可点；
  - 同一标的的 trend/range/breakout 报告通过选项卡切换（回测报告 / 交互K线）；
  - 每次刷新（或每 10 秒轮询）实时扫描 output/plots，报告逐步生成、逐步可点；
  - 主页顶部展示 live_trades.csv 聚合的当前实盘持仓（含最新收盘价/市值/浮动盈亏）。
"""

import argparse
import json
import logging
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import yaml

from comm import CONFIG_PATH, PLOT_DIR, PROJECT_ROOT, setup_logging, LOG_FILE
from live_trading import compute_holdings

setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)

# 展示口径固定为全量已注册策略（与 strategy.ACTIVE_STRATEGIES 顺序一致）
STRATEGY_IDS = ["trend", "range", "breakout"]
STRATEGY_CN = {"trend": "趋势", "range": "震荡", "breakout": "突破"}
REGIME_CN = {"trend": "趋势", "range": "震荡", "breakout": "突破",
             "unclassified": "未分类"}

STOCK_CACHE_DIR = PROJECT_ROOT / "cache" / "stock_cache"
LIVE_TRADES_CSV = PROJECT_ROOT / "live_trades.csv"
METRICS_CSV = PROJECT_ROOT / "output" / "metrics_summary.csv"
LIVE_REPORT_HTML = PLOT_DIR / "live_portfolio_report.html"

UP_RED = "#F04848"
DOWN_GREEN = "#0EAE7C"


# ===================== 状态采集 =====================
def _load_metrics() -> dict:
    """读取 metrics_summary.csv -> {(code, strategy): row_dict}。"""
    if not METRICS_CSV.exists():
        return {}
    df = pd.read_csv(METRICS_CSV, dtype={"stock_code": str})
    out = {}
    for _, r in df.iterrows():
        out[(str(r["stock_code"]), str(r["strategy"]))] = {
            "total_return": _round(r.get("total_return"), 4),
            "sharpe": _round(r.get("sharpe_ratio"), 2),
            "max_drawdown": _round(r.get("max_drawdown"), 4),
        }
    return out


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


def build_state() -> dict:
    """扫描配置/报告目录/持仓，生成看板渲染所需的全部状态（每次请求实时计算）。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    stock_list = cfg.get("stock_list", [])
    metrics = _load_metrics()

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
                "metrics": metrics.get((code, sid)),
            }
            strategies[sid] = entry
        sectors.setdefault(sector, []).append({
            "code": code,
            "name": s.get("name", ""),
            "regime": s.get("regime"),
            "sector_index_name": s.get("sector_index_name") or "",
            "available": any_report,
            "strategies": strategies,
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
</style>
</head>
<body>

<div id="view-home">
  <header>
    <h1>Sky<span class="accent">Quant</span> 回测看板</h1>
    <span class="header-meta" id="meta-line"></span>
    <span class="header-spacer"></span>
    <a class="header-link" id="live-link" href="/plots/live_portfolio_report.html" target="_blank" style="display:none">实盘组合报告 ↗</a>
  </header>

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
            <div class="sc-tags">${s.regime ? `<span class="regime-tag">${esc(STATE.regime_cn[s.regime] || s.regime)}</span>` : ""}</div>
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
            ${s.regime ? `<span class="regime-tag">${esc(STATE.regime_cn[s.regime] || s.regime)}</span>` : ""}
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

window.addEventListener("hashchange", renderDetail);
fetch("/api/state").then(r => r.json()).then(s => { STATE = s; renderAll(); });
setInterval(pollState, 10000);
</script>
</body>
</html>
"""


# ===================== HTTP 服务 =====================
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
        if path.startswith("/plots/"):
            self._serve_plot(path[len("/plots/"):])
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


def main():
    parser = argparse.ArgumentParser(description="SkyQuant 回测看板主页")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    # 启动时先验证一次状态采集，配置/缓存异常能立刻暴露
    build_state()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}/"
    logger.info(f"SkyQuant 看板已启动: {url}")
    print(f"\n  SkyQuant 回测看板: {url}\n  (Ctrl+C 停止；回测报告生成后刷新页面即可点击)\n")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
