"""
Regenerate range_cache.json using Angel One data (no 58-day limit).
Run: py regen_cache.py
"""
from datetime import date, datetime, timedelta
from backtest import fetch_range_data_angel, simulate_day
import json, os

start = date.today() - timedelta(days=365)
end   = date.today() - timedelta(days=1)
while end.weekday() >= 5:
    end -= timedelta(days=1)

print(f"\nBacktest range : {start}  to  {end}")
print("Data source    : Angel One Smart API  (NIFTYBEES x 88.31 as Nifty proxy)\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_angel(start, end)

if df_5m.empty:
    print("ERROR: No data returned from Angel One.")
    exit(1)

print(f"Nifty proxy 5m: {len(df_5m)} rows  "
      f"({df_5m.index[0].date()} to {df_5m.index[-1].date()})")
print(f"Nifty daily   : {len(df_1d)} rows")
print(f"NIFTYBEES 5m  : {len(df_nbees)} rows")
print(f"BANKBEES  5m  : {len(df_bnf)} rows")
print(f"VIX daily     : {len(df_vix)} rows\n")

results   = []
current   = start
total_pnl = 0
trades    = 0
wins      = 0

while current <= end:
    if current.weekday() < 5:
        result = simulate_day(current, df_5m, df_1d,
                              df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
        if result:
            results.append(result)
            total_pnl += result["daily_pnl"]
            trades    += result["trade_count"]
            wins      += result["win_count"]
            if result["trade_count"] > 0:
                tag = "W" if result["daily_pnl"] > 0 else "L"
                print(f"  {current}  PnL={result['daily_pnl']:+,.0f}  [{tag}]")
    current += timedelta(days=1)

win_rate = round(wins / trades * 100) if trades else 0
print(f"\n{'='*50}")
print(f"Total P&L  : Rs.{total_pnl:,.2f}")
print(f"Trades     : {trades}   Winners: {wins}   Win rate: {win_rate}%")
print(f"{'='*50}\n")

cache = {
    "start"    : str(start),
    "end"      : str(end),
    "label"    : f"V2 v2.3 - Angel One data ({start} to {end})",
    "generated": datetime.now().strftime("%d %b %Y %H:%M"),
    "results"  : results,
}
os.makedirs("logs", exist_ok=True)
with open("logs/range_cache.json", "w") as f:
    json.dump(cache, f, indent=2, default=str)
print("Cache saved -> logs/range_cache.json")
print("Open http://localhost:5000 -> Date Range Analysis to view results.")
