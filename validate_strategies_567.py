"""
Proper validation of Strategies 5b, 6, 7 -- the three carried forward from
the 8-strategy batch test. That test ran on the same 82-day window as
~20 other variants tried this session, so a good-looking result there is as
likely to be noise/overfitting as a real edge. This script checks that
before trusting any of them:

  1. Longer history  : ~1 year instead of ~4 months, more trading days.
  2. Split stability  : performance broken into 3 roughly-equal sub-periods.
                        A real edge should show up as reasonably consistent
                        sign/magnitude across chunks, not concentrated in
                        one lucky stretch.
  3. Estimated costs  : a flat Rs.40 per round-trip trade (brokerage + STT +
                        exchange/GST/SEBI charges -- a standard discount-
                        broker ballpark for Indian options) deducted from
                        every trade, reported alongside the gross (cost-free)
                        number every prior script in this session has used.
                        Bid-ask slippage on the option premium itself is
                        still NOT modeled (would need live quote data).

Strategies (unchanged logic from strategies_3_to_10.py):
  5b. EMA9/21 cross + VWAP gate, ATR(2x) trailing exit
  6.  Supertrend(10,3) directional follower, reverse on flip
  7.  EMA pullback continuation (50EMA-15m trend + 20EMA-5m pullback), ATR(2x) exit
"""
from datetime import date, timedelta
import pandas as pd
import numpy as np
import backtest as bt

START = date.today() - timedelta(days=365)
END   = date.today() - timedelta(days=1)

QTY        = 1 * bt.LOT_SIZE
ATR_PERIOD = 14
COST_PER_TRADE = 40   # Rs., flat round-trip estimate (brokerage+STT+exch+GST+SEBI)

