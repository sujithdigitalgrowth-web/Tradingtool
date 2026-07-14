"""
Compare V2 (legacy all-conditions AND-gate) vs V4 (fresh-crossover +
majority-vote confirmation score) over the longest available Angel One
history.

V4 changes:
  1. Mandatory: fresh EMA9/EMA20 crossover within V2_CROSS_LOOKBACK candles
     (catches entries near the start of a move instead of deep into it)
  2. Mandatory: price on correct side of VWAP right now
  3. Confirmation score: need V2_CONFIRM_MIN of 5 (RSI, Supertrend, volume,
     BNF alignment, candle color) instead of requiring all of them

Run this AFTER market close (15:30 IST) — it logs into Angel One fresh,
which can disrupt the live bot's active session.
"""
from datetime import date, timedelta
import backtest as bt

# Live account trades 1 lot only (trading_config.json: "lots": 1). backtest.py's
# module default QTY = LOT_SIZE*2 (2 lots) would silently simulate the WRONG
# exit path (partial-exit-then-trail) instead of live's actual 1-lot hard-TP
# behavior — override to match live before running anything.
bt.QTY = bt.LOT_SIZE

START = date(2026, 1, 1)
END   = date.today() - timedelta(days=1)

print(f"\nFetching Angel One data {START} to {END} (one-time, ~6 months)...")
df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
print("Data fetched.\n")

days = [START + timedelta(days=i) for i in range((END - START).days + 1)
        if (START + timedelta(days=i)).weekday() < 5]
print(f"Trading days in range: {len(days)}\n")


def run(entry_mode, ema_exit_confirm=1, ema_exit_min_loss=0.0,
        trail_trigger=None, trail_floor_1lot=0.0):
    results = []
    for d in days:
        r = bt.simulate_day(d, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf,
                            df_vix=df_vix, entry_mode=entry_mode,
                            ema_exit_confirm=ema_exit_confirm,
                            ema_exit_min_loss=ema_exit_min_loss,
                            trail_trigger=trail_trigger,
                            trail_floor_1lot=trail_floor_1lot)
        if r:
            results.append(r)
    return results


def _hold_min(t):
    """Minutes between entry and exit candle (backtest resolution = 5 min, so
    this can never go below 5 — it's a proxy for 'exited on the very next
    candle', the fastest possible stop-out at this backtest's resolution)."""
    if not (t.get("time") and t.get("exit_time")):
        return None
    em = int(t["time"][:2]) * 60 + int(t["time"][3:5])
    xm = int(t["exit_time"][:2]) * 60 + int(t["exit_time"][3:5])
    return xm - em


def stats(results, label):
    trades = [t for r in results for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]
    total_pnl = sum(r["daily_pnl"] for r in results)
    wins   = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    wr     = round(wins / len(trades) * 100) if trades else 0
    avg_loss = (sum(t["pnl"] for t in trades if t["pnl"] <= 0) / losses) if losses else 0
    sl_hits  = sum(1 for t in trades if t["reason"] in ("SL", "SL_HARD", "SPOT_SL", "SPOT_SL_HARD"))
    holds = [_hold_min(t) for t in trades if _hold_min(t) is not None]
    next_candle_exits = sum(1 for h in holds if h <= 5)  # fastest possible at 5-min resolution

    print(f"{'='*72}")
    print(f"{label}")
    print(f"{'='*72}")
    print(f"  Trades       : {len(trades)}  ({len(trades)/len(days)*100:.0f}% of {len(days)} days)")
    print(f"  Win rate     : {wr}%  ({wins}W / {losses}L)")
    print(f"  Total P&L    : Rs.{total_pnl:,.0f}")
    print(f"  Avg loser    : Rs.{avg_loss:,.0f}")
    print(f"  Avg hold     : {sum(holds)/len(holds):.0f} min" if holds else "  Avg hold     : n/a")
    print(f"  Exited on very next candle (fastest possible @ 5-min resolution): {next_candle_exits}")
    print(f"  SL-type exits: {sl_hits}")

    reasons = {}
    for t in trades:
        r = t["reason"]
        reasons.setdefault(r, [0, 0.0])
        reasons[r][0] += 1
        reasons[r][1] += t["pnl"]
    print("  By exit reason:")
    for r, (n, pnl) in sorted(reasons.items(), key=lambda kv: kv[1][1]):
        print(f"    {r:16s} n={n:4d}  total Rs.{pnl:>10,.0f}  avg Rs.{pnl/n:>8,.0f}")

    ema_pcts = sorted((t["exit"] - t["entry"]) / t["entry"] * 100
                       for t in trades if t["reason"] == "EMA_EXIT")
    if ema_pcts:
        mid = ema_pcts[len(ema_pcts)//2]
        print(f"  EMA_EXIT opt_pct at exit: min={ema_pcts[0]:+.1f}%  median={mid:+.1f}%  max={ema_pcts[-1]:+.1f}%")
    print()
    return trades


print("\n" + "#" * 72)
print("# PHASE 4 — drought analysis: does V4 actually kill multi-week silent")
print("# stretches, or just raise the average? (V2 = current live entry logic)")
print("#" * 72 + "\n")

bt.V2_1LOT_TP_PCT    = 9.99      # disable hard cap (best exit config found)
bt.V2_1LOT_TP_RUPEES = 999999
bt.V2_SL_WARN_PCT    = 0.17
bt.V2_SL_OPTION_PCT  = 0.20


def gap_analysis(entry_mode, label):
    results = run(entry_mode, ema_exit_confirm=1, ema_exit_min_loss=0.0,
                  trail_trigger=0.10, trail_floor_1lot=0.0)
    traded_days = {r["date"] for r in results if r.get("trade_count", 0) > 0}
    gaps, streak = [], 0
    for d in days:
        if str(d) in traded_days:
            if streak > 0:
                gaps.append(streak)
            streak = 0
        else:
            streak += 1
    if streak > 0:
        gaps.append(streak)
    longest = max(gaps) if gaps else 0
    over_5  = sum(1 for g in gaps if g >= 5)
    print(f"{label}")
    print(f"  Days with >=1 trade : {len(traded_days)} / {len(days)}")
    print(f"  Longest silent streak (trading days): {longest}")
    print(f"  Silent streaks >=5 trading days: {over_5}  -> {sorted([g for g in gaps if g >= 5], reverse=True)}")
    print()


gap_analysis("v2", "V2 — current live entry logic")
gap_analysis("v4", "V4 — fresh-crossover + confirmation score")

print("\n" + "#" * 72)
print("# PHASE 5 — best-of-both test: V2 entry (good frequency) + no-cap")
print("# trailing exit (good P&L), vs V4 entry + same exit fix")
print("#" * 72 + "\n")

for entry_mode, label in [("v2", "V2 entry + no-cap trailing exit"),
                          ("v4", "V4 entry + no-cap trailing exit (already tested: -Rs.5,009)")]:
    print(f"Running {label}...")
    r = run(entry_mode, ema_exit_confirm=1, ema_exit_min_loss=0.0,
            trail_trigger=0.10, trail_floor_1lot=0.0)
    stats(r, label)

print(f"{'='*72}")
print("tune_params.py target: 35-50 trades over ~74 days, win rate >= 45%, positive P&L")
print(f"{'='*72}")
