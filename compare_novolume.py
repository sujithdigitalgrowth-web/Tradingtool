"""
Last-30-day test: does removing the volume-surge entry filter help or hurt?
Hypothesis (from today's 07-14 trade diagnosis): the volume filter tends to
catch capitulation/climax candles (a late spike after a long grind), not
early trend starts, since every other condition (EMA9/EMA20/VWAP/RSI/ST/BNF)
was already satisfied continuously for an hour before the volume spike
finally arrived on the exhaustion candle.

Both configs use the already-validated 1-lot, no-cap trailing-to-breakeven
exit (current backtest.py defaults) — only the entry-side volume filter changes.
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


def run(require_vol_surge):
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf,
                            df_vix=df_vix, require_vol_surge=require_vol_surge)
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
    return trades


print("Running WITH volume filter (current live default)...")
r_with = run(require_vol_surge=True)
stats(r_with, "WITH volume filter (current default)")

print("Running WITHOUT volume filter (test)...")
r_without = run(require_vol_surge=False)
stats(r_without, "WITHOUT volume filter (test)")
