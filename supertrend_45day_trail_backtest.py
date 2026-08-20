"""
Strategy 6c: Supertrend(10,3) Directional Follower + spot SL + 3-tier
profit-lock -- N-day backtest (default 45, pass --90 for 90 days).

Same entries/base exits as supertrend_45day_sl_backtest.py (Strategy 6b):
  Entry : Supertrend flips Red->Green => long CE, Green->Red => long PE.
  Exit  : (1) 50pt adverse spot move -> SPOT_SL (checked first, every candle)
          (2) 3-tier profit floor -> ST_TRAIL_EXIT (added here)
          (3) Supertrend flips back      -> ST_FLIP
          (4) forced EOD square-off at 15:15

The floor mirrors bt.ST6_STEP1/STEP2/TRAIL_* constants, which
live_trader.py's _manage_position_supertrend now uses directly — always the
highest tier whose trigger the peak has reached, never stepping back down:
  peak >= STEP1_TRIGGER (15%) -> floor = STEP1_FLOOR (breakeven) — a trade
      that got this far can never close as a real loss for the day
  peak >= STEP2_TRIGGER (25%) -> floor = STEP2_FLOOR (10%)
  peak >= TRAIL_LOCK_TRIGGER (32%) -> floor = peak - TRAIL_GIVEBACK (3pts),
      continuous, no cap

Chosen deliberately over the higher-EV "32%-only" version (Rs.51,905/45d)
for the loss guarantee below 32% -- this hybrid nets Rs.43,905/45d, about
Rs.8,000 less, in exchange for guaranteeing no trade that touched +15%
closes as a loss. No partial exit, no hard TP cap (both backtested worse).

P&L: 0.5-delta spot approximation x qty, same methodology as every backtest
in this repo. Rs.40/trade flat cost deducted per closing fill.

Usage:
  python supertrend_45day_trail_backtest.py            # 45-day, default params
  python supertrend_45day_trail_backtest.py --90        # 90-day, default params
  python supertrend_45day_trail_backtest.py --sweep     # 45-day grid search
  python supertrend_45day_trail_backtest.py --90 --sweep
"""
import sys
from datetime import date, timedelta
import pandas as pd
import backtest as bt

DAYS = 90 if "--90" in sys.argv else 45
START = date.today() - timedelta(days=DAYS)
END   = date.today() - timedelta(days=1)

LOT            = bt.LOT_SIZE
QTY            = 2 * LOT      # matches real live sizing (2 lots)
COST_PER_TRADE = 40
SPOT_SL        = bt.ST6_SPOT_SL

ST_PERIOD = bt.ST6_PERIOD
ST_MULT   = bt.ST6_MULT


def dte_for(d):
    return max(1, (3 - d.weekday()) % 7 or 7)


def _floor_for(peak_pct, lock_trigger, giveback):
    """Highest applicable tier — never steps back down as peak_pct rises."""
    if peak_pct >= lock_trigger:
        return peak_pct - giveback
    elif peak_pct >= bt.ST6_STEP2_TRIGGER:
        return bt.ST6_STEP2_FLOOR
    elif peak_pct >= bt.ST6_STEP1_TRIGGER:
        return bt.ST6_STEP1_FLOOR
    return None


