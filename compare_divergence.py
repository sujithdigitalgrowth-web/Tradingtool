"""
Test the RSI-divergence entry filter: skip PE entries where price makes a
new N-candle low but RSI does NOT also make a new low (down-move losing
momentum), and mirror for CE. First confirms it blocks today's (07-14)
actual losing trade, then backtests the filter over the last 60 days.
"""
from datetime import date, timedelta
import backtest as bt

TODAY = date(2026, 7, 14)
END   = date.today() - timedelta(days=1)
START = END - timedelta(days=60)

print(f"Fetching Angel One data {START} to {TODAY}...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, TODAY)
print("Data fetched.\n")

# ── Step 1: does it block today's actual trade? ───────────────────
print("="*70)
print("STEP 1 — does the divergence filter block today's (07-14) trade?")
print("="*70)
for label, div in [("WITHOUT divergence filter", False), ("WITH divergence filter", True)]:
    r = bt.simulate_day(TODAY, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix,
                        require_no_divergence=div)
    trades = r["trades"] if r else []
    print(f"{label}: {len(trades)} trade(s)", end="")
    for t in trades:
        print(f" | {t['time']} {t['entry']:.2f}->{t['exit']:.2f} {t['reason']} pnl={t['pnl']:.0f}", end="")
    print()

# ── Step 2: 60-day backtest ─────────────────────────────────────────
days = [START + timedelta(days=i) for i in range((END - START).days + 1)
        if (START + timedelta(days=i)).weekday() < 5]

def run(**kwargs):
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix, **kwargs)
        if r:
            results.append(r)
    return results

def stats(results, label):
    trades = [t for r in results for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
    total_pnl = sum(r["daily_pnl"] for r in results)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    wr = round(wins/len(trades)*100) if trades else 0
    traded_days = {r["date"] for r in results if r.get("trade_count", 0) > 0}
    print(f"{label:45s} trades={len(trades):4d}  days={len(traded_days):3d}/{len(days)}  "
          f"WR={wr:3d}%  P&L=Rs.{total_pnl:>10,.0f}")

print("\n" + "="*70)
print(f"STEP 2 — 60-day backtest ({START} to {END}, {len(days)} trading days)")
print("="*70)
stats(run(require_no_divergence=False), "WITHOUT divergence filter")
stats(run(require_no_divergence=True),  "WITH divergence filter")
