from .base import BaseStrategy
from .boll_ma import BollMAStrategy
from .maatr_base import MAATRBaseStrategy
from .momentum import MomentumStrategy
from .multi_factor import MultiFactorStrategy
from .short_reversal import ShortReversalStrategy

STRATEGY_MAPPING = {
    "maatr_base": MAATRBaseStrategy,
    "momentum": MomentumStrategy,
    "short_reversal": ShortReversalStrategy,
    "boll_ma": BollMAStrategy,
    "multi_factor": MultiFactorStrategy,
}

# Short human-readable description per strategy id; surfaced in `main.py --help`
STRATEGY_DESCRIPTIONS = {
    "maatr_base": "MA + ATR: enter on ATR channel breakout (close > prev close + atr_multiple * ATR) with SMA(20) > SMA(60) trend filter; exit on fixed ATR stop",
    "momentum": "Momentum: enter when Momentum(20) > 0; exit on ATR stop or momentum reversal",
    "short_reversal": "Short reversal: enter when daily drop > fall_ratio; exit on ATR stop",
    "boll_ma": "Bollinger + MA: enter on close <= lower band and close > SMA(60); exit on ATR stop or close > upper band",
    "multi_factor": "Multi-factor: enter on SMA(20) > SMA(60) and pctChg > -5; exit on ATR stop or SMA(20) < SMA(60)",
}

# Default strategy parameters, used as fallback when a stock has no optimized config
DEFAULT_STRATEGY_PARAMS = {
    "maatr_base": {"atr_multiple": 1.8, "max_risk_ratio": 0.02},
    "momentum": {"atr_multiple": 1.5, "momentum_period": 20, "max_risk_ratio": 0.02},
    "short_reversal": {"atr_mult": 2.0, "fall_ratio": 0.18, "max_risk_ratio": 0.02},
    "boll_ma": {"atr_mult": 1.6, "boll_period": 20, "max_risk_ratio": 0.02},
    "multi_factor": {"atr_mult": 1.7, "max_risk_ratio": 0.02},
}
