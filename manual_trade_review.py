import pandas as pd
import backtrader as bt
from data_source import DataSource, AStockData
from strategy import STRATEGY_MAPPING
from comm import AStockCommission

REQUIRED_COLS = ["trade_date", "stock_code", "side"]

class ManualTradeReview:
    def __init__(self, config_path="config.yaml", trade_csv="manual_trades.csv"):
        self.ds = DataSource(config_path=config_path)
        self.cfg = self.ds.cfg
        # dtype=str 防止股票代码前导零丢失（000725 -> 725）
        self.trade_df = pd.read_csv(trade_csv, parse_dates=["trade_date"], dtype={"stock_code": str})
        missing = [c for c in REQUIRED_COLS if c not in self.trade_df.columns]
        if missing:
            raise ValueError(f"manual_trades.csv 缺少必要列: {missing}，现有列: {list(self.trade_df.columns)}，"
                             f"标准格式为 trade_date,stock_code,side,price,size,profit_loss")
        self.result_list = []
        comm_cfg = self.cfg["commission_config"]
        self.comminfo = AStockCommission(
            commission=comm_cfg["commission"],
            stamp_duty=comm_cfg["stamp_duty"],
            transfer_fee=comm_cfg["transfer_fee"],
        )

    def get_strategy_signal(self, code, strat_id, param):
        """运行单标的单策略，通过统一交易记录接口提取每日买卖信号"""
        df = self.ds.load_cached_data(code)
        if df is None or len(df) == 0:
            # 无缓存时回退增量拉取（带本地缓存加速）
            df = self.ds.fetch_stock(code, force_refresh=False)
        if df is None or len(df) == 0:
            return None
        cerebro = bt.Cerebro()
        strat_cls = STRATEGY_MAPPING[strat_id]
        cerebro.addstrategy(strat_cls, **param)
        feed = AStockData(
            dataname=df,
            datetime="datetime",
            open="open", high="high", low="low", close="close", volume="volume"
        )
        cerebro.adddata(feed)
        # 与正式回测保持一致的资金与手续费设置
        cerebro.broker.setcash(self.cfg["global_setting"]["initial_capital"])
        cerebro.broker.addcommissioninfo(self.comminfo)
        strat_instance = cerebro.run()[0]
        trades_df = strat_instance.get_trade_dataframe()
        # 一笔平仓交易对应两个信号：entry_date买入 / exit_date卖出
        sig = []
        if len(trades_df) > 0:
            for _, t in trades_df.iterrows():
                sig.append({"date": t["entry_date"], "side": "BUY", "price": t["entry_price"]})
                sig.append({"date": t["exit_date"], "side": "SELL", "price": t["exit_price"]})
        return pd.DataFrame(sig)

    def match_manual_trade(self):
        """遍历每一条手工交易，匹配策略同期信号，做对比统计"""
        # 键统一转str（yaml中未加引号的数字代码会被解析为int）
        strategy_params = {str(code): p for code, p in (self.cfg.get("strategy_params", {}) or {}).items()}
        for _, row in self.trade_df.iterrows():
            code = str(row["stock_code"])
            trade_date = row["trade_date"].date()
            side = row["side"] if "side" in row else None
            manual_profit_loss = row["profit_loss"] if "profit_loss" in row else None
            # 读取该标的已优选策略参数
            if code not in strategy_params:
                self.result_list.append({
                    "stock_code":code, "trade_date":trade_date,
                    "manual_side":side, "manual_profit_loss":manual_profit_loss,
                    "strategy":None, "strategy_signal": None,
                    "match":False, "note":"No strategy config"
                })
                continue
            for strat_id, param in strategy_params[code].items():
                sig_df = self.get_strategy_signal(code, strat_id, param)
                signal_side = None
                if sig_df is not None and len(sig_df) > 0:
                    sig_today = sig_df[sig_df["date"] == trade_date]
                    if len(sig_today) > 0:
                        signal_side = sig_today.iloc[0]["side"]
                match_flag = (signal_side == side) if pd.notna(side) else False
                self.result_list.append({
                    "stock_code":code,
                    "trade_date": trade_date,
                    "strategy": strat_id,
                    "manual_side": side,
                    "strategy_signal": signal_side,
                    "match": match_flag,
                    "manual_profit_loss": manual_profit_loss,
                    "note": "" if pd.notna(side) else "manual side missing"
                })
        return pd.DataFrame(self.result_list)

    def summary_report(self, out_csv="output/manual_review_result.csv"):
        df = self.match_manual_trade()
        df.to_csv(out_csv, index=False, encoding="utf8")
        print("===== 手工交易 vs 策略信号复盘报告 =====")
        total_cnt = len(df)
        if total_cnt == 0:
            print("无复盘结果（手工交易无对应策略配置或无行情数据），已输出空报告")
            return df
        match_cnt = int(df["match"].sum())
        match_rate = match_cnt / total_cnt
        # 胜率/盈亏仅统计含 profit_loss 记录的手工交易（未平仓或盈亏缺失的行不参与）
        profit_loss_df = df[df["manual_profit_loss"].notna()]
        if len(profit_loss_df) > 0:
            win_rate = (profit_loss_df[profit_loss_df["manual_profit_loss"] > 0].shape[0]) / len(profit_loss_df)
            avg_profit_loss = profit_loss_df["manual_profit_loss"].mean()
            win_str, profit_loss_str = f"{win_rate:.2%}", f"{avg_profit_loss:.2f}"
        else:
            win_str, profit_loss_str = "N/A", "N/A"
        print(f"总交易次数: {total_cnt}")
        print(f"和策略信号一致次数: {match_cnt}, 匹配率: {match_rate:.2%}")
        print(f"手工交易胜率: {win_str} (基于{len(profit_loss_df)}笔含盈亏记录)")
        print(f"平均单笔盈亏: {profit_loss_str}")
        return df

if __name__ == "__main__":
    reviewer = ManualTradeReview()
    reviewer.summary_report()
