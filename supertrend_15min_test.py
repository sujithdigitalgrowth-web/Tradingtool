"""
Strategy 6: Supertrend(10,3) follower -- 15-minute candle version, tested
against the same 5-min version already backtested (90-day net: +Rs.23,238;
1-year net: +Rs.71,873, but front-loaded/uneven across sub-periods).

Everything else is identical to the 5-min version: reverse-on-flip, always
in the market during trading hours, forced EOD square-off at 15:15, 0.5-delta
spot P&L approximation, Rs.40/trade flat cost. Only the candle the
Supertrend indicator (and entry/exit decisions) is computed on changes,
from 5-min to a native 15-min resample of the same underlying data
(standard start-labeled bins, e.g. a bar timestamped 09:15 covers
09:15-09:30 -- same labeling convention Angel's raw 5-min data already
uses, so the time-window checks need no adjustment).

Reports both:
  A. Last 90 days  (directly comparable to the clean 5-min 90-day backtest)
  B. Full 1 year, split into 3 sub-periods (directly comparable to the
     5-min version's stability check)
"""
from datetime import date, timedelta
import pandas as pd
import backtest as bt

START = date.today() - timedelta(days=365)
END   = date.today() - timedelta(days=1)

QTY        = 1 * bt.LOT_SIZE
ST_PERIOD  = 10
ST_MULT    = 3.0
COST_PER_TRADE = 40

print(f"\nStrategy 6 - Supertrend(10,3), 15-min candles: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
print(f"Got {len(df_5m)} 5-min candles, {df_5m.index.date.min()} to {df_5m.index.date.max()}")

df_15 = df_5m.resample("15min").agg(
    {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
).dropna()
print(f"Resampled to {len(df_15)} 15-min candles\n")

st_15 = bt._supertrend(df_15, ST_PERIOD, ST_MULT)


def dte_for(d):
    return max(1, (3 - d.weekday()) % 7 or 7)


def close_trade(position, ts_str, spot, reason):
    sc = spot - position["entry_spot"]
    pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
    gross = pnl_pu * QTY
    net = gross - COST_PER_TRADE
    return {**position, "exit_time": ts_str, "exit_spot": spot,
            "gross_pnl": gross, "pnl": net, "reason": reason}


def run_supertrend(df, st_series):
    position = None
    trades_by_date = {}
    idx = df.index
    dte_cache = {}

    for i in range(1, len(idx)):
        ts = idx[i]
        d = ts.date()
        time_str = ts.strftime("%H:%M")
        cl = float(df["Close"].iloc[i])
        trades_by_date.setdefault(d.isoformat(), [])

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                t = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades_by_date[position["entry_date"]].append(t)
                position = None
            continue

        if position:
            st_prev, st_now = int(st_series.iloc[i - 1]), int(st_series.iloc[i])
            exit_now = ((position["type"] == "CE" and st_now == -1 and st_prev == 1) or
                       (position["type"] == "PE" and st_now == 1 and st_prev == -1))
            if exit_now:
                t = close_trade(position, time_str, cl, "ST_FLIP")
                trades_by_date[position["entry_date"]].append(t)
                position = None

        if not position and "09:15" <= time_str < bt.SQUAREOFF_TIME:
            dte = dte_cache.setdefault(d, dte_for(d))
            st_prev, st_now = int(st_series.iloc[i - 1]), int(st_series.iloc[i])
            if st_now == 1 and st_prev == -1:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "CE", "entry_spot": cl, "entry_time": time_str,
                           "entry_option_price": entry_price, "entry_date": d.isoformat()}
            elif st_now == -1 and st_prev == 1:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "PE", "entry_spot": cl, "entry_time": time_str,
                           "entry_option_price": entry_price, "entry_date": d.isoformat()}

    return trades_by_date


trades_by_date = run_supertrend(df_15, st_15)

trading_days = sorted(set(df_5m.index.date))
trading_days = [d for d in trading_days if d.weekday() < 5]

