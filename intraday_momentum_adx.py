"""
Intraday Momentum (ORB + VWAP + ATR trail) -- ADX regime-filter test.

Prior results (intraday_momentum_trailing.py, 82 days):
  VWAP-cross exit (baseline)      : -Rs.18,975  (30.5% win rate)
  ATR(2x) trail exit (best so far): -Rs. 8,095  (39.7% win rate)  <- carried forward as base

This script adds Wilder's ADX(14) as an entry regime filter on top of the
ATR-trail exit: only take the ORB breakout if ADX(14) > threshold at the
entry candle. Low ADX = range-bound/choppy market, where breakouts are
expected to fail regardless of how the exit is managed. Tests threshold
20 and 25 against the no-ADX baseline.
"""
from datetime import date, timedelta
import pandas as pd
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY        = 1 * bt.LOT_SIZE
ATR_MULT   = 2.0
ATR_PERIOD = 14
ADX_PERIOD = 14

print(f"\nIntraday Momentum ADX-filter test: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)


def simulate_orb_day(target_date, df_5m_all, adx_min=0):
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
    adx_s   = _slice(bt._adx(warm, ADX_PERIOD))

    dte = max(1, (3 - target_date.weekday()) % 7 or 7)

    position = None
    trades   = []
    daily_pnl = 0.0
    adx_skips = 0

    candles = list(day.iloc[3:].iterrows())
    for ts, row in candles:
        time_str = ts.strftime("%H:%M")
        cl  = float(row["Close"])
        vw  = float(vwap.loc[ts])
        at  = float(atr_s.loc[ts]) if not pd.isna(atr_s.loc[ts]) else 0.0
        adx = float(adx_s.loc[ts]) if not pd.isna(adx_s.loc[ts]) else 0.0

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
            if adx_min > 0 and adx <= adx_min:
                if raw_buy or raw_sell:
                    adx_skips += 1
                raw_buy = raw_sell = False

            if raw_buy:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "CE", "entry_spot": cl, "peak": cl,
                           "entry_option_price": entry_price, "entry_time": time_str,
                           "entry_adx": round(adx, 1)}
            elif raw_sell:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "PE", "entry_spot": cl, "peak": cl,
                           "entry_option_price": entry_price, "entry_time": time_str,
                           "entry_adx": round(adx, 1)}

    return {"date": target_date.isoformat(), "trades": trades, "daily_pnl": daily_pnl,
            "adx_skips": adx_skips}


def run_variant(adx_min):
    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = simulate_orb_day(current, df_5m, adx_min=adx_min)
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
    best_trade  = max((t["pnl"] for t in trades), default=0)
    worst_trade = min((t["pnl"] for t in trades), default=0)
    avg_win  = sum(t["pnl"] for t in trades if t["pnl"] > 0) / wins if wins else 0
    avg_loss = sum(t["pnl"] for t in trades if t["pnl"] < 0) / losses if losses else 0
    total_skips = sum(r["adx_skips"] for r in results)
    return {
        "total_pnl": total_pnl, "n_trades": n_trades, "win_rate": win_rate,
        "win_days": win_days, "loss_days": loss_days,
        "avg_trade": total_pnl / n_trades if n_trades else 0,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "best_day": best_day, "worst_day": worst_day,
        "best_trade": best_trade, "worst_trade": worst_trade,
        "adx_skips": total_skips,
    }


VARIANTS = [
    ("No ADX filter (base)", 0),
    ("ADX > 20",             20),
    ("ADX > 25",             25),
]

all_results = {}
print(f"{'Variant':<20} {'Trades':>7} {'Win%':>6} {'AvgWin':>9} {'AvgLoss':>9} {'AvgTrade':>10} {'TotalP&L':>12} {'WinD/LossD':>11} {'WorstDay':>10} {'ADXskips':>9}")
print("-" * 115)
for name, adx_min in VARIANTS:
    res = run_variant(adx_min)
    all_results[name] = res
    s = stats(res)
    print(f"{name:<20} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['avg_win']:>+9,.0f} {s['avg_loss']:>+9,.0f} {s['avg_trade']:>+10,.0f} "
          f"{s['total_pnl']:>+12,.0f} {s['win_days']:>5}/{s['loss_days']:<5} "
          f"{s['worst_day']:>+10,.0f} {s['adx_skips']:>9}")

n_days = len(all_results["No ADX filter (base)"])
print(f"\n({n_days} trading days, {START} to {END}, 1 lot = {QTY} qty)")

# ── Per-day detail for the best ADX variant vs base ──────────────
best_name = max(VARIANTS[1:], key=lambda v: stats(all_results[v[0]])["total_pnl"])[0]
print(f"\nBest ADX variant: {best_name}")
print(f"\n{'DATE':<12} {'DOW':<4} {'BASE #TR':<10} {'BASE PNL':>10}   {best_name+' #TR':<14} {'PNL':>10}")
base_by_date = {r["date"]: r for r in all_results["No ADX filter (base)"]}
best_by_date = {r["date"]: r for r in all_results[best_name]}
for day in sorted(base_by_date):
    rb = base_by_date[day]
    rp = best_by_date.get(day)
    dow = date.fromisoformat(day).strftime("%a")
    print(f"{day:<12} {dow:<4} {len(rb['trades']):<10} {rb['daily_pnl']:>+10,.0f}   "
          f"{len(rp['trades']) if rp else 0:<14} {rp['daily_pnl'] if rp else 0:>+10,.0f}")
