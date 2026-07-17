"""
Backtest engine: V2 Volume/VWAP/EMA strategy on Nifty 50 5-min candles.
Signal source: NIFTYBEES.NS (5m) + BANKBEES.NS (5m) + India VIX filter.
P&L source: ^NSEI spot price.

Improvements (v2.1):
  1. Dual EMA (9 fast + 20 slow) — both must agree for entry
  2. RSI(14) filter — >60 for CE, <40 for PE
  3. Partial exit — 50% qty exits at +20%, rest runs to +40% TP
  4. Trail stop floor raised to +5% (not breakeven)
  5. Skip Tuesday afternoon (weekly expiry — high theta decay)
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
QTY                  = LOT_SIZE   # default: 1 lot — matches live trading_config.json ("lots": 1)
ATM_OPTION_IV        = 0.15
NO_ENTRY_AFTER       = "14:50"
SQUAREOFF_TIME       = "15:15"
MAX_DAILY_LOSS       = -8000
DAILY_PROFIT_TARGET  = 6000

# ── V2 Strategy constants ─────────────────────────────────────────
V2_TP_OPTION_PCT   = 0.20   # 2-lot: remaining lot hard TP at +20%
V2_SL_OPTION_PCT   = 0.20   # premium hard stop — immediate exit, no confirmation needed
V2_SL_WARN_PCT     = 0.17   # premium warning zone — 2 polls needed (slow bleed filter)
V2_SIGNAL_EXIT_LOSS = 0.05  # A+B exit: min loss before counter-signal/VWAP-flip triggers
V2_SPOT_SL_WARN    = 50     # spot warning zone — 2 polls needed (small move, wait and see)
V2_SPOT_SL_HARD    = 80     # spot hard stop — immediate exit (market genuinely reversed)
V2_PARTIAL_PCT     = 0.10   # 2-lot: partial exit 1 lot at +10%
V2_TRAIL_TRIGGER   = 0.10   # activate trail at +10%
V2_TRAIL_FLOOR     = 0.00   # after partial: SL steps to breakeven (0%)
V2_TRAIL_LOCK_TRIGGER = 0.15   # only start ratcheting the floor once a trade has been a genuinely
                                # big winner (peak >= this) — below it, EMA_EXIT stays the primary
                                # exit and is left alone (moderate winners keep running as before).
                                # Backtested Jul 2026 (60 days): trigger/giveback swept 15-25%/6-10%;
                                # this combo protects big spikes (like the Jul-17 +18% -> -165 trade)
                                # at a small average cost (~-Rs.600/44 days) vs. no lock at all.
V2_TRAIL_LOCK_GIVEBACK = 0.08  # once past the lock trigger, floor = peak_pct - this many points,
                                # so a big spike can't fully round-trip back to breakeven/loss
V2_1LOT_TP_PCT     = 0.10   # 1-lot: exit at +10% option gain …
V2_1LOT_TP_RUPEES  = 1100   # … or ₹1,100 absolute P&L — whichever comes first (only used if V2_1LOT_HARD_TP)
V2_1LOT_HARD_TP    = False  # if False (validated default): 1-lot skips the hard cap above and
                             # lets the trailing stop (V2_TRAIL_TRIGGER/trail_floor_1lot) manage
                             # the exit instead — backtested Jan-Jul 2026: -Rs.34,059 -> +Rs.1,046
                             # over 138 days by letting winners run past +10% instead of capping them.
V2_VOL_SURGE_MULT  = 0.9    # volume > 0.9× 20-bar avg
V2_EMA_FAST        = 9      # fast EMA — entry filter + exit trigger
V2_EMA_SLOW        = 20     # slow EMA — trend direction
V2_RSI_PERIOD      = 14
V2_RSI_MIN_CE      = 60     # RSI > 60 for CE entry
V2_RSI_MAX_PE      = 40     # RSI < 40 for PE entry
V2_ATR_PERIOD      = 14
V2_REV_ATR_MULT    = 2.0
V2_NO_ENTRY_BEFORE = "10:15"   # skip noisy opening; warm indicators ready by 10:15
V2_MAX_TRADES      = 2         # allow 2 trades per day (morning + afternoon)
V2_EXPIRY_WEEKDAY  = 1         # Tuesday = Nifty weekly expiry (morning only, afternoon blocked)
V2_VIX_MIN         = 13        # India VIX lower bound — below 13 premiums too thin to buy
V2_VIX_MAX         = 30        # raised from 22 — VIX 22-30 still tradeable with good premiums
V2_MORNING_END     = "12:00"   # morning session end
V2_AFTERNOON_START = "13:30"   # afternoon session start (lunch 12:00-13:30 blocked)
V2_ST_PERIOD       = 7         # Supertrend ATR period
V2_ST_MULT         = 2.0       # Supertrend ATR multiplier
V2_MAX_FROM_OPEN_PCT = 0.5     # skip entry if price already moved >0.5% from day open
V2_DIVERGENCE_LOOKBACK = 5     # candles to look back for RSI-divergence (exhaustion) check
V2_PULLBACK_LOOKBACK = 6       # candles to look back for a pullback-to-EMA9 touch
V2_PULLBACK_TOL_PCT = 0.001    # 0.1% tolerance band around EMA9 counted as a "touch"

# ── V4 scoring-gate constants (entry_mode="v4") ───────────────────
V2_CROSS_LOOKBACK  = 3         # candles to look back for a fresh EMA9/EMA20 cross
V2_CONFIRM_MIN     = 3         # confirmations required out of 5 (RSI, ST, volume, BNF, candle color)
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

    # VIX — historical daily data from Yahoo Finance (correct per-day values)
    df_vix = pd.DataFrame()
    try:
        vix_t  = yf.Ticker("^INDIAVIX")
        df_vix = vix_t.history(start=start - timedelta(days=10),
                               end=end + timedelta(days=2), interval="1d")
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


def _fresh_cross(ema_fast_s: pd.Series, ema_slow_s: pd.Series, i: int,
                  lookback: int, direction: str) -> bool:
    """True if EMA9 crossed EMA20 in the given direction within `lookback` candles ending at i."""
    lo = max(1, i - lookback + 1)
    for j in range(lo, i + 1):
        prev_up = ema_fast_s.iloc[j - 1] > ema_slow_s.iloc[j - 1]
        curr_up = ema_fast_s.iloc[j] > ema_slow_s.iloc[j]
        if direction == "up" and curr_up and not prev_up:
            return True
        if direction == "down" and not curr_up and prev_up:
            return True
    return False


def _recent_pullback(low_s: pd.Series, high_s: pd.Series, ema_fast_s: pd.Series,
                      i: int, lookback: int, tol_pct: float, direction: str) -> bool:
    """
    True if price touched within tol_pct of EMA9 (a pullback/retest) at some
    point in the `lookback` candles strictly before i — i.e. the current
    breakout candle is a bounce off a retest, not the very first candle to
    ever clear EMA9. Simplified proxy for a full pullback-then-confirm entry.
    """
    lo = max(0, i - lookback)
    for j in range(lo, i):
        ema = ema_fast_s.iloc[j]
        tol = ema * tol_pct
        if direction == "up" and low_s.iloc[j] <= ema + tol:
            return True
        if direction == "down" and high_s.iloc[j] >= ema - tol:
            return True
    return False


def compute_v2_signal(cl, op, vw, ef, es, vm, vol, rsi, st, bnf_bull, bnf_bear,
                       cross_recent_ce, cross_recent_pe, entry_mode="v2") -> tuple:
    """
    Shared entry-signal logic used by both simulate_day() and live_trader's
    _check_signal() — the single source of truth so backtest results stay
    representative of live behavior.

    entry_mode="v2" : legacy all-conditions-must-agree AND-gate.
    entry_mode="v4" : mandatory (fresh EMA cross + VWAP side) + majority-vote
                      confirmation score (RSI, Supertrend, volume, BNF, candle color).

    Returns (raw_buy, raw_sell, debug: dict) — debug feeds dashboard filter_reason.
    """
    vol_surge = vm > 0 and vol > vm * V2_VOL_SURGE_MULT
    debug = {}

    if entry_mode != "v4":
        raw_buy  = (cl > vw and cl > ef and cl > es and cl > op
                    and vol_surge and rsi > V2_RSI_MIN_CE and bnf_bull and st == 1)
        raw_sell = (cl < vw and cl < ef and cl < es and cl < op
                    and vol_surge and rsi < V2_RSI_MAX_PE and bnf_bear and st == -1)
        return raw_buy, raw_sell, debug

    # ── v4: mandatory timing gate + confirmation score ────────────
    confirms_ce = {
        "rsi"    : rsi > V2_RSI_MIN_CE,
        "st"     : st == 1,
        "volume" : vol_surge,
        "bnf"    : bnf_bull,
        "candle" : cl > op,
    }
    confirms_pe = {
        "rsi"    : rsi < V2_RSI_MAX_PE,
        "st"     : st == -1,
        "volume" : vol_surge,
        "bnf"    : bnf_bear,
        "candle" : cl < op,
    }
    score_ce = sum(confirms_ce.values())
    score_pe = sum(confirms_pe.values())

    raw_buy  = cross_recent_ce and (cl > vw) and score_ce >= V2_CONFIRM_MIN
    raw_sell = cross_recent_pe and (cl < vw) and score_pe >= V2_CONFIRM_MIN

    debug = {
        "cross_ce": cross_recent_ce, "cross_pe": cross_recent_pe,
        "vwap_ce": cl > vw, "vwap_pe": cl < vw,
        "score_ce": score_ce, "score_pe": score_pe,
        "confirms_ce": confirms_ce, "confirms_pe": confirms_pe,
    }
    return raw_buy, raw_sell, debug


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
                 df_vix:     pd.DataFrame = None,
                 entry_mode: str = "v2",
                 signal_aware_exit: bool = False,
                 rsi_floor_pe: float = 0,
                 rsi_ceil_ce: float = 100,
                 max_from_open_pct: float = 0,
                 ema_exit_confirm: int = 1,
                 ema_exit_min_loss: float = 0.0,
                 trail_trigger: float = None,
                 trail_floor_1lot: float = 0.0,
                 trail_lock_trigger: float = None,
                 trail_lock_giveback: float = None,
                 require_vol_surge: bool = False,
                 require_supertrend: bool = True,
                 require_bnf: bool = True,
                 require_candle_color: bool = True,
                 require_no_divergence: bool = False,
                 require_pullback: bool = False):
    """
    Simulate V2 strategy for one trading day.
    All 11 improvements active: dual EMA, RSI, partial exit,
    trail floor, Tuesday expiry skip, BNF alignment, trail trigger,
    India VIX filter, time window, Supertrend, 2m signals.
    """
    # ── Expiry day (Tuesday): morning only — afternoon theta too high ─
    is_thursday = target_date.weekday() == V2_EXPIRY_WEEKDAY

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

    prev_rows = df_1d_all[df_1d_all.index.date < target_date] if not df_1d_all.empty else pd.DataFrame()
    if prev_rows.empty:
        prev_5m = df_5m_all[df_5m_all.index.date < target_date]
        if prev_5m.empty:
            return None
        prev_close = float(prev_5m.iloc[-1]["Close"])
    else:
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
    day_open_s = float(sday.iloc[0]["Open"]) if not sday.empty else 0.0

    # ── Indicators (NIFTYBEES scale, with prev-day warm-up) ─────
    # VWAP and vol_ma reset each day → today-only. EMA/RSI/ST need
    # historical data to be accurate on the first candle of the day.
    vwap_s = _vwap(sday)

    _has_nbees = (df_nbees is not None and not df_nbees.empty
                  and isinstance(df_nbees.index, pd.DatetimeIndex)
                  and sday is not nifty_day)

    if _has_nbees:
        _prev     = df_nbees[df_nbees.index.date < target_date].between_time("09:15", "15:30").tail(30)
        _today_r  = df_nbees[df_nbees.index.date == target_date].between_time("09:15", "15:30")
        _warm     = pd.concat([_prev, _today_r]) if not _prev.empty else _today_r
        _n        = len(_prev)

        def _slice(s):
            part = s.iloc[_n: _n + len(sday)]
            if len(part) != len(sday):
                return s.iloc[-len(sday):].set_axis(sday.index)
            return pd.Series(part.values, index=sday.index)

        ema_fast = _slice(_warm["Close"].ewm(span=V2_EMA_FAST, adjust=False).mean())
        ema_slow = _slice(_warm["Close"].ewm(span=V2_EMA_SLOW, adjust=False).mean())
        rsi_s    = _slice(_rsi(_warm["Close"], V2_RSI_PERIOD))
        st_s     = _slice(_supertrend(_warm, V2_ST_PERIOD, V2_ST_MULT))
        vol_ma   = _slice(_warm["Volume"].rolling(20, min_periods=5).mean())
    else:
        ema_fast = sday["Close"].ewm(span=V2_EMA_FAST, adjust=False).mean()
        ema_slow = sday["Close"].ewm(span=V2_EMA_SLOW, adjust=False).mean()
        rsi_s    = _rsi(sday["Close"], V2_RSI_PERIOD)
        st_s     = _supertrend(sday, V2_ST_PERIOD, V2_ST_MULT)
        vol_ma   = sday["Volume"].rolling(20, min_periods=5).mean()

    atr_s = _atr(sday, V2_ATR_PERIOD)

    # ── RSI divergence (exhaustion) check ─────────────────────────
    # New price low without RSI also making a new low (or new high without
    # RSI making a new high) means the move is losing momentum even as
    # price extends — a standard "this move is exhausted" signal.
    prior_low_close  = sday["Close"].rolling(V2_DIVERGENCE_LOOKBACK, min_periods=1).min().shift(1)
    prior_low_rsi    = rsi_s.rolling(V2_DIVERGENCE_LOOKBACK, min_periods=1).min().shift(1)
    prior_high_close = sday["Close"].rolling(V2_DIVERGENCE_LOOKBACK, min_periods=1).max().shift(1)
    prior_high_rsi   = rsi_s.rolling(V2_DIVERGENCE_LOOKBACK, min_periods=1).max().shift(1)

    # ── Bank Nifty VWAP (BANKBEES scale) ─────────────────────────
    has_bnf   = (bnf is not nifty_day)
    bnf_vwap  = _vwap(bnf) if has_bnf else None

    balance     = float(INITIAL_BALANCE)
    trades      = []
    daily_pnl   = 0.0
    trade_count = 0
    last_signal = None

    position = {
        "active"      : False, "type": None,
        "entry_spot"  : 0.0,   "entry_option_price": 0.0,
        "entry_time"  : None,  "qty": QTY,
        "trail_on"    : False, "partial_done": False, "trail_peak_pct": 0.0,
        "sl_warn_count": 0,   # consecutive candle closes in SL warning zone
        "ema_warn_count": 0,  # consecutive candle closes back through EMA9
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

        # ── Compute raw signal for this candle (used by both exit + entry) ──
        vol_surge = vol > vm * (1.1 if entry_mode == "v14" else V2_VOL_SURGE_MULT)
        if entry_mode == "v14":
            raw_buy  = cl > vw and cl > es and cl > op and vol_surge
            raw_sell = cl < vw and cl < es and cl < op and vol_surge
        elif entry_mode == "v4":
            cross_recent_ce = _fresh_cross(ema_fast, ema_slow, i, V2_CROSS_LOOKBACK, "up")
            cross_recent_pe = _fresh_cross(ema_fast, ema_slow, i, V2_CROSS_LOOKBACK, "down")
            raw_buy, raw_sell, _ = compute_v2_signal(
                cl, op, vw, ef, es, vm, vol, rsi, st, bnf_bull, bnf_bear,
                cross_recent_ce, cross_recent_pe, entry_mode="v4")
        else:
            vol_ok    = (vol_surge if require_vol_surge else True)
            st_ok_ce  = (st == 1  if require_supertrend else True)
            st_ok_pe  = (st == -1 if require_supertrend else True)
            bnf_ok_ce = (bnf_bull if require_bnf else True)
            bnf_ok_pe = (bnf_bear if require_bnf else True)
            candle_ok_ce = (cl > op if require_candle_color else True)
            candle_ok_pe = (cl < op if require_candle_color else True)
            raw_buy  = (cl > vw and cl > ef and cl > es and candle_ok_ce
                        and vol_ok and rsi > V2_RSI_MIN_CE and rsi <= rsi_ceil_ce
                        and bnf_ok_ce and st_ok_ce)
            raw_sell = (cl < vw and cl < ef and cl < es and candle_ok_pe
                        and vol_ok and rsi < V2_RSI_MAX_PE and rsi >= rsi_floor_pe
                        and bnf_ok_pe and st_ok_pe)

            if require_no_divergence:
                plc, plr = prior_low_close.iloc[i], prior_low_rsi.iloc[i]
                phc, phr = prior_high_close.iloc[i], prior_high_rsi.iloc[i]
                # New price low but RSI didn't also make a new low -> down-move exhausted, skip PE
                if raw_sell and not np.isnan(plc) and not np.isnan(plr) and cl < plc and rsi > plr:
                    raw_sell = False
                # New price high but RSI didn't also make a new high -> up-move exhausted, skip CE
                if raw_buy and not np.isnan(phc) and not np.isnan(phr) and cl > phc and rsi < phr:
                    raw_buy = False

            if require_pullback:
                # Only take the breakout if price recently retested EMA9 first —
                # filters out chasing a candle that never paused/pulled back.
                if raw_buy and not _recent_pullback(sday["Low"], sday["High"], ema_fast,
                                                     i, V2_PULLBACK_LOOKBACK, V2_PULLBACK_TOL_PCT, "up"):
                    raw_buy = False
                if raw_sell and not _recent_pullback(sday["Low"], sday["High"], ema_fast,
                                                      i, V2_PULLBACK_LOOKBACK, V2_PULLBACK_TOL_PCT, "down"):
                    raw_sell = False

        # ── Manage open position ──────────────────────────────────
        if position["active"]:
            sc      = spot_cl - position["entry_spot"]
            pnl_pu  = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
            opt_pct = pnl_pu / position["entry_option_price"]

            # Activate trailing stop once up V2_TRAIL_TRIGGER (or override)
            _trig = trail_trigger if trail_trigger is not None else V2_TRAIL_TRIGGER
            if not position["trail_on"] and opt_pct >= _trig:
                position["trail_on"] = True
                position["trail_peak_pct"] = opt_pct
            if position["trail_on"] and opt_pct > position["trail_peak_pct"]:
                position["trail_peak_pct"] = opt_pct

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

            # After partial (2-lot): SL steps to V2_TRAIL_FLOOR.
            # 1-lot never partials — floor is independently tunable via trail_floor_1lot.
            base_floor = V2_TRAIL_FLOOR if position["partial_done"] else trail_floor_1lot
            # Big-winner lock: EMA_EXIT stays the primary exit for moderate gains.
            # Only once a trade has peaked well past the lock trigger does the floor
            # ratchet up with the peak, so a large spike can't fully round-trip back
            # to breakeven/loss before EMA9 gets a chance to flip.
            _lock_trig = trail_lock_trigger if trail_lock_trigger is not None else V2_TRAIL_LOCK_TRIGGER
            _lock_gb   = trail_lock_giveback if trail_lock_giveback is not None else V2_TRAIL_LOCK_GIVEBACK
            if position["trail_peak_pct"] >= _lock_trig:
                trail_floor = max(base_floor, position["trail_peak_pct"] - _lock_gb)
            else:
                trail_floor = base_floor

            # ── TARGET condition — differs for 1-lot vs 2-lot ───────────
            is_one_lot = position["initial_qty"] == LOT_SIZE
            abs_pnl    = pnl_pu * position["qty"]
            if is_one_lot:
                tp_hit = V2_1LOT_HARD_TP and (opt_pct >= V2_1LOT_TP_PCT or abs_pnl >= V2_1LOT_TP_RUPEES)
            else:
                tp_hit = opt_pct >= V2_TP_OPTION_PCT

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

            # EMA9 exit: require `ema_exit_confirm` consecutive candle closes
            # back through EMA9, AND (if ema_exit_min_loss > 0) the trade must
            # already be down at least that much before it counts as an exit.
            # Default (1, 0.0) = legacy: fires instantly on a single noisy
            # candle regardless of P&L — was the dominant historical loss driver.
            ema_breach  = ((position["type"] == "CE" and cl < ef) or
                           (position["type"] == "PE" and cl > ef))
            if ema_breach:
                position["ema_warn_count"] = position.get("ema_warn_count", 0) + 1
            else:
                position["ema_warn_count"] = 0
            ema_confirmed = position["ema_warn_count"] >= ema_exit_confirm
            ema_exit    = (ema_confirmed and opt_pct <= -ema_exit_min_loss
                           if ema_exit_min_loss > 0 else ema_confirmed)
            rev_exit    = (at > 0 and (hi - lo) > at * V2_REV_ATR_MULT and
                           ((position["type"] == "CE" and cl < op) or
                            (position["type"] == "PE" and cl > op)))

            # A: counter-signal exit — opposite signal fired AND already losing
            signal_flip = ((position["type"] == "CE" and raw_sell) or
                           (position["type"] == "PE" and raw_buy))
            # B: VWAP flip exit — price crossed back through VWAP AND already losing
            vwap_flip   = ((position["type"] == "CE" and cl < vw) or
                           (position["type"] == "PE" and cl > vw))
            ab_exit = (signal_aware_exit
                       and opt_pct <= -V2_SIGNAL_EXIT_LOSS
                       and (signal_flip or vwap_flip))

            exit_reason = None
            if hard_action in ("SL", "TARGET"):
                exit_reason = hard_action
            elif trail_exit:
                exit_reason = "TRAIL_EXIT"
            elif ab_exit:
                exit_reason = "SIGNAL_EXIT"
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
        in_afternoon = (V2_AFTERNOON_START <= time_str < NO_ENTRY_AFTER) and not is_thursday
        in_window    = in_morning or in_afternoon

        if (not position["active"]
                and in_window
                and trade_count < V2_MAX_TRADES
                and daily_pnl > MAX_DAILY_LOSS
                and daily_pnl < DAILY_PROFIT_TARGET
                and vm > 0):

            # raw_buy / raw_sell already computed above for this candle

            # "Move already done" filter: skip if price has already traveled
            # more than max_from_open_pct% from the day open in the signal direction.
            # Prevents late entries after the bulk of the move has happened.
            if max_from_open_pct > 0 and day_open_s > 0:
                move_ce = (cl - day_open_s) / day_open_s * 100   # how far up from open
                move_pe = (day_open_s - cl) / day_open_s * 100   # how far down from open
                if raw_buy  and move_ce > max_from_open_pct:
                    raw_buy  = False
                if raw_sell and move_pe > max_from_open_pct:
                    raw_sell = False

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
                        "trail_on"          : False, "partial_done": False, "trail_peak_pct": 0.0,
                        "sl_warn_count"     : 0,
                        "ema_warn_count"    : 0,
                    })

    result = _build_result(target_date, nifty_day, prev_close, daily_pnl,
                           balance, trades, trade_count)
    return result


def _no_trade_result(target_date, df_5m_all, df_1d_all, note=""):
    """Return a zero-trade result for days we deliberately skip."""
    nifty_day = df_5m_all[df_5m_all.index.date == target_date].between_time("09:15", "15:30")
    if nifty_day.empty:
        return None
    prev_rows = df_1d_all[df_1d_all.index.date < target_date] if not df_1d_all.empty else pd.DataFrame()
    if prev_rows.empty:
        prev_5m = df_5m_all[df_5m_all.index.date < target_date]
        if prev_5m.empty:
            return None
        prev_close = float(prev_5m.iloc[-1]["Close"])
    else:
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
