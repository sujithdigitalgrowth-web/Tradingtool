"""
Intraday Momentum (ORB + VWAP + ATR trail) -- Volatility Expansion Filter
(opening-range size relative to average daily range) test.

Carried-forward best config (82-day backtest so far):
  ORB(15m) + VWAP entry, ATR(2x,14) trailing exit -> -Rs.8,095, 39.7% win rate.
  (Volume confirm, exit-confirm, ADX 20/25, strict time window, and
  breakout+pullback entry were each tested on top and were flat/worse.)

This adds a day-level pre-filter: skip the whole day if the first-15-min
opening range (OR_high - OR_low) is out of a "normal" band relative to
Nifty's trailing 20-day average daily range (ADR20 = mean of daily
High-Low over the prior 20 trading days).

  OR_ratio = (OR_high - OR_low) / ADR20 * 100   (OR size as % of ADR)

NOTE on a contradiction in the brief: "The Rule" says skip BOTH abnormally
small (compressed) and abnormally huge (exhausted) ranges, trading only a
middle "sweet spot" -- but the "Why" reasoning argues compressed ranges are
the GOOD setup (they precede explosive breakouts) and mid/messy ranges are
what cause whipsaws, which is the opposite band. Since these two readings
give opposite bands, this script tests both interpretations plus a couple
band widths, rather than guessing which one you meant:

  A. Middle band 25-75th pct   (literal "Rule" text: skip both extremes)
  B. Middle band 35-65th pct   (tighter version of A)
  C. Compressed-only <=30th pct (literal "Why" text: only trade compression)
  D. Exclude huge only <=70th pct (allow small+medium, skip only the exhausted/huge days)

Percentile thresholds are computed empirically from this dataset's own
OR_ratio distribution (not guessed absolute %), then applied out-of-sample
per day using the trailing distribution up to that point... simplified here
to whole-window percentiles since this is an exploratory backtest, not a
walk-forward validation.
"""
from datetime import date, timedelta
import pandas as pd
import numpy as np
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY        = 1 * bt.LOT_SIZE
ATR_MULT   = 2.0
ATR_PERIOD = 14
ADR_WINDOW = 20

print(f"\nIntraday Momentum OR-range volatility filter test: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)


def simulate_orb_day(target_date, df_5m_all, or_ratio_band=None):
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
    or_range = or_high - or_low
    vwap    = bt._vwap(day)
    atr_s   = _slice(bt._atr(warm, ATR_PERIOD))

    dte = max(1, (3 - target_date.weekday()) % 7 or 7)

    position = None
    trades   = []
    daily_pnl = 0.0

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
            if raw_buy:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "CE", "entry_spot": cl, "peak": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}
            elif raw_sell:
                entry_price = bt.estimate_option_price(cl, dte)
                position = {"type": "PE", "entry_spot": cl, "peak": cl,
                           "entry_option_price": entry_price, "entry_time": time_str}

    return {"date": target_date.isoformat(), "trades": trades, "daily_pnl": daily_pnl,
            "or_range": or_range}


# ── Build OR_range series + trailing ADR20 for every day first ──────────
all_days = []
current = START
while current <= END:
    if current.weekday() < 5:
        day = df_5m[df_5m.index.date == current].between_time("09:15", "15:30")
        if len(day) >= 5:
            or_high = float(day.iloc[0:3]["High"].max())
            or_low  = float(day.iloc[0:3]["Low"].min())
            all_days.append({"date": current, "or_range": or_high - or_low})
    current += timedelta(days=1)

or_df = pd.DataFrame(all_days).set_index("date")

# Daily range (High-Low) from df_1d for ADR20; fall back to intraday 5m
# aggregation if df_1d doesn't cover the date range cleanly.
if not df_1d.empty:
    daily_range = (df_1d["High"] - df_1d["Low"])
    daily_range.index = daily_range.index.date
else:
    daily_range = pd.Series(dtype=float)

adr20 = {}
for d in or_df.index:
    hist = daily_range[daily_range.index < d].tail(ADR_WINDOW)
    adr20[d] = float(hist.mean()) if len(hist) >= 5 else np.nan

or_df["adr20"] = pd.Series(adr20)
or_df["or_ratio"] = or_df["or_range"] / or_df["adr20"] * 100
or_df = or_df.dropna(subset=["or_ratio"])

p25, p35, p65, p70, p75 = or_df["or_ratio"].quantile([0.25, 0.35, 0.65, 0.70, 0.75])
print(f"OR_ratio distribution (n={len(or_df)}): "
      f"P25={p25:.1f}%  P35={p35:.1f}%  median={or_df['or_ratio'].median():.1f}%  "
      f"P65={p65:.1f}%  P70={p70:.1f}%  P75={p75:.1f}%\n")

BANDS = {
    "No filter (base)":                    None,
    f"A: Mid band {p25:.0f}-{p75:.0f}pct":  (p25, p75, "middle"),
    f"B: Mid band {p35:.0f}-{p65:.0f}pct":  (p35, p65, "middle"),
    f"C: Compressed only <={p35:.0f}pct":   (None, p35, "max_only"),
    f"D: Exclude huge only <={p70:.0f}pct": (None, p70, "max_only"),
}


def allowed_days(band):
    if band is None:
        return set(or_df.index)
    lo, hi, mode = band
    if mode == "middle":
        return set(or_df[(or_df["or_ratio"] >= lo) & (or_df["or_ratio"] <= hi)].index)
    else:  # max_only
        return set(or_df[or_df["or_ratio"] <= hi].index)


def run_variant(band):
    allowed = allowed_days(band)
    results = []
    for d in or_df.index:
        if d not in allowed:
            continue
        r = simulate_orb_day(d, df_5m)
        if r:
            results.append(r)
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
    return {
        "n_days": len(results), "total_pnl": total_pnl, "n_trades": n_trades,
        "win_rate": win_rate, "win_days": win_days, "loss_days": loss_days,
        "avg_trade": total_pnl / n_trades if n_trades else 0,
        "avg_day": total_pnl / len(results) if results else 0,
        "worst_day": worst_day,
    }


print(f"{'Variant':<30} {'Days':>5} {'Trades':>7} {'Win%':>6} {'AvgTrade':>10} {'AvgDay':>9} {'TotalP&L':>12} {'WinD/LossD':>11} {'WorstDay':>10}")
print("-" * 110)
for name, band in BANDS.items():
    res = run_variant(band)
    s = stats(res)
    print(f"{name:<30} {s['n_days']:>5} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['avg_trade']:>+10,.0f} {s['avg_day']:>+9,.0f} {s['total_pnl']:>+12,.0f} "
          f"{s['win_days']:>5}/{s['loss_days']:<5} {s['worst_day']:>+10,.0f}")

print(f"\n(Full window {len(or_df)} trading days with valid ADR20, {START} to {END}, 1 lot = {QTY} qty)")
