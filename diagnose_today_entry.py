"""Dump today's (2026-07-14) 5-min candles with all V2 indicators computed,
around the actual entry time (10:20), to see how extended the down-move
already was by the time the PE signal fired, and what happened right after."""
from datetime import date, timedelta
import backtest as bt
import pandas as pd

TODAY = date(2026, 7, 14)
START = TODAY - timedelta(days=60)

print(f"Fetching Angel One data {START} to {TODAY}...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, TODAY)
print("Data fetched.\n")

sday = df_nbees[df_nbees.index.date == TODAY].between_time("09:15", "15:30")
bnf_day = df_bnf[df_bnf.index.date == TODAY].between_time("09:15", "15:30")

# Warm up indicators with prior-day history, same approach as backtest.py
prev = df_nbees[df_nbees.index.date < TODAY].between_time("09:15", "15:30").tail(30)
warm = pd.concat([prev, sday])
n = len(prev)

ema_fast = warm["Close"].ewm(span=bt.V2_EMA_FAST, adjust=False).mean().iloc[n:n+len(sday)]
ema_slow = warm["Close"].ewm(span=bt.V2_EMA_SLOW, adjust=False).mean().iloc[n:n+len(sday)]
rsi_s    = bt._rsi(warm["Close"], bt.V2_RSI_PERIOD).iloc[n:n+len(sday)]
st_s     = bt._supertrend(warm, bt.V2_ST_PERIOD, bt.V2_ST_MULT).iloc[n:n+len(sday)]
vol_ma   = warm["Volume"].rolling(20, min_periods=5).mean().iloc[n:n+len(sday)]
vwap_s   = bt._vwap(sday)
bnf_vwap = bt._vwap(bnf_day) if not bnf_day.empty else None

day_open = float(sday.iloc[0]["Open"])

print(f"{'Time':6s} {'Close':>8s} {'Open':>8s} {'VWAP':>8s} {'EMA9':>8s} {'EMA20':>8s} "
      f"{'RSI':>6s} {'ST':>3s} {'Vol':>8s} {'VolAvg':>8s} {'BNFbear':>7s} {'%fromOpen':>9s} raw_sell?")
for i in range(len(sday)):
    row = sday.iloc[i]
    cl, op, vol = float(row["Close"]), float(row["Open"]), float(row["Volume"])
    vw = float(vwap_s.iloc[i])
    ef = float(ema_fast.iloc[i]) if i < len(ema_fast) else float('nan')
    es = float(ema_slow.iloc[i]) if i < len(ema_slow) else float('nan')
    rsi = float(rsi_s.iloc[i]) if i < len(rsi_s) else float('nan')
    st = int(st_s.iloc[i]) if i < len(st_s) else 0
    vm = float(vol_ma.iloc[i]) if i < len(vol_ma) and not pd.isna(vol_ma.iloc[i]) else 0.0
    vol_surge = vm > 0 and vol > vm * bt.V2_VOL_SURGE_MULT
    bnf_bear = True
    if bnf_vwap is not None and i < len(bnf_day):
        bnf_bear = float(bnf_day.iloc[i]["Close"]) < float(bnf_vwap.iloc[i])
    pct_from_open = (day_open - cl) / day_open * 100
    raw_sell = (cl < vw and cl < ef and cl < es and cl < op
                and vol_surge and rsi < bt.V2_RSI_MAX_PE and bnf_bear and st == -1)
    tstr = sday.index[i].strftime("%H:%M")
    print(f"{tstr:6s} {cl:8.2f} {op:8.2f} {vw:8.2f} {ef:8.2f} {es:8.2f} "
          f"{rsi:6.1f} {st:3d} {vol:8.0f} {vm:8.0f} {str(bnf_bear):>7s} {pct_from_open:9.2f} "
          f"{'*** SELL ***' if raw_sell else ''}")
