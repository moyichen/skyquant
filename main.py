import backtrader as bt
import pandas as pd
import os
import argparse
from comm import AStockCommission
from data_source import DataSource, AStockData
from strategy import STRATEGY_MAPPING
from metrics_utils import calc_metrics
from plot_utils import plot_all

# ===================== 路径常量 =====================
CACHE_DIR = "cache/stock_cache"
OUTPUT_DIR = "output"
EQUITY_OUT = os.path.join(OUTPUT_DIR, "equity_curve")
PLOT_OUT = os.path.join(OUTPUT_DIR, "plots")
METRICS_SUMMARY = os.path.join(OUTPUT_DIR, "metrics_summary.csv")
TRADE_CSV = "manual_trades.csv"

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(EQUITY_OUT, exist_ok=True)
os.makedirs(PLOT_OUT, exist_ok=True)

# ===================== 加载配置 =====================
DS = DataSource()
CFG = DS.cfg
GLOBAL = CFG["global_setting"]
COMMISSION = CFG["commission_config"]
STOCK_LIST = CFG["stock_list"]
PARAM_POOL = {str(code): p for code, p in CFG["strategy_params"].items()}  # 键统一转str（yaml中未加引号的数字代码会被解析为int）
VALID_CODES = [item["code"] for item in STOCK_LIST]

# 从config.yaml读取手续费配置
comm_cfg = CFG["commission_config"]
comminfo = AStockCommission(
    commission=comm_cfg["commission"],
    stamp_duty=comm_cfg["stamp_duty"],
    transfer_fee=comm_cfg["transfer_fee"],
)

# ===================== 手工交易单据校验 =====================
def check_manual_trade_code():
    """校验手工交易csv内股票代码是否在config标的列表"""
    df_trade = pd.read_csv(TRADE_CSV, dtype={"stock_code": str}, parse_dates=["trade_date"])
    used_codes = df_trade["stock_code"].unique()
    invalid = [c for c in used_codes if c not in VALID_CODES]
    if invalid:
        raise Exception(f"manual_trades.csv包含不在标的列表的代码: {invalid}")
    return df_trade

# ===================== 获取策略与参数 =====================
def get_strategy_param(code: str, strat_id: str):
    strat_cls = STRATEGY_MAPPING[strat_id]
    params = PARAM_POOL.get(code, {}).get(strat_id, {})
    return strat_cls, params

# ===================== 单标的回测入口 =====================
def run_backtest(code: str, strat_id: str, force_refresh: bool):
    df_data = DS.fetch_stock(code, force_refresh)
    if df_data is None:
        print(f"标的 {code} 获取行情失败，跳过")
        return None

    cerebro = bt.Cerebro()
    strat_cls, param_dict = get_strategy_param(code, strat_id)
    cerebro.addstrategy(strat_cls,**param_dict)

    # A股扩展feed：标准OHLCV + preclose/amount/turn/pctChg 扩展字段
    data_feed = AStockData(
        dataname=df_data,
        datetime="datetime",
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        timeframe=bt.TimeFrame.Days
    )
    cerebro.adddata(data_feed)

    # 资金与手续费
    cerebro.broker.setcash(GLOBAL["initial_capital"])
    cerebro.broker.addcommissioninfo(comminfo)
    strat_result = cerebro.run()
    strat_instance = strat_result[0]

    equity_df = strat_instance.get_equity_dataframe()
    trades_df = strat_instance.get_trade_dataframe()
    equity_path = os.path.join(EQUITY_OUT, f"{code}_{strat_id}_equity.csv")
    equity_df.to_csv(equity_path, index=False, encoding="utf-8")

    # 计算指标
    metrics = calc_metrics(equity_df, trades_df)
    # 绘图
    plot_all(equity_df, trades_df, metrics, PLOT_OUT, code, strat_id)
    metrics["stock_code"] = code
    metrics["strategy"] = strat_id
    print(f"{code} {strat_id} 期末总资产:{cerebro.broker.getvalue():.2f}")
    print(f"指标 {metrics}")
    return metrics

# ===================== 主入口 =====================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force_refresh", action="store_true", help="强制全量重拉行情覆盖缓存")
    parser.add_argument("--strategy", default="maatr_base", help="指定策略标识")
    args = parser.parse_args()

    # 校验手工交易文件
    try:
        check_manual_trade_code()
    except Exception as e:
        print("交易单据校验警告：", e)

    metric_rows = []
    # 批量回测标的
    for stock_info in STOCK_LIST:
        c = stock_info["code"]
        name = stock_info["name"]
        print(f"\n==== 开始回测标的:{name}({c}) ====")
        res = run_backtest(c, args.strategy, args.force_refresh)
        if res is not None:
            metric_rows.append(res)
    # 保存汇总指标
    if metric_rows:
        pd.DataFrame(metric_rows).to_csv(METRICS_SUMMARY, index=False, encoding="utf-8")
        print(f"\n指标汇总已保存到 {METRICS_SUMMARY}")
