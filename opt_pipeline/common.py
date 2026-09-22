# opt_pipeline 各阶段共享的基础设施：
# 路径引导、标准回测执行器、CSV行参数提取、阶段文件路径
import sys
import os
from typing import Optional

# 以脚本方式运行（python3 param_optimize.py）时，sys.path[0] 是本目录，
# 项目根目录需要手动加入。各阶段脚本统一从本模块引导，sys.path hack 只保留这一处。
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
import backtrader as bt

from comm import AStockCommission
from data_source import DataSource, AStockData
from strategy import STRATEGY_MAPPING


# ===================== 流水线阶段文件路径（绝对路径，不依赖 cwd） =====================
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
PARAM_GRID_CSV = os.path.join(OUTPUT_DIR, "param_optimize_result.csv")
OUT_SAMPLE_CSV = os.path.join(OUTPUT_DIR, "out_sample_verify_result.csv")
ROLLING_CSV = os.path.join(OUTPUT_DIR, "rolling_verify.csv")
AGGREGATE_CSV = os.path.join(OUTPUT_DIR, "aggregate_common_param.csv")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")


class BacktestRunner:
    """标准回测执行器：统一初始资金、A股费率、feed 构建，供网格/外样本/滚动校验复用。"""

    def __init__(self, data_source: Optional = None):
        self.ds = data_source or DataSource()
        cfg = self.ds.cfg
        self.initial_capital = cfg["global_setting"]["initial_capital"]
        comm_cfg = cfg["commission_config"]
        self.comminfo = AStockCommission(
            commission=comm_cfg["commission"],
            stamp_duty=comm_cfg["stamp_duty"],
            transfer_fee=comm_cfg["transfer_fee"],
        )

    def run(self, df: pd.DataFrame, strat_id: str, params: dict) -> float:
        """在给定行情片段上运行单策略，返回期末资产"""
        cerebro = bt.Cerebro()
        cerebro.addstrategy(STRATEGY_MAPPING[strat_id], **params)
        cerebro.adddata(AStockData(dataname=df, datetime="datetime"))
        cerebro.broker.setcash(self.initial_capital)
        cerebro.broker.addcommissioninfo(self.comminfo)
        cerebro.run()
        return cerebro.broker.getvalue()

    def profit_rate(self, final_value: float) -> float:
        """期末资产相对初始资金的收益率"""
        return (final_value - self.initial_capital) / self.initial_capital


def extract_params(row, exclude_cols) -> dict:
    """从阶段 CSV 的一行提取策略参数：
    - 剔除非参数列
    - 丢弃 NaN（不同策略参数列不同，缺失列读出为 NaN）
    - period 类参数强转 int（CSV 含 NaN 列会整体变 float）
    """
    param_cols = [col for col in row.index if col not in exclude_cols]
    params = {k: v for k, v in row[param_cols].to_dict().items() if pd.notna(v)}
    return {k: (int(v) if "period" in k else v) for k, v in params.items()}


def to_native(params: dict) -> dict:
    """numpy 标量转 Python 原生类型（yaml.dump 不支持 np.float64 等，会报 RepresenterError）"""
    return {k: (v.item() if hasattr(v, "item") else v) for k, v in params.items()}


def read_stage_csv(path: str) -> pd.DataFrame:
    """读取流水线阶段产物 CSV：
    stock_code 强制字符串防止前导零丢失（000725 -> 725）；空文件返回空 DataFrame"""
    try:
        return pd.read_csv(path, dtype={"stock_code": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
