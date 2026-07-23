"""
One-off backtest: June 2026, VIX range 12-30, 2 lots.
Compare with current VIX min=15 to see extra trades unlocked.
"""
from datetime import date, timedelta
import backtest as bt

START = date(2026, 6, 1)
END   = date(2026, 6, 24)   # up to yesterday

MAX_FROM_OPEN = 0.5

def run(vix_min, vix_max, label):
    bt.V2_VIX_MIN = vix_min
    bt.V2_VIX_MAX = vix_max

    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = bt.simulate_day(current, df_5m, df_1d, df_nbees, df_bnf, df_vix,
                                max_from_open_pct=MAX_FROM_OPEN)
            if r:
                results.append(r)
        current += timedelta(days=1)

    print(f"\n{'='*65}")
    print(f"  {label}  |  VIX {vix_min}–{vix_max}  |  {START} to {END}")
    print(f"{'='*65}\n")

    total_pnl   = 0.0
    win_days    = 0
    loss_days   = 0
    skip_days   = 0
    trade_count = 0
    sl_count    = 0
    tp_count    = 0

    print(f"{'DATE':<12} {'P&L':>8}  {'TRADES':>6}  DETAIL")
    print("-" * 65)

    for r in results:
        pnl    = r["daily_pnl"]
        trades = [t for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
        n      = len(trades)
        total_pnl   += pnl
        trade_count += n

        if n == 0:
            skip_days += 1
            insight = next((i for i in r.get("insights", []) if "VIX" in i or "No V2" in i), "no signal")
            print(f"{r['date']:<12} {'---':>8}  {'skip':>6}  {insight[:50]}")
            continue

        sign = "+" if pnl >= 0 else ""
        if pnl > 0:
            win_days  += 1
        else:
            loss_days += 1

        detail_parts = []
        for t in trades:
            sl_count += t["reason"] == "SL"
            tp_count += t["reason"] in ("TARGET", "PARTIAL_TP", "TRAIL_EXIT")
            pct = (t["exit"] - t["entry"]) / t["entry"] * 100 if t["entry"] else 0
            spct = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
            detail_parts.append(f"{t['side']}@{t['entry']:.0f} {spct} [{t['reason']}]")

        marker = " <-- WIN" if pnl > 0 else ""
        print(f"{r['date']:<12} {sign}{pnl:>7.0f}  {n:>6}  {' | '.join(detail_parts)}{marker}")

    n_active = len(results) - skip_days
    win_rate = win_days / n_active * 100 if n_active else 0
    trade_win_count = trade_count - sl_count
    trade_win_rate  = trade_win_count / trade_count * 100 if trade_count else 0

    print(f"\n{'='*65}")
    print(f"  Period       : {START} to {END}  ({len(results)} trading days)")
    print(f"  Total P&L    : Rs.{total_pnl:+,.0f}  (2 lots)")
    print(f"  Days active  : {n_active}  (skipped {skip_days})")
    print(f"  Win days     : {win_days}/{n_active}  ({win_rate:.0f}% day win rate)")
    print(f"  Total trades : {trade_count}")
    print(f"  Win trades   : {trade_win_count}/{trade_count}  ({trade_win_rate:.0f}% trade win rate)")
    print(f"  SL hits      : {sl_count}")
    print(f"  TP exits     : {tp_count}")
    print(f"  Avg P&L/trade: Rs.{total_pnl/trade_count:+.0f}" if trade_count else "  No trades")
    print(f"{'='*65}\n")

    return results


print("Fetching June 2026 data...\n")
try:
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print("Data fetched.\n")
except Exception as e:
    print(f"Data fetch failed: {e}")
    raise

# Run both so you can compare
run(15, 30, "CURRENT  (VIX min=15)")
run(12, 30, "PROPOSED (VIX min=12)")
