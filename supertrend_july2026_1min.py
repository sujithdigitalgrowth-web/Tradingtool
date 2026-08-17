"""
Strategy 6: Supertrend(10,3) follower -- same July 2026 test, on 1-minute
candles instead of 5-minute (which returned +Rs.4,817 net).

Angel One's historical API only allows ~25-30 days per request for
ONE_MINUTE candles (tighter than the 60-day chunk size used for 5-min), so
this fetches in smaller chunks via the same angel_data internals rather than
angel_data.fetch_all() (which is hardcoded to FIVE_MINUTE).
"""
from datetime import date, datetime, timedelta
import pandas as pd
import backtest as bt
import angel_data as ad

START = date(2026, 7, 1)
END   = date(2026, 7, 31)

QTY        = 1 * bt.LOT_SIZE
ST_PERIOD  = 10
ST_MULT    = 3.0
COST_PER_TRADE = 40
CHUNK_DAYS_1MIN = 25

print(f"\nStrategy 6 - Supertrend(10,3), 1-min candles: {START} to {END}")
print("Fetching 1-min data (Angel One)...\n")

_, auth_token, api_key = ad._angel_login()


def fetch_1min(start, end):
    all_rows = []
    chunk_start = datetime.combine(start, datetime.min.time().replace(hour=9, minute=15))
    final_end   = datetime.combine(end,   datetime.min.time().replace(hour=15, minute=30))
    while chunk_start <= final_end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS_1MIN - 1, hours=6, minutes=15), final_end)
        print(f"  Fetching 1-min {chunk_start.date()} -> {chunk_end.date()}")
        rows = ad._fetch_chunk(auth_token, api_key, ad.NIFTYBEES_TOKEN, "NSE",
                               "ONE_MINUTE", chunk_start, chunk_end)
        all_rows.extend(rows)
        chunk_start = chunk_end + timedelta(minutes=1)
    df = ad._to_df(all_rows, ad.NIFTY_MULTIPLIER)
    return df[~df.index.duplicated(keep="first")]


df_1m = fetch_1min(START, END)
if df_1m.empty:
    print("No 1-min data returned -- Angel may not provide 1-min history this far back, "
          "or the historical data subscription doesn't include ONE_MINUTE. Stopping.")
    raise SystemExit(1)

print(f"\nGot {len(df_1m)} 1-min candles, {df_1m.index.date.min()} to {df_1m.index.date.max()}\n")

st_c = bt._supertrend(df_1m, ST_PERIOD, ST_MULT)

trading_days = sorted(set(df_1m.index.date))
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
idx = df_1m.index
dte_cache = {}

for i in range(1, len(idx)):
    ts = idx[i]
    d = ts.date()
    time_str = ts.strftime("%H:%M")
    cl = float(df_1m["Close"].iloc[i])
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
print(f"SUMMARY -- Supertrend(10,3), 1-min, July 2026 ({n_days} trading days)")
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

print(f"\n  vs. 5-min version, July 2026: net +Rs.4,817, 31 trades, 51.6% win, PF 1.27")
