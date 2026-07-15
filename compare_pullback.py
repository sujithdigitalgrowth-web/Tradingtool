"""
Test the pullback-entry filter: only take a breakout if price recently
retested EMA9 first (within V2_PULLBACK_LOOKBACK candles), instead of
entering on the very first candle to clear all conditions. Simplified proxy
for the "wait for pullback, enter on the bounce" entry style research
suggests reduces whipsaw risk vs. chasing a breakout close directly.

Runs on top of the current finalized defaults (1-lot, no-cap trailing,
volume filter off).
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


def run(require_pullback):
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf,
                            df_vix=df_vix, require_pullback=require_pullback)
        if r:
            results.append(r)
    return results


def _hold_min(t):
    if not (t.get("time") and t.get("exit_time")):
        return None
    em = int(t["time"][:2]) * 60 + int(t["time"][3:5])
    xm = int(t["exit_time"][:2]) * 60 + int(t["exit_time"][3:5])
    return xm - em


def stats(results, label):
    trades = [t for r in results for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
    total_pnl = sum(r["daily_pnl"] for r in results)
    wins   = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    wr     = round(wins / len(trades) * 100) if trades else 0
    traded_days = {r["date"] for r in results if r.get("trade_count", 0) > 0}
    holds = [_hold_min(t) for t in trades if _hold_min(t) is not None]
    next_candle = sum(1 for h in holds if h <= 5)

    print(f"{'='*72}")
    print(label)
    print(f"{'='*72}")
    print(f"  Trades          : {len(trades)}")
    print(f"  Days with trade : {len(traded_days)} / {len(days)}")
    print(f"  Win rate        : {wr}%  ({wins}W / {losses}L)")
    print(f"  Total P&L       : Rs.{total_pnl:,.0f}")
    print(f"  Exited on very next candle (fastest possible): {next_candle}")

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


print("Running WITHOUT pullback filter (current baseline)...")
stats(run(False), "WITHOUT pullback filter (current baseline)")

print("Running WITH pullback filter (require recent EMA9 retest)...")
stats(run(True), "WITH pullback filter (require recent EMA9 retest)")
