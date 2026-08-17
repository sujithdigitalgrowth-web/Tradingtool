"""
Strategy 6: Supertrend(10,3) follower -- clean single-month backtest,
July 2026. Same 5-min candle version that showed +Rs.23,238 net over the
last 90 days (5-min beat 15-min clearly on that same recent window).
"""
from datetime import date
import pandas as pd
import backtest as bt

START = date(2026, 7, 1)
END   = date(2026, 7, 31)

QTY        = 1 * bt.LOT_SIZE
ST_PERIOD  = 10
ST_MULT    = 3.0
COST_PER_TRADE = 40

print(f"\nStrategy 6 - Supertrend(10,3), 5-min: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
print(f"Got {len(df_5m)} 5-min candles, {df_5m.index.date.min()} to {df_5m.index.date.max()}\n")

st_c = bt._supertrend(df_5m, ST_PERIOD, ST_MULT)

trading_days = sorted(set(df_5m.index.date))
trading_days = [d for d in trading_days if d.weekday() < 5 and START <= d <= END]


def dte_for(d):
    return max(1, (3 - d.weekday()) % 7 or 7)


def close_trade(position, ts_str, spot, reason):
    sc = spot - position["entry_spot"]
    pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
    gross = pnl_pu * QTY
    net = gross - COST_PER_TRADE
    return {**position, "exit_time": ts_str, "exit_spot": spot,
            "gross_pnl": gross, "pnl": net, "reason": reason}


position = None
trades_by_date = {}
idx = df_5m.index
dte_cache = {}

for i in range(1, len(idx)):
    ts = idx[i]
    d = ts.date()
    time_str = ts.strftime("%H:%M")
    cl = float(df_5m["Close"].iloc[i])
    trades_by_date.setdefault(d.isoformat(), [])

    if time_str >= bt.SQUAREOFF_TIME:
        if position:
            t = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
            trades_by_date[position["entry_date"]].append(t)
            position = None
        continue

    if position:
        st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
        exit_now = ((position["type"] == "CE" and st_now == -1 and st_prev == 1) or
                   (position["type"] == "PE" and st_now == 1 and st_prev == -1))
        if exit_now:
            t = close_trade(position, time_str, cl, "ST_FLIP")
            trades_by_date[position["entry_date"]].append(t)
            position = None

    if not position and "09:15" <= time_str < bt.SQUAREOFF_TIME:
        d_in_range = START <= d <= END
        if not d_in_range:
            continue
        dte = dte_cache.setdefault(d, dte_for(d))
        st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
        if st_now == 1 and st_prev == -1:
            entry_price = bt.estimate_option_price(cl, dte)
            position = {"type": "CE", "entry_spot": cl, "entry_time": time_str,
                       "entry_option_price": entry_price, "entry_date": d.isoformat()}
        elif st_now == -1 and st_prev == 1:
            entry_price = bt.estimate_option_price(cl, dte)
            position = {"type": "PE", "entry_spot": cl, "entry_time": time_str,
                       "entry_option_price": entry_price, "entry_date": d.isoformat()}

results = []
for d in trading_days:
    trades = trades_by_date.get(d.isoformat(), [])
    daily_pnl = sum(t["pnl"] for t in trades)
    results.append({"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl})

all_trades = [t for r in results for t in r["trades"]]
n_days     = len(results)
n_trades   = len(all_trades)
gross_pnl  = sum(t["gross_pnl"] for t in all_trades)
net_pnl    = sum(t["pnl"] for t in all_trades)
wins       = [t["pnl"] for t in all_trades if t["pnl"] > 0]
losses     = [t["pnl"] for t in all_trades if t["pnl"] < 0]
win_rate   = len(wins) / n_trades * 100 if n_trades else 0
avg_win    = sum(wins) / len(wins) if wins else 0
avg_loss   = sum(losses) / len(losses) if losses else 0
profit_factor = abs(sum(wins) / sum(losses)) if losses else float("inf")
win_days   = sum(1 for r in results if r["daily_pnl"] > 0)
loss_days  = sum(1 for r in results if r["daily_pnl"] < 0)
flat_days  = sum(1 for r in results if r["daily_pnl"] == 0)
best_day   = max((r["daily_pnl"] for r in results), default=0)
worst_day  = min((r["daily_pnl"] for r in results), default=0)
best_trade  = max((t["pnl"] for t in all_trades), default=0)
worst_trade = min((t["pnl"] for t in all_trades), default=0)

print(f"{'DATE':<12} {'DOW':<4} {'#TR':<4} {'DAILY PNL':>12} {'CUM PNL':>12}")
print("-" * 50)
cum = 0.0
for r in results:
    dow = date.fromisoformat(r["date"]).strftime("%a")
    cum += r["daily_pnl"]
    print(f"{r['date']:<12} {dow:<4} {len(r['trades']):<4} {r['daily_pnl']:>+12,.0f} {cum:>+12,.0f}")

print("\n" + "=" * 60)
print(f"SUMMARY -- Supertrend(10,3), 5-min, July 2026 ({n_days} trading days)")
print("=" * 60)
print(f"Total trades           : {n_trades}  ({n_trades/n_days:.2f}/day)" if n_days else "n/a")
print(f"Win rate                : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
print(f"Avg win / Avg loss      : Rs.{avg_win:+,.0f} / Rs.{avg_loss:+,.0f}")
print(f"Profit factor           : {profit_factor:.2f}")
print(f"Gross P&L               : Rs.{gross_pnl:+,.0f}")
print(f"Net P&L (@Rs.{COST_PER_TRADE}/trade) : Rs.{net_pnl:+,.0f}")
print(f"Avg P&L/day             : Rs.{net_pnl/n_days:+,.0f}" if n_days else "n/a")
print(f"Win days / Loss days / Flat : {win_days} / {loss_days} / {flat_days}")
print(f"Best day / Worst day    : Rs.{best_day:+,.0f} / Rs.{worst_day:+,.0f}")
print(f"Best trade / Worst trade: Rs.{best_trade:+,.0f} / Rs.{worst_trade:+,.0f}")

exit_reasons = pd.Series([t["reason"] for t in all_trades]).value_counts()
print("\nExit reason breakdown:")
for reason, cnt in exit_reasons.items():
    print(f"  {reason:<15} {cnt}")
