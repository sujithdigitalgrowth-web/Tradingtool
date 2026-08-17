"""
Price-action reader: fetches recent Nifty 5-min candles (via the same
NIFTYBEES x88.31 proxy the backtest/live bot already uses), computes
swing-based support/resistance zones, support/resistance trendlines, and
classic candlestick patterns, then emits a single self-contained HTML file
with an annotated candlestick chart.

This is exploratory/visual only -- it does not feed into backtest.py or
live_trader.py. Run: python chart_price_action.py
"""
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd

from backtest import fetch_range_data_angel

# ── Config ───────────────────────────────────────────────────────
DISPLAY_DAYS = 5      # trading days shown/analyzed on the chart
FETCH_DAYS   = 12      # calendar days to pull (buffer for weekends/holidays)
SWING_K      = 3      # candles on each side to confirm a swing high/low
CLUSTER_TOL_PCT = 0.0012   # ~0.12% of price -- merge swing points into one S/R zone
MIN_TOUCHES  = 2       # a zone needs at least this many swing touches to be drawn
MAX_LEVELS   = 4        # cap levels per side so the chart doesn't get cluttered


def find_swings(df: pd.DataFrame, k: int = SWING_K):
    """Return (swing_high_idx, swing_low_idx) -- positions where High/Low is
    the local extreme over a +/-k candle window (simple fractal pivots)."""
    highs, lows = df["High"].values, df["Low"].values
    n = len(df)
    swing_high, swing_low = [], []
    for i in range(k, n - k):
        window_h = highs[i - k:i + k + 1]
        window_l = lows[i - k:i + k + 1]
        if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
            swing_high.append(i)
        if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
            swing_low.append(i)
    return swing_high, swing_low


def cluster_levels(prices: list, tol_pct: float, min_touches: int, max_levels: int):
    """Greedy 1D clustering of pivot prices into S/R zones."""
    if not prices:
        return []
    prices = sorted(prices)
    clusters = [[prices[0]]]
    for p in prices[1:]:
        if abs(p - clusters[-1][-1]) / clusters[-1][-1] <= tol_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = [{"price": round(float(np.mean(c)), 2), "touches": len(c)} for c in clusters]
    levels = [l for l in levels if l["touches"] >= min_touches]
    levels.sort(key=lambda l: l["touches"], reverse=True)
    return levels[:max_levels]


def fit_trendline(df: pd.DataFrame, idxs: list, n_recent: int = 6):
    """Least-squares line through the most recent n_recent swing points."""
    if len(idxs) < 2:
        return None
    recent = idxs[-n_recent:]
    xs = np.array(recent, dtype=float)
    ys = np.array([df["Low"].iloc[i] if False else 0 for i in recent])  # placeholder, overwritten by caller
    return xs, ys  # unused -- see fit_support_resistance_trendline below


def _linfit(xs, ys):
    slope, intercept = np.polyfit(xs, ys, 1)
    return slope, intercept


def build_trendline(df, idxs, price_col, n_recent=6):
    if len(idxs) < 2:
        return None
    recent = idxs[-n_recent:]
    xs = np.array(recent, dtype=float)
    ys = df[price_col].values[recent].astype(float)
    slope, intercept = _linfit(xs, ys)
    x1, x2 = recent[0], len(df) - 1
    y1 = slope * x1 + intercept
    y2 = slope * x2 + intercept
    return {
        "x1": int(x1), "y1": round(float(y1), 2),
        "x2": int(x2), "y2": round(float(y2), 2),
        "slope": round(float(slope), 4),
    }


def _body(o, c):
    return abs(c - o)


