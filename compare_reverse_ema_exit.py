"""
Test reversing the position on EMA_EXIT: the moment a CE exits via EMA9
flipping against it, immediately open a PE (and vice versa), instead of
waiting for a fresh signal. Motivated by 2026-07-20's trades, which both
ran to ~Rs.800 open profit then gave it back and exited near breakeven on
EMA_EXIT -- the EMA flip that closed the losing side is itself directional
evidence for the other side.

Compares against baseline (no reversal) across the last 30 trading days.
Uses real India VIX from Angel One directly (not Yahoo -- see
compare_vix13_vs_12.py note on the silent Yahoo failure bug).
"""
from datetime import date, timedelta
import angel_data as ad
import backtest as bt

VIX_TOKEN = "99926017"

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=30)

print(f"\nBacktest range: {START} to {END}")
print("Fetching data...\n")

df_5m, df_1d, df_nbees, df_bnf = ad.fetch_all(START, END)

print("Fetching real India VIX history from Angel One...")
_, auth_token, api_key = ad._angel_login()
df_vix = ad._fetch_daily(auth_token, api_key, VIX_TOKEN, START, END)
print(f"  {len(df_vix)} VIX rows fetched\n")

days = [START + timedelta(days=i) for i in range((END - START).days + 1)
        if (START + timedelta(days=i)).weekday() < 5]
print(f"Trading days in range: {len(days)}\n")


def run(reverse):
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf,
                            df_vix=df_vix, reverse_on_ema_exit=reverse)
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
        for r in results:
            for t in r.get("trades", []):
                if t["reason"] != "PARTIAL_TP":
                    print(f"    {r['date']} {t.get('time','?')}->{t.get('exit_time','?')}  "
                          f"{t['side']:<2} {t.get('strike','?')}  pnl={t['pnl']:+,.0f}  {t['reason']}")
    print()
    return total_pnl, trades


print("Running BASELINE (no reversal, current behavior)...")
baseline_pnl, baseline_trades = stats(run(False), "BASELINE (no reversal)", show_trades=True)

print("Running REVERSE_ON_EMA_EXIT...")
rev_pnl, rev_trades = stats(run(True), "REVERSE ON EMA_EXIT", show_trades=True)

print("=" * 72)
print(f"Extra trades from reversal : {len(rev_trades) - len(baseline_trades)}")
print(f"P&L vs baseline            : Rs.{rev_pnl - baseline_pnl:+,.0f}")
