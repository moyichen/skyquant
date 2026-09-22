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
