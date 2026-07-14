"""
Last-30-day analysis: old live behavior (2-lot, hard 10% TP cap) vs the new
validated defaults (1-lot, no-cap trailing-to-breakeven) now baked into
backtest.py. Single data fetch, one Angel One login.
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=30)

print(f"\nFetching Angel One data {START} to {END} (last 30 days)...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
print("Data fetched.\n")

days = [START + timedelta(days=i) for i in range((END - START).days + 1)
        if (START + timedelta(days=i)).weekday() < 5]
print(f"Trading days in range: {len(days)}\n")


def run():
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
        if r:
            results.append(r)
    return results


def stats(results, label):
    trades = [t for r in results for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
    total_pnl = sum(r["daily_pnl"] for r in results)
    wins   = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    wr     = round(wins / len(trades) * 100) if trades else 0
    traded_days = {r["date"] for r in results if r.get("trade_count", 0) > 0}
    gaps, streak = [], 0
    for d in days:
        if str(d) in traded_days:
            if streak > 0:
                gaps.append(streak)
            streak = 0
        else:
            streak += 1
    if streak > 0:
        gaps.append(streak)
    longest = max(gaps) if gaps else 0

    print(f"{'='*72}")
    print(label)
    print(f"{'='*72}")
    print(f"  Trades          : {len(trades)}")
    print(f"  Days with trade : {len(traded_days)} / {len(days)}  (longest silent streak: {longest} days)")
    print(f"  Win rate        : {wr}%  ({wins}W / {losses}L)")
    print(f"  Total P&L       : Rs.{total_pnl:,.0f}")

    reasons = {}
    for t in trades:
        r = t["reason"]
        reasons.setdefault(r, [0, 0.0])
        reasons[r][0] += 1
        reasons[r][1] += t["pnl"]
    print("  By exit reason:")
    for r, (n, pnl) in sorted(reasons.items(), key=lambda kv: kv[1][1]):
        print(f"    {r:16s} n={n:4d}  total Rs.{pnl:>10,.0f}  avg Rs.{pnl/n:>8,.0f}")
    print()


print("Running OLD live behavior (TRUE 1-lot, hard 10% TP cap)...")
bt.QTY = bt.LOT_SIZE            # 1 lot — matches actual live sizing
bt.V2_1LOT_HARD_TP = True       # restores the hard cap (this is what was actually live)
r_old = run()
stats(r_old, "OLD — TRUE 1-lot, hard TP cap @10%/Rs.1100 (what was actually running live)")

print("Running NEW defaults (1-lot, no-cap trailing-to-breakeven)...")
bt.QTY = bt.LOT_SIZE
bt.V2_1LOT_HARD_TP = False
r_new = run()
stats(r_new, "NEW — 1-lot, no hard cap, trail-to-breakeven (validated fix)")

# ── Trade-by-trade diff: same entries, different exit outcome ────
old_trades = {}
for r in r_old:
    for t in r.get("trades", []):
        if t["reason"] != "PARTIAL_TP":
            old_trades[(r["date"], t["time"])] = t
new_trades = {}
for r in r_new:
    for t in r.get("trades", []):
        if t["reason"] != "PARTIAL_TP":
            new_trades[(r["date"], t["time"])] = t

print(f"{'='*90}")
print("TRADE-BY-TRADE DIFF (same entry, different exit mechanism)")
print(f"{'='*90}")
print(f"{'Date':11s} {'Entry':6s} {'EntryPx':>8s} | {'OLD exit':>9s} {'reason':>10s} {'pnl':>8s} | {'NEW exit':>9s} {'reason':>10s} {'pnl':>8s}")
for key in sorted(set(old_trades) | set(new_trades)):
    o = old_trades.get(key)
    n = new_trades.get(key)
    d, tm = key
    entry_px = (o or n)["entry"]
    o_str = f"{o['exit']:>9.2f} {o['reason']:>10s} {o['pnl']:>8.0f}" if o else " "*30
    n_str = f"{n['exit']:>9.2f} {n['reason']:>10s} {n['pnl']:>8.0f}" if n else " "*30
    print(f"{d:11s} {tm:6s} {entry_px:>8.2f} | {o_str} | {n_str}")
