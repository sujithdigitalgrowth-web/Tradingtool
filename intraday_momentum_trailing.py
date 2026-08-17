"""
Intraday Momentum (ORB + VWAP) -- trailing-stop exit test.

Replaces the flat "exit the instant price crosses back over VWAP" rule with
a trailing exit that gives winners room to run:

  ATR trail : stop = highest close since entry - 2xATR(14)  (CE)
                    = lowest  close since entry + 2xATR(14)  (PE)
              Exit when a closed candle breaches the trail.

  EMA9 trail: dynamic exit line = EMA9 of the ORB signal series.
              Exit when a closed candle closes back through EMA9
              against the position (same mechanism V2 calls EMA_EXIT,
              reimplemented here standalone for Intraday Momentum).

Entry rule is unchanged (ORB 15-min breakout + VWAP), tested both with and
without the volume-confirm filter from the previous refinement pass, since
that filter improved win rate there. EOD square-off at 15:15 always applies
as a backstop regardless of exit mode.

ATR/EMA9 use a short prior-day warm-up tail so they're not garbage on the
first few candles of the day (mirrors the pattern backtest.py uses for V2).
"""
from datetime import date, timedelta
import pandas as pd
import numpy as np
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY       = 1 * bt.LOT_SIZE
VOL_MULT  = 1.2
ATR_MULT  = 2.0
ATR_PERIOD = 14
EMA_PERIOD = 9

print(f"\nIntraday Momentum trailing-stop test: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)


def simulate_orb_day(target_date, df_5m_all, require_vol=False, exit_mode="vwap"):
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
    vol_ma  = day["Volume"].rolling(20, min_periods=5).mean()
    atr_s   = _slice(bt._atr(warm, ATR_PERIOD))
    ema_s   = _slice(warm["Close"].ewm(span=EMA_PERIOD, adjust=False).mean())

    dte = max(1, (3 - target_date.weekday()) % 7 or 7)

    position = None
    trades   = []
    daily_pnl = 0.0

    candles = list(day.iloc[3:].iterrows())
    for ts, row in candles:
        time_str = ts.strftime("%H:%M")
        cl  = float(row["Close"])
        vw  = float(vwap.loc[ts])
        vol = float(row["Volume"])
        vm  = float(vol_ma.loc[ts]) if not pd.isna(vol_ma.loc[ts]) else 0.0
        at  = float(atr_s.loc[ts]) if not pd.isna(atr_s.loc[ts]) else 0.0
        em  = float(ema_s.loc[ts]) if not pd.isna(ema_s.loc[ts]) else cl

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
            exit_now = False
            if exit_mode == "vwap":
                exit_now = ((position["type"] == "CE" and cl < vw) or
                           (position["type"] == "PE" and cl > vw))
            elif exit_mode == "atr":
                if position["type"] == "CE":
                    position["peak"] = max(position["peak"], cl)
                    trail = position["peak"] - ATR_MULT * at
                    exit_now = cl < trail
                else:
                    position["peak"] = min(position["peak"], cl)
                    trail = position["peak"] + ATR_MULT * at
                    exit_now = cl > trail
            elif exit_mode == "ema9":
                exit_now = ((position["type"] == "CE" and cl < em) or
                           (position["type"] == "PE" and cl > em))

            if exit_now:
                sc     = cl - position["entry_spot"]
                pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
                pnl    = pnl_pu * QTY
                daily_pnl += pnl
                trades.append({**position, "exit_time": time_str, "exit_spot": cl,
                               "pnl": pnl, "reason": f"{exit_mode.upper()}_EXIT"})
                position = None

        if not position:
            vol_ok = (vol > vm * VOL_MULT) if require_vol else True
            if cl > or_high and cl > vw and vol_ok:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "CE", "entry_spot": cl, "peak": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}
            elif cl < or_low and cl < vw and vol_ok:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "PE", "entry_spot": cl, "peak": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}

    return {"date": target_date.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


def run_variant(require_vol, exit_mode):
    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = simulate_orb_day(current, df_5m, require_vol=require_vol, exit_mode=exit_mode)
            if r:
                results.append(r)
        current += timedelta(days=1)
    return results


def stats(results):
    trades    = [t for r in results for t in r["trades"]]
    total_pnl = sum(r["daily_pnl"] for r in results)
    n_trades  = len(trades)
    wins      = sum(1 for t in trades if t["pnl"] > 0)
    win_rate  = wins / n_trades * 100 if n_trades else 0
    best_day  = max((r["daily_pnl"] for r in results), default=0)
    worst_day = min((r["daily_pnl"] for r in results), default=0)
    best_trade  = max((t["pnl"] for t in trades), default=0)
    worst_trade = min((t["pnl"] for t in trades), default=0)
    return {
        "total_pnl": total_pnl, "n_trades": n_trades, "win_rate": win_rate,
        "avg_trade": total_pnl / n_trades if n_trades else 0,
        "best_day": best_day, "worst_day": worst_day,
        "best_trade": best_trade, "worst_trade": worst_trade,
    }


VARIANTS = [
    ("VWAP-cross (baseline)",         False, "vwap"),
    ("ATR(2x) trail",                 False, "atr"),
    ("ATR(2x) trail + Vol confirm",   True,  "atr"),
    ("EMA9 trail",                    False, "ema9"),
    ("EMA9 trail + Vol confirm",      True,  "ema9"),
]

print(f"{'Variant':<30} {'Trades':>7} {'Win%':>6} {'AvgTrade':>10} {'TotalP&L':>12} {'BestTrade':>10} {'WorstTrade':>11} {'BestDay':>10} {'WorstDay':>10}")
print("-" * 120)
for name, req_vol, mode in VARIANTS:
    res = run_variant(req_vol, mode)
    s = stats(res)
    print(f"{name:<30} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['avg_trade']:>+10,.0f} {s['total_pnl']:>+12,.0f} "
          f"{s['best_trade']:>+10,.0f} {s['worst_trade']:>+11,.0f} "
          f"{s['best_day']:>+10,.0f} {s['worst_day']:>+10,.0f}")

n_days = len(run_variant(False, "vwap"))
print(f"\n({n_days} trading days, {START} to {END}, 1 lot = {QTY} qty)")
