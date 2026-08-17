"""
Backtest: "Intraday Momentum" strategy (Opening Range Breakout + VWAP).

A new, standalone strategy -- independent of the V2 strategy in backtest.py.

Rules (as specified):
  1. Setup   : mark High/Low of the first 15 minutes (09:15-09:30).
  2. Buy     : close breaks above the 15-min High AND close > VWAP  -> long CE.
  3. Sell    : close breaks below the 15-min Low  AND close < VWAP  -> long PE.
  4. Exit    : end of day (15:15 square-off) OR price crosses back
               over VWAP against the position.

No RSI/EMA/Supertrend/ADX/VIX filters -- this is a test of the ORB+VWAP
rules exactly as given. Re-entry is allowed if the market goes flat (VWAP
exit) and a fresh breakout fires again later the same day, since the rules
don't cap trades/day.

P&L uses the same spot-delta approximation (0.5 delta, ATM option) and
estimate_option_price() borrowed from backtest.py purely as shared utility
functions, for a P&L methodology consistent with prior backtests.
"""
from datetime import date, timedelta
import pandas as pd
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY = 1 * bt.LOT_SIZE

print(f"\nORB + VWAP backtest: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)


def simulate_orb_day(target_date, df_5m_all):
    day = df_5m_all[df_5m_all.index.date == target_date].between_time("09:15", "15:30")
    if len(day) < 5:
        return None

    or_high = float(day.iloc[0:3]["High"].max())
    or_low  = float(day.iloc[0:3]["Low"].min())
    vwap    = bt._vwap(day)

    dte = max(1, (3 - target_date.weekday()) % 7 or 7)

    position = None   # {"type","entry_spot","entry_option_price","entry_time"}
    trades   = []
    daily_pnl = 0.0

    candles = list(day.iloc[3:].iterrows())
    for ts, row in candles:
        time_str = ts.strftime("%H:%M")
        cl = float(row["Close"])
        vw = float(vwap.loc[ts])

        # ── EOD square-off ──────────────────────────────────────
        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                sc     = cl - position["entry_spot"]
                pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
                pnl    = pnl_pu * QTY
                daily_pnl += pnl
                trades.append({**position, "exit_time": time_str, "exit_spot": cl,
                               "pnl": pnl, "reason": "EOD_SQUAREOFF"})
                position = None
            break

        # ── Manage open position: VWAP cross-back exit ──────────
        if position:
            exit_now = ((position["type"] == "CE" and cl < vw) or
                        (position["type"] == "PE" and cl > vw))
            if exit_now:
                sc     = cl - position["entry_spot"]
                pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
                pnl    = pnl_pu * QTY
                daily_pnl += pnl
                trades.append({**position, "exit_time": time_str, "exit_spot": cl,
                               "pnl": pnl, "reason": "VWAP_CROSS"})
                position = None

        # ── Entry: ORB breakout + VWAP confirmation ─────────────
        if not position:
            if cl > or_high and cl > vw:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "CE", "entry_spot": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}
            elif cl < or_low and cl < vw:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "PE", "entry_spot": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}

    return {"date": target_date.isoformat(), "or_high": or_high, "or_low": or_low,
            "trades": trades, "daily_pnl": daily_pnl}


results = []
current = START
while current <= END:
    if current.weekday() < 5:
        r = simulate_orb_day(current, df_5m)
        if r:
            results.append(r)
    current += timedelta(days=1)

# ── Report ──────────────────────────────────────────────────────
all_trades = [t for r in results for t in r["trades"]]
n_days     = len(results)
n_trades   = len(all_trades)
total_pnl  = sum(r["daily_pnl"] for r in results)
win_days   = sum(1 for r in results if r["daily_pnl"] > 0)
loss_days  = sum(1 for r in results if r["daily_pnl"] < 0)
flat_days  = sum(1 for r in results if r["daily_pnl"] == 0 and not r["trades"])
win_trades  = sum(1 for t in all_trades if t["pnl"] > 0)
loss_trades = sum(1 for t in all_trades if t["pnl"] < 0)
best_day   = max((r["daily_pnl"] for r in results), default=0)
worst_day  = min((r["daily_pnl"] for r in results), default=0)
best_trade  = max((t["pnl"] for t in all_trades), default=0)
worst_trade = min((t["pnl"] for t in all_trades), default=0)
avg_trades_per_day = n_trades / n_days if n_days else 0

print(f"{'DATE':<12} {'DOW':<4} {'#TR':<4} {'DAILY PNL':>12}")
print("-" * 40)
for r in results:
    dow = date.fromisoformat(r["date"]).strftime("%a")
    print(f"{r['date']:<12} {dow:<4} {len(r['trades']):<4} {r['daily_pnl']:>+12,.0f}")

print("\n" + "=" * 55)
print("SUMMARY — ORB (15-min) + VWAP  |  1 lot = %d qty" % QTY)
print("=" * 55)
print(f"Period               : {START} to {END} ({n_days} trading days)")
print(f"Total trades         : {n_trades}  ({avg_trades_per_day:.2f}/day)")
print(f"Total P&L            : Rs.{total_pnl:+,.0f}")
print(f"Avg P&L/day          : Rs.{total_pnl/n_days if n_days else 0:+,.0f}")
print(f"Avg P&L/trade        : Rs.{total_pnl/n_trades if n_trades else 0:+,.0f}")
print(f"Win days / Loss days / Flat : {win_days} / {loss_days} / {flat_days}")
print(f"Win trades / Loss trades    : {win_trades} / {loss_trades}  "
      f"(win rate {win_trades/n_trades*100 if n_trades else 0:.1f}%)")
print(f"Best day / Worst day        : Rs.{best_day:+,.0f} / Rs.{worst_day:+,.0f}")
print(f"Best trade / Worst trade    : Rs.{best_trade:+,.0f} / Rs.{worst_trade:+,.0f}")

exit_reasons = pd.Series([t["reason"] for t in all_trades]).value_counts()
print("\nExit reason breakdown:")
for reason, cnt in exit_reasons.items():
    print(f"  {reason:<15} {cnt}")
