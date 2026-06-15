"""
30-day backtest with all updated V2 logic:
  - VIX filter via NSE API (15–30 range)
  - 0.5% move-from-open filter (don't enter after move is already done)
  - Dual EMA + RSI + BNF alignment + Supertrend + Volume surge
  - 2-tier SL: warn@17%, hard@20%
  - Partial TP at +10%, trail stop at breakeven after partial
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=30)

MAX_FROM_OPEN = 0.5   # % — don't enter if move already done

print(f"\n{'='*60}")
print(f"  V2 Backtest (with 0.5% move filter)  |  {START} to {END}")
print(f"{'='*60}\n")
print("Fetching data via Angel One...\n")

try:
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print("Data fetched.\n")
except Exception as e:
    print(f"Angel One fetch failed: {e}")
    raise

# ── Run each trading day ─────────────────────────────────────────
results = []
current = START
while current <= END:
    if current.weekday() < 5:
        r = bt.simulate_day(current, df_5m, df_1d, df_nbees, df_bnf, df_vix,
                            max_from_open_pct=MAX_FROM_OPEN)
        if r:
            results.append(r)
    current += timedelta(days=1)

if not results:
    print("No results.")
    raise SystemExit

# ── Per-day table ────────────────────────────────────────────────
print(f"{'DATE':<12} {'P&L':>8}  {'TRADES':>6}  DETAIL")
print("-" * 60)

total_pnl   = 0.0
win_days    = 0
loss_days   = 0
skip_days   = 0
trade_count = 0
sl_count    = 0
tp_count    = 0

for r in results:
    pnl    = r["daily_pnl"]
    trades = [t for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
    n      = len(trades)
    total_pnl   += pnl
    trade_count += n

    if n == 0:
        skip_days += 1
        insight = next((i for i in r.get("insights", []) if "VIX" in i or "No V2" in i), "no signal")
        print(f"{r['date']:<12} {'---':>8}  {'skip':>6}  {insight[:45]}")
        continue

    sign = "+" if pnl >= 0 else ""
    if pnl > 0:
        win_days  += 1
    else:
        loss_days += 1

    # Trade detail
    detail_parts = []
    for t in trades:
        sl_count += t["reason"] == "SL"
        tp_count += t["reason"] in ("TARGET", "PARTIAL_TP", "TRAIL_EXIT")
        pct = (t["exit"] - t["entry"]) / t["entry"] * 100 if t["entry"] else 0
        spct = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
        detail_parts.append(f"{t['side']}@{t['entry']:.0f} {spct} [{t['reason']}]")

    marker = " <-- WIN" if pnl > 0 else ""
    print(f"{r['date']:<12} {sign}{pnl:>7.0f}  {n:>6}  {' | '.join(detail_parts)}{marker}")

# ── Summary ──────────────────────────────────────────────────────
n_active = len(results) - skip_days
win_rate = win_days / n_active * 100 if n_active else 0

print(f"\n{'='*60}")
print(f"  Period      : {START} to {END}  ({len(results)} trading days)")
print(f"  Total P&L   : Rs.{total_pnl:+,.0f}")
print(f"  Days active : {n_active}  (skipped {skip_days} — no signal / VIX out of range)")
print(f"  Win days    : {win_days}/{n_active}  ({win_rate:.0f}% win rate)")
print(f"  Loss days   : {loss_days}/{n_active}")
print(f"  Total trades: {trade_count}")
print(f"  SL hits     : {sl_count}")
print(f"  TP exits    : {tp_count}")
print(f"  Avg P&L/day : Rs.{total_pnl/len(results):+.0f}  (all days incl. skips)")
print(f"  Avg P&L/trade: Rs.{total_pnl/trade_count:+.0f}" if trade_count else "")
print(f"\n  Move filter : skip if already moved >{MAX_FROM_OPEN}% from day open")
print(f"  VIX range   : {bt.V2_VIX_MIN}–{bt.V2_VIX_MAX}")
print(f"  SL levels   : warn@{bt.V2_SL_WARN_PCT*100:.0f}%  hard@{bt.V2_SL_OPTION_PCT*100:.0f}%")
print(f"{'='*60}\n")
