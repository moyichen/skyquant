import tushare as ts
import pandas as pd
import os
import datetime
import yaml
from typing import Optional

import backtrader as bt


# 用户级私有凭据目录（token 不随项目配置入库）
DEFAULT_CREDENTIALS_PATH = os.path.expanduser("~/.skyquant/tushare.yaml")


# ===================== 数据格式常量 =====================
# 完整业务字段（最终全部需要的字段，包含 turn 换手率）
RAW_COLS = [
    "trade_date", "open", "high", "low", "close",
    "pre_close", "vol", "amount", "turn", "pct_chg"
]

# 映射适配backtrader命名
RENAME_MAP = {
    "trade_date": "trade_date",
    "pre_close": "preclose",
    "vol": "volume",
    "pct_chg": "pctChg"
}


class AStockData(bt.feeds.PandasData):
    """A股扩展K线feed：在标准OHLCV之外挂载 preclose/amount/turn/pctChg 扩展字段，
    策略内可通过 self.data.preclose[0] 等方式读取（-1 表示按列名自动匹配DataFrame列）"""
    lines = ("preclose", "amount", "turn", "pctChg",)
    params = (
        ("preclose", -1),
        ("amount", -1),
        ("turn", -1),
        ("pctChg", -1),
    )


class DataSource:
    """
    A股行情数据源：封装配置加载、Tushare接口、本地缓存、增量更新、字段格式化。
    支持多实例（不同config/cache路径），也可通过模块级默认实例直接调用。
    """

    RAW_COLS = RAW_COLS
    RENAME_MAP = RENAME_MAP

    def __init__(self, config_path: Optional[str] = None, cache_root: Optional[str] = None,
                 credentials_path: Optional[str] = None):
        self.src_dir = os.path.dirname(os.path.abspath(__file__))

        # 配置文件路径
        self.config_path = config_path or os.path.join(self.src_dir, "config.yaml")
        self.config_path = os.path.normpath(self.config_path)

        # 凭据文件路径（默认 ~/.skyquant/tushare.yaml）
        self.credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH

        # 缓存根目录
        self.cache_root = cache_root or os.path.join(self.src_dir, "cache/stock_cache")
        os.makedirs(self.cache_root, exist_ok=True)

        # 加载配置并初始化Tushare接口
        self.cfg = self.load_config()
        self.tushare_token = self._load_token()
        self.start_date = self.cfg["global_setting"]["start_date"]
        self.end_date = self.cfg["global_setting"]["end_date"]
        ts.set_token(self.tushare_token)
        self.pro = ts.pro_api()

    # ---------------- 配置 ----------------
    def load_config(self) -> dict:
        """加载并返回config.yaml全文"""
        if not os.path.isfile(self.config_path):
            raise FileNotFoundError(f"配置文件不存在！期望路径：{self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _load_token(self) -> str:
        """从用户私有凭据文件加载 Tushare token"""
        if os.path.isfile(self.credentials_path):
            with open(self.credentials_path, "r", encoding="utf-8") as f:
                cred = yaml.safe_load(f) or {}
            token = (cred.get("tushare") or {}).get("token")
            if token:
                return token

        raise FileNotFoundError(
            f"Tushare token 未找到。请创建 {DEFAULT_CREDENTIALS_PATH}，内容格式：\n"
            "tushare:\n  token: <your_tushare_token>"
        )

    # ---------------- 工具 ----------------
    @staticmethod
    def get_ts_code(stock_code: str) -> str:
        """6开头沪市SH，其余深市SZ"""
        if stock_code.startswith("6"):
            return f"{stock_code}.SH"
        return f"{stock_code}.SZ"

    @staticmethod
    def _merge_kline_and_turn(df_kline: pd.DataFrame, df_turn: pd.DataFrame) -> pd.DataFrame:
        """合并K线数据 + 换手率数据（turnover_rate -> turn，空值填0）"""
        df_turn = df_turn.rename(columns={"turnover_rate": "turn"})
        df_merge = pd.merge(
            df_kline,
            df_turn[["trade_date", "turn"]],
            on="trade_date",
            how="left"
        )
        df_merge["turn"] = df_merge["turn"].fillna(0.0)
        return df_merge

    def format_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """字段清洗、时间转换、列名适配、保留全部业务字段（含turn）"""
        missing_cols = [col for col in self.RAW_COLS if col not in df.columns]
        if missing_cols:
            raise ValueError(f"返回行情缺少必要字段: {missing_cols}, 原始列:{list(df.columns)}")

        df = df[self.RAW_COLS].copy()
        df["datetime"] = pd.to_datetime(df["trade_date"])
        df.rename(columns=self.RENAME_MAP, inplace=True)
        df.sort_values("datetime", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    # ---------------- 下载 ----------------
    def full_download_save(self, stock_code: str) -> Optional[pd.DataFrame]:
        """首次/强制刷新：下载完整时间段K线+换手率并合并存盘"""
        ts_code = self.get_ts_code(stock_code)
        try:
            df_kline = ts.pro_bar(
                ts_code=ts_code, adj="qfq",
                start_date=self.start_date, end_date=self.end_date
            )
            df_turn = self.pro.daily_basic(
                ts_code=ts_code,
                start_date=self.start_date, end_date=self.end_date,
                fields="trade_date,turnover_rate"
            )
        except Exception as err:
            print(f"【接口异常】{stock_code} 请求失败:{str(err)}")
            return None

        if df_kline is None or df_kline.empty:
            print(f"【警告】{stock_code} K线区间无行情数据")
            return None

        df_raw = self._merge_kline_and_turn(df_kline, df_turn)
        df_formatted = self.format_df(df_raw)
        df_formatted.to_csv(os.path.join(self.cache_root, f"{stock_code}.csv"), index=False)
        return df_formatted

    def incremental_download(self, stock_code: str, start_dt: str, end_dt: str) -> Optional[pd.DataFrame]:
        """增量拉取区间数据（K线+换手率合并）"""
        ts_code = self.get_ts_code(stock_code)
        try:
            df_kline = ts.pro_bar(
                ts_code=ts_code, adj="qfq",
                start_date=start_dt, end_date=end_dt
            )
            df_turn = self.pro.daily_basic(
                ts_code=ts_code,
                start_date=start_dt, end_date=end_dt,
                fields="trade_date,turnover_rate"
            )
        except Exception as err:
            print(f"{stock_code}增量更新失败:{err}")
            return None

        if df_kline is None or df_kline.empty:
            return None

        df_raw = self._merge_kline_and_turn(df_kline, df_turn)
        return self.format_df(df_raw)

    # ---------------- 对外主接口 ----------------
    def fetch_stock(self, stock_code: str, force_refresh: bool = False) -> Optional[pd.DataFrame]:
        """
        主拉取函数：增量更新 + 当日缓存校验 + 全字段存储（含换手率）
        :param stock_code: 六位股票代码字符串
        :param force_refresh: True全量重拉覆盖；False走增量+当日校验
        :return: 格式化DataFrame，None为空数据
        """
        cache_path = os.path.join(self.cache_root, f"{stock_code}.csv")

        # 分支1：强制刷新，直接请求全量数据
        if force_refresh:
            return self.full_download_save(stock_code)

        # 分支2：本地缓存存在，校验更新状态
        if os.path.exists(cache_path):
            df_local = pd.read_csv(cache_path, parse_dates=["datetime"])
            local_latest_dt = df_local["datetime"].max()
            local_latest_day = local_latest_dt.date()

            # 当日已经更新完毕，直接返回本地数据，不请求接口节省积分
            if local_latest_day >= datetime.date.today():
                return df_local

            # 存在历史缓存，执行增量拉取：最新本地日期 ~ 配置截止日期
            start_increment = local_latest_day.strftime("%Y%m%d")
            df_increment = self.incremental_download(stock_code, start_increment, self.end_date)

            if df_increment is not None and not df_increment.empty:
                df_merge = pd.concat([df_local, df_increment], ignore_index=True)
                df_merge.drop_duplicates(subset=["datetime"], keep="last", inplace=True)
                df_merge.sort_values("datetime", inplace=True)
                df_merge.to_csv(cache_path, index=False)
                return df_merge

            # 无新增数据直接返回旧缓存
            return df_local

        # 分支3：无本地缓存，首次全量下载
        return self.full_download_save(stock_code)

    def load_cached_data(self, stock_code: str) -> Optional[pd.DataFrame]:
        """仅读取本地缓存csv,不调用Tushare接口、不消耗积分"""
        cache_file = os.path.join(self.cache_root, f"{stock_code}.csv")
        if not os.path.exists(cache_file):
            return None
        return pd.read_csv(cache_file, parse_dates=["datetime"])
