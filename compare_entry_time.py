"""
Compare morning entry start times with warm indicators active.
Tests: 09:30 | 09:45 | 10:00 | 10:15
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


def run_scenario(label, no_entry_before):
    bt.V2_NO_ENTRY_BEFORE = no_entry_before

    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = bt.simulate_day(current, df_5m, df_1d, df_nbees, df_bnf, df_vix,
                                max_from_open_pct=0.5)
            if r:
                results.append(r)
        current += timedelta(days=1)

    total_pnl  = sum(r["daily_pnl"] for r in results)
    all_trades = [t for r in results for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
    trade_days = [r for r in results if any(t["reason"] != "PARTIAL_TP" for t in r.get("trades", []))]
    win_days   = sum(1 for r in trade_days if r["daily_pnl"] > 0)
    sl_hits    = sum(1 for t in all_trades if t["reason"] in ("SL", "SL_HARD", "SPOT_SL", "SPOT_SL_HARD"))
    tp_hits    = sum(1 for t in all_trades if t["reason"] in ("TARGET", "TRAIL_EXIT", "EOD_SQUAREOFF"))

    print(f"{'='*68}")
    print(f"  {label}  (entry from {no_entry_before}, morning window {no_entry_before}-{bt.V2_MORNING_END})")
    print(f"{'='*68}")
    print(f"  {'DATE':<12} {'P&L':>8}  {'TRADES':>6}  DETAIL")
    print(f"  {'-'*60}")

    for r in results:
        trades = [t for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
        n   = len(trades)
        pnl = r["daily_pnl"]
        if n == 0:
            print(f"  {r['date']:<12} {'---':>8}  {'skip':>6}")
            continue
        sign  = "+" if pnl >= 0 else ""
        parts = []
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
    avg_day = total_pnl / len(results) if results else 0
    avg_trade = total_pnl / len(all_trades) if all_trades else 0
    print(f"  Avg P&L/day  : Rs.{avg_day:+.0f}  |  Avg P&L/trade: Rs.{avg_trade:+.0f}")
    print()

    return total_pnl, len(all_trades), sl_hits, win_days, n_active


orig = bt.V2_NO_ENTRY_BEFORE

results_all = {}
for start_time in ["09:30", "09:45", "10:00", "10:15"]:
    label = f"START {start_time}"
    results_all[start_time] = run_scenario(label, start_time)

bt.V2_NO_ENTRY_BEFORE = orig

# ── Side-by-side summary ──────────────────────────────────────────
print(f"\n{'='*68}")
print(f"  SUMMARY — Entry Start Time Comparison  ({START} to {END})")
print(f"{'='*68}")
print(f"  {'START':>8}  {'TOTAL P&L':>12}  {'TRADES':>7}  {'WIN%':>5}  {'SL':>4}  {'AVG/TRADE':>10}")
print(f"  {'-'*60}")
for t, (pnl, trades, sl, wins, active) in results_all.items():
    wr = wins / active * 100 if active else 0
    avg = pnl / trades if trades else 0
    print(f"  {t:>8}  Rs.{pnl:>+10,.0f}  {trades:>7}  {wr:>4.0f}%  {sl:>4}  Rs.{avg:>+8,.0f}")
print(f"{'='*68}\n")