def detect_patterns(df: pd.DataFrame):
    """Classic single/two-candle patterns. Returns list of
    {index, type, direction} keyed to df row position."""
    patterns = []
    o, h, l, c = (df["Open"].values, df["High"].values,
                  df["Low"].values, df["Close"].values)
    n = len(df)
    for i in range(1, n):
        rng = h[i] - l[i]
        if rng <= 0:
            continue
        body = _body(o[i], c[i])
        upper_wick = h[i] - max(o[i], c[i])
        lower_wick = min(o[i], c[i]) - l[i]
        bullish = c[i] > o[i]

        # trend context: simple 5-candle slope before this candle
        lookback = max(0, i - 5)
        prior_close = c[lookback]
        downtrend = c[i - 1] < prior_close
        uptrend = c[i - 1] > prior_close

        # Doji
        if body <= 0.1 * rng:
            patterns.append({"index": i, "type": "Doji", "direction": "neutral"})
            continue

        # Hammer (bullish reversal, needs prior downtrend)
        if downtrend and lower_wick >= 2 * body and upper_wick <= 0.3 * body:
            patterns.append({"index": i, "type": "Hammer", "direction": "bullish"})

        # Shooting Star (bearish reversal, needs prior uptrend)
        if uptrend and upper_wick >= 2 * body and lower_wick <= 0.3 * body:
            patterns.append({"index": i, "type": "Shooting Star", "direction": "bearish"})

        # Engulfing (two-candle)
        prev_body = _body(o[i - 1], c[i - 1])
        prev_bearish = c[i - 1] < o[i - 1]
        prev_bullish = c[i - 1] > o[i - 1]
        if bullish and prev_bearish and body > prev_body and c[i] >= o[i - 1] and o[i] <= c[i - 1]:
            patterns.append({"index": i, "type": "Bullish Engulfing", "direction": "bullish"})
        elif (not bullish) and prev_bullish and body > prev_body and o[i] >= c[i - 1] and c[i] <= o[i - 1]:
            patterns.append({"index": i, "type": "Bearish Engulfing", "direction": "bearish"})

    return patterns


def main():
    target = date.today()
    fetch_start = target - timedelta(days=FETCH_DAYS)

    print(f"Fetching Angel One data: {fetch_start} -> {target} ...")
    df_5m, _, _, _, _ = fetch_range_data_angel(fetch_start, target)
    if df_5m.empty:
        raise SystemExit("ERROR: no data returned.")

    # Keep only the most recent DISPLAY_DAYS trading days, market hours only.
    df_5m = df_5m.between_time("09:15", "15:30")
    trading_days = sorted(set(df_5m.index.date))[-DISPLAY_DAYS:]
    df = df_5m[df_5m.index.to_series().dt.date.isin(trading_days)].copy()
    df = df.sort_index()
    print(f"Analyzing {len(df)} candles across {len(trading_days)} trading days: "
          f"{trading_days[0]} -> {trading_days[-1]}")

    swing_high_idx, swing_low_idx = find_swings(df)
    print(f"Swing highs: {len(swing_high_idx)}  Swing lows: {len(swing_low_idx)}")

    resistance_levels = cluster_levels(
        [float(df["High"].iloc[i]) for i in swing_high_idx],
        CLUSTER_TOL_PCT, MIN_TOUCHES, MAX_LEVELS)
    support_levels = cluster_levels(
        [float(df["Low"].iloc[i]) for i in swing_low_idx],
        CLUSTER_TOL_PCT, MIN_TOUCHES, MAX_LEVELS)

    resistance_trendline = build_trendline(df, swing_high_idx, "High")
    support_trendline = build_trendline(df, swing_low_idx, "Low")

    patterns = detect_patterns(df)
    print(f"Support levels: {support_levels}")
    print(f"Resistance levels: {resistance_levels}")
    print(f"Patterns detected: {len(patterns)}")

    candles = []
    day_boundaries = []
    last_day = None
    for i, (ts, row) in enumerate(df.iterrows()):
        d = ts.date()
        if d != last_day:
            day_boundaries.append({"index": i, "label": ts.strftime("%d %b")})
            last_day = d
        candles.append({
            "t": ts.strftime("%d %b %H:%M"),
            "o": round(float(row["Open"]), 2),
            "h": round(float(row["High"]), 2),
            "l": round(float(row["Low"]), 2),
            "c": round(float(row["Close"]), 2),
            "v": float(row["Volume"]),
        })

    data = {
        "meta": {
            "symbol": "NIFTY 50 (NIFTYBEES x88.31 proxy)",
            "range": f"{trading_days[0]} to {trading_days[-1]}",
            "generated": pd.Timestamp.now(tz='Asia/Kolkata').strftime("%d %b %Y %H:%M IST"),
            "candle_count": len(candles),
        },
        "candles": candles,
        "day_boundaries": day_boundaries,
        "support_levels": support_levels,
        "resistance_levels": resistance_levels,
        "support_trendline": support_trendline,
        "resistance_trendline": resistance_trendline,
        "patterns": patterns,
    }

    out_path = r"C:\Users\91703\AppData\Local\Temp\claude\c--Users-91703-OneDrive-Desktop-Artha-Trading-Bot---Nifty-50\a1ef1ac2-d291-4929-9932-50f4400af7a7\scratchpad\price_action_data.json"
    with open(out_path, "w") as f:
        json.dump(data, f)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
