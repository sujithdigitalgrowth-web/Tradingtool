"""
Compare V2 (current) vs V14 (simplified entry) strategy over last 58 days.

V2  : VWAP + EMA9 + EMA20 + RSI + BankNifty + Supertrend + volume 1.5x
V14 : VWAP + EMA20 + green/red candle + volume 1.1x  (no RSI/ST/BNF/EMA9)
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
    print("Trying Yahoo Finance fallback...")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_v2(START, END)


def run(label, mode):
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

    trading_days = len(results)
    traded_days  = sum(1 for r in results if r["trade_count"] > 0)
    total_trades = sum(r["trade_count"] for r in results)
    total_pnl    = sum(r["daily_pnl"] for r in results)
    win_days     = sum(1 for r in results if r["daily_pnl"] > 0)
    loss_days    = sum(1 for r in results if r["daily_pnl"] < 0)
    best_day     = max((r["daily_pnl"] for r in results), default=0)
    worst_day    = min((r["daily_pnl"] for r in results), default=0)

    all_trades = [t for r in results for t in r.get("trades", [])]
    wins   = [t for t in all_trades if t.get("pnl", 0) > 0]
    losses = [t for t in all_trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
    avg_win  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    print(f"{'='*52}")
    print(f"  {label}")
    print(f"{'='*52}")
    print(f"  Trading days       : {trading_days}  (traded: {traded_days})")
    print(f"  Total trades       : {total_trades}")
    print(f"  Win rate           : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L          : Rs.{total_pnl:+,.2f}")
    print(f"  Win days/Loss days : {win_days} / {loss_days}")
    print(f"  Best day           : Rs.{best_day:+,.2f}")
    print(f"  Worst day          : Rs.{worst_day:+,.2f}")
    print(f"  Avg win/trade      : Rs.{avg_win:+,.2f}")
    print(f"  Avg loss/trade     : Rs.{avg_loss:+,.2f}")
    print()
    return total_pnl, total_trades, win_rate


pnl_v2,  trades_v2,  wr_v2  = run("V2  — Current  (VWAP+EMA9+EMA20+RSI+BNF+ST, vol 1.5x)", "v2")
pnl_v14, trades_v14, wr_v14 = run("V14 — Simplified (VWAP+EMA20+candle, vol 1.1x)", "v14")

print(f"{'='*52}")
print(f"  COMPARISON SUMMARY")
print(f"{'='*52}")
print(f"  P&L    : Rs.{pnl_v2:+,.2f}  →  Rs.{pnl_v14:+,.2f}  ({pnl_v14 - pnl_v2:+,.2f})")
print(f"  Trades : {trades_v2}  →  {trades_v14}  ({trades_v14 - trades_v2:+d})")
print(f"  Win %  : {wr_v2:.1f}%  →  {wr_v14:.1f}%  ({wr_v14 - wr_v2:+.1f}%)")
verdict = "BETTER" if pnl_v14 > pnl_v2 else "WORSE"
print(f"\n  VERDICT: V14 is {verdict} than V2")
print(f"{'='*52}\n")
