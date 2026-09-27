# Stock pool pre-filter layer (sits between data_source and the strategies).
#
# The strategy layer only owns signal rules; deciding WHICH stocks may enter
# the pipeline is this layer's job. Three independent responsibilities:
#
#   1. Basic quality filter (cache-only, always applied by run_all.py):
#      data sufficiency, zero-volume bars, long suspension gaps, liquidity
#      (average daily turnover amount / turnover rate), low-price floor,
#      ST / delisting-risk name flags.
#   2. Optional trendability filter (enabled with run_all.py --screen):
#      ADX / EMA structure metrics, keeps only sustained-trend names for the
#      trend strategy grid.
#   3. Regime classification -> strategy routing:
#      trend / range / breakout labels are written to output/stock_filter.csv;
#      param_optimize.py and main.py route each symbol to its matching strategy.
#
# All thresholds live in config.yaml -> stock_filter (basic/trend/regime groups).
import argparse
import os
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from comm import OUTPUT_DIR  # noqa: E402
from data_source import DataSource  # noqa: E402
from strategy import ACTIVE_STRATEGIES, STRATEGY_MAPPING  # noqa: E402

FILTER_CSV = os.path.join(OUTPUT_DIR, "stock_filter.csv")

# ===================== Default thresholds (overridable via config.yaml) =====================
DEFAULT_BASIC = {
    "min_bars": 120,              # minimum trading bars in the window (indicator warm-up)
    "max_zero_volume_ratio": 0.01,  # max share of zero-volume bars
    "max_gap_days": 20,           # max calendar-day gap between adjacent bars (long suspension)
    "min_avg_amount_yi": 0.5,     # min average daily traded amount, in 100M CNY (yi)
    "min_avg_turn": 0.2,          # min average daily turnover rate, in percent
                                   # (kept permissive; mega-caps naturally have
                                   # low turn, the amount floor is the main gate)
    "min_mean_close": 1.0,        # min mean close price, CNY (exclude penny/delisting-risk names)
}

DEFAULT_TREND = {
    "adx_period": 14,
    "min_adx_mean": 18.0,              # average trend strength
    "min_trend_ratio": 0.30,           # share of strong-trend (ADX>=25) days
    "min_bull_alignment": 0.35,        # has meaningful bull-structure periods
    "max_ma_crossings_year": 6.0,      # not whipsawing constantly
    "min_efficiency": 0.04,            # net move is not pure noise
    "min_price_range": 0.40,           # price is not compressed in a tight box
    "min_max_bull_streak": 150,        # at least one sustained trend wave (~7.5 months)
}

DEFAULT_REGIME = {
    "breakout_min_range": 0.8,    # large price amplitude (period high-low)/mean
    "breakout_min_adx": 22.0,     # plus non-trivial trend strength
    "trend_min_adx": 22.0,
    "trend_min_efficiency": 0.04,
    "trend_min_bull_streak": 150,
    "range_max_adx": 22.0,        # below trend strength -> mean reversion territory
    "range_max_efficiency": 0.035,
}

REGIME_TO_STRATEGY = {"trend": "trend", "range": "range", "breakout": "breakout"}


# ===================== ADX (Wilder) =====================
def _wilders_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (equivalent to EWM with alpha=1/period)."""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Compute Wilder ADX/+DI/-DI and return a DataFrame indexed like df."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = (high - prev_high).where((high - prev_high) > (prev_low - low), 0.0)
    plus_dm = plus_dm.where(plus_dm > 0, 0.0)
    minus_dm = (prev_low - low).where((prev_low - low) > (high - prev_high), 0.0)
    minus_dm = minus_dm.where(minus_dm > 0, 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = _wilders_smooth(tr, period)
    plus_di = 100.0 * _wilders_smooth(plus_dm, period) / atr.replace(0, float("nan"))
    minus_di = 100.0 * _wilders_smooth(minus_dm, period) / atr.replace(0, float("nan"))
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    adx = _wilders_smooth(dx, period)
    return pd.DataFrame({"adx": adx, "plus_di": plus_di, "minus_di": minus_di}, index=df.index)


# ===================== Basic quality metrics =====================
def compute_basic_metrics(df: pd.DataFrame) -> dict:
    """Data-quality / liquidity metrics for one symbol's cached OHLCV DataFrame."""
    if df is None or len(df) == 0:
        return None
    volume = df["volume"].astype(float)
    close = df["close"].astype(float)
    amount = df["amount"].astype(float) if "amount" in df.columns else pd.Series(dtype=float)
    turn = df["turn"].astype(float) if "turn" in df.columns else pd.Series(dtype=float)
    dates = pd.to_datetime(df["datetime"])

    zero_volume_ratio = float((volume <= 0).mean())
    # Suspensions appear as missing trading days, i.e. large calendar gaps
    gap_days = dates.diff().dt.days.dropna()
    max_gap_days = int(gap_days.max()) if not gap_days.empty else 0
    # Tushare amount is in thousands of CNY; /1e5 converts to 100M (yi)
    avg_amount_yi = float(amount.mean() / 100_000.0) if not amount.empty else 0.0
    avg_turn = float(turn.mean()) if not turn.empty else 0.0

    return {
        "bars": int(len(df)),
        "zero_volume_ratio": round(zero_volume_ratio, 4),
        "max_gap_days": max_gap_days,
        "avg_amount_yi": round(avg_amount_yi, 3),
        "avg_turn": round(avg_turn, 3),
        "mean_close": round(float(close.mean()), 3),
    }


