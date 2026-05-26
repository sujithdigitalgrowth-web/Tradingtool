"""
V2 vs V3 Strategy Comparison — same Apr 1 to May 21 2026 data.

V3 changes tested:
  1. RSI threshold  : 60 / 40  →  55 / 45   (more signals on moderate momentum)
  2. Volume surge   : 1.5×     →  1.2×       (less strict, catches more moves)
  3. Trend filter   : OFF      →  ON         (only CE when Nifty > 20d EMA, PE when below)
  4. Thursday skip  : ON       →  OFF        (trade Thursdays — adds ~4 days/month)
"""
import json
from datetime import date, timedelta
import pandas as pd
import backtest as bt

START = date(2026, 4, 1)
END   = date(2026, 5, 21)

# ── Load V2 results already saved ────────────────────────────────
with open("logs/range_cache.json") as f:
    v2_cache = json.load(f)
v2_results = v2_cache["results"]

# ── Fetch data once, reuse for both runs ──────────────────────────
print("Fetching data from Angel One...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(START, END)
print("Done.\n")

# ── Trend bias helper (20-day EMA of daily Nifty closes) ─────────
def _trend_bias(target_date, df_1d_scaled):
    prev = df_1d_scaled[df_1d_scaled.index.date < target_date]
    if len(prev) < 20:
        return None
    ema20      = prev["Close"].ewm(span=20, adjust=False).mean()
    last_close = float(prev.iloc[-1]["Close"])
    last_ema20 = float(ema20.iloc[-1])
    return "bull" if last_close > last_ema20 else "bear"

# ── Apply V3 overrides ────────────────────────────────────────────
bt.V2_RSI_MIN_CE     = 55
bt.V2_RSI_MAX_PE     = 45
bt.V2_VOL_SURGE_MULT = 1.2
bt.V2_SKIP_THURSDAY  = False
bt.QTY               = bt.LOT_SIZE   # 1 lot = 65 units

print("Running V3 backtest...")
v3_results = []
current = START
while current <= END:
    if current.weekday() < 5:
        bias   = _trend_bias(current, df_1d)
        result = bt.simulate_day(current, df_5m, df_1d,
                                 df_nbees=df_nbees, df_bnf=df_bnf,
                                 df_vix=df_vix, trend_bias=bias)
        if result:
            result["trend_bias"] = bias
            v3_results.append(result)
            tag    = f"[{bias.upper() if bias else 'ANY'}]"
            status = "+" if result["daily_pnl"] >= 0 else ""
            print(f"  {current}  {tag:<7}  PnL={status}{result['daily_pnl']:,.0f}"
                  f"  Trades={result['trade_count']}")
    current += timedelta(days=1)

# ── Summary helpers ───────────────────────────────────────────────
def _summary(results, label, lots=1):
    trades    = sum(r["trade_count"] for r in results)
    wins      = sum(r["win_count"]   for r in results)
    pnl       = sum(r["daily_pnl"]   for r in results)
    trade_days= sum(1 for r in results if r["trade_count"] > 0)
    zero_days = sum(1 for r in results if r["trade_count"] == 0
                    and "Thursday" not in str(r.get("insights", "")))
    thu_days  = sum(1 for r in results if "Thursday" in str(r.get("insights", "")))
    weeks     = (END - START).days / 7
    wr        = wins / trades * 100 if trades else 0
    # scale pnl to 1 lot if needed
    pnl_1lot  = pnl / 2 if lots == 2 else pnl
    return {
        "label"      : label,
        "trades"     : trades,
        "wins"       : wins,
        "wr"         : wr,
        "pnl_1lot"   : pnl_1lot,
        "trade_days" : trade_days,
        "zero_days"  : zero_days,
        "thu_days"   : thu_days,
        "per_week"   : trades / weeks,
    }

# V2 was run with 2 lots (QTY=130), V3 with 1 lot (QTY=65)
v2 = _summary(v2_results, "V2 (current)",  lots=2)
v3 = _summary(v3_results, "V3 (proposed)", lots=1)

# ── Print comparison ──────────────────────────────────────────────
print()
print("=" * 60)
print(f"  STRATEGY COMPARISON  —  {START} to {END}")
print("=" * 60)
print(f"{'Metric':<28} {'V2 (current)':>14} {'V3 (proposed)':>14}")
print("-" * 60)
print(f"{'Total trade entries':<28} {v2['trades']:>14} {v3['trades']:>14}")
print(f"{'Trades per week (avg)':<28} {v2['per_week']:>14.1f} {v3['per_week']:>14.1f}")
print(f"{'Days with trades':<28} {v2['trade_days']:>14} {v3['trade_days']:>14}")
print(f"{'Silent days (no signal)':<28} {v2['zero_days']:>14} {v3['zero_days']:>14}")
print(f"{'Thursdays skipped':<28} {v2['thu_days']:>14} {v3['thu_days']:>14}")
v2wr, v3wr = v2["wr"], v3["wr"]
v2pnl, v3pnl = v2["pnl_1lot"], v3["pnl_1lot"]
print(f"{'Win rate':<28} {v2wr:>13.0f}% {v3wr:>13.0f}%")
print(f"{'Net P&L (1 lot, Rs)':<28} {v2pnl:>+14,.0f} {v3pnl:>+14,.0f}")
print("=" * 60)

# Per-week breakdown
print("\nWeek-by-week P&L (1 lot):")
print(f"  {'Week':<12} {'V2 trades':>10} {'V2 P&L':>12} {'V3 trades':>10} {'V3 P&L':>12}")
print("  " + "-" * 58)

def _by_week(results):
    weeks = {}
    for r in results:
        d  = date.fromisoformat(r["date"])
        wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        if wk not in weeks:
            weeks[wk] = {"t": 0, "p": 0}
        weeks[wk]["t"] += r["trade_count"]
        weeks[wk]["p"] += r["daily_pnl"]
    return weeks

wv2 = _by_week(v2_results)
wv3 = _by_week(v3_results)
all_weeks = sorted(set(list(wv2) + list(wv3)))
for wk in all_weeks:
    d2 = wv2.get(wk, {"t": 0, "p": 0})
    d3 = wv3.get(wk, {"t": 0, "p": 0})
    # V2 was 2 lots, halve the P&L for 1-lot comparison
    p2 = d2["p"] / 2
    p3 = d3["p"]
    print(f"  {wk:<12} {d2['t']:>10} {p2:>+12,.0f} {d3['t']:>10} {p3:>+12,.0f}")

print()
print("V3 changes applied:")
print("  RSI threshold  : 60/40 -> 55/45")
print("  Volume surge   : 1.5x  -> 1.2x")
print("  Trend filter   : OFF   -> ON  (20d EMA daily bias)")
print("  Thursday skip  : ON    -> OFF")
