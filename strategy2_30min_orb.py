"""
Strategy 2: 30-Minute Opening Range Breakout (wider range, no VWAP filter).

Setup : High/Low of the first 30 minutes (09:15-09:45), i.e. first 6 x 5-min
        candles. Meant to filter early noise vs. the 15-min version.
Entry : Long  when a closed 5-min candle's Close > 30-min High.
        Short when a closed 5-min candle's Close < 30-min Low.
        (No VWAP confirmation in this version, unlike Strategy 1.)
Exit  : Two variants tested, since the brief said "1:2 R:R OR trailing stop":
        A. Fixed 1:2 R:R  -- initial stop = opposite boundary of the 30-min
           range (the natural structure stop for an ORB), target = entry +
           2x that risk. Whichever is hit first on a closed candle.
        B. ATR(2x,14) trailing stop -- same mechanism used for Strategy 1,
           for direct comparability.
        Both back-stopped by EOD square-off at 15:15.
"""
from datetime import date, timedelta
import pandas as pd
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY        = 1 * bt.LOT_SIZE
ATR_MULT   = 2.0
ATR_PERIOD = 14
RR_MULT    = 2.0   # target = RR_MULT x initial risk

print(f"\nStrategy 2: 30-min ORB (no VWAP): {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)


def simulate_day(target_date, df_5m_all, exit_mode="rr"):
    day = df_5m_all[df_5m_all.index.date == target_date].between_time("09:15", "15:30")
    if len(day) < 8:
        return None

    prev = df_5m_all[df_5m_all.index.date < target_date].between_time("09:15", "15:30").tail(30)
    warm = pd.concat([prev, day]) if not prev.empty else day
    n_prev = len(prev)

    def _slice(s):
        part = s.iloc[n_prev: n_prev + len(day)]
        if len(part) != len(day):
            return s.iloc[-len(day):].set_axis(day.index)
        return pd.Series(part.values, index=day.index)

    or_high = float(day.iloc[0:6]["High"].max())   # first 30 min = 6 candles
    or_low  = float(day.iloc[0:6]["Low"].min())
    atr_s   = _slice(bt._atr(warm, ATR_PERIOD))

    dte = max(1, (3 - target_date.weekday()) % 7 or 7)

    position = None
    trades   = []
    daily_pnl = 0.0

    candles = list(day.iloc[6:].iterrows())
    for ts, row in candles:
        time_str = ts.strftime("%H:%M")
        cl = float(row["Close"])
        at = float(atr_s.loc[ts]) if not pd.isna(atr_s.loc[ts]) else 0.0

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
            exit_now  = False
            reason    = None
            if exit_mode == "rr":
                if position["type"] == "CE":
                    if cl <= position["stop"]:
                        exit_now, reason = True, "SL"
                    elif cl >= position["target"]:
                        exit_now, reason = True, "TARGET"
                else:
                    if cl >= position["stop"]:
                        exit_now, reason = True, "SL"
                    elif cl <= position["target"]:
                        exit_now, reason = True, "TARGET"
            else:  # atr trail
                if position["type"] == "CE":
                    position["peak"] = max(position["peak"], cl)
                    trail = position["peak"] - ATR_MULT * at
                    exit_now = cl < trail
                else:
                    position["peak"] = min(position["peak"], cl)
                    trail = position["peak"] + ATR_MULT * at
                    exit_now = cl > trail
                reason = "ATR_EXIT"

            if exit_now:
                sc     = cl - position["entry_spot"]
                pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
                pnl    = pnl_pu * QTY
                daily_pnl += pnl
                trades.append({**position, "exit_time": time_str, "exit_spot": cl,
                               "pnl": pnl, "reason": reason})
                position = None

        if not position:
            if cl > or_high:
                risk = cl - or_low
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "CE", "entry_spot": cl, "peak": cl,
                           "stop": or_low, "target": cl + RR_MULT * risk,
                           "entry_option_price": entry_price, "entry_time": time_str}
            elif cl < or_low:
                risk = or_high - cl
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "PE", "entry_spot": cl, "peak": cl,
                           "stop": or_high, "target": cl - RR_MULT * risk,
                           "entry_option_price": entry_price, "entry_time": time_str}

    return {"date": target_date.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


def run_variant(exit_mode):
    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = simulate_day(current, df_5m, exit_mode=exit_mode)
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
    worst_day = min((r["daily_pnl"] for r in results), default=0)
    best_day  = max((r["daily_pnl"] for r in results), default=0)
    avg_win  = sum(t["pnl"] for t in trades if t["pnl"] > 0) / wins if wins else 0
    avg_loss = sum(t["pnl"] for t in trades if t["pnl"] < 0) / losses if losses else 0
    return {
        "total_pnl": total_pnl, "n_trades": n_trades, "win_rate": win_rate,
        "win_days": win_days, "loss_days": loss_days,
        "avg_trade": total_pnl / n_trades if n_trades else 0,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "best_day": best_day, "worst_day": worst_day,
    }


print(f"{'Exit':<24} {'Trades':>7} {'Win%':>6} {'AvgWin':>9} {'AvgLoss':>9} {'AvgTrade':>10} {'TotalP&L':>12} {'WinD/LossD':>11} {'WorstDay':>10}")
print("-" * 110)
for name, mode in [("Fixed 1:2 R:R", "rr"), ("ATR(2x) trail", "atr")]:
    res = run_variant(mode)
    s = stats(res)
    print(f"{name:<24} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['avg_win']:>+9,.0f} {s['avg_loss']:>+9,.0f} {s['avg_trade']:>+10,.0f} "
          f"{s['total_pnl']:>+12,.0f} {s['win_days']:>5}/{s['loss_days']:<5} {s['worst_day']:>+10,.0f}")

n_days = len(run_variant("rr"))
print(f"\n({n_days} trading days, {START} to {END}, 1 lot = {QTY} qty)")
