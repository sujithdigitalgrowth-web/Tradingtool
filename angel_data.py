"""
Fetch historical OHLCV data from Angel One Smart API for backtesting.
Replaces Yahoo Finance for intraday data — no 58-day limit.

Angel One note: Index tokens (Nifty 50 = 26000) require a paid historical
data subscription. ETF tokens work on standard API.

Nifty 50 proxy: NIFTYBEES × NIFTY_MULTIPLIER (88.31 ± 0.12 over Mar-May 2026).
Verified over 37 trading days — variation <0.5% — accurate for backtesting.

Tokens (NSE):
  NIFTYBEES : 10576   (Nifty 50 ETF  — ×88.31 = Nifty spot proxy)
  BANKBEES  : 11439   (Bank Nifty ETF — alignment check)
"""

import pandas as pd
import requests
import time as _time
from datetime import datetime, date, timedelta
from logzero import logger

NIFTYBEES_TOKEN   = "10576"
BANKBEES_TOKEN    = "11439"
NIFTY_MULTIPLIER  = 88.31   # NIFTYBEES close × this ≈ Nifty 50 spot price
BANKBEES_MULTIPLIER = 100.0  # BANKBEES close × this ≈ BankNifty spot price (recalibrate if ETF drifts)

CHUNK_DAYS = 60            # safe chunk size for 5m API requests
API_URL    = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/historical/v1/getCandleData"


def _headers(auth_token: str, api_key: str) -> dict:
    return {
        "Authorization"   : auth_token,
        "Content-Type"    : "application/json",
        "Accept"          : "application/json",
        "X-UserType"      : "USER",
        "X-SourceID"      : "WEB",
        "X-ClientLocalIP" : "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress"    : "00:00:00:00:00:00",
        "X-PrivateKey"    : api_key,
    }


def _angel_login():
    from login import login
    from dotenv import load_dotenv
    import os
    load_dotenv()
    obj, auth_token, _, _ = login()
    api_key = os.getenv("ANGEL_API_KEY", "")
    return obj, auth_token, api_key


def _fetch_chunk(auth_token: str, api_key: str, token: str, exchange: str,
                 interval: str, from_dt: datetime, to_dt: datetime,
                 retries: int = 3) -> list:
    """Single-chunk candle fetch with retry via direct REST call."""
    body = {
        "exchange"   : exchange,
        "symboltoken": token,
        "interval"   : interval,
        "fromdate"   : from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate"     : to_dt.strftime("%Y-%m-%d %H:%M"),
    }
    hdrs = _headers(auth_token, api_key)
    for attempt in range(retries):
        resp = None
        try:
            resp = requests.post(API_URL, headers=hdrs, json=body, timeout=15)
            data = resp.json()
            if data.get("status") and data.get("data"):
                return data["data"]
            logger.debug(f"Empty response token={token} attempt={attempt+1}: {data.get('message','')}")
        except Exception as e:
            status = resp.status_code if resp is not None else "?"
            logger.warning(f"Fetch error token={token} attempt={attempt+1} status={status}: {e}")
        if attempt < retries - 1:
            _time.sleep(2)
    return []


