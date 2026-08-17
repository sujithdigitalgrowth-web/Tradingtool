"""
Standalone backtest for the Hammer-reversal strategy (NOT wired into live_trader.py).

Rules (as specified):
  1. Downtrend filter : close < SMA(20)  and  close < close[3 candles ago]
  2. Hammer candle     : (min(open,close) - low) > 2 * abs(open-close)   [long lower wick]
                         and (high - max(open,close)) < abs(open-close)  [small/no upper wick]
  3. Entry             : buy-stop at the hammer's High (breakout above hammer high),
                         must trigger within HAMMER_VALID_CANDLES candles of the hammer
                         forming, else the signal expires.
  4. Stop-loss         : hammer's Low
  5. Target            : entry_price * (1 + TARGET_PCT)   [+5% move in spot points]
  6. One position at a time. P&L tracked in raw Nifty 50 spot points (no options
     premium/theta modeling) so this measures the price-action edge in isolation.

Timeframe : 15-min candles, Nifty 50 spot proxy (NIFTYBEES x88.31 via Angel One —
            Yahoo Finance was IP-rate-limited, so this reuses backtest.py's
            established Angel One fallback and resamples 5m -> 15m).
"""

import pandas as pd
import numpy as np
from datetime import date, timedelta
import backtest as bt

SYMBOL               = "NIFTY 50 (Angel proxy)"
INTERVAL             = "15m"
SMA_PERIOD            = 20
DOWNTREND_LOOKBACK    = 3
TARGET_PCT            = 0.05     # +5% profit target
HAMMER_VALID_CANDLES  = 10       # breakout must occur within this many candles or signal expires
LOOKBACK_DAYS         = 120       # Angel One has no 58-day cap like Yahoo


def fetch_data():
    end   = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    df_5m, _, _, _, _ = bt.fetch_range_data_angel(start, end)
    if df_5m.empty:
        raise SystemExit("No data returned from Angel One.")

    df_5m = df_5m.between_time("09:15", "15:30")
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    df15 = df_5m.resample("15min", label="left", closed="left").agg(agg)
    df15 = df15.dropna(subset=["Open", "High", "Low", "Close"])
    df15 = df15.between_time("09:15", "15:30")
    return df15


def run_backtest(df: pd.DataFrame):
    close = df["Close"]
    sma   = close.rolling(SMA_PERIOD).mean()

    trades  = []
    pending = None   # {'hammer_high','hammer_low','idx'}
    position = None  # {'entry_idx','entry_time','entry_price','sl','target'}

    n = len(df)
    for i in range(n):
        row = df.iloc[i]
        o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
        ts = df.index[i]

        # ── Manage open position first ──────────────────────────
        if position is not None:
            hit_sl     = l <= position["sl"]
            hit_target = h >= position["target"]
            if hit_sl and hit_target:
                # Conservative: assume the worse outcome (SL) hit first within the candle
                exit_price, reason = position["sl"], "SL"
            elif hit_sl:
                exit_price, reason = position["sl"], "SL"
            elif hit_target:
                exit_price, reason = position["target"], "TARGET"
            else:
                continue

            pnl_pts = exit_price - position["entry_price"]
            trades.append({
                "entry_time": position["entry_time"], "exit_time": ts,
                "entry": round(position["entry_price"], 2), "exit": round(exit_price, 2),
                "sl": round(position["sl"], 2), "target": round(position["target"], 2),
                "pnl_pts": round(pnl_pts, 2), "pnl_pct": round(pnl_pts / position["entry_price"] * 100, 2),
                "reason": reason,
                "bars_held": i - position["entry_idx"],
            })
            position = None
            continue  # don't also process a new signal on the exit candle

        # ── Check pending breakout order ────────────────────────
        if pending is not None:
            if i - pending["idx"] > HAMMER_VALID_CANDLES:
                pending = None
            elif h > pending["hammer_high"]:
                entry_price = pending["hammer_high"]
                position = {
                    "entry_idx": i, "entry_time": ts, "entry_price": entry_price,
                    "sl": pending["hammer_low"],
                    "target": entry_price * (1 + TARGET_PCT),
                }
                pending = None
                continue

        # ── Look for a new hammer signal ────────────────────────
        if pending is None and i >= SMA_PERIOD and i >= DOWNTREND_LOOKBACK:
            ma_val = sma.iloc[i]
            if np.isnan(ma_val):
                continue
            downtrend = c < ma_val and c < float(close.iloc[i - DOWNTREND_LOOKBACK])
            body       = abs(o - c)
            lower_wick = min(o, c) - l
            upper_wick = h - max(o, c)
            is_hammer  = lower_wick > 2 * body and upper_wick < body
            if downtrend and is_hammer:
                pending = {"hammer_high": h, "hammer_low": l, "idx": i}

    return trades


def summarize(trades: list):
    if not trades:
        print("No trades were generated by this strategy over the test window.")
        return

    df_t = pd.DataFrame(trades)
    wins   = df_t[df_t["pnl_pts"] > 0]
    losses = df_t[df_t["pnl_pts"] <= 0]
    total_pts = df_t["pnl_pts"].sum()

    print(f"\n{'='*60}")
    print(f"  Hammer Reversal Strategy — Backtest Results")
    print(f"  {SYMBOL} | {INTERVAL} candles | SMA{SMA_PERIOD} | target +{TARGET_PCT*100:.0f}% | SL @ hammer low")
    print(f"{'='*60}")
    print(f"  Total trades   : {len(df_t)}")
    print(f"  Wins / Losses  : {len(wins)} / {len(losses)}")
    print(f"  Win rate       : {len(wins)/len(df_t)*100:.1f}%")
    print(f"  Total P&L      : {total_pts:+.1f} pts")
    print(f"  Avg win        : {wins['pnl_pts'].mean():+.1f} pts" if len(wins) else "  Avg win        : n/a")
    print(f"  Avg loss       : {losses['pnl_pts'].mean():+.1f} pts" if len(losses) else "  Avg loss       : n/a")
    if len(losses) and losses['pnl_pts'].sum() != 0:
        pf = wins['pnl_pts'].sum() / abs(losses['pnl_pts'].sum())
        print(f"  Profit factor  : {pf:.2f}")
    print(f"  Avg bars held  : {df_t['bars_held'].mean():.1f}  ({df_t['bars_held'].mean()*15:.0f} min)")
    print(f"  Exit breakdown : SL={sum(df_t['reason']=='SL')}  TARGET={sum(df_t['reason']=='TARGET')}")
    print(f"{'='*60}\n")

    print(df_t.to_string(index=False))
    return df_t


if __name__ == "__main__":
    print(f"Fetching {SYMBOL} {INTERVAL} data ({LOOKBACK_DAYS} days) via Angel One...")
    data = fetch_data()
    print(f"Got {len(data)} candles from {data.index[0]} to {data.index[-1]}\n")
    all_trades = run_backtest(data)
    summarize(all_trades)
