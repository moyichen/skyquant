from .base import BaseStrategy
from .breakout import BreakoutStrategy
from .range import RangeStrategy
from .trend import TrendStrategy

STRATEGY_MAPPING = {
    "trend": TrendStrategy,
    "range": RangeStrategy,
    "breakout": BreakoutStrategy,
}

# Short human-readable description per strategy id; surfaced in `main.py --help`.
# All strategies share ATR trailing stop / dynamic take-profit / max-loss floor /
# average-down logic in BaseStrategy (no directional gate). The difference is the
# entry signal; trend additionally requires its own EMA/MACD/volatility/ADX filter.
STRATEGY_DESCRIPTIONS = {
    "trend": "Trend (trend_follow + momentum merged): enter when EMA bull alignment, MACD/volatility/ADX trend filters pass AND Momentum > 0; exit on ATR chandelier trailing stop or momentum reversal.",
    "range": "Range/mean-reversion (boll_ma + short_reversal merged): enter on close <= bb_lowerband OR daily drop > drop_ratio (oversold bounce); exit on trailing stop or close > bb_upperband. No trend gate.",
    "breakout": "Breakout (Donchian channel): enter when close > donchian_upper of breakout_period candles; exit on trailing stop or close < donchian_lower. No trend gate (for high-volatility names where price breaks out before ADX confirms).",
}

# Default strategy parameters, used as fallback when a stock has no optimized config
DEFAULT_STRATEGY_PARAMS = {
    "trend": {"trail_atr_multiple": 1.6, "momentum_period": 20, "max_risk_ratio": 0.02},
    "range": {"trail_atr_multiple": 2.0, "bb_period": 20, "drop_ratio": 0.18, "max_risk_ratio": 0.02},
    "breakout": {"trail_atr_multiple": 2.0, "breakout_period": 20, "max_risk_ratio": 0.02},
}

# Strategies allowed to run in the optimization pipeline and daily signal
# generation. Set to None to re-enable all registered strategies.
ACTIVE_STRATEGIES = ["trend", "range", "breakout"]


def filter_active_strategies(params: dict) -> dict:
    """Keep only active strategies in a {strategy_id: params} mapping.

    Falls back to active-strategy defaults when filtering leaves nothing
    (e.g. a stock whose optimized config has no active strategy).
    """
    if ACTIVE_STRATEGIES is None:
        return params
    filtered = {sid: p for sid, p in params.items() if sid in ACTIVE_STRATEGIES}
    if not filtered:
        filtered = {sid: DEFAULT_STRATEGY_PARAMS[sid] for sid in ACTIVE_STRATEGIES if sid in DEFAULT_STRATEGY_PARAMS}
    return filtered
