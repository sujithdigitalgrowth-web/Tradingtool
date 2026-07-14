"""What would today's (2026-07-14) trade have looked like under the new
(1-lot, no-cap trailing) defaults vs the old (1-lot, hard 10% TP cap) behavior
that's actually live? Single-day check, one Angel One login."""
from datetime import date, timedelta
import backtest as bt

TODAY = date(2026, 7, 14)
START = TODAY - timedelta(days=60)   # warm-up history for EMA/RSI/Supertrend
END   = TODAY

print(f"Fetching Angel One data {START} to {END}...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
print("Data fetched.\n")


def show(label, require_vol_surge=True):
    r = bt.simulate_day(TODAY, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix,
                        require_vol_surge=require_vol_surge)
    print(f"{'='*70}\n{label}\n{'='*70}")
    if not r or not r.get("trades"):
        print("  No trade simulated for today under this config.")
        return
    for t in r["trades"]:
        print(f"  {t['time']} entry {t['entry']:.2f} -> exit {t['exit']:.2f}  "
              f"reason={t['reason']}  pnl={t['pnl']:.0f}")
    print(f"  Day total P&L: Rs.{r['daily_pnl']:.0f}")
    print()


print("Actual live trade today: entry 10:20 @127.40, exit 10:21 @104.25, SL, pnl=-1504.75\n")

bt.QTY = bt.LOT_SIZE
bt.V2_1LOT_HARD_TP = True
show("OLD — 1-lot, hard TP cap (matches what's actually live)")

bt.QTY = bt.LOT_SIZE
bt.V2_1LOT_HARD_TP = False
show("NEW — 1-lot, no-cap trailing-to-breakeven (the validated fix)")

show("NEW + NO VOLUME FILTER — would it have entered earlier, before the exhaustion candle?",
     require_vol_surge=False)
