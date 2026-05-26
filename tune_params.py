"""
Grid-search key parameters to find the sweet spot:
  - target: 35-50 trades over Jan-May (50%+ of 74 trading days)
  - constraint: positive total P&L, win rate >= 45%
"""
from datetime import date, timedelta
import backtest as bt

print("Loading data (one-time)...")
start = date(2026, 1, 1)
end   = date(2026, 5, 20)
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(start, end)
days = [start + timedelta(days=i) for i in range((end-start).days+1)
        if (start+timedelta(days=i)).weekday() < 5]
print(f"Trading days: {len(days)}\n")

def run_sim(vol_mult, rsi_ce, rsi_pe, vix_max, max_trades, vix_min=13):
    bt.V2_VOL_SURGE_MULT = vol_mult
    bt.V2_RSI_MIN_CE     = rsi_ce
    bt.V2_RSI_MAX_PE     = rsi_pe
    bt.V2_VIX_MAX        = vix_max
    bt.V2_VIX_MIN        = vix_min
    bt.V2_MAX_TRADES     = max_trades
    total, trades, wins = 0, 0, 0
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
        if r:
            total  += r["daily_pnl"]
            trades += r["trade_count"]
            wins   += r["win_count"]
    wr = round(wins/trades*100) if trades else 0
    return total, trades, wins, wr

print(f"{'Vol':>5} {'RSI':>5} {'VIX':>4} {'MaxT':>4}  {'Trades':>7} {'WinRate':>8} {'P&L':>10}")
print("-" * 55)

configs = [
    # (vol_mult, rsi_ce, rsi_pe, vix_max, max_trades)
    (1.5, 60, 40, 30, 2),
    (1.4, 58, 42, 30, 2),
    (1.4, 55, 45, 30, 2),
    (1.3, 58, 42, 30, 2),
    (1.3, 55, 45, 30, 2),
    (1.3, 55, 45, 35, 2),
    (1.4, 55, 45, 35, 2),
    (1.4, 58, 42, 35, 2),
    (1.5, 55, 45, 35, 2),
    (1.5, 58, 42, 35, 2),
    (1.3, 60, 40, 35, 2),
    (1.4, 60, 40, 35, 2),
]

for (vol, rsi_ce, rsi_pe, vix_max, maxt) in configs:
    total, trades, wins, wr = run_sim(vol, rsi_ce, rsi_pe, vix_max, maxt)
    mark = " <-- TARGET" if 35 <= trades <= 60 and total > 0 and wr >= 45 else ""
    print(f"{vol:>5.1f} {rsi_ce:>5} {vix_max:>4} {maxt:>4}  {trades:>7} {wr:>7}%  Rs.{total:>8,.0f}{mark}")
