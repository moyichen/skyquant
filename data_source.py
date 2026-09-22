import datetime
import os
from typing import Optional

import backtrader as bt
import pandas as pd
import tushare as ts
import yaml

# User-level private credentials directory (token not stored in project config)
DEFAULT_CREDENTIALS_PATH = os.path.expanduser("~/.skyquant/tushare.yaml")


# ===================== Data format constants =====================
# Complete business fields (all fields needed, including turn turnover rate)
RAW_COLS = [
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "vol",
    "amount",
    "turn",
    "pct_chg",
]

# Mapping adapted to backtrader naming
RENAME_MAP = {
    "trade_date": "trade_date",
    "pre_close": "preclose",
    "vol": "volume",
    "pct_chg": "pctChg",
}


class AStockData(bt.feeds.PandasData):
    """A-share extended K-line feed: mounts preclose/amount/turn/pctChg extension fields
    beyond standard OHLCV; strategies can read them via self.data.preclose[0] etc.
    (-1 means auto-match by column name against the DataFrame columns)"""

    lines = (
        "preclose",
        "amount",
        "turn",
        "pctChg",
    )
    params = (
        ("preclose", -1),
        ("amount", -1),
        ("turn", -1),
        ("pctChg", -1),
    )


class DataSource:
    """
    A-share market data source: encapsulates config loading, Tushare interface,
    local caching, incremental update, and field formatting.
    Supports multiple instances (different config/cache paths), or can be used
    directly via the module-level default instance.
    """

    RAW_COLS = RAW_COLS
    RENAME_MAP = RENAME_MAP

    def __init__(
        self,
        config_path: Optional[str] = None,
        cache_root: Optional[str] = None,
        credentials_path: Optional[str] = None,
    ):
        self.src_dir = os.path.dirname(os.path.abspath(__file__))

        # Config file path
        self.config_path = config_path or os.path.join(self.src_dir, "config.yaml")
        self.config_path = os.path.normpath(self.config_path)

        # Credentials file path (default ~/.skyquant/tushare.yaml)
        self.credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH

        # Cache root directory
        self.cache_root = cache_root or os.path.join(self.src_dir, "cache/stock_cache")
        os.makedirs(self.cache_root, exist_ok=True)

        # Load config and initialize Tushare interface
        self.cfg = self.load_config()
        self.tushare_token = self._load_token()
        self.start_date = self.cfg["global_setting"]["start_date"]
        self.end_date = self.cfg["global_setting"]["end_date"]
        ts.set_token(self.tushare_token)
        self.pro = ts.pro_api()

    # ---------------- Config ----------------
    def load_config(self) -> dict:
        """Load and return the full config.yaml"""
        if not os.path.isfile(self.config_path):
            raise FileNotFoundError(f"Config file not found! Expected path: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _load_token(self) -> str:
        """Load Tushare token from the user's private credentials file"""
        if os.path.isfile(self.credentials_path):
            with open(self.credentials_path, "r", encoding="utf-8") as f:
                cred = yaml.safe_load(f) or {}
            token = (cred.get("tushare") or {}).get("token")
            if token:
                return token

        raise FileNotFoundError(f"Tushare token not found. Please create {DEFAULT_CREDENTIALS_PATH} with format:\ntushare:\n  token: <your_tushare_token>")

    # ---------------- Utilities ----------------
    @staticmethod
    def get_ts_code(stock_code: str) -> str:
        """6 prefix -> Shanghai SH, otherwise Shenzhen SZ"""
        if stock_code.startswith("6"):
            return f"{stock_code}.SH"
        return f"{stock_code}.SZ"

    @staticmethod
    def _merge_kline_and_turn(df_kline: pd.DataFrame, df_turn: pd.DataFrame) -> pd.DataFrame:
        """Merge K-line data + turnover data (turnover_rate -> turn, fill NaN with 0)"""
        df_turn = df_turn.rename(columns={"turnover_rate": "turn"})
        df_merge = pd.merge(df_kline, df_turn[["trade_date", "turn"]], on="trade_date", how="left")
        df_merge["turn"] = df_merge["turn"].fillna(0.0)
        return df_merge

    def format_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """Field cleaning, datetime conversion, column rename, keep all business fields (including turn)"""
        missing_cols = [col for col in self.RAW_COLS if col not in df.columns]
        if missing_cols:
            raise ValueError(f"Returned data missing required fields: {missing_cols}, original columns: {list(df.columns)}")

        df = df[self.RAW_COLS].copy()
        df["datetime"] = pd.to_datetime(df["trade_date"])
        df.rename(columns=self.RENAME_MAP, inplace=True)
        df.sort_values("datetime", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    # ---------------- Download ----------------
    def full_download_save(self, stock_code: str) -> Optional[pd.DataFrame]:
        """First/forced refresh: download full time range K-line + turnover, merge and save"""
        ts_code = self.get_ts_code(stock_code)
        try:
            df_kline = ts.pro_bar(
                ts_code=ts_code,
                adj="qfq",
                start_date=self.start_date,
                end_date=self.end_date,
            )
            df_turn = self.pro.daily_basic(
                ts_code=ts_code,
                start_date=self.start_date,
                end_date=self.end_date,
                fields="trade_date,turnover_rate",
            )
        except Exception as err:
            print(f"[Interface error] {stock_code} request failed: {err!s}")
            return None

        if df_kline is None or df_kline.empty:
            print(f"[Warning] {stock_code} no market data in K-line range")
            return None

        df_raw = self._merge_kline_and_turn(df_kline, df_turn)
        df_formatted = self.format_df(df_raw)
        df_formatted.to_csv(os.path.join(self.cache_root, f"{stock_code}.csv"), index=False)
        return df_formatted

    def incremental_download(self, stock_code: str, start_dt: str, end_dt: str) -> Optional[pd.DataFrame]:
        """Incremental fetch for date range (K-line + turnover merged)"""
        ts_code = self.get_ts_code(stock_code)
        try:
            df_kline = ts.pro_bar(ts_code=ts_code, adj="qfq", start_date=start_dt, end_date=end_dt)
            df_turn = self.pro.daily_basic(
                ts_code=ts_code,
                start_date=start_dt,
                end_date=end_dt,
                fields="trade_date,turnover_rate",
            )
        except Exception as err:
            print(f"{stock_code} incremental update failed: {err}")
            return None

        if df_kline is None or df_kline.empty:
            return None

        df_raw = self._merge_kline_and_turn(df_kline, df_turn)
        return self.format_df(df_raw)

    # ---------------- Public main interface ----------------
    def fetch_stock(self, stock_code: str, force_refresh: bool = False) -> Optional[pd.DataFrame]:
        """
        Main fetch function: incremental update + same-day cache validation + full-field storage (including turnover)
        :param stock_code: six-digit stock code string
        :param force_refresh: True for full re-download; False for incremental + same-day validation
        :return: formatted DataFrame, or None if data is empty
        """
        cache_path = os.path.join(self.cache_root, f"{stock_code}.csv")

        # Branch 1: forced refresh, request full data directly
        if force_refresh:
            return self.full_download_save(stock_code)

        # Branch 2: local cache exists, check update status
        if os.path.exists(cache_path):
            df_local = pd.read_csv(cache_path, parse_dates=["datetime"])
            local_latest_dt = df_local["datetime"].max()
            local_latest_day = local_latest_dt.date()

            # Already updated today, return local data without calling API to save credits
            if local_latest_day >= datetime.date.today():
                return df_local

            # Historical cache exists, run incremental fetch: latest local date ~ config end date
            start_increment = local_latest_day.strftime("%Y%m%d")
            df_increment = self.incremental_download(stock_code, start_increment, self.end_date)

            if df_increment is not None and not df_increment.empty:
                df_merge = pd.concat([df_local, df_increment], ignore_index=True)
                df_merge.drop_duplicates(subset=["datetime"], keep="last", inplace=True)
                df_merge.sort_values("datetime", inplace=True)
                df_merge.to_csv(cache_path, index=False)
                return df_merge

            # No new data, return old cache directly
            return df_local

        # Branch 3: no local cache, first full download
        return self.full_download_save(stock_code)

    def load_cached_data(self, stock_code: str) -> Optional[pd.DataFrame]:
        """Read local cache CSV only; does not call Tushare API or consume credits"""
        cache_file = os.path.join(self.cache_root, f"{stock_code}.csv")
        if not os.path.exists(cache_file):
            return None
        return pd.read_csv(cache_file, parse_dates=["datetime"])
