"""
Count how many entry attempts each filter blocks over the full Jan-May dataset.
This tells us exactly which filters to relax.
"""
from datetime import date, timedelta
import pandas as pd
import numpy as np
import backtest as bt

# Load cached data (already fetched)
print("Fetching Angel One data Jan-May...")
start = date(2026, 1, 1)
end   = date(2026, 5, 20)
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(start, end)
print(f"Data loaded: {len(df_5m)} rows\n")

# Count filter blocks across all candles in all trading days
counters = {
    "total_candles_in_window" : 0,
    "passed_all"              : 0,
    "blocked_day_bias"        : 0,
    "blocked_vix"             : 0,
    "blocked_time_window"     : 0,
    "blocked_vol_surge"       : 0,
    "blocked_vwap_ema"        : 0,
    "blocked_rsi"             : 0,
    "blocked_bnf"             : 0,
    "blocked_supertrend"      : 0,
    "days_with_trade"         : 0,
    "total_days"              : 0,
}

current = start
while current <= end:
    if current.weekday() >= 5 or (bt.V2_SKIP_THURSDAY and current.weekday() == 3):
        current += timedelta(days=1)
        continue

    # VIX check
    vix_ok = True
    if df_vix is not None and not df_vix.empty:
        vix_rows = df_vix[df_vix.index.date <= current]
        if not vix_rows.empty:
            vix_val = float(vix_rows.iloc[-1]["Close"])
            vix_ok = bt.V2_VIX_MIN <= vix_val <= bt.V2_VIX_MAX

    nifty_day = df_5m[df_5m.index.date == current].between_time("09:15","15:30")
    if len(nifty_day) < bt.V2_EMA_SLOW + 2:
        current += timedelta(days=1)
        continue

    prev_rows = df_1d[df_1d.index.date < current]
    if prev_rows.empty:
        current += timedelta(days=1)
        continue
    prev_close = float(prev_rows.iloc[-1]["Close"])

    def _align(df_etf):
        if df_etf is None or df_etf.empty: return nifty_day
        d = df_etf[df_etf.index.date == current].between_time("09:15","15:30")
        d = d.reindex(nifty_day.index, method="nearest", tolerance=pd.Timedelta("3min"))
        return d if (not d.empty and d["Volume"].sum() > 0) else nifty_day

    sday = _align(df_nbees)
    bnf  = _align(df_bnf)

    vwap_s   = bt._vwap(sday)
    ema_fast = sday["Close"].ewm(span=bt.V2_EMA_FAST,  adjust=False).mean()
    ema_slow = sday["Close"].ewm(span=bt.V2_EMA_SLOW,  adjust=False).mean()
    vol_ma   = sday["Volume"].rolling(20).mean()
    rsi_s    = bt._rsi(sday["Close"], bt.V2_RSI_PERIOD)
    st_s     = bt._supertrend(sday, bt.V2_ST_PERIOD, bt.V2_ST_MULT)
    has_bnf  = (bnf is not nifty_day)
    bnf_vwap = bt._vwap(bnf) if has_bnf else None

    day_open  = float(nifty_day.iloc[0]["Open"])
    day_bias  = "CE" if day_open >= prev_close else "PE"
    day_had_trade = False
    counters["total_days"] += 1

    sig_candles   = list(sday.iterrows())
    nifty_candles = list(nifty_day.iterrows())

    for i in range(1, len(nifty_candles)):
        ts, _ = nifty_candles[i]
        _, srow = sig_candles[i]
        time_str = ts.strftime("%H:%M")

        in_morning   = bt.V2_NO_ENTRY_BEFORE <= time_str <= bt.V2_MORNING_END
        in_afternoon = bt.V2_AFTERNOON_START  <= time_str <  bt.NO_ENTRY_AFTER

        if not (in_morning or in_afternoon):
            continue

        if not vix_ok:
            counters["blocked_vix"] += 1
            counters["total_candles_in_window"] += 1
            continue

        counters["total_candles_in_window"] += 1

        cl  = float(srow["Close"])
        op  = float(srow["Open"])
        vol = float(srow["Volume"])
        vw  = float(vwap_s.iloc[i])
        ef  = float(ema_fast.iloc[i])
        es  = float(ema_slow.iloc[i])
        vm  = float(vol_ma.iloc[i]) if not np.isnan(vol_ma.iloc[i]) else 0.0
        rsi = float(rsi_s.iloc[i])  if not np.isnan(rsi_s.iloc[i])  else 50.0
        st  = int(st_s.iloc[i])

        if has_bnf and bnf_vwap is not None:
            bnf_cl   = float(bnf.iloc[i]["Close"])
            bnf_vw   = float(bnf_vwap.iloc[i])
            bnf_bull = bnf_cl > bnf_vw
            bnf_bear = bnf_cl < bnf_vw
        else:
            bnf_bull = bnf_bear = True

        vol_surge = vm > 0 and vol > vm * bt.V2_VOL_SURGE_MULT

        # Check each layer independently for a CE signal
        vwap_ema_ok = (cl > vw and cl > ef and cl > es and cl > op)
        rsi_ok      = (rsi > bt.V2_RSI_MIN_CE)
        bnf_ok      = bnf_bull
        st_ok       = (st == 1)
        bias_ok     = (day_bias == "CE")

        if not vol_surge:
            counters["blocked_vol_surge"] += 1
        elif not vwap_ema_ok:
            counters["blocked_vwap_ema"] += 1
        elif not rsi_ok:
            counters["blocked_rsi"] += 1
        elif not bnf_ok:
            counters["blocked_bnf"] += 1
        elif not st_ok:
            counters["blocked_supertrend"] += 1
        elif not bias_ok:
            counters["blocked_day_bias"] += 1
        else:
            counters["passed_all"] += 1
            day_had_trade = True

    if day_had_trade:
        counters["days_with_trade"] += 1
    current += timedelta(days=1)

print("=== Filter Block Analysis (CE direction) ===\n")
total = counters["total_candles_in_window"]
print(f"Total candles in time window : {total:>6}")
print(f"Blocked by VIX filter        : {counters['blocked_vix']:>6}  ({counters['blocked_vix']/total*100:.1f}%)")
print(f"Blocked by volume surge      : {counters['blocked_vol_surge']:>6}  ({counters['blocked_vol_surge']/total*100:.1f}%)")
print(f"Blocked by VWAP+EMA          : {counters['blocked_vwap_ema']:>6}  ({counters['blocked_vwap_ema']/total*100:.1f}%)")
print(f"Blocked by RSI>60            : {counters['blocked_rsi']:>6}  ({counters['blocked_rsi']/total*100:.1f}%)")
print(f"Blocked by BNF alignment     : {counters['blocked_bnf']:>6}  ({counters['blocked_bnf']/total*100:.1f}%)")
print(f"Blocked by Supertrend        : {counters['blocked_supertrend']:>6}  ({counters['blocked_supertrend']/total*100:.1f}%)")
print(f"Blocked by Day Bias          : {counters['blocked_day_bias']:>6}  ({counters['blocked_day_bias']/total*100:.1f}%)")
print(f"PASSED ALL filters           : {counters['passed_all']:>6}  ({counters['passed_all']/total*100:.1f}%)")
print(f"\nDays with at least 1 signal  : {counters['days_with_trade']} / {counters['total_days']}")
