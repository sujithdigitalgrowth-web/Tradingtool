"""
Compare: current time windows vs opening the 12:00-13:30 lunch gap.
Current  : 09:30-12:00  |  13:30-14:50
With lunch: 09:30-14:50  (no gap)
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=30)

print(f"\nFetching data {START} to {END}...")
try:
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print("Data fetched.\n")
except Exception as e:
    print(f"Fetch failed: {e}")
    raise


def run_scenario(label, morning_end, afternoon_start):
    bt.V2_MORNING_END     = morning_end
    bt.V2_AFTERNOON_START = afternoon_start

    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = bt.simulate_day(current, df_5m, df_1d, df_nbees, df_bnf, df_vix,
                                max_from_open_pct=0.5)
            if r:
                results.append(r)
        current += timedelta(days=1)

    total_pnl   = sum(r["daily_pnl"] for r in results)
    trade_days  = [r for r in results if any(t["reason"] != "PARTIAL_TP" for t in r.get("trades", []))]
    all_trades  = [t for r in results for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
    win_days    = sum(1 for r in trade_days if r["daily_pnl"] > 0)
    sl_hits     = sum(1 for t in all_trades if t["reason"] in ("SL", "SL_HARD", "SPOT_SL", "SPOT_SL_HARD"))
    tp_hits     = sum(1 for t in all_trades if t["reason"] in ("TARGET", "TRAIL_EXIT", "EOD_SQUAREOFF"))

    print(f"{'='*65}")
    print(f"  {label}")
    print(f"  Window : {bt.V2_NO_ENTRY_BEFORE}–{morning_end}  |  {afternoon_start}–{bt.NO_ENTRY_AFTER}")
    print(f"{'='*65}")
    print(f"  {'DATE':<12} {'P&L':>8}  {'TRADES':>6}  DETAIL")
    print(f"  {'-'*60}")

    for r in results:
        trades = [t for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
        n      = len(trades)
        pnl    = r["daily_pnl"]
        if n == 0:
            print(f"  {r['date']:<12} {'---':>8}  {'skip':>6}")
            continue
        sign   = "+" if pnl >= 0 else ""
        parts  = []
        for t in trades:
            pct  = (t["exit"] - t["entry"]) / t["entry"] * 100 if t["entry"] else 0
            spct = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
            parts.append(f"{t['side']}@{t['entry']:.0f} {spct}[{t['reason']}]")
        marker = " WIN" if pnl > 0 else " LOSS"
        print(f"  {r['date']:<12} {sign}{pnl:>7.0f}  {n:>6}  {' | '.join(parts)}{marker}")

    n_active = len(trade_days)
    win_rate = win_days / n_active * 100 if n_active else 0
    print(f"  {'-'*60}")
    print(f"  Total P&L    : Rs.{total_pnl:+,.0f}")
    print(f"  Trade days   : {n_active}  |  Win rate: {win_rate:.0f}%  ({win_days}W / {n_active-win_days}L)")
    print(f"  Total trades : {len(all_trades)}  |  SL: {sl_hits}  TP/Trail: {tp_hits}")
    print(f"  Avg P&L/day  : Rs.{total_pnl/len(results):+.0f}")
    print()

    return total_pnl, len(all_trades), sl_hits


# ── Run both scenarios ────────────────────────────────────────────
orig_morning_end     = bt.V2_MORNING_END
orig_afternoon_start = bt.V2_AFTERNOON_START

pnl_current, trades_current, sl_current = run_scenario(
    "CURRENT  — with lunch gap blocked (12:00–13:30)",
    morning_end="12:00", afternoon_start="13:30"
)

pnl_lunch, trades_lunch, sl_lunch = run_scenario(
    "WITH LUNCH — gap open (09:30–14:50 continuous)",
    morning_end="14:50", afternoon_start="09:30"
)

# Restore
bt.V2_MORNING_END     = orig_morning_end
bt.V2_AFTERNOON_START = orig_afternoon_start

# ── Side-by-side summary ──────────────────────────────────────────
diff = pnl_lunch - pnl_current
print(f"\n{'='*65}")
print(f"  VERDICT")
print(f"{'='*65}")
print(f"  Current (gap blocked) : Rs.{pnl_current:+,.0f}  |  {trades_current} trades  |  {sl_current} SL hits")
print(f"  With lunch open       : Rs.{pnl_lunch:+,.0f}  |  {trades_lunch} trades  |  {sl_lunch} SL hits")
print(f"  Difference            : Rs.{diff:+,.0f}")
if diff > 0:
    extra_trades = trades_lunch - trades_current
    print(f"  Opening lunch adds Rs.{diff:+,.0f} over 30 days ({extra_trades} extra trades)")
    print(f"  ✓ Recommend opening the window")
else:
    print(f"  ✗ Lunch trades cost Rs.{abs(diff):,.0f} — keep the gap blocked")
print(f"{'='*65}\n")
