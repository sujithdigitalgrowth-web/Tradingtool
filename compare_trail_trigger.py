"""
Test lowering the trail-activation trigger from the current +10% (V2_TRAIL_TRIGGER)
down to +6%, so trades that spike but don't quite reach +10% still get a
breakeven-ish floor instead of zero protection.
Compares against the current baseline (10%) across the last 60 trading days.
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=60)

print(f"\nFetching Angel One data {START} to {END} (last 60 days)...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
print("Data fetched.\n")

days = [START + timedelta(days=i) for i in range((END - START).days + 1)
        if (START + timedelta(days=i)).weekday() < 5]
print(f"Trading days in range: {len(days)}\n")


def run(trigger):
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf,
                            df_vix=df_vix, trail_trigger=trigger)
        if r:
            results.append(r)
    return results


def stats(results, label, show_trades=False):
    trades = [t for r in results for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
    total_pnl = sum(r["daily_pnl"] for r in results)
    wins   = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    wr     = round(wins / len(trades) * 100) if trades else 0

    print(f"{'='*72}")
    print(label)
    print(f"{'='*72}")
    print(f"  Trades          : {len(trades)}")
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
    if show_trades:
        for t in trades:
            if t["reason"] == "TRAIL_EXIT":
                print(f"    {t.get('date','?')} {t.get('time','?')}->{t.get('exit_time','?')}  pnl={t['pnl']:+,.0f}")
    print()
    return total_pnl


print("Running BASELINE (trail_trigger=10%, current behavior)...")
baseline_pnl = stats(run(0.10), "BASELINE (trail_trigger=10%)")

for trig in [0.04, 0.05, 0.06, 0.07, 0.08]:
    pnl = stats(run(trig), f"TRAIL_TRIGGER={trig:.0%}", show_trades=True)
    diff = pnl - baseline_pnl
    print(f"  vs baseline: {diff:+,.0f}\n")
