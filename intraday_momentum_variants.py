"""
Intraday Momentum (ORB + VWAP) -- filter refinement A/B test.

Baseline (intraday_momentum_backtest.py) lost Rs.-18,975 over 82 days at a
30.5% win rate, driven mainly by VWAP-cross whipsaws (61/105 exits) on weak
breakouts that reverse immediately. This script tests two confirming filters,
alone and combined, against the same baseline rules:

  A. Volume confirmation on entry  -- breakout candle's volume must exceed
     its 20-candle average x VOL_MULT. A true institutional breakout should
     come with above-average volume; a low-volume "breakout" is more likely
     noise that fades straight back through VWAP.

  B. Exit confirmation (2 closes)  -- require 2 consecutive closed candles
     on the wrong side of VWAP before exiting, instead of 1. Filters a
     single-candle VWAP wick/noise cross that reverses next candle.

Variants: baseline, +volume, +exit-confirm, +both.
"""
from datetime import date, timedelta
import pandas as pd
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY       = 1 * bt.LOT_SIZE
VOL_MULT  = 1.2   # entry candle volume must exceed 1.2x its 20-candle average

print(f"\nIntraday Momentum variants: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)


def simulate_orb_day(target_date, df_5m_all, require_vol=False, exit_confirm=1):
    day = df_5m_all[df_5m_all.index.date == target_date].between_time("09:15", "15:30")
    if len(day) < 5:
        return None

    or_high = float(day.iloc[0:3]["High"].max())
    or_low  = float(day.iloc[0:3]["Low"].min())
    vwap    = bt._vwap(day)
    vol_ma  = day["Volume"].rolling(20, min_periods=5).mean()

    dte = max(1, (3 - target_date.weekday()) % 7 or 7)

    position = None
    trades   = []
    daily_pnl = 0.0
    wrong_side_count = 0

    candles = list(day.iloc[3:].iterrows())
    for idx, (ts, row) in enumerate(candles):
        time_str = ts.strftime("%H:%M")
        cl  = float(row["Close"])
        vw  = float(vwap.loc[ts])
        vol = float(row["Volume"])
        vm  = float(vol_ma.loc[ts]) if not pd.isna(vol_ma.loc[ts]) else 0.0

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
            wrong_side = ((position["type"] == "CE" and cl < vw) or
                         (position["type"] == "PE" and cl > vw))
            if wrong_side:
                wrong_side_count += 1
            else:
                wrong_side_count = 0

            if wrong_side_count >= exit_confirm:
                sc     = cl - position["entry_spot"]
                pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
                pnl    = pnl_pu * QTY
                daily_pnl += pnl
                trades.append({**position, "exit_time": time_str, "exit_spot": cl,
                               "pnl": pnl, "reason": "VWAP_CROSS"})
                position = None
                wrong_side_count = 0

        if not position:
            vol_ok = (vol > vm * VOL_MULT) if require_vol else True
            if cl > or_high and cl > vw and vol_ok:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "CE", "entry_spot": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}
            elif cl < or_low and cl < vw and vol_ok:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "PE", "entry_spot": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}

    return {"date": target_date.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


def run_variant(require_vol, exit_confirm):
    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = simulate_orb_day(current, df_5m, require_vol=require_vol, exit_confirm=exit_confirm)
            if r:
                results.append(r)
        current += timedelta(days=1)
    return results


def stats(results):
    trades    = [t for r in results for t in r["trades"]]
    total_pnl = sum(r["daily_pnl"] for r in results)
    n_days    = len(results)
    n_trades  = len(trades)
    wins      = sum(1 for t in trades if t["pnl"] > 0)
    losses    = sum(1 for t in trades if t["pnl"] < 0)
    win_rate  = wins / n_trades * 100 if n_trades else 0
    best_day  = max((r["daily_pnl"] for r in results), default=0)
    worst_day = min((r["daily_pnl"] for r in results), default=0)
    return {
        "total_pnl": total_pnl, "n_trades": n_trades, "wins": wins, "losses": losses,
        "win_rate": win_rate, "avg_trade": total_pnl / n_trades if n_trades else 0,
        "best_day": best_day, "worst_day": worst_day, "n_days": n_days,
    }


VARIANTS = [
    ("Baseline (no filters)",      False, 1),
    ("+ Volume confirm",           True,  1),
    ("+ Exit confirm (2 closes)",  False, 2),
    ("+ Volume + Exit confirm",    True,  2),
]

print(f"{'Variant':<28} {'Trades':>7} {'Win%':>6} {'AvgTrade':>10} {'TotalP&L':>12} {'BestDay':>10} {'WorstDay':>10}")
print("-" * 90)
for name, req_vol, ex_conf in VARIANTS:
    res = run_variant(req_vol, ex_conf)
    s = stats(res)
    print(f"{name:<28} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['avg_trade']:>+10,.0f} {s['total_pnl']:>+12,.0f} "
          f"{s['best_day']:>+10,.0f} {s['worst_day']:>+10,.0f}")

print(f"\n({stats(run_variant(False,1))['n_days']} trading days, {START} to {END}, 1 lot = {QTY} qty)")
