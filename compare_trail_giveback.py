"""
Test the big-winner profit lock: once a trade has peaked well past a high
threshold (trail_lock_trigger), the floor ratchets up with the peak
(peak_pct - trail_lock_giveback) instead of sitting flat at breakeven.
Below the lock trigger, behavior is unchanged (EMA_EXIT stays primary).
Compares against the current baseline (no lock) across combos.
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


def run(lock_trigger, lock_giveback):
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf,
                            df_vix=df_vix, trail_lock_trigger=lock_trigger,
                            trail_lock_giveback=lock_giveback)
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


print("Running BASELINE (lock_trigger=1.0 -> never fires, current behavior)...")
baseline_pnl = stats(run(1.0, 0.0), "BASELINE (no profit lock)")

for trig, gb in [(0.15, 0.06), (0.15, 0.08), (0.18, 0.06), (0.18, 0.08),
                  (0.20, 0.08), (0.20, 0.10), (0.25, 0.10)]:
    pnl = stats(run(trig, gb), f"LOCK_TRIGGER={trig:.0%}  LOCK_GIVEBACK={gb:.0%}", show_trades=True)
    diff = pnl - baseline_pnl
    print(f"  vs baseline: {diff:+,.0f}\n")