def run_backtest(df_5m, st_c, trading_days, dte_cache,
                  lock_trigger=bt.ST6_TRAIL_LOCK_TRIGGER,
                  giveback=bt.ST6_TRAIL_GIVEBACK,
                  trail_enabled=True, qty=QTY):
    idx = df_5m.index
    position = None
    trades_by_date = {}

    for i in range(1, len(idx)):
        ts = idx[i]
        d = ts.date()
        time_str = ts.strftime("%H:%M")
        cl = float(df_5m["Close"].iloc[i])
        trades_by_date.setdefault(d.isoformat(), [])

        def close_full(reason, floor_pct=None):
            nonlocal position
            spot_for_pnl = cl
            if floor_pct is not None:
                # pin to the exact floor price so the exit isn't inflated by
                # same-candle overshoot past the trigger.
                pnl_pu_target = position["entry_option_price"] * floor_pct
                sc = pnl_pu_target / 0.5
                spot_for_pnl = (position["entry_spot"] + sc if position["type"] == "CE"
                               else position["entry_spot"] - sc)
            sc = spot_for_pnl - position["entry_spot"]
            pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
            gross = pnl_pu * position["qty"]
            net = gross - COST_PER_TRADE
            trades_by_date[position["entry_date"]].append({"pnl": net, "reason": reason})
            position = None

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                close_full("EOD_SQUAREOFF")
            continue

        if position:
            adverse = (position["entry_spot"] - cl if position["type"] == "CE"
                       else cl - position["entry_spot"])
            if adverse >= SPOT_SL:
                close_full("SPOT_SL")

        if position and trail_enabled:
            sc = cl - position["entry_spot"]
            pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
            opt_pct = pnl_pu / position["entry_option_price"]
            if opt_pct > position["peak_pct"]:
                position["peak_pct"] = opt_pct
            floor = _floor_for(position["peak_pct"], lock_trigger, giveback)
            if floor is not None and opt_pct <= floor:
                close_full("ST_TRAIL_EXIT", floor_pct=floor)

        if position:
            st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
            exit_now = ((position["type"] == "CE" and st_now == -1 and st_prev == 1) or
                       (position["type"] == "PE" and st_now == 1 and st_prev == -1))
            if exit_now:
                close_full("ST_FLIP")

        if not position and "09:15" <= time_str < bt.SQUAREOFF_TIME:
            dte = dte_cache.setdefault(d, dte_for(d))
            st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
            side = None
            if st_now == 1 and st_prev == -1:
                side = "CE"
            elif st_now == -1 and st_prev == 1:
                side = "PE"
            if side:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": side, "entry_spot": cl, "entry_time": time_str,
                           "entry_option_price": entry_price, "entry_date": d.isoformat(),
                           "qty": qty, "peak_pct": 0.0}

    results = []
    for d in trading_days:
        trades = trades_by_date.get(d.isoformat(), [])
        daily_pnl = sum(t["pnl"] for t in trades)
        results.append({"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl})
    return results


def summarize(results, label):
    all_trades = [t for r in results for t in r["trades"]]
    n_days   = len(results)
    n_trades = len(all_trades)
    net_pnl  = sum(t["pnl"] for t in all_trades)
    wins     = [t["pnl"] for t in all_trades if t["pnl"] > 0]
    losses   = [t["pnl"] for t in all_trades if t["pnl"] < 0]
    win_rate = len(wins) / n_trades * 100 if n_trades else 0
    avg_win  = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses else float("inf")

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in results:
        equity += r["daily_pnl"]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    exit_reasons = pd.Series([t["reason"] for t in all_trades]).value_counts()

    print(f"\n{'='*64}\n{label}\n{'='*64}")
    print(f"Total trades          : {n_trades}  ({n_trades/n_days:.2f}/day)")
    print(f"Win rate               : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Avg win / Avg loss     : Rs.{avg_win:+,.0f} / Rs.{avg_loss:+,.0f}")
    print(f"Profit factor          : {profit_factor:.2f}")
    print(f"Net P&L (@Rs.{COST_PER_TRADE}/trade): Rs.{net_pnl:+,.0f}")
    print(f"Avg P&L/day             : Rs.{net_pnl/n_days:+,.0f}")
    print(f"Max drawdown            : Rs.{max_dd:+,.0f}")
    print("Exit reasons:")
    for reason, cnt in exit_reasons.items():
        print(f"  {reason:<15} {cnt}")
    return net_pnl


if __name__ == "__main__":
    print(f"\nFetching data (Angel One), {DAYS}-day window: {START} to {END}...\n")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print(f"Got {len(df_5m)} 5-min candles, {df_5m.index.date.min()} to {df_5m.index.date.max()}\n")

    st_c = bt._supertrend(df_5m, ST_PERIOD, ST_MULT)
    trading_days = sorted(set(df_5m.index.date))
    trading_days = [d for d in trading_days if d.weekday() < 5]
    dte_cache = {}

    if "--sweep" in sys.argv:
        print("Parameter sweep: lock_trigger x giveback\n")
        grid = []
        for lt in (0.20, 0.25, 0.28, 0.30, 0.32, 0.35, 0.40):
            for gb in (0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
                results = run_backtest(df_5m, st_c, trading_days, dict(dte_cache),
                                       lock_trigger=lt, giveback=gb)
                net = sum(r["daily_pnl"] for r in results)
                grid.append((lt, gb, net))
        print(f"{'lock_trigger':>12} {'giveback':>10} {'net_pnl':>14}")
        for lt, gb, net in grid:
            print(f"{lt:>12.0%} {gb:>10.0%} {net:>14,.0f}")
        best = max(grid, key=lambda x: x[2])
        print(f"\nBest combo: lock_trigger={best[0]:.0%} giveback={best[1]:.0%} -> Rs.{best[2]:+,.0f}")
    else:
        baseline_results = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), trail_enabled=False)
        trail_results = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), trail_enabled=True)

        summarize(baseline_results, f"BASELINE -- no trail (SPOT_SL + ST_FLIP + EOD only), qty={QTY}, {DAYS}d")
        summarize(trail_results, f"WITH TRAIL -- lock={bt.ST6_TRAIL_LOCK_TRIGGER:.0%}, giveback={bt.ST6_TRAIL_GIVEBACK:.0%}, no partial, no cap, qty={QTY}, {DAYS}d")
