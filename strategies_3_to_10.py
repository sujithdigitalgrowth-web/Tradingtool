"""
Strategies 3-10 backtest (Category A/B/C from the strategy list).

Shared setup: same data (Angel One, Nifty proxy via NIFTYBEES x88.31, 5-min,
120 calendar days -> ~82 trading days), same P&L method (0.5-delta spot
approximation, estimate_option_price for premium, 1 lot = 65 qty) as every
prior script in this session, so results are directly comparable to
Strategy 1 (-Rs.8,095) and Strategy 2 (+Rs.4,016, best so far).

Where a rule had two exit options joined by "or" (ambiguous which one you
want), both are tested as separate variants rather than guessed. Where a
rule referenced a higher timeframe (e.g. "50 EMA on 15-min chart"), the
5-min data is resampled to that timeframe with label='right', closed='left'
and forward-filled back, so only a *fully closed* higher-TF bar is ever
used (no lookahead into a still-forming bar).

Interpretation notes (flagged inline near each strategy):
  S3  "opens near the middle of the range"    -> open within 25-75th pct of PDH-PDL
  S3  "strong momentum"                        -> candle color agrees + volume > 20-bar avg
  S4  "first 15-min candle" on 5-min data      -> synthetic candle = first 3x 5-min bars
  S9  "key level" simplified to the 15-min OR high/low only (not also PDH/PDL)
  S9  exit "opposite end of OR or 1.5x ATR"    -> target = opposite OR edge,
                                                    protective invalidation stop = 1.5x ATR
  S10 "lowest 20% of historical OR ranges"     -> whole-window percentile of OR-range
                                                    as %% of day's open (same lookahead
                                                    caveat as the earlier compression test)
"""
from datetime import date, timedelta
import pandas as pd
import numpy as np
import backtest as bt

START = date.today() - timedelta(days=120)
END   = date.today() - timedelta(days=1)

QTY        = 1 * bt.LOT_SIZE
ATR_PERIOD = 14

print(f"\nStrategies 3-10 backtest: {START} to {END}")
print("Fetching data (Angel One)...\n")

df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)

# ── Global continuous indicators (no daily reset) ───────────────────────
ema9_c  = df_5m["Close"].ewm(span=9,  adjust=False).mean()
ema20_c = df_5m["Close"].ewm(span=20, adjust=False).mean()
ema21_c = df_5m["Close"].ewm(span=21, adjust=False).mean()
atr14_c = bt._atr(df_5m, ATR_PERIOD)
st_c    = bt._supertrend(df_5m, 10, 3.0)

