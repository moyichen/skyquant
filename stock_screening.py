# Stock trendability screening: filter out long-term range-oscillating stocks
# before feeding the pool into the trend-following pipeline.
#
# The quadruple-filter trend strategy only works on stocks with medium-long term
# bull trends; optimizing parameters on box-oscillating names (e.g. 000725)
# produces fragile fits. This module scores each cached symbol on trend quality
# and keeps only those with sustained trend structure.
#
# Metrics (all computed on the full cached history trimmed by start_date):
#   1. adx_mean          : average Wilder ADX(14) over the period
#   2. trend_ratio       : fraction of bars with ADX >= 25 (strong-trend days)
#   3. bull_alignment    : fraction of bars where EMA60 > EMA120 (has bull structure)
#   4. ma_crossings_year : EMA60/EMA120 crossovers per year (lower = less whipsaw)
#   5. efficiency        : Kaufman efficiency ratio = |net change| / sum(|daily change|)
#   6. price_range       : (period high - period low) / mean close (not compressed)
#   7. max_bull_streak   : longest consecutive EMA60 > EMA120 streak (trend wave length)
#
# A stock passes when ALL thresholds are satisfied. Thresholds live in
# config.yaml -> stock_screening.
import argparse
import os
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_source import DataSource  # noqa: E402

SCREENING_CSV = os.path.join(PROJECT_ROOT, "output", "stock_screening.csv")

DEFAULT_THRESHOLDS = {
    "adx_period": 14,
    "min_adx_mean": 18.0,              # average trend strength
    "min_trend_ratio": 0.30,           # share of strong-trend (ADX>=25) days
    "min_bull_alignment": 0.35,        # has meaningful bull-structure periods
    "max_ma_crossings_year": 6.0,      # not whipsawing constantly
    "min_efficiency": 0.04,            # net move is not pure noise
    "min_price_range": 0.40,           # price is not compressed in a tight box
    "min_max_bull_streak": 150,        # at least one sustained trend wave (~7.5 months);
                                       # 000725 has only 119d -> correctly filtered out
}


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


# ===================== Screening core =====================
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


def evaluate(metrics: dict, thresholds: dict) -> tuple[bool, str]:
    """Return (pass, fail_reason) for one symbol's metrics against thresholds."""
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


def screen_trendable_stocks(ds: DataSource, codes: list, thresholds: dict = None) -> pd.DataFrame:
    """Screen a list of codes; return a DataFrame with metrics, pass flag and reason.

    Reads cached data only (no API calls). Stocks without cache are skipped
    (run data fetch first).
    """
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    rows = []
    stock_names = {str(s["code"]): s.get("name", "") for s in ds.cfg.get("stock_list", [])}
    for code in codes:
        df = ds.load_cached_data(str(code))
        metrics = compute_trend_metrics(df, adx_period=thresholds["adx_period"])
        passed, reason = evaluate(metrics, thresholds)
        row = {
            "stock_code": str(code),
            "name": stock_names.get(str(code), ""),
            "passed": passed,
            "fail_reason": "" if passed else reason,
        }
        if metrics:
            row.update(metrics)
        rows.append(row)
    return pd.DataFrame(rows)


# ===================== CLI =====================
def main():
    parser = argparse.ArgumentParser(description="Screen stock pool for trendability")
    parser.add_argument(
        "--stock-list",
        type=str,
        default=None,
        help="Comma-separated codes to screen (default: all codes in config.yaml stock_list)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=SCREENING_CSV,
        help=f"Output CSV path (default: {SCREENING_CSV})",
    )
    args = parser.parse_args()

    ds = DataSource()
    cfg_thresholds = ds.cfg.get("stock_screening", {})
    thresholds = {**DEFAULT_THRESHOLDS, **cfg_thresholds}

    if args.stock_list:
        codes = [c.strip() for c in args.stock_list.split(",") if c.strip()]
    else:
        codes = [str(item["code"]) for item in ds.cfg.get("stock_list", [])]

    result = screen_trendable_stocks(ds, codes, thresholds)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    result.to_csv(args.output, index=False)

    passed = result[result["passed"]]
    print(f"Screened {len(result)} symbols, {len(passed)} passed trendability filter")
    if len(passed) > 0:
        print("Passed:", ", ".join(passed["stock_code"].tolist()))
    failed = result[~result["passed"]]
    if len(failed) > 0:
        print(f"\nFailed ({len(failed)}):")
        for _, r in failed.iterrows():
            print(f"  {r['stock_code']} {r['name']}: {r['fail_reason']}")
    print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
