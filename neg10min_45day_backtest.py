"""
Strategy 6c + NEG_10MIN_EXIT -- N-day backtest (default 45, pass --90 for 90).

Extends supertrend_45day_trail_backtest.py's engine (identical entries, spot
SL, 3-tier trail floor, ST_FLIP, EOD square-off; same 0.5-delta spot
approximation for P&L, same Rs.40/trade cost) with the live NEG_10MIN_EXIT
rule added on top:

  Still down at least NEG10_LOSS_PCT (option %) 10 minutes after entry ->
  cut the position and immediately flip into the opposite side at the same
  strike, managed by the exact same exit rules. Never chains a second
  reversal (is_reversal positions skip this check). Skips the flip (still
  cuts, just no re-entry) within 30min of square-off or once the day's
  trade/loss caps are already hit -- mirrors live_trader.py's guards.

Compares three variants over the same window:
  NONE  : NEG_10MIN_EXIT disabled                    -- pre-Sep-1 behavior
  0%    : fires on ANY negative tick at 10min          -- what shipped Sep 1
  5%    : fires only if down >=5% at 10min             -- today's fix

Usage:
  python neg10min_45day_backtest.py            # 45-day, compare NONE/0%/5%
  python neg10min_45day_backtest.py --90       # 90-day
  python neg10min_45day_backtest.py --sweep    # sweep NEG10_LOSS_PCT grid
"""
import sys
from datetime import date, timedelta
import pandas as pd
import backtest as bt

DAYS  = 90 if "--90" in sys.argv else 45
START = date.today() - timedelta(days=DAYS)
END   = date.today() - timedelta(days=1)

LOT            = bt.LOT_SIZE
QTY            = 2 * LOT      # matches real live sizing (2 lots)
COST_PER_TRADE = 40
SPOT_SL        = bt.ST6_SPOT_SL
MAX_TRADES     = 5            # matches current live dashboard config
MAX_DAILY_LOSS = bt.MAX_DAILY_LOSS

ST_PERIOD = bt.ST6_PERIOD
ST_MULT   = bt.ST6_MULT

NO_REVERSAL_AFTER = "14:45"   # skip the flip (still cuts) this close to square-off


def dte_for(d):
    return max(1, (3 - d.weekday()) % 7 or 7)


