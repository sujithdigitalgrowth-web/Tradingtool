"""
Backtest engine: V2 Volume/VWAP/EMA strategy on Nifty 50 5-min candles.
Signal source: NIFTYBEES.NS (5m) + BANKBEES.NS (5m) + India VIX filter.
P&L source: ^NSEI spot price.

Improvements (v2.1):
  1. Dual EMA (9 fast + 20 slow) — both must agree for entry
  2. RSI(14) filter — >60 for CE, <40 for PE
  3. Partial exit — 50% qty exits at +20%, rest runs to +40% TP
  4. Trail stop floor raised to +5% (not breakeven)
  5. Skip Thursday (weekly expiry — high theta decay)
  6. Bank Nifty alignment — BANKBEES VWAP must agree with direction
  7. Trail activates at +10%, floor at +5% after partial exit

Improvements (v2.2):
  8. India VIX filter — only trade when VIX 13–22 (sweet spot)
  9. Time window — morning 9:45–11:15 or afternoon 14:00–15:00 only
 10. Supertrend (7, ATR×2) — must align with trade direction
 11. Faster signals — NIFTYBEES fetched at 2-min interval
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json, os, time as _time
from datetime import datetime, date, timedelta

# ── Config ───────────────────────────────────────────────────────
SYMBOL               = "^NSEI"
INITIAL_BALANCE      = 30_000
LOT_SIZE             = 65   # NSE lot size effective Oct 28 2025
QTY                  = LOT_SIZE * 2   # default: 2 lots
ATM_OPTION_IV        = 0.15
NO_ENTRY_AFTER       = "14:50"
SQUAREOFF_TIME       = "15:15"
MAX_DAILY_LOSS       = -8000
DAILY_PROFIT_TARGET  = 6000

# ── V2 Strategy constants ─────────────────────────────────────────
V2_TP_OPTION_PCT   = 0.20   # 2-lot: remaining lot hard TP at +20%
V2_SL_OPTION_PCT   = 0.20   # premium hard stop — immediate exit, no confirmation needed
V2_SL_WARN_PCT     = 0.13   # premium warning zone — 2 polls needed (slow bleed filter)
V2_SPOT_SL_WARN    = 50     # spot warning zone — 2 polls needed (small move, wait and see)
V2_SPOT_SL_HARD    = 80     # spot hard stop — immediate exit (market genuinely reversed)
V2_PARTIAL_PCT     = 0.10   # 2-lot: partial exit 1 lot at +10%
V2_TRAIL_TRIGGER   = 0.10   # activate trail at +10%
V2_TRAIL_FLOOR     = 0.00   # after partial: SL steps to breakeven (0%)
V2_1LOT_TP_PCT     = 0.10   # 1-lot: exit at +10% option gain …
V2_1LOT_TP_RUPEES  = 1100   # … or ₹1,100 absolute P&L — whichever comes first
V2_VOL_SURGE_MULT  = 1.5    # volume > 1.5× 20-bar avg
V2_EMA_FAST        = 9      # fast EMA — entry filter + exit trigger
V2_EMA_SLOW        = 20     # slow EMA — trend direction
V2_RSI_PERIOD      = 14
V2_RSI_MIN_CE      = 60     # RSI > 60 for CE entry
V2_RSI_MAX_PE      = 40     # RSI < 40 for PE entry
V2_ATR_PERIOD      = 14
V2_REV_ATR_MULT    = 2.0
V2_NO_ENTRY_BEFORE = "09:30"   # start earlier — catch opening momentum
V2_MAX_TRADES      = 2         # allow 2 trades per day (morning + afternoon)
V2_SKIP_THURSDAY   = True      # avoid Nifty weekly expiry day
V2_VIX_MIN         = 15        # India VIX lower bound — below 15 premiums too thin to buy
V2_VIX_MAX         = 30        # raised from 22 — VIX 22-30 still tradeable with good premiums
V2_MORNING_END     = "12:00"   # extended morning window
V2_AFTERNOON_START = "13:30"   # earlier afternoon start
V2_ST_PERIOD       = 7         # Supertrend ATR period
V2_ST_MULT         = 2.0       # Supertrend ATR multiplier
# ─────────────────────────────────────────────────────────────────


def estimate_option_price(spot: float, days_to_expiry: float = 7.0) -> float:
    t = days_to_expiry / 365
    return round(max(spot * ATM_OPTION_IV * np.sqrt(t) * 0.4, 10.0), 2)


def fetch_range_data(start: date, end: date):
    """Fetch ^NSEI 5-min + 1-day data. Retries 3x, clamps to 58-day window."""
    ticker    = yf.Ticker(SYMBOL)
    fetch_end = end + timedelta(days=2)
    earliest  = date.today() - timedelta(days=58)
    if start < earliest:
        start = earliest

    df_5m = pd.DataFrame()
    for attempt in range(3):
        df_5m = ticker.history(start=start, end=fetch_end, interval="5m")
        if not df_5m.empty:
            break
        if attempt < 2:
            _time.sleep(2)

    df_1d = ticker.history(start=start - timedelta(days=30), end=fetch_end, interval="1d")

    if df_5m.empty:
        raise Exception(
            f"No Nifty 50 5-min data from Yahoo Finance for {start} to {end}. "
            "Possible causes: (1) market holiday/weekend, "
            "(2) market hasn't opened yet, "
            "(3) date older than Yahoo's 58-day intraday limit."
        )

    df_5m.index = df_5m.index.tz_convert("Asia/Kolkata")
    if df_1d.index.tz is not None:
        df_1d.index = df_1d.index.tz_convert("Asia/Kolkata")
    return df_5m, df_1d


def _fetch_etf(ticker_sym: str, start: date, end: date, interval: str = "5m") -> pd.DataFrame:
    """Fetch an NSE ETF intraday data. Returns empty DataFrame on failure."""
    try:
        t         = yf.Ticker(ticker_sym)
        fetch_end = end + timedelta(days=2)
        df = t.history(start=start, end=fetch_end, interval=interval)
        if not df.empty:
            df.index = df.index.tz_convert("Asia/Kolkata")
        return df
    except Exception:
        return pd.DataFrame()


def fetch_range_data_angel(start: date, end: date):
    """
    Fetch all V2 data from Angel One Smart API (no 58-day limit).
    Returns (df_nsei_5m, df_1d, df_nbees_5m, df_bnf_5m, df_vix_1d)
    VIX still fetched from Yahoo Finance (daily data, no limit issue).
    """
    from angel_data import fetch_all as _angel_fetch_all
    df_5m, df_1d, df_nbees, df_bnf = _angel_fetch_all(start, end)

    # VIX — Yahoo Finance daily (no 58-day limit on 1d data)
    df_vix = pd.DataFrame()
    try:
        import yfinance as yf
        vix_t  = yf.Ticker("^INDIAVIX")
        fetch_end = end + timedelta(days=2)
        df_vix = vix_t.history(start=start - timedelta(days=10), end=fetch_end, interval="1d")
        if not df_vix.empty and df_vix.index.tz is not None:
            df_vix.index = df_vix.index.tz_convert("Asia/Kolkata")
    except Exception:
        pass

    return df_5m, df_1d, df_nbees, df_bnf, df_vix


def fetch_range_data_v2(start: date, end: date):
    """
    Fetch all data needed for V2 strategy.
    Returns (df_nsei_5m, df_1d, df_nbees_2m, df_bnf_5m, df_vix_1d)
      - df_nbees : NIFTYBEES.NS at 2m — faster signals (real volume)
      - df_bnf   : BANKBEES.NS  at 5m — Bank Nifty alignment confirmation
      - df_vix   : ^INDIAVIX    at 1d — India VIX filter
    """
    df_5m, df_1d = fetch_range_data(start, end)
    df_nbees     = _fetch_etf("NIFTYBEES.NS", start, end, interval="5m")
    df_bnf       = _fetch_etf("BANKBEES.NS",  start, end, interval="5m")

    df_vix = pd.DataFrame()
    try:
        vix_t     = yf.Ticker("^INDIAVIX")
        fetch_end = end + timedelta(days=2)
        df_vix    = vix_t.history(start=start - timedelta(days=10), end=fetch_end, interval="1d")
        if not df_vix.empty and df_vix.index.tz is not None:
            df_vix.index = df_vix.index.tz_convert("Asia/Kolkata")
    except Exception:
        pass

    return df_5m, df_1d, df_nbees, df_bnf, df_vix


# ── Indicator helpers ─────────────────────────────────────────────

def _vwap(df: pd.DataFrame) -> pd.Series:
    tp     = (df["High"] + df["Low"] + df["Close"]) / 3
    cumvol = df["Volume"].cumsum().replace(0, np.nan)
    return (tp * df["Volume"]).cumsum() / cumvol


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1).fillna(c.iloc[0])
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _supertrend(df: pd.DataFrame, period: int = 7, multiplier: float = 2.0) -> pd.Series:
    """Supertrend indicator. Returns +1 (bullish) / -1 (bearish) per candle."""
    atr   = _atr(df, period)
    hl2   = (df["High"] + df["Low"]) / 2
    close = df["Close"].values
    bu    = (hl2 + multiplier * atr).values   # basic upper
    bl    = (hl2 - multiplier * atr).values   # basic lower

    n     = len(df)
    fu    = bu.copy()   # final upper
    fl    = bl.copy()   # final lower
    dirn  = np.ones(n, dtype=int)

    for i in range(1, n):
        fu[i] = bu[i] if bu[i] < fu[i-1] or close[i-1] > fu[i-1] else fu[i-1]
        fl[i] = bl[i] if bl[i] > fl[i-1] or close[i-1] < fl[i-1] else fl[i-1]
        if   close[i] > fu[i]: dirn[i] =  1
        elif close[i] < fl[i]: dirn[i] = -1
        else:                   dirn[i] = dirn[i-1]

    return pd.Series(dirn, index=df.index)


def _trade_record(position, exit_time, exit_spot, opt_pnl_per, reason, qty=None):
    q        = qty if qty is not None else position["qty"]
    opt_exit = round(position["entry_option_price"] + opt_pnl_per, 2)
    strike   = round(position["entry_spot"] / 50) * 50
    return {
        "time"        : position["entry_time"],
        "exit_time"   : exit_time,
        "index"       : "Nifty 50",
        "symbol"      : f"NIFTY {strike} {position['type']}",
        "side"        : position["type"],
        "strike"      : strike,
        "entry"       : round(position["entry_option_price"], 2),
        "exit"        : max(opt_exit, 0),
        "entry_spot"  : round(position["entry_spot"], 2),
        "exit_spot"   : round(exit_spot, 2),
        "qty_bought"  : position.get("initial_qty", position["qty"]),  # total lots bought at entry
        "qty"         : q,                                              # lots sold in this exit
        "pnl"         : round(opt_pnl_per * q, 2),
        "reason"      : reason,
    }


# ── Insights ──────────────────────────────────────────────────────

def generate_insights(result: dict) -> list:
    insights = []
    trades   = result.get("trades", [])
    market   = result.get("market", {})

    prev_close = market.get("prev_close", 0)
    open_p     = market.get("open", 0)
    close_p    = market.get("close", 0)
    high_p     = market.get("high", 0)
    low_p      = market.get("low", 0)

    if prev_close:
        gap_pts = open_p - prev_close
        gap_pct = gap_pts / prev_close * 100
        day_pct = (close_p - prev_close) / prev_close * 100

        if abs(gap_pct) >= 0.3:
            d = "gap-up" if gap_pts > 0 else "gap-down"
            insights.append(f"Nifty 50 {d} opened {abs(gap_pts):.0f} pts ({abs(gap_pct):.1f}%) from prev close.")

        if day_pct > 0.8:
            insights.append(f"Strong bullish trend — gained {day_pct:.1f}% on the day.")
        elif day_pct < -0.8:
            insights.append(f"Strong bearish trend — fell {abs(day_pct):.1f}% on the day.")
        else:
            insights.append(f"Range-bound day — intraday range {high_p - low_p:.0f} pts, net {day_pct:+.1f}%.")

    if not trades:
        insights.append("No V2 signal: volume surge + dual EMA + RSI + BNF + Supertrend conditions not met.")
    else:
        real_trades = [t for t in trades if t["reason"] != "PARTIAL_TP"]
        wins   = [t for t in real_trades if t["pnl"] > 0]
        losses = [t for t in real_trades if t["pnl"] <= 0]
        ce_n   = sum(1 for t in trades if t["side"] == "CE")
        pe_n   = sum(1 for t in trades if t["side"] == "PE")

        if ce_n > 0 and pe_n == 0:
            insights.append("Bullish setup — CE entry on volume surge, dual EMA + RSI > 60 + BNF aligned.")
        elif pe_n > 0 and ce_n == 0:
            insights.append("Bearish setup — PE entry on volume surge, dual EMA + RSI < 40 + BNF aligned.")

        if any(t["reason"] == "PARTIAL_TP" for t in trades):
            insights.append("Partial exit at +20%: exited 1 lot, remainder runs to +40% target.")

        if wins and not losses:
            reasons = list(set(t["reason"] for t in wins))
            insights.append(f"Clean win — exited via {', '.join(reasons)}.")
        elif losses and not wins:
            insights.append("Signal fired but move reversed — stopped out.")
        else:
            insights.append(f"{len(wins)} winner(s) vs {len(losses)} stop-out(s).")

        if any(t["reason"] == "TRAIL_EXIT" for t in trades):
            insights.append("Trailing stop protected gains after partial exit.")

    return insights


# ── Main simulator ────────────────────────────────────────────────

def simulate_day(target_date: date,
                 df_5m_all:  pd.DataFrame,
                 df_1d_all:  pd.DataFrame,
                 df_nbees:   pd.DataFrame = None,
                 df_bnf:     pd.DataFrame = None,
                 df_vix:     pd.DataFrame = None):
    """
    Simulate V2 strategy for one trading day.
    All 11 improvements active: dual EMA, RSI, partial exit,
    trail floor, Thursday skip, BNF alignment, trail trigger,
    India VIX filter, time window, Supertrend, 2m signals.
    """
    # ── Thursday skip (weekly expiry) ────────────────────────────
    if V2_SKIP_THURSDAY and target_date.weekday() == 3:
        return _no_trade_result(target_date, df_5m_all, df_1d_all,
                                note="Thursday — weekly expiry skip")

    # ── India VIX filter ─────────────────────────────────────────
    if df_vix is not None and not df_vix.empty:
        vix_rows = df_vix[df_vix.index.date <= target_date]
        if not vix_rows.empty:
            today_vix = float(vix_rows.iloc[-1]["Close"])
            if not (V2_VIX_MIN <= today_vix <= V2_VIX_MAX):
                return _no_trade_result(
                    target_date, df_5m_all, df_1d_all,
                    note=f"VIX {today_vix:.1f} outside range {V2_VIX_MIN}-{V2_VIX_MAX} — skip"
                )

    nifty_day = df_5m_all[df_5m_all.index.date == target_date].between_time("09:15", "15:30")
    if len(nifty_day) < V2_EMA_SLOW + 2:
        return None

    prev_rows = df_1d_all[df_1d_all.index.date < target_date]
    if prev_rows.empty:
        return None
    prev_close = float(prev_rows.iloc[-1]["Close"])

    # ── Signal source: NIFTYBEES (real volume) ───────────────────
    def _align_etf(df_etf):
        if df_etf is None or df_etf.empty:
            return nifty_day
        d = df_etf[df_etf.index.date == target_date].between_time("09:15", "15:30")
        d = d.reindex(nifty_day.index, method="nearest", tolerance=pd.Timedelta("3min"))
        return d if (not d.empty and d["Volume"].sum() > 0) else nifty_day

    sday  = _align_etf(df_nbees)
    bnf   = _align_etf(df_bnf)   # Bank Nifty ETF for alignment

    # ── Indicators (NIFTYBEES scale) ─────────────────────────────
    vwap_s    = _vwap(sday)
    ema_fast  = sday["Close"].ewm(span=V2_EMA_FAST,  adjust=False).mean()
    ema_slow  = sday["Close"].ewm(span=V2_EMA_SLOW,  adjust=False).mean()
    vol_ma    = sday["Volume"].rolling(20).mean()
    atr_s     = _atr(sday, V2_ATR_PERIOD)
    rsi_s     = _rsi(sday["Close"], V2_RSI_PERIOD)
    st_s      = _supertrend(sday, V2_ST_PERIOD, V2_ST_MULT)

    # ── Bank Nifty VWAP (BANKBEES scale) ─────────────────────────
    has_bnf   = (bnf is not nifty_day)
    bnf_vwap  = _vwap(bnf) if has_bnf else None

    # ── Daily bias from first candle open ────────────────────────
    day_bias  = "CE" if float(nifty_day.iloc[0]["Open"]) >= prev_close else "PE"

    balance     = float(INITIAL_BALANCE)
    trades      = []
    daily_pnl   = 0.0
    trade_count = 0
    last_signal = None

    position = {
        "active"      : False, "type": None,
        "entry_spot"  : 0.0,   "entry_option_price": 0.0,
        "entry_time"  : None,  "qty": QTY,
        "trail_on"    : False, "partial_done": False,
        "sl_warn_count": 0,   # consecutive candle closes in SL warning zone
    }

    nifty_candles = list(nifty_day.iterrows())
    sig_candles   = list(sday.iterrows())
    bnf_candles   = list(bnf.iterrows()) if has_bnf else None

    for i in range(1, len(nifty_candles)):
        ts, nrow = nifty_candles[i]
        _,  srow = sig_candles[i]
        time_str = ts.strftime("%H:%M")

        spot_cl = float(nrow["Close"])

        # NIFTYBEES signal values
        cl  = float(srow["Close"])
        op  = float(srow["Open"])
        hi  = float(srow["High"])
        lo  = float(srow["Low"])
        vol = float(srow["Volume"])

        vw   = float(vwap_s.iloc[i])
        ef   = float(ema_fast.iloc[i])
        es   = float(ema_slow.iloc[i])
        vm   = float(vol_ma.iloc[i])  if not np.isnan(vol_ma.iloc[i])  else 0.0
        at   = float(atr_s.iloc[i])   if not np.isnan(atr_s.iloc[i])   else 0.0
        rsi  = float(rsi_s.iloc[i])   if not np.isnan(rsi_s.iloc[i])   else 50.0
        st   = int(st_s.iloc[i])

        # Bank Nifty alignment
        if has_bnf and bnf_vwap is not None:
            bnf_cl   = float(bnf_candles[i][1]["Close"])
            bnf_vw   = float(bnf_vwap.iloc[i])
            bnf_bull = bnf_cl > bnf_vw
            bnf_bear = bnf_cl < bnf_vw
        else:
            bnf_bull = bnf_bear = True   # skip check if no data

        # ── EOD square-off ────────────────────────────────────────
        if time_str >= SQUAREOFF_TIME:
            if position["active"]:
                sc      = spot_cl - position["entry_spot"]
                pnl_pu  = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
                pnl     = pnl_pu * position["qty"]
                balance += pnl; daily_pnl += pnl
                trades.append(_trade_record(position, time_str, spot_cl, pnl_pu, "EOD_SQUAREOFF"))
                trade_count     += 1
                position["active"] = False
            break

        # ── Manage open position ──────────────────────────────────
        if position["active"]:
            sc      = spot_cl - position["entry_spot"]
            pnl_pu  = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
            opt_pct = pnl_pu / position["entry_option_price"]

            # Activate trailing stop once up V2_TRAIL_TRIGGER
            if not position["trail_on"] and opt_pct >= V2_TRAIL_TRIGGER:
                position["trail_on"] = True

            # ── Partial exit at +10% (only when holding 2+ lots) ────
            # Minimum tradeable unit on NSE is 1 full lot (LOT_SIZE units).
            # If only 1 lot is held, skip partial — exit via 1-lot TARGET logic.
            if (not position["partial_done"]
                    and opt_pct >= V2_PARTIAL_PCT
                    and position["qty"] >= LOT_SIZE * 2):
                # Exit exactly 1 lot; pin to exact +10% price so P&L is not
                # inflated if TARGET also triggers in the same candle.
                p_pnl_pu = position["entry_option_price"] * V2_PARTIAL_PCT
                p_spot   = (position["entry_spot"] + p_pnl_pu / 0.5
                            if position["type"] == "CE"
                            else position["entry_spot"] - p_pnl_pu / 0.5)
                partial_pnl = p_pnl_pu * LOT_SIZE
                balance    += partial_pnl; daily_pnl += partial_pnl
                trades.append(_trade_record(position, time_str, p_spot, p_pnl_pu, "PARTIAL_TP", qty=LOT_SIZE))
                position["qty"]          -= LOT_SIZE
                position["partial_done"]  = True

            # After partial, SL steps to breakeven (trail_floor = 0%)
            trail_floor = V2_TRAIL_FLOOR if position["partial_done"] else 0.0

            # ── TARGET condition — differs for 1-lot vs 2-lot ───────────
            is_one_lot = position["initial_qty"] == LOT_SIZE
            abs_pnl    = pnl_pu * position["qty"]
            tp_hit = ((opt_pct >= V2_1LOT_TP_PCT or abs_pnl >= V2_1LOT_TP_RUPEES)
                      if is_one_lot else opt_pct >= V2_TP_OPTION_PCT)

            # ── SL logic: 2-close confirmation for boundary zone ────────
            if opt_pct <= -V2_SL_OPTION_PCT:
                hard_action = "SL"                       # immediate — no ambiguity
                position["sl_warn_count"] = 0
            elif opt_pct <= -V2_SL_WARN_PCT:
                position["sl_warn_count"] += 1
                hard_action = "SL" if position["sl_warn_count"] >= 2 else "HOLD"
            else:
                position["sl_warn_count"] = 0            # recovered — reset counter
                hard_action = "TARGET" if tp_hit else "HOLD"

            if hard_action != "SL":
                hard_action = "TARGET" if tp_hit else "HOLD"

            trail_exit  = position["trail_on"] and opt_pct <= trail_floor
            ema_exit    = ((position["type"] == "CE" and cl < ef) or
                           (position["type"] == "PE" and cl > ef))
            rev_exit    = (at > 0 and (hi - lo) > at * V2_REV_ATR_MULT and
                           ((position["type"] == "CE" and cl < op) or
                            (position["type"] == "PE" and cl > op)))

            exit_reason = None
            if hard_action in ("SL", "TARGET"):
                exit_reason = hard_action
            elif trail_exit:
                exit_reason = "TRAIL_EXIT"
            elif ema_exit:
                exit_reason = "EMA_EXIT"
            elif rev_exit:
                exit_reason = "REV_EXIT"

            if exit_reason:
                # Pin TARGET/SL exits to their exact threshold prices too,
                # so the recorded opt-out price matches the stated exit rule.
                if exit_reason == "TARGET":
                    if is_one_lot:
                        # Pin to whichever threshold triggered first
                        pct_pnl  = position["entry_option_price"] * V2_1LOT_TP_PCT
                        rupi_pnl = V2_1LOT_TP_RUPEES / position["qty"]
                        e_pnl_pu = min(pct_pnl, rupi_pnl)
                    else:
                        e_pnl_pu = position["entry_option_price"] * V2_TP_OPTION_PCT
                    e_spot   = (position["entry_spot"] + e_pnl_pu / 0.5
                                if position["type"] == "CE"
                                else position["entry_spot"] - e_pnl_pu / 0.5)
                elif exit_reason == "SL":
                    e_pnl_pu = -position["entry_option_price"] * V2_SL_OPTION_PCT
                    e_spot   = (position["entry_spot"] + e_pnl_pu / 0.5
                                if position["type"] == "CE"
                                else position["entry_spot"] - e_pnl_pu / 0.5)
                else:
                    e_pnl_pu = pnl_pu
                    e_spot   = spot_cl
                pnl      = e_pnl_pu * position["qty"]
                balance += pnl; daily_pnl += pnl
                trades.append(_trade_record(position, time_str, e_spot, e_pnl_pu, exit_reason))
                trade_count     += 1
                position["active"] = False
                last_signal        = None

        # ── Entry gate ────────────────────────────────────────────
        in_morning   = V2_NO_ENTRY_BEFORE <= time_str <= V2_MORNING_END
        in_afternoon = V2_AFTERNOON_START  <= time_str <  NO_ENTRY_AFTER
        in_window    = in_morning or in_afternoon

        if (not position["active"]
                and in_window
                and trade_count < V2_MAX_TRADES
                and daily_pnl > MAX_DAILY_LOSS
                and daily_pnl < DAILY_PROFIT_TARGET
                and vm > 0):

            vol_surge = vol > vm * V2_VOL_SURGE_MULT

            # All conditions: VWAP + dual EMA + volume + RSI + BNF + Supertrend + bias
            raw_buy  = (cl > vw and cl > ef and cl > es and cl > op
                        and vol_surge and rsi > V2_RSI_MIN_CE
                        and bnf_bull and st == 1)
            raw_sell = (cl < vw and cl < ef and cl < es and cl < op
                        and vol_surge and rsi < V2_RSI_MAX_PE
                        and bnf_bear and st == -1)

            signal = None
            if raw_buy  and last_signal != "buy":
                signal = "BUY_CE"; last_signal = "buy"
            elif raw_sell and last_signal != "sell":
                signal = "BUY_PE"; last_signal = "sell"

            if signal:
                opt_type        = "CE" if signal == "BUY_CE" else "PE"
                dte             = max(1, (3 - target_date.weekday()) % 7 or 7)
                entry_opt_price = estimate_option_price(spot_cl, dte)
                if entry_opt_price * QTY <= balance:
                    position.update({
                        "active"            : True,  "type": opt_type,
                        "entry_spot"        : spot_cl, "entry_option_price": entry_opt_price,
                        "entry_time"        : time_str, "qty": QTY,
                        "initial_qty"       : QTY,   # locked at entry, never changes
                        "trail_on"          : False, "partial_done": False,
                        "sl_warn_count"     : 0,
                    })

    result = _build_result(target_date, nifty_day, prev_close, daily_pnl,
                           balance, trades, trade_count)
    return result


def _no_trade_result(target_date, df_5m_all, df_1d_all, note=""):
    """Return a zero-trade result for days we deliberately skip."""
    nifty_day = df_5m_all[df_5m_all.index.date == target_date].between_time("09:15", "15:30")
    if nifty_day.empty:
        return None
    prev_rows = df_1d_all[df_1d_all.index.date < target_date]
    if prev_rows.empty:
        return None
    prev_close = float(prev_rows.iloc[-1]["Close"])
    result = _build_result(target_date, nifty_day, prev_close, 0.0,
                           float(INITIAL_BALANCE), [], 0)
    if note:
        result["insights"].insert(0, note)
    return result


def _build_result(target_date, nifty_day, prev_close, daily_pnl, balance, trades, trade_count):
    day_open  = float(nifty_day.iloc[0]["Open"])
    day_high  = float(nifty_day["High"].max())
    day_low   = float(nifty_day["Low"].min())
    day_close = float(nifty_day.iloc[-1]["Close"])
    result = {
        "date"          : str(target_date),
        "daily_pnl"     : round(daily_pnl, 2),
        "final_balance" : round(balance, 2),
        "trade_count"   : trade_count,
        "win_count"     : sum(1 for t in trades if t["pnl"] > 0 and t["reason"] != "PARTIAL_TP"),
        "trades"        : trades,
        "market"        : {
            "open": round(day_open, 2), "high": round(day_high, 2),
            "low":  round(day_low, 2),  "close": round(day_close, 2),
            "prev_close": round(prev_close, 2),
        },
    }
    result["insights"] = generate_insights(result)
    return result


# ── Range runner ──────────────────────────────────────────────────

def run_range(start: date, end: date) -> list:
    """Run V2 strategy for every trading day in [start, end]."""
    print(f"Fetching Nifty + NIFTYBEES + BANKBEES + VIX data {start} to {end}...")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_v2(start, end)
    results = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result = simulate_day(current, df_5m, df_1d,
                                  df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
            if result:
                results.append(result)
                tag = "SKIP" if result["trade_count"] == 0 and "expiry" in " ".join(result.get("insights", [])) else ""
                status = "+" if result["daily_pnl"] >= 0 else ""
                print(f"  {current}  PnL={status}{result['daily_pnl']:,.0f}  "
                      f"Trades={result['trade_count']}  {tag}")
        current += timedelta(days=1)
    return results


# ── CLI single-date runner ────────────────────────────────────────

def run_backtest(target: date = None):
    target = target or date.today()
    print(f"\n{'='*55}")
    print(f"  Nifty 50 V2 Backtest — {target.strftime('%d %b %Y')}")
    print(f"  Starting Balance: Rs.{INITIAL_BALANCE:,.0f}")
    print(f"{'='*55}\n")

    start = target - timedelta(days=40)
    df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_v2(start, target)

    result = simulate_day(target, df_5m, df_1d, df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
    if not result:
        print("No data found for this date.")
        return

    print(f"Previous close : Rs.{result['market']['prev_close']:,.2f}")
    for t in result["trades"]:
        sign = "+" if t["pnl"] >= 0 else ""
        print(f"  [{t['time']}] {t['reason']!s:<12} {t['side']} "
              f"qty={t['qty']}  Spot={t['entry_spot']:.0f}->{t['exit_spot']:.0f}  "
              f"PnL={sign}Rs.{t['pnl']:.2f}")

    total_pnl = result["daily_pnl"]
    print(f"\n{'='*55}")
    print(f"  Trades : {result['trade_count']}  |  P&L : Rs.{total_pnl:,.2f}")
    for ins in result["insights"]:
        print(f"    - {ins}")
    print(f"{'='*55}\n")

    state = {
        "backtest"       : True,
        "backtest_date"  : target.strftime("%d %b %Y"),
        "initial_balance": INITIAL_BALANCE,
        "final_balance"  : result["final_balance"],
        "market"         : result["market"],
        "gann"           : {},
        "risk"           : {
            "daily_pnl"  : result["daily_pnl"],
            "trade_count": result["trade_count"],
            "trades"     : result["trades"],
        },
        "position": {
            "active": False, "type": None, "symbol": None,
            "token": None, "entry_price": 0.0, "entry_spot": 0.0, "qty": QTY,
        },
    }
    os.makedirs("logs", exist_ok=True)
    with open("logs/state.json", "w") as f:
        json.dump(state, f, indent=2, default=str)
    print("Dashboard state updated -> http://localhost:5000\n")


if __name__ == "__main__":
    run_backtest()
