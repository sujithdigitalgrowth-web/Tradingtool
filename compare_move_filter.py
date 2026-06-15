"""
Compare V2 baseline vs "move already done" filter at multiple thresholds.

Hypothesis: By the time all V2 conditions align, Nifty has often already moved
a significant chunk in that direction from the day open. Entering then is chasing.

Filter: skip entry if price has moved > X% from day open in the signal direction.

  max_from_open_pct = 0.0  → baseline (no filter)
  max_from_open_pct = 0.3  → block if already moved 0.3% from open (~70 Nifty pts)
  max_from_open_pct = 0.4  → block if already moved 0.4% from open (~93 Nifty pts)
  max_from_open_pct = 0.5  → block if already moved 0.5% from open (~116 Nifty pts)
  max_from_open_pct = 0.6  → block if already moved 0.6% from open (~140 Nifty pts)

Jun-08 PE signal: price had moved 0.67% from open when signal fired → blocked by all.
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=57)

THRESHOLDS = [0.0, 0.3, 0.4, 0.5, 0.6]

print(f"\nBacktest range: {START} to {END}")
print("Fetching data via Angel One (~60 seconds)...\n")

try:
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print("Data fetched.\n")
except Exception as e:
    print(f"Angel One fetch failed: {e}")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(START, END)

# ── Run each threshold ────────────────────────────────────────────
all_results = {t: [] for t in THRESHOLDS}

current = START
while current <= END:
    if current.weekday() < 5:
        for thresh in THRESHOLDS:
            r = bt.simulate_day(current, df_5m, df_1d, df_nbees, df_bnf, df_vix,
                                max_from_open_pct=thresh)
            if r:
                all_results[thresh].append(r)
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

smap = {t: stats(all_results[t]) for t in THRESHOLDS}
n    = smap[0.0]["n_days"]

# ── Per-day comparison (show days where results differ) ───────────
base_rows = all_results[0.0]
labels    = [f"{t:.1f}%" if t > 0 else "BASE" for t in THRESHOLDS]

print(f"{'DATE':<12}", end="")
for lbl in labels:
    print(f"  {lbl:>8}", end="")
print("  BLOCKED_BY")
print("-" * (12 + len(THRESHOLDS) * 10 + 20))

for rb in base_rows:
    day = rb["date"]
    pnls = []
    for t in THRESHOLDS:
        match = next((r for r in all_results[t] if r["date"] == day), None)
        pnls.append(match["daily_pnl"] if match else 0.0)

    if any(abs(pnls[j] - pnls[0]) > 50 for j in range(1, len(pnls))):
        print(f"{day:<12}", end="")
        for p in pnls:
            sign = "+" if p > 0 else ""
            print(f"  {sign}{p:>7.0f}", end="")

        # Show which thresholds first block this day's trades
        blocked_at = next((f"{THRESHOLDS[j]:.1f}%" for j in range(1, len(THRESHOLDS))
                           if abs(pnls[j] - pnls[0]) > 50), "")

        rb_trades = [t for t in rb.get("trades", []) if t["reason"] != "PARTIAL_TP"]
        side = rb_trades[0]["side"] if rb_trades else "?"
        result = "WIN" if pnls[0] > 0 else "LOSS"
        print(f"  blocked@{blocked_at} [{side} {result}]")

# ── Summary table ─────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"{'Metric':<22}", end="")
for lbl in labels:
    print(f"  {lbl:>9}", end="")
print()
print("-" * 72)

metrics = [
    ("Total P&L (Rs)",  "total_pnl",  ".0f"),
    ("Win days",        "win_days",   "d"),
    ("Loss days",       "loss_days",  "d"),
    ("Win rate %",      "win_rate",   ".1f"),
    ("Trades taken",    "n_trades",   "d"),
    ("SL hits",         "sl_hits",    "d"),
    ("Avg P&L/day",     "avg_pnl",    ".0f"),
]

for label, key, fmt in metrics:
    print(f"{label:<22}", end="")
    for t in THRESHOLDS:
        v = smap[t][key]
        if fmt == "d":
            print(f"  {int(v):>9}", end="")
        elif ".1f" in fmt:
            print(f"  {v:>9.1f}", end="")
        else:
            sign = "+" if isinstance(v, float) and v > 0 else ""
            print(f"  {sign}{v:>8.0f}", end="")
    print()

print(f"\n(/{n} trading days  |  Nifty ATM ~23,000 range)")
print()

# ── Find best threshold ───────────────────────────────────────────
best_t   = max(THRESHOLDS, key=lambda t: smap[t]["total_pnl"])
best_pnl = smap[best_t]["total_pnl"]
print(f"Best threshold by total P&L: {best_t:.1f}%  (Rs.{best_pnl:+.0f})")

# Show trades blocked vs wins/losses breakdown
print()
print("How many WINS vs LOSSES does each threshold block vs baseline?")
print(f"{'Threshold':<12} {'Blocked days':>13}  {'  Wins blocked':>14}  {'Losses blocked':>14}  {'Net saved':>10}")
print("-" * 70)
for t in THRESHOLDS[1:]:
    wins_blocked   = 0
    losses_blocked = 0
    for rb in base_rows:
        day    = rb["date"]
        base_p = rb["daily_pnl"]
        match  = next((r for r in all_results[t] if r["date"] == day), None)
        filt_p = match["daily_pnl"] if match else 0.0
        if abs(filt_p - base_p) > 50:
            if base_p > 0:
                wins_blocked   += 1
            else:
                losses_blocked += 1
    net_saved = losses_blocked - wins_blocked
    print(f"{t:.1f}%        {wins_blocked + losses_blocked:>10}    {wins_blocked:>10}       {losses_blocked:>10}    {net_saved:>+7}")
