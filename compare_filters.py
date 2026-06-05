"""
Compare old strategy (no bias/move filters) vs new strategy (with filters).
Runs both over the last 58 days and prints a side-by-side summary.
"""
from datetime import date, timedelta
import backtest as bt

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=57)

print(f"\nBacktest range: {START} to {END}")
print("Fetching data (this takes ~30 seconds)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(START, END)

def run(label, use_bias, use_move):
    bt.V2_USE_BIAS_FILTER = use_bias
    bt.V2_USE_MOVE_FILTER = use_move
    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = bt.simulate_day(current, df_5m, df_1d,
                                df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
            if r:
                results.append(r)
        current += timedelta(days=1)

    trading_days  = len(results)
    traded_days   = sum(1 for r in results if r["trade_count"] > 0)
    total_trades  = sum(r["trade_count"] for r in results)
    total_pnl     = sum(r["daily_pnl"] for r in results)
    win_days      = sum(1 for r in results if r["daily_pnl"] > 0)
    loss_days     = sum(1 for r in results if r["daily_pnl"] < 0)
    best_day      = max((r["daily_pnl"] for r in results), default=0)
    worst_day     = min((r["daily_pnl"] for r in results), default=0)

    all_trades = [t for r in results for t in r.get("trades", [])]
    wins  = [t for t in all_trades if t.get("pnl", 0) > 0]
    losses = [t for t in all_trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
    avg_win  = sum(t["pnl"] for t in wins)  / len(wins)  if wins  else 0
    avg_loss = sum(t["pnl"] for t in losses)/ len(losses) if losses else 0

    print(f"{'='*50}")
    print(f"  {label}")
    print(f"{'='*50}")
    print(f"  Trading days     : {trading_days}  (traded: {traded_days})")
    print(f"  Total trades     : {total_trades}")
    print(f"  Win rate         : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L        : Rs.{total_pnl:+,.2f}")
    print(f"  Win days / Loss days : {win_days} / {loss_days}")
    print(f"  Best day         : Rs.{best_day:+,.2f}")
    print(f"  Worst day        : Rs.{worst_day:+,.2f}")
    print(f"  Avg win per trade: Rs.{avg_win:+,.2f}")
    print(f"  Avg loss/trade   : Rs.{avg_loss:+,.2f}")
    print()
    return total_pnl, total_trades, win_rate

pnl_old, trades_old, wr_old = run("OLD STRATEGY  (no filters)", False, False)
pnl_new, trades_new, wr_new = run("NEW STRATEGY  (bias + move filters)", True, True)

print(f"{'='*50}")
print(f"  COMPARISON SUMMARY")
print(f"{'='*50}")
print(f"  P&L change     : Rs.{pnl_old:+,.2f}  →  Rs.{pnl_new:+,.2f}  ({pnl_new - pnl_old:+,.2f})")
print(f"  Trades change  : {trades_old}  →  {trades_new}  ({trades_new - trades_old:+d})")
print(f"  Win rate change: {wr_old:.1f}%  →  {wr_new:.1f}%  ({wr_new - wr_old:+.1f}%)")
verdict = "BETTER" if pnl_new > pnl_old else "WORSE"
print(f"\n  VERDICT: New filters are {verdict}")
print(f"{'='*50}\n")