df_15 = df_5m.resample("15min", label="right", closed="left").agg(
    {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
).dropna()
ema50_15   = df_15["Close"].ewm(span=50, adjust=False).mean()
sma20_15   = df_15["Close"].rolling(20).mean()
std20_15   = df_15["Close"].rolling(20).std()
bb_upper_15 = sma20_15 + 2 * std20_15
bb_lower_15 = sma20_15 - 2 * std20_15
rsi14_15    = bt._rsi(df_15["Close"], 14)


def _ffill_to_5m(s15):
    return s15.reindex(df_5m.index, method="ffill")


ema50_15_5m = _ffill_to_5m(ema50_15)
bb_mid_5m   = _ffill_to_5m(sma20_15)
bb_up_5m    = _ffill_to_5m(bb_upper_15)
bb_lo_5m    = _ffill_to_5m(bb_lower_15)
rsi15_5m    = _ffill_to_5m(rsi14_15)

# ── Daily reference series (PDH/PDL/prev close) from df_1d ──────────────
daily_high  = df_1d["High"].copy();  daily_high.index  = df_1d.index.date
daily_low   = df_1d["Low"].copy();   daily_low.index   = df_1d.index.date
daily_close = df_1d["Close"].copy(); daily_close.index = df_1d.index.date

trading_days = sorted(set(df_5m.index.date) & set(
    d for d in [START + timedelta(days=i) for i in range((END - START).days + 1)]
    if d.weekday() < 5
))


def prev_day_ref(d):
    prior = [dd for dd in daily_high.index if dd < d]
    if not prior:
        return None
    p = max(prior)
    return {"pdh": float(daily_high[p]), "pdl": float(daily_low[p]), "pclose": float(daily_close[p])}


def day_slice(d):
    day = df_5m[df_5m.index.date == d].between_time("09:15", "15:30")
    return day if len(day) >= 3 else None


def close_trade(position, ts_str, spot, reason):
    sc = spot - position["entry_spot"]
    pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
    pnl = pnl_pu * QTY
    return {**position, "exit_time": ts_str, "exit_spot": spot, "pnl": pnl, "reason": reason}, pnl


def make_position(ptype, ts_str, spot, dte, **extra):
    entry_price = bt.estimate_option_price(spot, dte)
    return {"type": ptype, "entry_spot": spot, "peak": spot, "entry_time": ts_str,
            "entry_option_price": entry_price, **extra}


def dte_for(d):
    return max(1, (3 - d.weekday()) % 7 or 7)


# ═══════════════════════════════════════════════════════════════════════
# Strategy 3: PDH/PDL breakout, EMA9 exit
# ═══════════════════════════════════════════════════════════════════════
def sim_s3(d):
    day = day_slice(d)
    ref = prev_day_ref(d)
    if day is None or ref is None:
        return None
    pdh, pdl, popen = ref["pdh"], ref["pdl"], float(day.iloc[0]["Open"])
    rng = pdh - pdl
    if rng <= 0:
        return {"date": d.isoformat(), "trades": [], "daily_pnl": 0.0}
    mid_lo, mid_hi = pdl + 0.25 * rng, pdl + 0.75 * rng
    if not (mid_lo <= popen <= mid_hi):
        return {"date": d.isoformat(), "trades": [], "daily_pnl": 0.0}   # doesn't open near middle

    vol_ma = day["Volume"].rolling(20, min_periods=5).mean()
    position, trades, daily_pnl = None, [], 0.0
    dte = dte_for(d)

    for ts, row in day.iterrows():
        time_str = ts.strftime("%H:%M")
        cl, op, vol = float(row["Close"]), float(row["Open"]), float(row["Volume"])
        vm = float(vol_ma.loc[ts]) if not pd.isna(vol_ma.loc[ts]) else 0.0

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades.append(t); daily_pnl += pnl; position = None
            break

        if position:
            e9 = float(ema9_c.loc[ts])
            exit_now = (position["type"] == "CE" and cl < e9) or (position["type"] == "PE" and cl > e9)
            if exit_now:
                t, pnl = close_trade(position, time_str, cl, "EMA9_EXIT")
                trades.append(t); daily_pnl += pnl; position = None

        if not position:
            strong_vol = vol > vm if vm > 0 else True
            if cl > pdh and cl > op and strong_vol:
                position = make_position("CE", time_str, cl, dte)
            elif cl < pdl and cl < op and strong_vol:
                position = make_position("PE", time_str, cl, dte)

    return {"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


# ═══════════════════════════════════════════════════════════════════════
# Strategy 4: Opening Gap Fade
# ═══════════════════════════════════════════════════════════════════════
def sim_s4(d):
    day = day_slice(d)
    ref = prev_day_ref(d)
    if day is None or ref is None or len(day) < 6:
        return None
    pclose = ref["pclose"]
    popen = float(day.iloc[0]["Open"])
    gap_pct = (popen - pclose) / pclose * 100

    if abs(gap_pct) < 0.75:
        return {"date": d.isoformat(), "trades": [], "daily_pnl": 0.0}

    first3 = day.iloc[0:3]
    agg_open, agg_close = float(first3.iloc[0]["Open"]), float(first3.iloc[-1]["Close"])
    agg_high, agg_low = float(first3["High"].max()), float(first3["Low"].min())
    rng = max(agg_high - agg_low, 0.01)
    upper_wick = agg_high - max(agg_open, agg_close)
    lower_wick = min(agg_open, agg_close) - agg_low

    direction, stop, target = None, None, None
    if gap_pct > 0:
        if agg_close < agg_open or upper_wick > 0.4 * rng:
            direction, stop, target = "PE", agg_high, pclose
    else:
        if agg_close > agg_open or lower_wick > 0.4 * rng:
            direction, stop, target = "CE", agg_low, pclose

    if direction is None:
        return {"date": d.isoformat(), "trades": [], "daily_pnl": 0.0}

    dte = dte_for(d)
    entry_ts_str = first3.index[-1].strftime("%H:%M")
    position = make_position(direction, entry_ts_str, agg_close, dte, stop=stop, target=target)
    trades, daily_pnl = [], 0.0

    for ts, row in day.iloc[3:].iterrows():
        time_str = ts.strftime("%H:%M")
        cl = float(row["Close"])
        if time_str >= bt.SQUAREOFF_TIME:
            t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
            trades.append(t); daily_pnl += pnl; position = None
            break
        if position["type"] == "PE":
            if cl <= target:
                t, pnl = close_trade(position, time_str, cl, "TARGET"); trades.append(t); daily_pnl += pnl; position = None; break
            if cl >= stop:
                t, pnl = close_trade(position, time_str, cl, "SL"); trades.append(t); daily_pnl += pnl; position = None; break
        else:
            if cl >= target:
                t, pnl = close_trade(position, time_str, cl, "TARGET"); trades.append(t); daily_pnl += pnl; position = None; break
            if cl <= stop:
                t, pnl = close_trade(position, time_str, cl, "SL"); trades.append(t); daily_pnl += pnl; position = None; break

    if position:
        t, pnl = close_trade(position, "15:30", float(day.iloc[-1]["Close"]), "EOD_SQUAREOFF")
        trades.append(t); daily_pnl += pnl

    return {"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


# ═══════════════════════════════════════════════════════════════════════
# Strategy 5: Dual EMA(9/21) crossover + VWAP gate  (continuous, global loop)
# Strategy 6: Supertrend(10,3) directional follower (continuous, global loop)
# ═══════════════════════════════════════════════════════════════════════
vwap_parts = []
for d in trading_days:
    day = day_slice(d)
    if day is not None:
        vwap_parts.append(bt._vwap(day))
vwap_global = pd.concat(vwap_parts) if vwap_parts else pd.Series(dtype=float)


def run_continuous(mode, exit_mode=None):
    position = None
    trades_by_date = {}
    idx = df_5m.index
    dte_cache = {}

    for i in range(1, len(idx)):
        ts = idx[i]
        d = ts.date()
        time_str = ts.strftime("%H:%M")
        cl = float(df_5m["Close"].iloc[i])
        trades_by_date.setdefault(d.isoformat(), [])

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades_by_date[position["entry_date"]].append((t, pnl))
                position = None
            continue

        if position:
            exit_now = False
            if mode == "ema_cross":
                if exit_mode == "opposite_cross":
                    e9, e21 = float(ema9_c.iloc[i]), float(ema21_c.iloc[i])
                    exit_now = (position["type"] == "CE" and e9 < e21) or (position["type"] == "PE" and e9 > e21)
                else:
                    at = float(atr14_c.iloc[i])
                    if position["type"] == "CE":
                        position["peak"] = max(position["peak"], cl); exit_now = cl < position["peak"] - 2 * at
                    else:
                        position["peak"] = min(position["peak"], cl); exit_now = cl > position["peak"] + 2 * at
            else:  # supertrend
                st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
                exit_now = ((position["type"] == "CE" and st_now == -1 and st_prev == 1) or
                           (position["type"] == "PE" and st_now == 1 and st_prev == -1))

            if exit_now:
                reason = "OPPOSITE_CROSS" if (mode == "ema_cross" and exit_mode == "opposite_cross") else \
                        ("ATR_EXIT" if mode == "ema_cross" else "ST_FLIP")
                t, pnl = close_trade(position, time_str, cl, reason)
                trades_by_date[position["entry_date"]].append((t, pnl))
                position = None

        if not position and "09:15" <= time_str < bt.SQUAREOFF_TIME:
            dte = dte_cache.setdefault(d, dte_for(d))
            if mode == "ema_cross":
                e9_prev, e21_prev = float(ema9_c.iloc[i - 1]), float(ema21_c.iloc[i - 1])
                e9, e21 = float(ema9_c.iloc[i]), float(ema21_c.iloc[i])
                vw = vwap_global.get(ts, np.nan)
                cross_up   = e9 > e21 and e9_prev <= e21_prev
                cross_down = e9 < e21 and e9_prev >= e21_prev
                if not np.isnan(vw):
                    if cross_up and cl > vw:
                        position = make_position("CE", time_str, cl, dte, entry_date=d.isoformat())
                    elif cross_down and cl < vw:
                        position = make_position("PE", time_str, cl, dte, entry_date=d.isoformat())
            else:
                st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
                if st_now == 1 and st_prev == -1:
                    position = make_position("CE", time_str, cl, dte, entry_date=d.isoformat())
                elif st_now == -1 and st_prev == 1:
                    position = make_position("PE", time_str, cl, dte, entry_date=d.isoformat())

    results = []
    for d in trading_days:
        pairs = trades_by_date.get(d.isoformat(), [])
        trades = [p[0] for p in pairs]
        daily_pnl = sum(p[1] for p in pairs)
        results.append({"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl})
    return results


# ═══════════════════════════════════════════════════════════════════════
# Strategy 7: EMA Pullback Continuation (50EMA-15m trend + 20EMA-5m pullback)
# ═══════════════════════════════════════════════════════════════════════
def sim_s7(d):
    day = day_slice(d)
    if day is None:
        return None
    dte = dte_for(d)
    position, trades, daily_pnl = None, [], 0.0
    touched_up = touched_dn = False
    wait_count = 0
    TOL = 0.0008
    MAX_WAIT = 6

    for ts, row in day.iterrows():
        time_str = ts.strftime("%H:%M")
        cl, op, lo, hi = float(row["Close"]), float(row["Open"]), float(row["Low"]), float(row["High"])

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades.append(t); daily_pnl += pnl; position = None
            break

        e50 = ema50_15_5m.loc[ts]
        if pd.isna(e50):
            continue
        e50 = float(e50)
        e20 = float(ema20_c.loc[ts])
        e20_prev = float(ema20_c.iloc[ema20_c.index.get_loc(ts) - 3])
        at = float(atr14_c.loc[ts])

        if position:
            if position["type"] == "CE":
                position["peak"] = max(position["peak"], cl)
                exit_now = cl < position["peak"] - 2 * at
            else:
                position["peak"] = min(position["peak"], cl)
                exit_now = cl > position["peak"] + 2 * at
            if exit_now:
                t, pnl = close_trade(position, time_str, cl, "ATR_EXIT")
                trades.append(t); daily_pnl += pnl; position = None
            continue

        uptrend   = cl > e50 * 1.001 and e20 > e20_prev
        downtrend = cl < e50 * 0.999 and e20 < e20_prev

        if uptrend:
            wait_count = min(wait_count, MAX_WAIT) if touched_up else 0
            if not touched_up:
                if lo <= e20 * (1 + TOL):
                    touched_up = True; wait_count = 0
            else:
                wait_count += 1
                if cl > op:
                    position = make_position("CE", time_str, cl, dte)
                    touched_up = False; wait_count = 0
                elif wait_count > MAX_WAIT:
                    touched_up = False; wait_count = 0
        elif downtrend:
            if not touched_dn:
                if hi >= e20 * (1 - TOL):
                    touched_dn = True; wait_count = 0
            else:
                wait_count += 1
                if cl < op:
                    position = make_position("PE", time_str, cl, dte)
                    touched_dn = False; wait_count = 0
                elif wait_count > MAX_WAIT:
                    touched_dn = False; wait_count = 0
        else:
            touched_up = touched_dn = False; wait_count = 0

    return {"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


# ═══════════════════════════════════════════════════════════════════════
# Strategy 8: Bollinger Band(20,2 on 15m) Extreme Reversion
# ═══════════════════════════════════════════════════════════════════════
def sim_s8(d):
    day = day_slice(d)
    if day is None:
        return None
    dte = dte_for(d)
    position, trades, daily_pnl = None, [], 0.0

    for ts, row in day.iterrows():
        time_str = ts.strftime("%H:%M")
        cl = float(row["Close"])
        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades.append(t); daily_pnl += pnl; position = None
            break

        mid, up, lo, rsi = bb_mid_5m.loc[ts], bb_up_5m.loc[ts], bb_lo_5m.loc[ts], rsi15_5m.loc[ts]
        if pd.isna(mid) or pd.isna(up) or pd.isna(lo) or pd.isna(rsi):
            continue
        mid, up, lo, rsi = float(mid), float(up), float(lo), float(rsi)

        if position:
            exit_now = (position["type"] == "CE" and cl >= mid) or (position["type"] == "PE" and cl <= mid)
            if exit_now:
                t, pnl = close_trade(position, time_str, cl, "BB_MID_REVERT")
                trades.append(t); daily_pnl += pnl; position = None

        if not position:
            if cl <= lo and rsi < 30:
                position = make_position("CE", time_str, cl, dte)
            elif cl >= up and rsi > 70:
                position = make_position("PE", time_str, cl, dte)

    return {"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


# ═══════════════════════════════════════════════════════════════════════
# Strategy 9: Liquidity Sweep / False Breakout Reversal (15-min OR level)
# ═══════════════════════════════════════════════════════════════════════
def sim_s9(d):
    day = day_slice(d)
    if day is None or len(day) < 5:
        return None
    or_high = float(day.iloc[0:3]["High"].max())
    or_low  = float(day.iloc[0:3]["Low"].min())
    dte = dte_for(d)
    position, trades, daily_pnl = None, [], 0.0
    pending_res = pending_sup = False

    for ts, row in day.iloc[3:].iterrows():
        time_str = ts.strftime("%H:%M")
        cl, hi, lo = float(row["Close"]), float(row["High"]), float(row["Low"])
        at = float(atr14_c.loc[ts])

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades.append(t); daily_pnl += pnl; position = None
            break

        if position:
            if position["type"] == "PE":
                exit_now, reason = (cl <= position["target"], "TARGET") if cl <= position["target"] else \
                                   ((cl >= position["stop"], "SL") if cl >= position["stop"] else (False, None))
            else:
                exit_now, reason = (cl >= position["target"], "TARGET") if cl >= position["target"] else \
                                   ((cl <= position["stop"], "SL") if cl <= position["stop"] else (False, None))
            if exit_now:
                t, pnl = close_trade(position, time_str, cl, reason)
                trades.append(t); daily_pnl += pnl; position = None
            continue

        # same-candle sweep + reject
        if hi > or_high and cl < or_high:
            position = make_position("PE", time_str, cl, dte, target=or_low, stop=cl + 1.5 * at)
            pending_res = False
        elif lo < or_low and cl > or_low:
            position = make_position("CE", time_str, cl, dte, target=or_high, stop=cl - 1.5 * at)
            pending_sup = False
        # 2-candle version: resolve a pending break from the previous candle
        elif pending_res and cl < or_high:
            position = make_position("PE", time_str, cl, dte, target=or_low, stop=cl + 1.5 * at)
            pending_res = False
        elif pending_sup and cl > or_low:
            position = make_position("CE", time_str, cl, dte, target=or_high, stop=cl - 1.5 * at)
            pending_sup = False
        else:
            pending_res = hi > or_high and cl >= or_high
            pending_sup = lo < or_low and cl <= or_low

    return {"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


# ═══════════════════════════════════════════════════════════════════════
# Strategy 10: Opening Range Compression (ORC) Breakout
# ═══════════════════════════════════════════════════════════════════════
or_records = []
for d in trading_days:
    day = day_slice(d)
    if day is not None and len(day) >= 5:
        oh, ol = float(day.iloc[0:3]["High"].max()), float(day.iloc[0:3]["Low"].min())
        popen = float(day.iloc[0]["Open"])
        or_records.append({"date": d, "or_range_pct": (oh - ol) / popen * 100, "or_high": oh, "or_low": ol})
or10_df = pd.DataFrame(or_records).set_index("date")
p20_thresh = or10_df["or_range_pct"].quantile(0.20)
compression_days = set(or10_df[or10_df["or_range_pct"] <= p20_thresh].index)


def sim_s10(d):
    if d not in compression_days:
        return {"date": d.isoformat(), "trades": [], "daily_pnl": 0.0}
    day = day_slice(d)
    if day is None:
        return None
    or_high = or10_df.loc[d, "or_high"]; or_low = or10_df.loc[d, "or_low"]
    dte = dte_for(d)
    position, trades, daily_pnl = None, [], 0.0
    ATR_MULT_WIDE = 3.0

    for ts, row in day.iloc[3:].iterrows():
        time_str = ts.strftime("%H:%M")
        cl = float(row["Close"])
        at = float(atr14_c.loc[ts])

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                t, pnl = close_trade(position, time_str, cl, "EOD_SQUAREOFF")
                trades.append(t); daily_pnl += pnl; position = None
            break

        if position:
            if position["type"] == "CE":
                position["peak"] = max(position["peak"], cl); exit_now = cl < position["peak"] - ATR_MULT_WIDE * at
            else:
                position["peak"] = min(position["peak"], cl); exit_now = cl > position["peak"] + ATR_MULT_WIDE * at
            if exit_now:
                t, pnl = close_trade(position, time_str, cl, "ATR_EXIT")
                trades.append(t); daily_pnl += pnl; position = None

        if not position and "10:00" <= time_str <= "10:30":
            if cl > or_high:
                position = make_position("CE", time_str, cl, dte)
            elif cl < or_low:
                position = make_position("PE", time_str, cl, dte)

    return {"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl}


# ═══════════════════════════════════════════════════════════════════════
# Run everything + report
# ═══════════════════════════════════════════════════════════════════════
def run_perday(sim_fn):
    return [r for d in trading_days if (r := sim_fn(d)) is not None]


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
        "total_pnl": total_pnl, "n_trades": n_trades, "win_rate": win_rate,
        "win_days": win_days, "loss_days": loss_days,
        "avg_trade": total_pnl / n_trades if n_trades else 0,
        "worst_day": worst_day,
    }


results_map = {
    "3. PDH/PDL breakout, EMA9 exit":              run_perday(sim_s3),
    "4. Opening gap fade":                          run_perday(sim_s4),
    "5a. EMA9/21 x VWAP, opp-cross exit":           run_continuous("ema_cross", "opposite_cross"),
    "5b. EMA9/21 x VWAP, ATR(2x) exit":             run_continuous("ema_cross", "atr"),
    "6. Supertrend(10,3) follower":                 run_continuous("supertrend"),
    "7. EMA pullback continuation":                 run_perday(sim_s7),
    "8. Bollinger(20,2,15m) reversion":             run_perday(sim_s8),
    "9. Liquidity sweep reversal (OR level)":       run_perday(sim_s9),
    "10. ORC compression breakout":                 run_perday(sim_s10),
}

print(f"{'Strategy':<38} {'Trades':>7} {'Win%':>6} {'AvgTrade':>10} {'TotalP&L':>12} {'WinD/LossD':>11} {'WorstDay':>10}")
print("-" * 100)
for name, res in results_map.items():
    s = stats(res)
    print(f"{name:<38} {s['n_trades']:>7} {s['win_rate']:>5.1f}% "
          f"{s['avg_trade']:>+10,.0f} {s['total_pnl']:>+12,.0f} "
          f"{s['win_days']:>5}/{s['loss_days']:<5} {s['worst_day']:>+10,.0f}")

print(f"\n({len(trading_days)} trading days, {START} to {END}, 1 lot = {QTY} qty)")
print(f"S10 compression threshold: OR range <= {p20_thresh:.3f}% of open "
      f"({len(compression_days)} qualifying days)")

print("\nRecap for context:")
print(f"  Strategy 1 (15m ORB+VWAP, ATR trail)     : -Rs.8,095   (39.7% win, 116 trades)")
print(f"  Strategy 2 (30m ORB no-VWAP, ATR trail)  : +Rs.4,016   (42.9% win, 105 trades)")