def evaluate_basic(metrics: dict, name: str, thresholds: dict) -> tuple:
    """Return (pass, fail_reason) for basic quality thresholds; ST is name-based."""
    if metrics is None:
        return False, "insufficient_data"
    fails = []
    if metrics["bars"] < thresholds["min_bars"]:
        fails.append(f"bars {metrics['bars']} < {thresholds['min_bars']}")
    if metrics["zero_volume_ratio"] > thresholds["max_zero_volume_ratio"]:
        fails.append(f"zero_vol {metrics['zero_volume_ratio']:.2%} > {thresholds['max_zero_volume_ratio']:.2%}")
    if metrics["max_gap_days"] > thresholds["max_gap_days"]:
        fails.append(f"gap {metrics['max_gap_days']}d > {thresholds['max_gap_days']}d")
    if metrics["avg_amount_yi"] < thresholds["min_avg_amount_yi"]:
        fails.append(f"amount {metrics['avg_amount_yi']:.2f}yi < {thresholds['min_avg_amount_yi']}yi")
    if metrics["avg_turn"] < thresholds["min_avg_turn"]:
        fails.append(f"turn {metrics['avg_turn']:.2f}% < {thresholds['min_avg_turn']}%")
    if metrics["mean_close"] < thresholds["min_mean_close"]:
        fails.append(f"price {metrics['mean_close']:.2f} < {thresholds['min_mean_close']}")
    # ST / *ST / 退 marks in the config-maintained name
    upper_name = (name or "").upper()
    if "ST" in upper_name or "退" in (name or ""):
        fails.append(f"ST/delisting-risk name: {name}")
    return (len(fails) == 0), "; ".join(fails)


# ===================== Trendability metrics =====================
def compute_trend_metrics(df: pd.DataFrame, adx_period: int = 14) -> dict:
    """Return trend-quality metrics for one symbol's OHLCV DataFrame."""
    if df is None or len(df) < adx_period * 4:
        return None
    close = df["close"].astype(float)
    adx_df = compute_adx(df, period=adx_period)
    adx = adx_df["adx"].dropna()

    ema60 = close.ewm(span=60, adjust=False).mean()
    ema120 = close.ewm(span=120, adjust=False).mean()
    aligned = ema60 > ema120
    # Only evaluate over the region where both MAs are warm (>= 120 bars)
    warm = close.index >= 120 - 1 if len(close) >= 120 else pd.Series(False, index=close.index)
    aligned_warm = aligned[warm]
    if aligned_warm.empty:
        aligned_warm = aligned

    # Crossovers of EMA60 vs EMA120 (bull/bear flips)
    cross = (aligned.astype(int).diff().abs() == 1).sum()
    years = max(len(df) / 252.0, 1e-6)

    # Kaufman efficiency ratio over the whole period
    net_change = abs(float(close.iloc[-1] - close.iloc[0]))
    path = float(close.diff().abs().sum())
    efficiency = net_change / path if path > 0 else 0.0

    # Longest consecutive bull-aligned streak (trend wave length)
    streak = 0
    max_streak = 0
    for flag in aligned.astype(int).tolist():
        if flag:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    period_high = float(df["high"].max())
    period_low = float(df["low"].min())
    mean_close = float(close.mean())
    price_range = (period_high - period_low) / mean_close if mean_close > 0 else 0.0

    return {
        "adx_mean": float(adx.mean()) if not adx.empty else 0.0,
        "trend_ratio": float((adx >= 25).mean()) if not adx.empty else 0.0,
        "bull_alignment": float(aligned_warm.mean()),
        "ma_crossings_year": float(cross / years),
        "efficiency": float(efficiency),
        "price_range": float(price_range),
        "max_bull_streak": int(max_streak),
    }


