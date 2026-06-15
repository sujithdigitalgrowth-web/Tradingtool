"""
Compare V2 baseline vs RSI exhaustion filter at multiple thresholds.

Hypothesis: When RSI is already deeply oversold (<35) on a PE signal,
the move is exhausted and a bounce is imminent — skip the trade.

Thresholds tested:
  rsi_floor_pe = 0  → baseline (current: RSI just needs to be < 40)
  rsi_floor_pe = 33 → skip PE when RSI < 33  (very strict oversold block)
  rsi_floor_pe = 35 → skip PE when RSI < 35
  rsi_floor_pe = 38 → skip PE when RSI < 38  (most aggressive filter)

Mirror logic applies to CE: rsi_ceil_ce = 100-floor
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=57)

FLOORS = [0, 33, 35, 38]   # rsi_floor_pe values to test

print(f"\nBacktest range: {START} to {END}")
print("Fetching data via Angel One (this takes ~60 seconds)...\n")

try:
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print("Data fetched.\n")
except Exception as e:
    print(f"Angel One fetch failed: {e}")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(START, END)

# ── Run each config ───────────────────────────────────────────────
all_results = {f: [] for f in FLOORS}

current = START
while current <= END:
    if current.weekday() < 5:
        for floor in FLOORS:
            r = bt.simulate_day(current, df_5m, df_1d, df_nbees, df_bnf, df_vix,
                                rsi_floor_pe=floor, rsi_ceil_ce=100 - floor)
            if r:
                all_results[floor].append(r)
    current += timedelta(days=1)


# ── Stats helper ──────────────────────────────────────────────────
def stats(results):
    trades    = [t for r in results for t in r.get("trades", [])
                 if t["reason"] != "PARTIAL_TP"]
    total_pnl = sum(r["daily_pnl"] for r in results)
    wins      = sum(1 for r in results if r["daily_pnl"] > 0)
    losses    = sum(1 for r in results if r["daily_pnl"] < 0)
    sl_hits   = sum(1 for t in trades if t["reason"] == "SL")
    n_days    = len(results)
    n_trades  = len(trades)
    win_rate  = wins / n_days * 100 if n_days else 0
    avg_pnl   = total_pnl / n_days if n_days else 0
    return {
        "total_pnl" : total_pnl,
        "win_days"  : wins,
        "loss_days" : losses,
        "win_rate"  : win_rate,
        "n_trades"  : n_trades,
        "sl_hits"   : sl_hits,
        "avg_pnl"   : avg_pnl,
        "n_days"    : n_days,
    }

smap = {f: stats(all_results[f]) for f in FLOORS}
n    = smap[0]["n_days"]

# ── Per-day detail (show days where configs differ) ───────────────
base_rows = all_results[0]
print(f"{'DATE':<12}", end="")
for f in FLOORS:
    label = f"FLOOR={f}" if f > 0 else "BASELINE"
    print(f"  {label:>9}", end="")
print()
print("-" * (12 + len(FLOORS) * 11))

for i, rb in enumerate(base_rows):
    day = rb["date"]
    pnls = []
    for f in FLOORS:
        rows = all_results[f]
        match = next((r for r in rows if r["date"] == day), None)
        pnls.append(match["daily_pnl"] if match else 0)

    if any(abs(pnls[j] - pnls[0]) > 10 for j in range(1, len(pnls))):
        print(f"{day:<12}", end="")
        for p in pnls:
            sign = "+" if p > 0 else ""
            print(f"  {sign}{p:>8.0f}", end="")
        # Show trade detail for baseline day
        rb_trades = [t for t in rb.get("trades", []) if t["reason"] != "PARTIAL_TP"]
        if rb_trades:
            t0 = rb_trades[0]
            print(f"   [{t0['side']} RSI=?]", end="")
        print()

# ── Summary table ─────────────────────────────────────────────────
print("\n" + "=" * 70)
print(f"{'Metric':<22}", end="")
for f in FLOORS:
    label = f"FLOOR={f}" if f > 0 else "BASELINE"
    print(f"  {label:>10}", end="")
print()
print("-" * 70)

metrics = [
    ("Total P&L",   "total_pnl",  ".0f"),
    ("Win days",    "win_days",   "d"),
    ("Loss days",   "loss_days",  "d"),
    ("Win rate %",  "win_rate",   ".1f"),
    ("Trades",      "n_trades",   "d"),
    ("SL hits",     "sl_hits",    "d"),
    ("Avg P&L/day", "avg_pnl",    ".0f"),
]

for label, key, fmt in metrics:
    print(f"{label:<22}", end="")
    for f in FLOORS:
        v = smap[f][key]
        if fmt == "d":
            print(f"  {int(v):>10}", end="")
        elif fmt == ".0f":
            sign = "+" if v > 0 else ""
            print(f"  {sign}{v:>9.0f}", end="")
        else:
            print(f"  {v:>10{fmt}}", end="")
    print()

print(f"\n(/{n} trading days in range)")
print("\nInterpretation:")
print("  FLOOR=0  : current live strategy (RSI just needs to be < 40)")
print("  FLOOR=35 : skip PE if RSI already below 35 (Jun-08 RSI=34.3 would be blocked)")
print("  FLOOR=38 : skip PE if RSI already below 38 (more aggressive filter)")
print("  Higher floor = fewer trades, but each trade has less exhaustion risk")
