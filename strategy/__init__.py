from .base import BaseStrategy
from .boll_ma import BollMAStrategy
from .momentum import MomentumStrategy
from .multi_factor import MultiFactorStrategy
from .short_reversal import ShortReversalStrategy
from .trend_follow import TrendFollowStrategy

STRATEGY_MAPPING = {
    "trend_follow": TrendFollowStrategy,
    "momentum": MomentumStrategy,
    "short_reversal": ShortReversalStrategy,
    "boll_ma": BollMAStrategy,
    "multi_factor": MultiFactorStrategy,
}

# Short human-readable description per strategy id; surfaced in `main.py --help`
# All strategies share the global triple trend filter (SMA bullish + MACD bullish +
# ATR volatility) enforced in BaseStrategy before any strategy-specific entry signal.
STRATEGY_DESCRIPTIONS = {
    "trend_follow": "Trend follow: enter unconditionally once the global triple filter (SMA bullish + MACD bullish + volatility) passes; exit on ATR chandelier trailing stop (ratchet-only)",
    "momentum": "Momentum: enter when Momentum(20) > 0 after global triple filter; exit on ATR stop or momentum reversal",
    "short_reversal": "Short reversal: enter when daily drop > drop_ratio after global triple filter; exit on ATR stop",
    "boll_ma": "Bollinger: enter on close <= lower band after global triple filter; exit on ATR stop or close > upper band",
    "multi_factor": "Multi-factor: enter when pctChg > -5 after global triple filter; exit on ATR stop or SMA(20) < SMA(60)",
}

# Default strategy parameters, used as fallback when a stock has no optimized config
DEFAULT_STRATEGY_PARAMS = {
    "trend_follow": {"trail_atr_multiple": 1.6, "max_risk_ratio": 0.015},
    "momentum": {"trail_atr_multiple": 1.5, "momentum_period": 20, "max_risk_ratio": 0.02},
    "short_reversal": {"trail_atr_multiple": 2.0, "drop_ratio": 0.18, "max_risk_ratio": 0.02},
    "boll_ma": {"trail_atr_multiple": 1.6, "boll_period": 20, "max_risk_ratio": 0.02},
    "multi_factor": {"trail_atr_multiple": 1.7, "max_risk_ratio": 0.02},
}

# Strategies allowed to run in the optimization pipeline and daily signal
# generation; the rest are paused (their classes/grids stay registered for
# easy re-enable). Set to None to re-enable all strategies.
ACTIVE_STRATEGIES = ["trend_follow"]


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
