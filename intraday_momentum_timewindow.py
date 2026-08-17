"""
Intraday Momentum (ORB + VWAP + ATR trail) -- entry time-window test.

Carried-forward best config from prior rounds (82-day backtest):
  ORB(15m) + VWAP entry, ATR(2x,14) trailing exit, no volume filter,
  no ADX filter -> -Rs.8,095 total, 39.7% win rate.
(ADX 20/25 entry filters were tested and didn't help -- flat or worse.)

This script adds a strict entry time window on top of that base:
  Morning   : 09:30-11:15
  Afternoon : 13:30-14:45
  Blocked   : everything outside those two windows (11:15-13:30 lunch/
              midday chop, and after 14:45).

Only ENTRIES are gated by the window -- a position opened inside the window
still manages its ATR trailing exit (and EOD square-off backstop at 15:15)
normally even if that carries it past 14:45.
"""
from datetime import date, timedelta
import pandas as pd
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY        = 1 * bt.LOT_SIZE
ATR_MULT   = 2.0
ATR_PERIOD = 14

MORNING_START   = "09:30"
MORNING_END     = "11:15"
AFTERNOON_START = "13:30"
AFTERNOON_END   = "14:45"

print(f"\nIntraday Momentum time-window test: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)


def simulate_orb_day(target_date, df_5m_all, use_time_window=False):
    day = df_5m_all[df_5m_all.index.date == target_date].between_time("09:15", "15:30")
    if len(day) < 5:
        return None

    prev = df_5m_all[df_5m_all.index.date < target_date].between_time("09:15", "15:30").tail(30)
    warm = pd.concat([prev, day]) if not prev.empty else day
    n_prev = len(prev)

    def _slice(s):
        part = s.iloc[n_prev: n_prev + len(day)]
        if len(part) != len(day):
            return s.iloc[-len(day):].set_axis(day.index)
        return pd.Series(part.values, index=day.index)

    or_high = float(day.iloc[0:3]["High"].max())
    or_low  = float(day.iloc[0:3]["Low"].min())
    vwap    = bt._vwap(day)
    atr_s   = _slice(bt._atr(warm, ATR_PERIOD))

    dte = max(1, (3 - target_date.weekday()) % 7 or 7)

    position = None
    trades   = []
    daily_pnl = 0.0
    window_skips = 0

    candles = list(day.iloc[3:].iterrows())
    for ts, row in candles:
        time_str = ts.strftime("%H:%M")
        cl  = float(row["Close"])
        vw  = float(vwap.loc[ts])
        at  = float(atr_s.loc[ts]) if not pd.isna(atr_s.loc[ts]) else 0.0

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

        if position:
            if position["type"] == "CE":
                position["peak"] = max(position["peak"], cl)
                trail = position["peak"] - ATR_MULT * at
                exit_now = cl < trail
            else:
                position["peak"] = min(position["peak"], cl)
                trail = position["peak"] + ATR_MULT * at
                exit_now = cl > trail

            if exit_now:
                sc     = cl - position["entry_spot"]
                pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
                pnl    = pnl_pu * QTY
                daily_pnl += pnl
                trades.append({**position, "exit_time": time_str, "exit_spot": cl,
                               "pnl": pnl, "reason": "ATR_EXIT"})
                position = None

        if not position:
            raw_buy  = cl > or_high and cl > vw
            raw_sell = cl < or_low  and cl < vw

            if use_time_window:
                in_morning   = MORNING_START <= time_str <= MORNING_END
                in_afternoon = AFTERNOON_START <= time_str <= AFTERNOON_END
                if not (in_morning or in_afternoon):
                    if raw_buy or raw_sell:
                        window_skips += 1
                    raw_buy = raw_sell = False

            if raw_buy:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "CE", "entry_spot": cl, "peak": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}
            elif raw_sell:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "PE", "entry_spot": cl, "peak": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}

    return {"date": target_date.isoformat(), "trades": trades, "daily_pnl": daily_pnl,
            "window_skips": window_skips}


def run_variant(use_time_window):
    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = simulate_orb_day(current, df_5m, use_time_window=use_time_window)
            if r:
                results.append(r)
        current += timedelta(days=1)
    return results


def stats(results):
    trades    = [t for r in results for t in r["trades"]]
    total_pnl = sum(r["daily_pnl"] for r in results)
    n_trades  = len(trades)
    wins      = sum(1 for t in trades if t["pnl"] > 0)
    losses    = sum(1 for t in trades if t["pnl"] < 0)
    win_rate  = wins / n_trades * 100 if n_trades else 0
    win_days  = sum(1 for r in results if r["daily_pnl"] > 0)
    loss_days = sum(1 for r in results if r["daily_pnl"] < 0)
    best_day  = max((r["daily_pnl"] for r in results), default=0)
    worst_day = min((r["daily_pnl"] for r in results), default=0)
    avg_win  = sum(t["pnl"] for t in trades if t["pnl"] > 0) / wins if wins else 0
    avg_loss = sum(t["pnl"] for t in trades if t["pnl"] < 0) / losses if losses else 0
    skips = sum(r.get("window_skips", 0) for r in results)
    return {
        "total_pnl": total_pnl, "n_trades": n_trades, "win_rate": win_rate,
        "win_days": win_days, "loss_days": loss_days,
        "avg_trade": total_pnl / n_trades if n_trades else 0,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "best_day": best_day, "worst_day": worst_day, "skips": skips,
    }


base_res = run_variant(False)
win_res  = run_variant(True)

print(f"{'Variant':<28} {'Trades':>7} {'Win%':>6} {'AvgWin':>9} {'AvgLoss':>9} {'AvgTrade':>10} {'TotalP&L':>12} {'WinD/LossD':>11} {'WorstDay':>10}")
print("-" * 110)
for name, res in [("No time window (base)", base_res), ("09:30-11:15 / 13:30-14:45", win_res)]:
    s = stats(res)
    print(f"{name:<28} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['avg_win']:>+9,.0f} {s['avg_loss']:>+9,.0f} {s['avg_trade']:>+10,.0f} "
          f"{s['total_pnl']:>+12,.0f} {s['win_days']:>5}/{s['loss_days']:<5} {s['worst_day']:>+10,.0f}")

s_win = stats(win_res)
print(f"\nEntries skipped by time window: {s_win['skips']}")
n_days = len(base_res)
print(f"({n_days} trading days, {START} to {END}, 1 lot = {QTY} qty)")

# ── Per-day detail ─────────────────────────────────────────────
print(f"\n{'DATE':<12} {'DOW':<4} {'BASE #TR':<10} {'BASE PNL':>10}   {'WINDOW #TR':<12} {'PNL':>10}")
base_by_date = {r["date"]: r for r in base_res}
win_by_date  = {r["date"]: r for r in win_res}
for day in sorted(base_by_date):
    rb = base_by_date[day]
    rw = win_by_date.get(day)
    dow = date.fromisoformat(day).strftime("%a")
    print(f"{day:<12} {dow:<4} {len(rb['trades']):<10} {rb['daily_pnl']:>+10,.0f}   "
          f"{len(rw['trades']) if rw else 0:<12} {rw['daily_pnl'] if rw else 0:>+10,.0f}")