def _to_df(raw: list, multiplier: float = 1.0) -> pd.DataFrame:
    """
    Convert raw candle list to DataFrame.
    If multiplier != 1.0, scales OHLC by it (to convert ETF → index proxy).
    Volume is kept as-is.
    Always returns a DataFrame with a DatetimeIndex (never a plain RangeIndex).
    """
    if not raw:
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        empty.index = pd.DatetimeIndex([], name="timestamp", tz="Asia/Kolkata")
        return empty
    df = pd.DataFrame(raw, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
    df = df.set_index("timestamp")
    df = df.astype({"Open": float, "High": float, "Low": float,
                    "Close": float, "Volume": float})
    if multiplier != 1.0:
        for col in ["Open", "High", "Low", "Close"]:
            df[col] = (df[col] * multiplier).round(2)
    return df


def _fetch_intraday(auth_token: str, api_key: str, token: str,
                    start: date, end: date, multiplier: float = 1.0) -> pd.DataFrame:
    """Fetch 5m data in CHUNK_DAYS blocks. Applies optional multiplier."""
    all_rows = []
    chunk_start = datetime.combine(start, datetime.min.time().replace(hour=9, minute=15))
    final_end   = datetime.combine(end,   datetime.min.time().replace(hour=15, minute=30))

    while chunk_start <= final_end:
        chunk_end = min(
            chunk_start + timedelta(days=CHUNK_DAYS - 1, hours=6, minutes=15),
            final_end
        )
        logger.info(f"  Fetching token={token} {chunk_start.date()} -> {chunk_end.date()}")
        rows = _fetch_chunk(auth_token, api_key, token, "NSE",
                            "FIVE_MINUTE", chunk_start, chunk_end)
        all_rows.extend(rows)
        chunk_start = chunk_end + timedelta(minutes=5)
        _time.sleep(0.4)

    df = _to_df(all_rows, multiplier)
    return df[~df.index.duplicated(keep="first")]


def _fetch_daily(auth_token: str, api_key: str, token: str,
                 start: date, end: date, multiplier: float = 1.0) -> pd.DataFrame:
    """Fetch 1-day candles (Angel One allows up to 2000 days)."""
    from_dt = datetime.combine(start - timedelta(days=60), datetime.min.time())
    to_dt   = datetime.combine(end   + timedelta(days=2),  datetime.min.time())
    rows    = _fetch_chunk(auth_token, api_key, token, "NSE",
                           "ONE_DAY", from_dt, to_dt)
    return _to_df(rows, multiplier)


def fetch_all(start: date, end: date):
    """
    Login to Angel One and fetch all data for V2 backtest.
    Returns (df_nifty_5m, df_nifty_1d, df_nbees_5m, df_bnf_5m).

    df_nifty_5m / df_nifty_1d : NIFTYBEES × 88.31 → Nifty spot proxy (for P&L)
    df_nbees_5m               : raw NIFTYBEES prices  (for signal generation)
    df_bnf_5m                 : raw BANKBEES prices   (for BNF alignment)
    """
    print("Logging into Angel One...")
    _, auth_token, api_key = _angel_login()
    print(f"Fetching data  : {start}  to  {end}\n")

    print("  [1/4] NIFTYBEES 5m  (-> Nifty proxy for P&L)...")
    df_nbees_5m = _fetch_intraday(auth_token, api_key, NIFTYBEES_TOKEN, start, end)
    df_nifty_5m = _to_df([], 1.0)   # will be built from df_nbees_5m below
    if not df_nbees_5m.empty:
        df_nifty_5m = df_nbees_5m.copy()
        for col in ["Open", "High", "Low", "Close"]:
            df_nifty_5m[col] = (df_nifty_5m[col] * NIFTY_MULTIPLIER).round(2)
    print(f"        {len(df_nbees_5m)} rows  (Nifty proxy range: "
          f"{df_nifty_5m['Close'].min():.0f} - {df_nifty_5m['Close'].max():.0f})")

    print("  [2/4] NIFTYBEES 1d  (-> Nifty daily for prev-close)...")
    df_nbees_1d = _fetch_daily(auth_token, api_key, NIFTYBEES_TOKEN, start, end)
    df_nifty_1d = _to_df([], 1.0)   # ensures DatetimeIndex even when 1D fetch fails
    if not df_nbees_1d.empty:
        df_nifty_1d = df_nbees_1d.copy()
        for col in ["Open", "High", "Low", "Close"]:
            df_nifty_1d[col] = (df_nifty_1d[col] * NIFTY_MULTIPLIER).round(2)
    print(f"        {len(df_nifty_1d)} rows")

    print("  [3/4] NIFTYBEES 5m  (signal source — raw ETF prices)...")
    # Already fetched above; just report
    print(f"        reusing above data")

    print("  [4/4] BANKBEES  5m  (BNF alignment)...")
    df_bnf_5m = _fetch_intraday(auth_token, api_key, BANKBEES_TOKEN, start, end)
    print(f"        {len(df_bnf_5m)} rows\n")

    return df_nifty_5m, df_nifty_1d, df_nbees_5m, df_bnf_5m
