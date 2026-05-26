"""
Diagnose which new v2.2 filters are blocking trades vs v2.1.
Tests VIX, time-window, Supertrend separately.
"""
from datetime import date, timedelta
import yfinance as yf
import pandas as pd

# Check VIX values for every trading day Apr-May 2026
vix = yf.Ticker("^INDIAVIX")
df_vix = vix.history(start="2026-03-25", end="2026-05-21", interval="1d")
if df_vix.index.tz is not None:
    df_vix.index = df_vix.index.tz_convert("Asia/Kolkata")

print("=== India VIX per day (Apr 1 - May 20) ===")
print(f"{'Date':<14} {'VIX':>8}  {'In 13-22?':>10}  {'In 13-26?':>10}")
print("-" * 50)

start = date(2026, 4, 1)
end   = date(2026, 5, 20)
current = start
while current <= end:
    if current.weekday() < 5:  # weekday only
        rows = df_vix[df_vix.index.date <= current]
        if not rows.empty:
            vix_val = float(rows.iloc[-1]["Close"])
            in_narrow = "YES" if 13 <= vix_val <= 22 else "NO <---"
            in_wide   = "YES" if 13 <= vix_val <= 26 else "NO <---"
            print(f"{str(current):<14} {vix_val:>8.2f}  {in_narrow:>10}  {in_wide:>10}")
    current += timedelta(days=1)
