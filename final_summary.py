"""
FINAL wrap-up: last 30 days under the finalized strategy.

Finalized changes from tonight (all now baked into backtest.py defaults):
  1. QTY = 1 lot (LOT_SIZE) — matches live trading_config.json
  2. V2_1LOT_HARD_TP = False — no hard 10%/Rs.1100 TP cap; let the trailing
     stop (activates @10%, floor @breakeven) manage exits instead
  3. require_vol_surge = False — volume-surge entry filter removed
     (was disproportionately catching capitulation/climax candles, not
     genuine trend starts)

Kept AS-IS (tested tonight, removing/loosening them made things worse):
  - Supertrend requirement
  - BankNifty alignment requirement
  - RSI 60/40 thresholds
  - 10:15 entry-time gate
  - Candle-color check (neutral either way, left on)

NOT adopted: RSI-divergence exhaustion filter (conceptually caught today's
07-14 trade, but net -Rs.2,210 worse over 60 days in backtest — excluded).

This script shows:
  A) 30-day result, 1-lot, max 2 trades/day  (the finalized baseline)
  B) 30-day result, 2-lot, max 2 trades/day  (existing 2-lot partial+target
     exit mechanism — NOT the no-cap 1-lot exit, since that logic is
     specifically gated to is_one_lot in the code)
  C) 30-day result, 1-lot, max 4 trades/day  (frequency check)
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=30)

print(f"Fetching Angel One data {START} to {END} (last 30 days)...")
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
    partials = [t for r in results for t in r.get("trades", []) if t["reason"] == "PARTIAL_TP"]
    total_pnl = sum(r["daily_pnl"] for r in results)
    wins   = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    wr     = round(wins / len(trades) * 100) if trades else 0
    traded_days = {r["date"] for r in results if r.get("trade_count", 0) > 0}

    print(f"{'='*72}")
    print(label)
    print(f"{'='*72}")
    print(f"  Trades          : {len(trades)}" + (f"  (+{len(partials)} partial exits)" if partials else ""))
    print(f"  Days with trade : {len(traded_days)} / {len(days)}")
    print(f"  Win rate        : {wr}%  ({wins}W / {losses}L)")
    print(f"  Total P&L       : Rs.{total_pnl:,.0f}")

    reasons = {}
    for t in trades + partials:
        r = t["reason"]
        reasons.setdefault(r, [0, 0.0])
        reasons[r][0] += 1
        reasons[r][1] += t["pnl"]
    print("  By exit reason:")
    for r, (n, pnl) in sorted(reasons.items(), key=lambda kv: kv[1][1]):
        print(f"    {r:16s} n={n:4d}  total Rs.{pnl:>10,.0f}  avg Rs.{pnl/n:>8,.0f}")
    print()


# ── A: finalized baseline — 1 lot, 2 trades/day ───────────────────
bt.QTY = bt.LOT_SIZE
bt.V2_MAX_TRADES = 2
stats(run(), "A) FINALIZED — 1 lot, max 2 trades/day")

# ── B: 2 lots, 2 trades/day ────────────────────────────────────────
bt.QTY = bt.LOT_SIZE * 2
bt.V2_MAX_TRADES = 2
stats(run(), "B) 2 LOTS, max 2 trades/day (uses existing partial-exit + 20% target mechanism)")

# ── C: 1 lot, 4 trades/day ─────────────────────────────────────────
bt.QTY = bt.LOT_SIZE
bt.V2_MAX_TRADES = 4
stats(run(), "C) 1 lot, max 4 trades/day (frequency check)")
