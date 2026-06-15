"""
Compare V2 strategy performance across different lot sizes: 1, 2, 3, 5 lots.
Also compares V2 vs V14 at the default lot size.
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=57)

print(f"\nBacktest range: {START} to {END}")
print("Fetching data via Angel One (this takes ~60 seconds)...\n")

try:
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print("Angel One data fetched successfully.\n")
except Exception as e:
    print(f"Angel One fetch failed: {e}")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(START, END)


def run(label, lots, mode="v2"):
    bt.QTY = lots * bt.LOT_SIZE
    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = bt.simulate_day(current, df_5m, df_1d,
                                df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix,
                                entry_mode=mode)
            if r:
                results.append(r)
        current += timedelta(days=1)

    total_trades = sum(r["trade_count"] for r in results)
    total_pnl    = sum(r["daily_pnl"] for r in results)
    worst_day    = min((r["daily_pnl"] for r in results), default=0)
    best_day     = max((r["daily_pnl"] for r in results), default=0)

    all_trades = [t for r in results for t in r.get("trades", [])]
    wins   = [t for t in all_trades if t.get("pnl", 0) > 0]
    losses = [t for t in all_trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0

    # Estimate capital needed (avg option price × lot size × lots)
    capitals = [t.get("capital", 0) for t in all_trades if t.get("capital", 0) > 0]
    avg_capital = sum(capitals) / len(capitals) if capitals else 0

    print(f"  {label:<42} | Trades:{total_trades:3d} | WR:{win_rate:5.1f}% | "
          f"P&L: Rs.{total_pnl:>+10,.0f} | Best: Rs.{best_day:>+8,.0f} | "
          f"Worst: Rs.{worst_day:>+8,.0f} | Avg Capital: Rs.{avg_capital:>7,.0f}")
    return total_pnl, win_rate


print(f"\n{'='*130}")
print(f"  {'STRATEGY':<42} | {'Trades':>6} | {'WR':>6} | {'Total P&L':>14} | {'Best Day':>12} | {'Worst Day':>12} | {'Avg Capital':>14}")
print(f"{'='*130}")

run("V2 — 1 lot  (65 qty)",  1)
run("V2 — 2 lots (130 qty)", 2)
run("V2 — 3 lots (195 qty)", 3)
run("V2 — 5 lots (325 qty)", 5)

print(f"{'='*130}")
print(f"\n  NOTE: Win rate stays the same across lots — only P&L and risk scale.")
print(f"  Capital required scales with lot size. Risk (worst day) also scales.\n")
