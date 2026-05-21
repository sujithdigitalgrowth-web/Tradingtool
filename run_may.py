"""
Run 2-lot simulation for May 2026 (1st to today).
Investment: Rs.30,000 | 2 lots (130 units) per trade
"""
from datetime import date, timedelta
from backtest import fetch_range_data_angel, simulate_day, INITIAL_BALANCE, QTY, LOT_SIZE

start = date(2026, 5, 1)
end   = date(2026, 5, 21)   # today

print(f"\n{'='*55}")
print(f"  2-LOT BACKTEST — MAY 2026")
print(f"  Initial Balance : Rs.{INITIAL_BALANCE:,.0f}")
print(f"  Lot Size        : {LOT_SIZE} units/lot")
print(f"  Qty per trade   : {QTY} units ({QTY//LOT_SIZE} lots)")
print(f"  Period          : {start}  to  {end}")
print(f"{'='*55}\n")

print("Fetching data from Angel One...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_angel(start, end)

if df_5m.empty:
    print("ERROR: No data returned.")
    exit(1)

results   = []
current   = start
total_pnl = 0
trades    = 0
wins      = 0
balance   = INITIAL_BALANCE

print(f"\n{'Date':<12} {'PnL':>10}  {'Trades':>6}  {'Balance':>12}  Note")
print("-" * 60)

while current <= end:
    if current.weekday() < 5:
        r = simulate_day(current, df_5m, df_1d,
                         df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
        if r:
            results.append(r)
            total_pnl += r["daily_pnl"]
            trades    += r["trade_count"]
            wins      += r["win_count"]
            balance   += r["daily_pnl"]

            tag = ""
            if r["trade_count"] > 0:
                tag = "WIN" if r["daily_pnl"] > 0 else "LOSS"
            else:
                note = r.get("insights", [""])
                tag = note[0][:40] if note else "no signal"

            pnl_str = f"Rs.{r['daily_pnl']:+,.0f}" if r["trade_count"] > 0 else "—"
            print(f"  {current}  {pnl_str:>10}  {r['trade_count']:>6}  Rs.{balance:>9,.0f}  {tag}")

    current += timedelta(days=1)

win_rate   = round(wins / trades * 100) if trades else 0
ret_pct    = total_pnl / INITIAL_BALANCE * 100
trade_days = sum(1 for r in results if r["trade_count"] > 0)
total_days = len(results)

print(f"\n{'='*55}")
print(f"  SUMMARY — MAY 2026 (2 lots @ Rs.30,000)")
print(f"{'='*55}")
print(f"  Net P&L         : Rs.{total_pnl:+,.2f}  ({ret_pct:+.1f}%)")
print(f"  Final Balance   : Rs.{balance:,.2f}")
print(f"  Trades          : {trades}  ({wins}W / {trades-wins}L)  |  Win rate: {win_rate}%")
print(f"  Active days     : {trade_days} / {total_days} trading days")
print(f"{'='*55}\n")