def evaluate_trend(metrics: dict, thresholds: dict) -> tuple:
    """Return (pass, fail_reason) for one symbol's trend metrics against thresholds."""
    if metrics is None:
        return False, "insufficient_data"
    fails = []
    if metrics["adx_mean"] < thresholds["min_adx_mean"]:
        fails.append(f"adx_mean {metrics['adx_mean']:.1f} < {thresholds['min_adx_mean']}")
    if metrics["trend_ratio"] < thresholds["min_trend_ratio"]:
        fails.append(f"trend_ratio {metrics['trend_ratio']:.2f} < {thresholds['min_trend_ratio']}")
    if metrics["bull_alignment"] < thresholds["min_bull_alignment"]:
        fails.append(f"bull_align {metrics['bull_alignment']:.2f} < {thresholds['min_bull_alignment']}")
    if metrics["ma_crossings_year"] > thresholds["max_ma_crossings_year"]:
        fails.append(f"crossovers {metrics['ma_crossings_year']:.1f}/yr > {thresholds['max_ma_crossings_year']}")
    if metrics["efficiency"] < thresholds["min_efficiency"]:
        fails.append(f"efficiency {metrics['efficiency']:.3f} < {thresholds['min_efficiency']}")
    if metrics["price_range"] < thresholds["min_price_range"]:
        fails.append(f"range {metrics['price_range']:.2f} < {thresholds['min_price_range']}")
    if metrics["max_bull_streak"] < thresholds["min_max_bull_streak"]:
        fails.append(f"max_streak {metrics['max_bull_streak']}d < {thresholds['min_max_bull_streak']}")
    return (len(fails) == 0), "; ".join(fails)


def classify_regime(metrics: dict, thresholds: dict = None) -> str:
    """Classify a stock into trend / range / breakout / unclassified.

    Mapping to strategy:
      trend    -> trend strategy    (strong sustained trend)
      range    -> range strategy    (oscillating, mean-reverting)
      breakout -> breakout strategy (high volatility, large swings)
    Priority: breakout (volatility) > trend (sustained) > range (default).
    """
    thresholds = {**DEFAULT_REGIME, **(thresholds or {})}
    if metrics is None:
        return "unclassified"
    if metrics["price_range"] >= thresholds["breakout_min_range"] and metrics["adx_mean"] >= thresholds["breakout_min_adx"]:
        return "breakout"
    if (
        metrics["adx_mean"] >= thresholds["trend_min_adx"]
        and metrics["efficiency"] >= thresholds["trend_min_efficiency"]
        and metrics["max_bull_streak"] >= thresholds["trend_min_bull_streak"]
    ):
        return "trend"
    if metrics["adx_mean"] < thresholds["range_max_adx"] or metrics["efficiency"] < thresholds["range_max_efficiency"]:
        return "range"
    return "range"  # ambiguous cases default to the mean-reversion bucket


# ===================== Filter layer entry point =====================
def filter_stock_pool(ds: DataSource, codes: list, filter_config: dict = None) -> pd.DataFrame:
    """Run basic + trend filters and regime classification for every code.

    Reads cached data only (no API calls); stocks without cache are marked
    failed (run the data fetch first). Returns one row per symbol with:
      basic_passed, passed (basic AND trend), regime, fail reasons, metrics.
    """
    filter_config = filter_config or {}
    basic_cfg = {**DEFAULT_BASIC, **(filter_config.get("basic") or {})}
    trend_cfg = {**DEFAULT_TREND, **(filter_config.get("trend") or {})}
    regime_cfg = {**DEFAULT_REGIME, **(filter_config.get("regime") or {})}

    rows = []
    stock_names = {str(s["code"]): s.get("name", "") for s in ds.cfg.get("stock_list", [])}
    for code in codes:
        code = str(code)
        name = stock_names.get(code, "")
        df = ds.load_cached_data(code)
        basic_metrics = compute_basic_metrics(df)
        basic_passed, basic_reason = evaluate_basic(basic_metrics, name, basic_cfg)

        # Trend metrics need enough history; compute only when basic data exists
        trend_metrics = compute_trend_metrics(df, adx_period=trend_cfg["adx_period"]) if basic_metrics else None
        trend_passed, trend_reason = evaluate_trend(trend_metrics, trend_cfg)
        regime = classify_regime(trend_metrics, regime_cfg)

        row = {
            "stock_code": code,
            "name": name,
            "regime": regime,
            "basic_passed": basic_passed,
            "passed": bool(basic_passed and trend_passed),
            "basic_fail_reason": "" if basic_passed else basic_reason,
            "fail_reason": "" if trend_passed else trend_reason,
        }
        if basic_metrics:
            row.update(basic_metrics)
        if trend_metrics:
            row.update(trend_metrics)
        rows.append(row)
    return pd.DataFrame(rows)


