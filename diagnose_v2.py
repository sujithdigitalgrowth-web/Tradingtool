"""
Compare v2.2 (all filters) vs removing Supertrend vs removing time-window.
"""
from datetime import date, timedelta
import backtest as bt
import pandas as pd

start = date(2026, 4, 1)
end   = date(2026, 5, 20)

print("Fetching data...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(start, end)

# --- v2.2 as-is ---
total, trades = 0, 0
for delta in range((end - start).days + 1):
    d = start + timedelta(days=delta)
    if d.weekday() < 5:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
        if r:
            total  += r["daily_pnl"]
            trades += r["trade_count"]
print(f"\nv2.2 (current):       PnL=Rs.{total:,.0f}  Trades={trades}")

# --- Without Supertrend (patch st check) ---
import numpy as np
orig_sim = bt.simulate_day

# Monkey-patch: override _supertrend to always return +1 for CE and -1 for PE
# Easiest: just change the condition in backtest constants
orig_ST_PERIOD = bt.V2_ST_PERIOD
orig_ST_MULT   = bt.V2_ST_MULT

# Create a version where Supertrend always passes
# We'll do this by temporarily monkey-patching _supertrend to return all +1
orig_supertrend = bt._supertrend
def _st_disabled(df, period=7, multiplier=2.0):
    # Return +1 everywhere (CE always passes, PE never) — to test CE impact
    # Better: return the direction from close > EMA as a proxy
    ema = df["Close"].ewm(span=10, adjust=False).mean()
    return (df["Close"] > ema).astype(int).replace({0: -1})
# Actually let's just return 0 (neutral, which won't block)
def _st_neutral(df, period=7, multiplier=2.0):
    import pandas as pd
    # Always return direction matching EMA — essentially same as EMA filter
    # Return +1 for CE compatible and -1 for PE compatible
    # For testing: just return all +1 so CE passes, and check separately for PE
    return pd.Series(1, index=df.index)  # all bullish — disables PE but tests CE removal

# Better approach: patch the check by replacing with no-op
# We'll test by setting supertrend to match the signal direction always
def _st_passthrough(df, period=7, multiplier=2.0):
    """Make supertrend always agree with the trade direction."""
    import pandas as pd
    # Supertrend aligned with fast EMA
    ema = df["Close"].ewm(span=bt.V2_EMA_FAST, adjust=False).mean()
    direction = (df["Close"] > ema).map({True: 1, False: -1})
    return direction

bt._supertrend = _st_passthrough
total2, trades2 = 0, 0
for delta in range((end - start).days + 1):
    d = start + timedelta(days=delta)
    if d.weekday() < 5:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
        if r:
            total2  += r["daily_pnl"]
            trades2 += r["trade_count"]
print(f"Without Supertrend:   PnL=Rs.{total2:,.0f}  Trades={trades2}")

bt._supertrend = orig_supertrend

# --- Without VIX filter ---
orig_vix_min = bt.V2_VIX_MIN
orig_vix_max = bt.V2_VIX_MAX
bt.V2_VIX_MIN = 0
bt.V2_VIX_MAX = 999
total3, trades3 = 0, 0
for delta in range((end - start).days + 1):
    d = start + timedelta(days=delta)
    if d.weekday() < 5:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
        if r:
            total3  += r["daily_pnl"]
            trades3 += r["trade_count"]
print(f"Without VIX filter:   PnL=Rs.{total3:,.0f}  Trades={trades3}")
bt.V2_VIX_MIN = orig_vix_min
bt.V2_VIX_MAX = orig_vix_max

# --- Without time window (use original NO_ENTRY_AFTER check only) ---
orig_morning_end     = bt.V2_MORNING_END
orig_afternoon_start = bt.V2_AFTERNOON_START
bt.V2_MORNING_END     = "14:50"   # extend morning all the way
bt.V2_AFTERNOON_START = "14:50"   # disable afternoon
total4, trades4 = 0, 0
for delta in range((end - start).days + 1):
    d = start + timedelta(days=delta)
    if d.weekday() < 5:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
        if r:
            total4  += r["daily_pnl"]
            trades4 += r["trade_count"]
print(f"Without time window:  PnL=Rs.{total4:,.0f}  Trades={trades4}")
bt.V2_MORNING_END     = orig_morning_end
bt.V2_AFTERNOON_START = orig_afternoon_start