print(f"\nValidating Strategies 5b/6/7: {START} to {END}")
print("Fetching data (Angel One) -- longer window, this will take a bit...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
print(f"Got {len(df_5m)} 5-min candles, spanning "
      f"{df_5m.index.date.min()} to {df_5m.index.date.max()}\n")

ema9_c   = df_5m["Close"].ewm(span=9,  adjust=False).mean()
ema20_c  = df_5m["Close"].ewm(span=20, adjust=False).mean()
ema21_c  = df_5m["Close"].ewm(span=21, adjust=False).mean()
atr14_c  = bt._atr(df_5m, ATR_PERIOD)
st_c     = bt._supertrend(df_5m, 10, 3.0)

df_15 = df_5m.resample("15min", label="right", closed="left").agg(
    {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
).dropna()
ema50_15 = df_15["Close"].ewm(span=50, adjust=False).mean()
ema50_15_5m = ema50_15.reindex(df_5m.index, method="ffill")

trading_days = sorted(set(df_5m.index.date))
trading_days = [d for d in trading_days if d.weekday() < 5]


def day_slice(d):
    day = df_5m[df_5m.index.date == d].between_time("09:15", "15:30")
    return day if len(day) >= 3 else None


def dte_for(d):
    return max(1, (3 - d.weekday()) % 7 or 7)


def close_trade(position, ts_str, spot, reason):
    sc = spot - position["entry_spot"]
    pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
    pnl = pnl_pu * QTY - COST_PER_TRADE
    return {**position, "exit_time": ts_str, "exit_spot": spot, "pnl": pnl, "reason": reason}, pnl


def make_position(ptype, ts_str, spot, dte, **extra):
    entry_price = bt.estimate_option_price(spot, dte)
    return {"type": ptype, "entry_spot": spot, "peak": spot, "entry_time": ts_str,
            "entry_option_price": entry_price, **extra}


# ── Strategy 7: per-day loop ─────────────────────────────────────────
def sim_s7(d):
    day = day_slice(d)
    if day is None:
        return None
    dte = dte_for(d)
    position, trades, daily_pnl = None, [], 0.0
    touched_up = touched_dn = False
    wait_count = 0
    TOL = 0.0008
    MAX_WAIT = 6

    for ts, row in day.iterrows():
        time_str = ts.strftime("%H:%M")
        cl, op, lo, hi = float(row["Close"]), float(row["Open"]), float(row["Low"]), float(row["High"])

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades.append(t); daily_pnl += pnl; position = None
            break

        e50 = ema50_15_5m.loc[ts]
        if pd.isna(e50):
            continue
        e50 = float(e50)
        e20 = float(ema20_c.loc[ts])
        e20_prev = float(ema20_c.iloc[ema20_c.index.get_loc(ts) - 3])
        at = float(atr14_c.loc[ts])

        if position:
            if position["type"] == "CE":
                position["peak"] = max(position["peak"], cl)
                exit_now = cl < position["peak"] - 2 * at
            else:
                position["peak"] = min(position["peak"], cl)
                exit_now = cl > position["peak"] + 2 * at
            if exit_now:
                t, pnl = close_trade(position, time_str, cl, "ATR_EXIT")
                trades.append(t); daily_pnl += pnl; position = None
            continue

        uptrend   = cl > e50 * 1.001 and e20 > e20_prev
        downtrend = cl < e50 * 0.999 and e20 < e20_prev

        if uptrend:
            if not touched_up:
                if lo <= e20 * (1 + TOL):
                    touched_up = True; wait_count = 0
            else:
                wait_count += 1
                if cl > op:
                    position = make_position("CE", time_str, cl, dte)
                    touched_up = False; wait_count = 0
                elif wait_count > MAX_WAIT:
                    touched_up = False; wait_count = 0
        elif downtrend:
            if not touched_dn:
                if hi >= e20 * (1 - TOL):
                    touched_dn = True; wait_count = 0
            else:
                wait_count += 1
                if cl < op:
                    position = make_position("PE", time_str, cl, dte)
                    touched_dn = False; wait_count = 0
                elif wait_count > MAX_WAIT:
                    touched_dn = False; wait_count = 0
        else:
            touched_up = touched_dn = False; wait_count = 0

    return {"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


# ── Strategies 5b/6: continuous global loop ──────────────────────────
vwap_parts = []
for d in trading_days:
    day = day_slice(d)
    if day is not None:
        vwap_parts.append(bt._vwap(day))
vwap_global = pd.concat(vwap_parts) if vwap_parts else pd.Series(dtype=float)


def run_continuous(mode):
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
                t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades_by_date[position["entry_date"]].append((t, pnl))
                position = None
            continue

        if position:
            exit_now = False
            if mode == "ema_cross":
                at = float(atr14_c.iloc[i])
                if position["type"] == "CE":
                    position["peak"] = max(position["peak"], cl); exit_now = cl < position["peak"] - 2 * at
                else:
                    position["peak"] = min(position["peak"], cl); exit_now = cl > position["peak"] + 2 * at
            else:
                st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
                exit_now = ((position["type"] == "CE" and st_now == -1 and st_prev == 1) or
                           (position["type"] == "PE" and st_now == 1 and st_prev == -1))

            if exit_now:
                reason = "ATR_EXIT" if mode == "ema_cross" else "ST_FLIP"
                t, pnl = close_trade(position, time_str, cl, reason)
                trades_by_date[position["entry_date"]].append((t, pnl))
                position = None

        if not position and "09:15" <= time_str < bt.SQUAREOFF_TIME:
            dte = dte_cache.setdefault(d, dte_for(d))
            if mode == "ema_cross":
                e9_prev, e21_prev = float(ema9_c.iloc[i - 1]), float(ema21_c.iloc[i - 1])
                e9, e21 = float(ema9_c.iloc[i]), float(ema21_c.iloc[i])
                vw = vwap_global.get(ts, np.nan)
                cross_up   = e9 > e21 and e9_prev <= e21_prev
                cross_down = e9 < e21 and e9_prev >= e21_prev
                if not np.isnan(vw):
                    if cross_up and cl > vw:
                        position = make_position("CE", time_str, cl, dte, entry_date=d.isoformat())
                    elif cross_down and cl < vw:
                        position = make_position("PE", time_str, cl, dte, entry_date=d.isoformat())
            else:
                st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
                if st_now == 1 and st_prev == -1:
                    position = make_position("CE", time_str, cl, dte, entry_date=d.isoformat())
                elif st_now == -1 and st_prev == 1:
                    position = make_position("PE", time_str, cl, dte, entry_date=d.isoformat())

    results = []
    for d in trading_days:
        pairs = trades_by_date.get(d.isoformat(), [])
        trades = [p[0] for p in pairs]
        daily_pnl = sum(p[1] for p in pairs)
        results.append({"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl})
    return results


def run_perday(sim_fn):
    return [r for d in trading_days if (r := sim_fn(d)) is not None]


def stats(results):
    trades    = [t for r in results for t in r["trades"]]
    gross_pnl = sum(t["pnl"] + COST_PER_TRADE for t in trades)   # add back cost to get gross
    net_pnl   = sum(t["pnl"] for t in trades)
    n_trades  = len(trades)
    wins      = sum(1 for t in trades if t["pnl"] > 0)
    win_rate  = wins / n_trades * 100 if n_trades else 0
    return {"n_trades": n_trades, "win_rate": win_rate, "gross_pnl": gross_pnl, "net_pnl": net_pnl}


print("Running Strategy 5b (EMA9/21 x VWAP, ATR exit)...")
res_5b = run_continuous("ema_cross")
print("Running Strategy 6 (Supertrend 10,3 follower)...")
res_6  = run_continuous("supertrend")
print("Running Strategy 7 (EMA pullback continuation)...")
res_7  = run_perday(sim_s7)

STRATS = {"5b. EMA9/21 x VWAP, ATR exit": res_5b,
          "6. Supertrend(10,3) follower": res_6,
          "7. EMA pullback continuation": res_7}

# ── Full-period summary ──────────────────────────────────────────────
print(f"\n{'='*90}\nFULL PERIOD ({len(trading_days)} trading days, {START} to {END})\n{'='*90}")
print(f"{'Strategy':<32} {'Trades':>7} {'Win%':>6} {'Gross P&L':>12} {'Net P&L (@Rs.40/trade)':>24}")
print("-" * 90)
for name, res in STRATS.items():
    s = stats(res)
    print(f"{name:<32} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['gross_pnl']:>+12,.0f} {s['net_pnl']:>+24,.0f}")

# ── 3-way split stability check ──────────────────────────────────────
n = len(trading_days)
third = n // 3
splits = [
    ("Period 1 (earliest)", trading_days[0:third]),
    ("Period 2 (middle)",   trading_days[third:2*third]),
    ("Period 3 (latest)",   trading_days[2*third:]),
]

for name, res in STRATS.items():
    print(f"\n{'-'*90}\n{name} -- stability across 3 sub-periods\n{'-'*90}")
    by_date = {r["date"]: r for r in res}
    print(f"{'Sub-period':<24} {'Days':>5} {'Trades':>7} {'Win%':>6} {'Gross P&L':>12} {'Net P&L':>12}")
    for pname, days in splits:
        sub = [by_date[d.isoformat()] for d in days if d.isoformat() in by_date]
        s = stats(sub)
        date_range = f"{days[0]} to {days[-1]}" if days else "n/a"
        print(f"{pname:<24} {len(days):>5} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
              f"{s['gross_pnl']:>+12,.0f} {s['net_pnl']:>+12,.0f}   [{date_range}]")
