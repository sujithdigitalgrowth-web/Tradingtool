"""
Compare baseline V2 vs V2 + A+B signal-aware exit.
A = counter-signal exit (opposite signal fires while in -5% loss)
B = VWAP flip exit (price crosses back through VWAP while in -5% loss)
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=57)

print(f"\nBacktest range: {START} to {END}")
print("Fetching data via Angel One (this takes ~60 seconds)...\n")

try:
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print("Data fetched.\n")
except Exception as e:
    print(f"Angel One fetch failed: {e}")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(START, END)

# ── Run both configs ──────────────────────────────────────────────
rows_base, rows_ab = [], []
current = START
while current <= END:
    if current.weekday() < 5:
        r_base = bt.simulate_day(current, df_5m, df_1d, df_nbees, df_bnf, df_vix,
                                 signal_aware_exit=False)
        r_ab   = bt.simulate_day(current, df_5m, df_1d, df_nbees, df_bnf, df_vix,
                                 signal_aware_exit=True)
        if r_base and r_ab:
            rows_base.append(r_base)
            rows_ab.append(r_ab)
    current += timedelta(days=1)

# ── Stats helper ──────────────────────────────────────────────────
def stats(results):
    trades  = [t for r in results for t in r.get("trades", [])
               if t["reason"] != "PARTIAL_TP"]
    total   = sum(r["daily_pnl"] for r in results)
    wins    = sum(1 for r in results if r["daily_pnl"] > 0)
    losses  = sum(1 for r in results if r["daily_pnl"] < 0)
    sl_hits = sum(1 for t in trades if t["reason"] in ("SL",))
    sig_exits = sum(1 for t in trades if t["reason"] == "SIGNAL_EXIT")
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "total_pnl" : total,
        "win_days"  : wins,
        "loss_days" : losses,
        "n_trades"  : len(trades),
        "sl_hits"   : sl_hits,
        "sig_exits" : sig_exits,
        "reasons"   : reasons,
    }

sb = stats(rows_base)
sa = stats(rows_ab)
n  = len(rows_base)

# ── Per-day comparison ────────────────────────────────────────────
print(f"{'DATE':<12} {'BASE':>8} {'A+B':>8} {'DIFF':>7}  CHANGE")
print("-" * 55)
for rb, ra in zip(rows_base, rows_ab):
    diff = ra["daily_pnl"] - rb["daily_pnl"]
    if abs(diff) > 10:
        marker = "BETTER" if diff > 0 else "WORSE"
        print(f"{rb['date']:<12} {rb['daily_pnl']:>8.0f} {ra['daily_pnl']:>8.0f} {diff:>+7.0f}  {marker}")

# ── Summary ───────────────────────────────────────────────────────
print("\n" + "=" * 55)
print(f"{'':20} {'BASELINE':>10} {'A+B EXIT':>10}")
print(f"{'Total P&L':<20} {sb['total_pnl']:>10.0f} {sa['total_pnl']:>10.0f}")
print(f"{'Win days':<20} {sb['win_days']:>10} {sa['win_days']:>10}  (/{n})")
print(f"{'Loss days':<20} {sb['loss_days']:>10} {sa['loss_days']:>10}")
print(f"{'Total trades':<20} {sb['n_trades']:>10} {sa['n_trades']:>10}")
print(f"{'SL hits':<20} {sb['sl_hits']:>10} {sa['sl_hits']:>10}")
print(f"{'Signal exits':<20} {sb['sig_exits']:>10} {sa['sig_exits']:>10}")
print(f"\nA+B exit reasons: {sa['reasons']}")
print(f"Threshold: -{bt.V2_SIGNAL_EXIT_LOSS*100:.0f}% min loss before A/B triggers")
