"""
Compare V2 strategy with VIX_MIN=15 (current) vs VIX_MIN=12 (proposed).

Hypothesis: Current threshold of 15 is too high — it blocks trades during
calm/low-VIX markets (VIX 12-15) when premiums are still tradeable.
Lowering to 12 should unlock more trades without degrading win rate.

Period: June 1 – June 19, 2026
"""
from datetime import date, timedelta
import backtest as bt

START = date(2026, 6, 1)
END   = date(2026, 6, 19)

VIX_MINS = [15, 14, 13, 12]

print(f"\nBacktest range: {START} to {END}")
print("Fetching data (this takes ~30 seconds)...\n")

try:
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print("Angel One data fetched.\n")
except Exception as e:
    print(f"Angel One fetch failed: {e}\nFalling back to Yahoo Finance...\n")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(START, END)

# ── Run each VIX_MIN config ───────────────────────────────────────
bt.QTY = 1 * bt.LOT_SIZE   # 1 lot — matches live config

all_results = {}
for vmin in VIX_MINS:
    bt.V2_VIX_MIN = vmin
    rows = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = bt.simulate_day(current, df_5m, df_1d,
                                df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
            if r:
                rows.append(r)
        current += timedelta(days=1)
    all_results[vmin] = rows

# reset to original
bt.V2_VIX_MIN = 15


# ── Stats helper ──────────────────────────────────────────────────
def stats(results):
    trades    = [t for r in results for t in r.get("trades", [])
                 if t["reason"] != "PARTIAL_TP"]
    total_pnl = sum(r["daily_pnl"] for r in results)
    wins      = sum(1 for r in results if r["daily_pnl"] > 0)
    losses    = sum(1 for r in results if r["daily_pnl"] < 0)
    zero_days = sum(1 for r in results if r["daily_pnl"] == 0)
    n_days    = len(results)
    n_trades  = len(trades)
    win_rate  = wins / n_days * 100 if n_days else 0
    avg_pnl   = total_pnl / n_days if n_days else 0
    best_day  = max((r["daily_pnl"] for r in results), default=0)
    worst_day = min((r["daily_pnl"] for r in results), default=0)
    blocked   = sum(1 for r in results if r.get("note", "") and "VIX" in r.get("note", ""))
    return {
        "total_pnl" : total_pnl,
        "win_days"  : wins,
        "loss_days" : losses,
        "zero_days" : zero_days,
        "win_rate"  : win_rate,
        "n_trades"  : n_trades,
        "avg_pnl"   : avg_pnl,
        "best_day"  : best_day,
        "worst_day" : worst_day,
        "blocked"   : blocked,
        "n_days"    : n_days,
    }

smap = {v: stats(all_results[v]) for v in VIX_MINS}

# ── Per-day detail — show every trading day ───────────────────────
print(f"{'DATE':<12}  {'DOW':<4}", end="")
for v in VIX_MINS:
    label = f"VIX≥{v}"
    print(f"  {label:>9}", end="")
print(f"  {'NOTE (baseline)'}")
print("-" * (18 + len(VIX_MINS) * 11 + 30))

base_rows = all_results[15]
for rb in base_rows:
    day = rb["date"]
    dow = date.fromisoformat(day).strftime("%a")
    note = rb.get("note", "") or ""

    pnls = []
    for v in VIX_MINS:
        match = next((r for r in all_results[v] if r["date"] == day), None)
        pnls.append(match["daily_pnl"] if match else 0)

    print(f"{day:<12}  {dow:<4}", end="")
    for p in pnls:
        sign = "+" if p > 0 else (" " if p == 0 else "")
        print(f"  {sign}{p:>8,.0f}", end="")

    # Short note
    short = note[:45] if note else ""
    if not short and rb.get("trades"):
        t = rb["trades"][0]
        short = f"{t['side']} → {t['reason']}"
    print(f"  {short}")


# ── Summary table ─────────────────────────────────────────────────
print("\n" + "=" * 75)
print(f"{'Metric':<22}", end="")
for v in VIX_MINS:
    label = f"VIX≥{v}"
    print(f"  {label:>10}", end="")
print()
print("-" * 75)

metrics = [
    ("Total P&L (₹)",   "total_pnl",  ".0f"),
    ("Win days",         "win_days",   "d"),
    ("Loss days",        "loss_days",  "d"),
    ("Zero-trade days",  "zero_days",  "d"),
    ("Win rate %",       "win_rate",   ".1f"),
    ("Total trades",     "n_trades",   "d"),
    ("Avg P&L/day (₹)",  "avg_pnl",    ".0f"),
    ("Best day (₹)",     "best_day",   ".0f"),
    ("Worst day (₹)",    "worst_day",  ".0f"),
    ("Days blocked VIX", "blocked",    "d"),
]

for label, key, fmt in metrics:
    print(f"{label:<22}", end="")
    for v in VIX_MINS:
        val = smap[v][key]
        if fmt == "d":
            print(f"  {int(val):>10}", end="")
        elif fmt == ".0f":
            sign = "+" if val > 0 else ""
            print(f"  {sign}{val:>9,.0f}", end="")
        else:
            print(f"  {val:>10{fmt}}", end="")
    print()

n = smap[15]["n_days"]
print(f"\n(/{n} trading days, June 1 – June 19 2026, 1 lot = {bt.LOT_SIZE} qty)")
print("\nRecommendation:")
best_vmin = min(VIX_MINS, key=lambda v: -smap[v]["total_pnl"])
print(f"  Best P&L at VIX_MIN={best_vmin}  →  ₹{smap[best_vmin]['total_pnl']:+,.0f}")
print(f"  Current  at VIX_MIN=15      →  ₹{smap[15]['total_pnl']:+,.0f}")
diff = smap[best_vmin]["total_pnl"] - smap[15]["total_pnl"]
print(f"  Extra P&L from lowering VIX_MIN: ₹{diff:+,.0f}")
