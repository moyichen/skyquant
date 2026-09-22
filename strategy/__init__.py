from .base import BaseStrategy
from .maatr_base import MAATRBaseStrategy
from .momentum import MomentumStrategy
from .short_reversal import ShortReversalStrategy
from .boll_ma import BollMAStrategy
from .multi_factor import MultiFactorStrategy

STRATEGY_MAPPING = {
    "maatr_base": MAATRBaseStrategy,
    "momentum": MomentumStrategy,
    "short_reversal": ShortReversalStrategy,
    "boll_ma": BollMAStrategy,
    "multi_factor": MultiFactorStrategy,
}

# Default strategy parameters, used as fallback when a stock has no optimized config
DEFAULT_STRATEGY_PARAMS = {
    "maatr_base": {"atr_multiple": 1.8, "max_risk_ratio": 0.02},
    "momentum": {"atr_multiple": 1.5, "momentum_period": 20, "max_risk_ratio": 0.02},
    "short_reversal": {"atr_mult": 2.0, "fall_ratio": 0.18, "max_risk_ratio": 0.02},
    "boll_ma": {"atr_mult": 1.6, "boll_period": 20, "max_risk_ratio": 0.02},
    "multi_factor": {"atr_mult": 1.7, "max_risk_ratio": 0.02},
}
