# comm.py — shared utilities for every SkyQuant entry point:
#   * AStockCommission: A-share fee model
#   * project path constants (single source, anchored to this file)
#   * setup_logging: unified console/file logging
#   * apply_blacklist: config-driven stock blacklist
#   * build_commission / apply_broker_settings: one cerebro broker wiring path
import logging
import sys
from pathlib import Path
from typing import List, Optional

import backtrader as bt

# ===================== Project paths (CWD-independent) =====================
PROJECT_ROOT = Path(__file__).parent.resolve()
CACHE_DIR = PROJECT_ROOT / "cache"
STOCK_CACHE_DIR = CACHE_DIR / "stock_cache"
OUTPUT_DIR = PROJECT_ROOT / "output"
EQUITY_DIR = OUTPUT_DIR / "equity_curve"
PLOT_DIR = OUTPUT_DIR / "plots"
LOG_FILE = OUTPUT_DIR / "run.log"
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class AStockCommission(bt.CommInfoBase):
    """
    Custom A-share trading commission model
    Inherits from bt.CommInfoBase
    Fee description:
        Buy: commission + transfer fee
        Sell: commission + transfer fee + stamp duty (levied only on the sell side)
    Parameters:
        commission: commission rate (percentage)
        stamp_duty: stamp duty rate, charged only on sell
        transfer_fee: transfer fee rate, applied to both buy and sell
    """

    params = (
        ("commission", 0.0003),
        ("stamp_duty", 0.001),
        ("transfer_fee", 0.00001),
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("percabs", True),
    )

    def _getcommission(self, size, price, pseudoexec):
        # size > 0 means buy; size < 0 means sell
        trade_value = abs(size) * price
        comm_fee = trade_value * self.p.commission
        transfer_fee = trade_value * self.p.transfer_fee

        if size > 0:
            # Buy
            total_fee = comm_fee + transfer_fee
        else:
            # Sell, add stamp duty
            stamp_fee = trade_value * self.p.stamp_duty
            total_fee = comm_fee + transfer_fee + stamp_fee
        return total_fee


# ===================== Logging =====================
def setup_logging(log_file: Optional[Path] = None, level: int = logging.INFO) -> None:
    """Configure root logging.

    log_file=None -> console only (subprocesses such as main.py; run_all.py
    captures their stdout into its own run.log). Pass LOG_FILE to additionally
    mirror every record to the rotating pipeline log file.
    """
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8", mode="w"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


# ===================== Stock pool helpers =====================
def apply_blacklist(codes: List[str], cfg: dict) -> List[str]:
    """Remove config stock_blacklist codes from a candidate list.

    The blacklist complements the whitelist-style stock_list: a code listed
    there is excluded even when it appears in stock_list or --stock-list.
    """
    blacklist = {str(c) for c in (cfg.get("stock_blacklist") or [])}
    if not blacklist:
        return list(codes)
    return [c for c in codes if str(c) not in blacklist]


def build_commission(cfg: dict) -> AStockCommission:
    """Build the A-share commission info from config commission_config."""
    comm_cfg = cfg["commission_config"]
    return AStockCommission(
        commission=comm_cfg["commission"],
        stamp_duty=comm_cfg["stamp_duty"],
        transfer_fee=comm_cfg["transfer_fee"],
    )


def apply_broker_settings(broker, cfg: dict, initial_capital: float, comminfo: AStockCommission) -> None:
    """Single cerebro broker wiring path: cash + commission + optional slippage.

    global_setting.slippage_perc is a fractional slippage applied to both buy
    and sell fills; 0 (default) disables slippage so daily backtests stay
    fee-only unless the user opts in.
    """
    broker.setcash(initial_capital)
    broker.addcommissioninfo(comminfo)
    slippage_perc = float(cfg.get("global_setting", {}).get("slippage_perc", 0.0) or 0.0)
    if slippage_perc > 0:
        broker.set_slippage_perc(perc=slippage_perc)