def _floor_for(peak_pct, lock_trigger, giveback):
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
                  qty=QTY, neg10_loss_pct=None, spot_sl=SPOT_SL, age_min_threshold=10):
    """neg10_loss_pct: None disables NEG_10MIN_EXIT entirely; 0.0 replicates
    the original 'any negative' rule; 0.05 is today's 5%-loss fix.
    age_min_threshold: minutes after entry before the check can fire (default 10)."""
    idx = df_5m.index
    position = None
    trades_by_date = {}
    day_trade_count = {}
    day_pnl = {}

    for i in range(1, len(idx)):
        ts = idx[i]
        d = ts.date()
        diso = d.isoformat()
        time_str = ts.strftime("%H:%M")
        cl = float(df_5m["Close"].iloc[i])
        trades_by_date.setdefault(diso, [])
        day_trade_count.setdefault(diso, 0)
        day_pnl.setdefault(diso, 0.0)

        def close_full(reason, floor_pct=None):
            nonlocal position
            spot_for_pnl = cl
            if floor_pct is not None:
                pnl_pu_target = position["entry_option_price"] * floor_pct
                sc = pnl_pu_target / 0.5
                spot_for_pnl = (position["entry_spot"] + sc if position["type"] == "CE"
                               else position["entry_spot"] - sc)
            sc = spot_for_pnl - position["entry_spot"]
            pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
            gross = pnl_pu * position["qty"]
            net = gross - COST_PER_TRADE
            trades_by_date[position["entry_date"]].append({"pnl": net, "reason": reason})
            day_trade_count[position["entry_date"]] += 1
            day_pnl[position["entry_date"]] += net
            position = None
            return net

        def open_position(side, spot, entry_time, is_reversal=False):
            nonlocal position
            dte = dte_cache.setdefault(d, dte_for(d))
            entry_price = bt.estimate_option_price(spot, dte)
            position = {"type": side, "entry_spot": spot, "entry_time": entry_time,
                       "entry_option_price": entry_price, "entry_date": diso,
                       "qty": qty, "peak_pct": 0.0, "is_reversal": is_reversal}

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                close_full("EOD_SQUAREOFF")
            continue

        if position:
            adverse = (position["entry_spot"] - cl if position["type"] == "CE"
                       else cl - position["entry_spot"])
            if adverse >= spot_sl:
                close_full("SPOT_SL")

        if position and neg10_loss_pct is not None and not position["is_reversal"]:
            entry_ts = pd.Timestamp(f"{diso} {position['entry_time']}", tz=ts.tz)
            age_min = (ts - entry_ts).total_seconds() / 60
            if age_min >= age_min_threshold:
                sc = cl - position["entry_spot"]
                pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
                opt_pct = pnl_pu / position["entry_option_price"]
                if opt_pct <= -neg10_loss_pct:
                    prev_side = position["type"]
                    close_full("NEG_10MIN_EXIT")
                    too_late = time_str >= NO_REVERSAL_AFTER
                    caps_hit = (day_trade_count[diso] >= MAX_TRADES or
                               day_pnl[diso] <= MAX_DAILY_LOSS)
                    if not too_late and not caps_hit:
                        rev_side = "PE" if prev_side == "CE" else "CE"
                        open_position(rev_side, cl, time_str, is_reversal=True)
                    continue

        if position:
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

        if (not position and "09:15" <= time_str < bt.SQUAREOFF_TIME
                and day_trade_count[diso] < MAX_TRADES
                and day_pnl[diso] > MAX_DAILY_LOSS):
            st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
            side = None
            if st_now == 1 and st_prev == -1:
                side = "CE"
            elif st_now == -1 and st_prev == 1:
                side = "PE"
            if side:
                open_position(side, cl, time_str)

    results = []
    for d in trading_days:
        diso = d.isoformat()
        trades = trades_by_date.get(diso, [])
        daily_pnl = sum(t["pnl"] for t in trades)
        results.append({"date": diso, "trades": trades, "daily_pnl": daily_pnl})
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
    n_reversals = sum(1 for r in results for t in r["trades"] if t["reason"] == "NEG_10MIN_EXIT")

    print(f"\n{'='*64}\n{label}\n{'='*64}")
    print(f"Total trades          : {n_trades}  ({n_trades/n_days:.2f}/day)")
    print(f"NEG_10MIN_EXIT fires   : {n_reversals}")
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
        print("NEG_10MIN_EXIT loss-threshold sweep\n")
        for pct in (None, 0.0, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15):
            results = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), neg10_loss_pct=pct)
            label = "disabled" if pct is None else f"{pct:.0%}"
            summarize(results, f"NEG_10MIN_EXIT threshold={label}, qty={QTY}, {DAYS}d")
    elif "--age-sweep" in sys.argv:
        print("NEG_10MIN_EXIT age-threshold sweep (loss pct fixed at 5%, SPOT_SL=50pt)\n")
        for mins in (10, 15, 20, 30):
            results = run_backtest(df_5m, st_c, trading_days, dict(dte_cache),
                                    neg10_loss_pct=0.05, spot_sl=50, age_min_threshold=mins)
            summarize(results, f"NEG_{mins}MIN_EXIT >=5% loss, SPOT_SL=50pt, qty={QTY}, {DAYS}d")
    elif "--grid" in sys.argv:
        print("NEG_MIN_EXIT grid: age threshold x loss pct (SPOT_SL=50pt)\n")
        ages = (10, 15, 20, 30)
        pcts = (0.03, 0.05, 0.10)
        rows = []
        for mins in ages:
            for pct in pcts:
                results = run_backtest(df_5m, st_c, trading_days, dict(dte_cache),
                                        neg10_loss_pct=pct, spot_sl=50, age_min_threshold=mins)
                all_trades = [t for r in results for t in r["trades"]]
                net_pnl = sum(t["pnl"] for t in all_trades)
                wins = [t["pnl"] for t in all_trades if t["pnl"] > 0]
                losses = [t["pnl"] for t in all_trades if t["pnl"] < 0]
                win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
                pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
                equity, peak, max_dd = 0.0, 0.0, 0.0
                for r in results:
                    equity += r["daily_pnl"]
                    peak = max(peak, equity)
                    max_dd = min(max_dd, equity - peak)
                n_rev = sum(1 for r in results for t in r["trades"] if t["reason"] == "NEG_10MIN_EXIT")
                rows.append((mins, pct, len(all_trades), win_rate, pf, net_pnl, max_dd, n_rev))

        print(f"{'Age':>5} {'Loss%':>6} {'Trades':>7} {'WinRate':>8} {'PF':>6} {'NetP&L':>12} {'MaxDD':>12} {'Reversals':>10}")
        for mins, pct, n, wr, pf, net, dd, nrev in rows:
            print(f"{mins:>4}m {pct:>5.0%} {n:>7} {wr:>7.1f}% {pf:>6.2f} {net:>+11,.0f} {dd:>+11,.0f} {nrev:>10}")

        best = max(rows, key=lambda r: r[5])
        print(f"\nBest net P&L: {best[0]}min / {best[1]:.0%} -> Rs.{best[5]:+,.0f} (DD Rs.{best[6]:+,.0f})")
    elif "--sl80" in sys.argv:
        print("NEG_10MIN_EXIT removed entirely, SPOT_SL 50 vs 80\n")
        none_50 = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), neg10_loss_pct=None, spot_sl=50)
        none_80 = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), neg10_loss_pct=None, spot_sl=80)
        five_50 = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), neg10_loss_pct=0.05, spot_sl=50)

        summarize(none_50, f"No reversal, SPOT_SL=50pt (current baseline), qty={QTY}, {DAYS}d")
        summarize(none_80, f"No reversal, SPOT_SL=80pt (proposed), qty={QTY}, {DAYS}d")
        summarize(five_50, f"NEG_10MIN_EXIT >=5% loss, SPOT_SL=50pt (today's live fix), qty={QTY}, {DAYS}d")
    else:
        none_results  = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), neg10_loss_pct=None)
        zero_results  = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), neg10_loss_pct=0.0)
        five_results  = run_backtest(df_5m, st_c, trading_days, dict(dte_cache), neg10_loss_pct=0.05)

        summarize(none_results, f"NEG_10MIN_EXIT disabled (pre-Sep-1 behavior), qty={QTY}, {DAYS}d")
        summarize(zero_results, f"NEG_10MIN_EXIT any-negative (shipped Sep 1), qty={QTY}, {DAYS}d")
        summarize(five_results, f"NEG_10MIN_EXIT >=5% loss (today's fix), qty={QTY}, {DAYS}d")