results = []
for d in trading_days:
    trades = trades_by_date.get(d.isoformat(), [])
    daily_pnl = sum(t["pnl"] for t in trades)
    results.append({"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl})


def stats(res):
    trades    = [t for r in res for t in r["trades"]]
    n_trades  = len(trades)
    gross_pnl = sum(t["gross_pnl"] for t in trades)
    net_pnl   = sum(t["pnl"] for t in trades)
    wins      = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses    = [t["pnl"] for t in trades if t["pnl"] < 0]
    win_rate  = len(wins) / n_trades * 100 if n_trades else 0
    avg_win   = sum(wins) / len(wins) if wins else 0
    avg_loss  = sum(losses) / len(losses) if losses else 0
    pf        = abs(sum(wins) / sum(losses)) if losses else float("inf")
    worst_day = min((r["daily_pnl"] for r in res), default=0)
    best_day  = max((r["daily_pnl"] for r in res), default=0)
    win_days  = sum(1 for r in res if r["daily_pnl"] > 0)
    loss_days = sum(1 for r in res if r["daily_pnl"] < 0)
    return {"n_days": len(res), "n_trades": n_trades, "win_rate": win_rate,
            "avg_win": avg_win, "avg_loss": avg_loss, "pf": pf,
            "gross_pnl": gross_pnl, "net_pnl": net_pnl,
            "best_day": best_day, "worst_day": worst_day,
            "win_days": win_days, "loss_days": loss_days}


# ── A. Last 90 days ────────────────────────────────────────────────
cutoff_90 = date.today() - timedelta(days=90)
res_90 = [r for r in results if date.fromisoformat(r["date"]) >= cutoff_90]
s90 = stats(res_90)

print("=" * 70)
print(f"A. LAST 90 DAYS -- 15-min Supertrend  ({s90['n_days']} trading days)")
print("=" * 70)
print(f"Trades              : {s90['n_trades']}  ({s90['n_trades']/s90['n_days']:.2f}/day)")
print(f"Win rate            : {s90['win_rate']:.1f}%")
print(f"Avg win / Avg loss  : Rs.{s90['avg_win']:+,.0f} / Rs.{s90['avg_loss']:+,.0f}")
print(f"Profit factor       : {s90['pf']:.2f}")
print(f"Gross P&L           : Rs.{s90['gross_pnl']:+,.0f}")
print(f"Net P&L (@Rs.{COST_PER_TRADE}/trade): Rs.{s90['net_pnl']:+,.0f}")
print(f"Win days / Loss days: {s90['win_days']} / {s90['loss_days']}")
print(f"Best day / Worst day: Rs.{s90['best_day']:+,.0f} / Rs.{s90['worst_day']:+,.0f}")
print(f"\n  vs. 5-min version (same 90-day-ish window): net +Rs.23,238, 90 trades, 50.0% win, PF 1.37")

# ── B. Full year, 3-way split ─────────────────────────────────────
n = len(trading_days)
third = n // 3
splits = [
    ("Period 1 (earliest)", trading_days[0:third]),
    ("Period 2 (middle)",   trading_days[third:2*third]),
    ("Period 3 (latest)",   trading_days[2*third:]),
]
by_date = {r["date"]: r for r in results}

print("\n" + "=" * 70)
print(f"B. FULL YEAR ({n} trading days) -- 3-way stability split")
print("=" * 70)
s_full = stats(results)
print(f"Full-year net P&L: Rs.{s_full['net_pnl']:+,.0f}  ({s_full['n_trades']} trades, {s_full['win_rate']:.1f}% win)\n")
print(f"{'Sub-period':<24} {'Days':>5} {'Trades':>7} {'Win%':>6} {'Gross P&L':>12} {'Net P&L':>12}")
for pname, days in splits:
    sub = [by_date[d.isoformat()] for d in days if d.isoformat() in by_date]
    s = stats(sub)
    date_range = f"{days[0]} to {days[-1]}" if days else "n/a"
    print(f"{pname:<24} {len(days):>5} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['gross_pnl']:>+12,.0f} {s['net_pnl']:>+12,.0f}   [{date_range}]")

print(f"\n  vs. 5-min version 3-way split: P1 -Rs.6,733 | P2 +Rs.41,356 | P3 +Rs.37,250")
