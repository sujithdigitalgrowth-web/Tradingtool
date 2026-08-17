"""
Intraday Momentum (ORB + VWAP + ATR trail) -- breakout/pullback/continuation
entry test.

Carried-forward best config (82-day backtest so far):
  ORB(15m) + VWAP entry, ATR(2x,14) trailing exit -> -Rs.8,095, 39.7% win rate.
  (Volume confirm, exit confirm, ADX 20/25, and a strict time window were
  each tested on top of that and made things flat or worse.)

This script replaces the entry trigger only (exit stays ATR(2x) trail, no
ADX/time-window/volume filter) with a breakout -> pullback -> continuation
structure meant to filter the 9:30 "hit-and-run" fakeouts:

  1. Breakout   : a closed candle's Close > OR high (or < OR low for PE)
                  while also beyond VWAP. Don't buy this candle -- just
                  mark the breakout and start tracking.
  2. Pullback   : subsequent candles that do NOT make a fresh high (CE) /
                  fresh low (PE) beyond the running extreme, AND stay on
                  the correct side of VWAP the whole time. Needs 2-4 such
                  candles (PULLBACK_MIN..PULLBACK_MAX) to count as a real
                  pause, not a single wick.
  3. Trigger    : the first candle after the pullback whose Close breaks
                  back above the pullback-phase high (CE) / below the
                  pullback-phase low (PE) -> that's the actual entry.
  4. Invalidate : if VWAP is broken at any point while tracking, or the
                  pullback drags past PULLBACK_MAX candles without
                  resolving, or price gaps back inside the OR range,
                  scrap the setup and go back to watching for a fresh
                  breakout of the OR level.
"""
from datetime import date, timedelta
import pandas as pd
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY        = 1 * bt.LOT_SIZE
ATR_MULT   = 2.0
ATR_PERIOD = 14
PULLBACK_MIN = 2
PULLBACK_MAX = 6

print(f"\nIntraday Momentum breakout/pullback entry test: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)


def simulate_orb_day(target_date, df_5m_all, use_pullback=True):
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

    # setup-tracking state (None when idle / not watching a breakout)
    setup = None   # {"dir","running_extreme","pullback_count","pullback_extreme"}

    candles = list(day.iloc[3:].iterrows())
    for ts, row in candles:
        time_str = ts.strftime("%H:%M")
        cl  = float(row["Close"])
        hi  = float(row["High"])
        lo  = float(row["Low"])
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

        # ── Manage open position: ATR trail ─────────────────────
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

        # ── Entry ────────────────────────────────────────────────
        if not position:
            if not use_pullback:
                raw_buy  = cl > or_high and cl > vw
                raw_sell = cl < or_low  and cl < vw
                if raw_buy:
                    entry_price = bt.estimate_option_price(cl, dte)
                    position = {"type": "CE", "entry_spot": cl, "peak": cl,
                               "entry_option_price": entry_price, "entry_time": time_str}
                elif raw_sell:
                    entry_price = bt.estimate_option_price(cl, dte)
                    position = {"type": "PE", "entry_spot": cl, "peak": cl,
                               "entry_option_price": entry_price, "entry_time": time_str}
                continue

            # ── Pullback state machine ──────────────────────────
            if setup is None:
                # Watch for a fresh breakout of the OR level + VWAP.
                if cl > or_high and cl > vw:
                    setup = {"dir": "CE", "running_extreme": hi,
                            "pullback_count": 0, "pullback_extreme": None}
                elif cl < or_low and cl < vw:
                    setup = {"dir": "PE", "running_extreme": lo,
                            "pullback_count": 0, "pullback_extreme": None}
            else:
                d = setup["dir"]
                # Invalidate: VWAP broken against the setup direction.
                if (d == "CE" and cl < vw) or (d == "PE" and cl > vw):
                    setup = None
                # Invalidate: pullback dragged too long without resolving.
                elif setup["pullback_count"] > PULLBACK_MAX:
                    setup = None
                else:
                    # Check trigger first: has price broken back above/below
                    # the pullback-phase extreme (only valid once we've
                    # actually accumulated a real pullback)?
                    triggered = False
                    if setup["pullback_count"] >= PULLBACK_MIN and setup["pullback_extreme"] is not None:
                        if d == "CE" and cl > setup["pullback_extreme"]:
                            triggered = True
                        elif d == "PE" and cl < setup["pullback_extreme"]:
                            triggered = True

                    if triggered:
                        entry_price = bt.estimate_option_price(cl, dte)
                        position = {"type": d, "entry_spot": cl, "peak": cl,
                                   "entry_option_price": entry_price, "entry_time": time_str,
                                   "pullback_bars": setup["pullback_count"]}
                        setup = None
                    else:
                        # Classify this candle: fresh impulse high/low (extend
                        # and reset pullback) vs. genuine pullback bar (count it).
                        if d == "CE":
                            if hi > setup["running_extreme"]:
                                setup["running_extreme"] = hi
                                setup["pullback_count"] = 0
                                setup["pullback_extreme"] = None
                            else:
                                setup["pullback_count"] += 1
                                setup["pullback_extreme"] = (hi if setup["pullback_extreme"] is None
                                                             else max(setup["pullback_extreme"], hi))
                        else:
                            if lo < setup["running_extreme"]:
                                setup["running_extreme"] = lo
                                setup["pullback_count"] = 0
                                setup["pullback_extreme"] = None
                            else:
                                setup["pullback_count"] += 1
                                setup["pullback_extreme"] = (lo if setup["pullback_extreme"] is None
                                                             else min(setup["pullback_extreme"], lo))

    return {"date": target_date.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


def run_variant(use_pullback):
    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = simulate_orb_day(current, df_5m, use_pullback=use_pullback)
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
    return {
        "total_pnl": total_pnl, "n_trades": n_trades, "win_rate": win_rate,
        "win_days": win_days, "loss_days": loss_days,
        "avg_trade": total_pnl / n_trades if n_trades else 0,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "best_day": best_day, "worst_day": worst_day,
    }


base_res = run_variant(False)
pb_res   = run_variant(True)

print(f"{'Variant':<32} {'Trades':>7} {'Win%':>6} {'AvgWin':>9} {'AvgLoss':>9} {'AvgTrade':>10} {'TotalP&L':>12} {'WinD/LossD':>11} {'WorstDay':>10}")
print("-" * 115)
for name, res in [("Immediate breakout (base)", base_res), ("Breakout+pullback+trigger", pb_res)]:
    s = stats(res)
    print(f"{name:<32} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['avg_win']:>+9,.0f} {s['avg_loss']:>+9,.0f} {s['avg_trade']:>+10,.0f} "
          f"{s['total_pnl']:>+12,.0f} {s['win_days']:>5}/{s['loss_days']:<5} {s['worst_day']:>+10,.0f}")

n_days = len(base_res)
print(f"\n({n_days} trading days, {START} to {END}, 1 lot = {QTY} qty)")

print(f"\n{'DATE':<12} {'DOW':<4} {'BASE #TR':<10} {'BASE PNL':>10}   {'PULLBACK #TR':<14} {'PNL':>10}")
base_by_date = {r["date"]: r for r in base_res}
pb_by_date   = {r["date"]: r for r in pb_res}
for day in sorted(base_by_date):
    rb = base_by_date[day]
    rp = pb_by_date.get(day)
    dow = date.fromisoformat(day).strftime("%a")
    print(f"{day:<12} {dow:<4} {len(rb['trades']):<10} {rb['daily_pnl']:>+10,.0f}   "
          f"{len(rp['trades']) if rp else 0:<14} {rp['daily_pnl'] if rp else 0:>+10,.0f}")