# ===================== Regime -> strategy routing =====================
def load_regime_map(csv_path: str = FILTER_CSV) -> dict:
    """Read the filter report and return {stock_code: regime}.

    Missing/stale report -> empty map; callers then fall back to running all
    active strategies (no silent routing without a fresh filter report).
    """
    if not os.path.exists(csv_path):
        return {}
    try:
        df = pd.read_csv(csv_path, dtype={"stock_code": str})
    except pd.errors.EmptyDataError:
        return {}
    valid = set(REGIME_TO_STRATEGY)
    return {str(r["stock_code"]): r["regime"] for _, r in df.iterrows() if r.get("regime") in valid}


def routed_strategies(code: str, regime_map: dict, active_strategies: list = None) -> list:
    """Resolve the strategy set to optimize for one symbol.

    A regime label routes the symbol to its single matching strategy; without
    a label every active strategy is optimized (backward-compatible default).
    """
    active = list(active_strategies if active_strategies is not None else (ACTIVE_STRATEGIES or STRATEGY_MAPPING.keys()))
    regime = regime_map.get(str(code))
    if regime and regime in active:
        return [regime]
    return active


def strategy_for_code(code: str, regime_map: dict, param_pool: dict = None, default: str = "trend") -> str:
    """Pick the single strategy to backtest for one symbol (main.py auto-route).

    Priority: regime label -> first strategy already configured in config.yaml
    strategy_params -> default strategy.
    """
    regime = regime_map.get(str(code))
    if regime and regime in STRATEGY_MAPPING:
        return regime
    configured = list((param_pool or {}).get(str(code), {}).keys())
    if configured:
        return configured[0]
    return default


# ===================== CLI =====================
def main():
    parser = argparse.ArgumentParser(description="Stock pool pre-filter: basic quality + trendability + regime routing")
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated codes to filter (default: all codes in config.yaml stock_list)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=FILTER_CSV,
        help=f"Output CSV path (default: {FILTER_CSV})",
    )
    args = parser.parse_args()

    ds = DataSource()
    if args.stock_list:
        codes = [c.strip() for c in args.stock_list.split(",") if c.strip()]
    else:
        codes = [str(item["code"]) for item in ds.cfg.get("stock_list", [])]

    result = filter_stock_pool(ds, codes, ds.cfg.get("stock_filter", {}))
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    result.to_csv(args.output, index=False)

    basic_failed = result[~result["basic_passed"]]
    trend_failed = result[result["basic_passed"] & ~result["passed"]]
    print(f"Filtered {len(result)} symbols: {len(result[result['basic_passed']])} passed basic filter, "
          f"{len(result[result['passed']])} passed basic + trend filter")
    if len(basic_failed) > 0:
        print(f"\nBasic-filter rejected ({len(basic_failed)}):")
        for _, r in basic_failed.iterrows():
            print(f"  {r['stock_code']} {r['name']}: {r['basic_fail_reason']}")
    if len(trend_failed) > 0:
        print(f"\nTrend-filter rejected ({len(trend_failed)}, use --screen in run_all.py to exclude):")
        for _, r in trend_failed.iterrows():
            print(f"  {r['stock_code']} {r['name']}: {r['fail_reason']}")

    print("\nRegime distribution (strategy routing):")
    regime_counts = result["regime"].value_counts()
    for regime, count in regime_counts.items():
        regime_codes = result[result["regime"] == regime]["stock_code"].tolist()
        strategy_id = REGIME_TO_STRATEGY.get(regime, "-")
        print(f"  {regime:12s} -> {strategy_id:8s} ({count:2d}): {', '.join(regime_codes)}")

    print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
