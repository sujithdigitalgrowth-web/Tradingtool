"""
Leave-one-out sensitivity sweep: with the volume filter already removed
(new default) and the 1-lot no-cap trailing exit (also new default), which
of the REMAINING entry conditions are helping vs just adding noise/blocking
good trades? Tests each one individually, then a combined "most relaxed" run.

Conditions tested:
  - Supertrend alignment (require_supertrend)
  - BankNifty alignment (require_bnf) — known unreliable due to BNF 403 fetch errors
  - Candle color / cl vs open (require_candle_color)
  - RSI threshold looseness (60/40 -> 55/45)
  - Entry time gate (V2_NO_ENTRY_BEFORE 10:15 -> 09:45) — identified as the actual
    cause of today's (07-14) late entry, separate from the volume filter
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


def run(**kwargs):
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf,
                            df_vix=df_vix, **kwargs)
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
    print(f"{label:70s} trades={len(trades):4d}  days={len(traded_days):3d}/{len(days)}  "
          f"WR={wr:3d}%  P&L=Rs.{total_pnl:>10,.0f}")
    return total_pnl


baseline = run()
stats(baseline, "BASELINE (vol off, ST on, BNF on, candle-color on, entry@10:15)")

r = run(require_supertrend=False)
stats(r, "No Supertrend requirement")

r = run(require_bnf=False)
stats(r, "No BankNifty alignment requirement")

r = run(require_candle_color=False)
stats(r, "No candle-color requirement")

bt.V2_RSI_MIN_CE, bt.V2_RSI_MAX_PE = 55, 45
r = run()
stats(r, "Looser RSI (60/40 -> 55/45)")
bt.V2_RSI_MIN_CE, bt.V2_RSI_MAX_PE = 60, 40   # restore

bt.V2_NO_ENTRY_BEFORE = "09:45"
r = run()
stats(r, "Earlier entry window (10:15 -> 09:45)")
bt.V2_NO_ENTRY_BEFORE = "10:15"   # restore

print()
bt.V2_RSI_MIN_CE, bt.V2_RSI_MAX_PE = 55, 45
bt.V2_NO_ENTRY_BEFORE = "09:45"
r = run(require_supertrend=False, require_bnf=False, require_candle_color=False)
stats(r, "KITCHEN SINK: no ST/BNF/candle-color, looser RSI, earlier entry")
