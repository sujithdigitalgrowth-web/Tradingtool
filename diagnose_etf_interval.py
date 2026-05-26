"""Test v2.2 with NIFTYBEES back at 5m to isolate the 2m vs 5m impact."""
from datetime import date, timedelta
import backtest as bt

start = date(2026, 4, 1)
end   = date(2026, 5, 20)

print("Fetching 5m NIFTYBEES data (original interval)...")
df_5m, df_1d = bt.fetch_range_data(start, end)
df_nbees_5m  = bt._fetch_etf("NIFTYBEES.NS", start, end, interval="5m")
df_bnf_5m    = bt._fetch_etf("BANKBEES.NS",  start, end, interval="5m")
import yfinance as yf
vix_t  = yf.Ticker("^INDIAVIX")
from datetime import timedelta
import pandas as pd
df_vix = vix_t.history(start=start - timedelta(days=10),
                        end=end + timedelta(days=2), interval="1d")
if not df_vix.empty and df_vix.index.tz is not None:
    df_vix.index = df_vix.index.tz_convert("Asia/Kolkata")

total, trades, wins = 0, 0, 0
for delta in range((end - start).days + 1):
    d = start + timedelta(days=delta)
    if d.weekday() < 5:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees_5m,
                            df_bnf=df_bnf_5m, df_vix=df_vix)
        if r:
            total  += r["daily_pnl"]
            trades += r["trade_count"]
            wins   += r["win_count"]
            if r["trade_count"] > 0:
                tag = "W" if r["daily_pnl"] > 0 else "L"
                print(f"  {d}  PnL={r['daily_pnl']:+,.0f}  [{tag}]")

print(f"\nWith 5m NIFTYBEES: PnL=Rs.{total:,.0f}  Trades={trades}  Wins={wins}")
print()
print("--- Comparison ---")
print(f"v2.1 original  : Rs. 7,330  10 trades  5W/5L")
print(f"v2.2 (2m NBEES): Rs. 2,443   5 trades  2W/3L")
print(f"v2.2 (5m NBEES): Rs.{total:,.0f}  {trades} trades  {wins}W/{trades-wins}L  <-- above")
