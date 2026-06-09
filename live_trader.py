"""
live_trader.py — Angel One Smart API live trading engine
Strategy : V2 (VWAP + EMA9/20 + RSI + Volume + BNF + Supertrend + VIX)
Lot size  : 65 (NSE, effective Oct 28 2025)
"""
import os, json, time, threading, requests
import pandas as pd, numpy as np
from datetime import date, datetime, timedelta, timezone
from logzero import logger, logfile
from dotenv import load_dotenv
load_dotenv()

def _setup_logfile():
    log_dir = f"logs/{_today().isoformat()}"
    os.makedirs(log_dir, exist_ok=True)
    logfile(f"{log_dir}/app.log", maxBytes=5_000_000, backupCount=3)

# Always use IST — Railway (and most cloud hosts) run UTC
_IST = timezone(timedelta(hours=5, minutes=30))

def _now() -> datetime:
    """Current datetime in IST, timezone-naive (for string comparisons)."""
    return datetime.now(_IST).replace(tzinfo=None)

def _today() -> date:
    """Today's date in IST."""
    return datetime.now(_IST).date()

# ── Telegram alerts ───────────────────────────────────────────────
_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID",   "")

def _tg(msg: str):
    """Fire-and-forget Telegram message. Silently drops on any error."""
    if not _TG_TOKEN or not _TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id": _TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass

import backtest as bt
from angel_data import (
    _fetch_intraday, _fetch_daily,
    NIFTYBEES_TOKEN, BANKBEES_TOKEN, NIFTY_MULTIPLIER,
)

# ── File paths ────────────────────────────────────────────────────
SCRIP_CACHE      = "logs/scrip_nfo.json"
LIVE_STATE_FILE  = "logs/live_state.json"
TRADE_LOG_FILE   = "logs/trade_history.json"


def _append_trade_log(record: dict):
    """Append a completed trade record to the persistent trade history file."""
    os.makedirs("logs", exist_ok=True)
    try:
        if os.path.exists(TRADE_LOG_FILE):
            with open(TRADE_LOG_FILE) as f:
                history = json.load(f)
        else:
            history = []
        history.append(record)
        tmp = TRADE_LOG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f, indent=2, default=str)
        os.replace(tmp, TRADE_LOG_FILE)
    except Exception as e:
        logger.warning(f"trade_log append error: {e}")

# ── Timing ────────────────────────────────────────────────────────
SQUAREOFF_TIME  = "15:15"
MARKET_OPEN     = "09:15"
MARKET_CLOSE    = "15:30"

# ── Scrip master helpers ──────────────────────────────────────────

def _load_scrip():
    """Download NFO scrip master from Angel One (cached daily)."""
    today = str(_today())
    if os.path.exists(SCRIP_CACHE):
        try:
            with open(SCRIP_CACHE) as f:
                d = json.load(f)
            if d.get("date") == today:
                return d["data"]
        except Exception:
            pass

    url = ("https://margincalculator.angelbroking.com"
           "/OpenAPI_File/files/OpenAPIScripMaster.json")
    logger.info("Downloading Angel One scrip master…")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    nifty_opts = [
        x for x in resp.json()
        if x.get("exch_seg") == "NFO"
        and x.get("instrumenttype") == "OPTIDX"
        and x.get("name") == "NIFTY"
    ]
    os.makedirs("logs", exist_ok=True)
    with open(SCRIP_CACHE, "w") as f:
        json.dump({"date": today, "data": nifty_opts}, f)
    logger.info(f"Scrip master cached: {len(nifty_opts)} Nifty options")
    return nifty_opts


def _next_thursday():
    """Nearest upcoming Tuesday — Nifty weekly expiry moved to Tuesday (NSE 2025)."""
    today = _today()
    for i in range(8):
        d = today + timedelta(days=i)
        if d.weekday() == 1:   # 1 = Tuesday
            return d
    return today + timedelta(days=2)


def _expiry_tag(d: date) -> str:
    """Returns 'DDMMMYY' e.g. '22MAY25' (zero-padded day, upper)."""
    return d.strftime("%d%b%y").upper()


def _find_option(scrip, strike: int, opt_type: str, expiry: date):
    """Return (token, symbol) for the given Nifty option."""
    # Try zero-padded (22MAY25) and non-padded (1JUN25) forms
    tried = []
    for exp in (_expiry_tag(expiry), str(expiry.day) + expiry.strftime("%b%y").upper()):
        target = f"NIFTY{exp}{strike}{opt_type}"
        tried.append(target)
        for x in scrip:
            if x.get("symbol") == target:
                return x["token"], x["symbol"]

    # Log nearby symbols to diagnose format mismatch
    nearby = [x.get("symbol","") for x in scrip
              if str(strike) in x.get("symbol","") and opt_type in x.get("symbol","")][:5]
    logger.warning(
        f"Option not found. Tried: {tried}. "
        f"Nearby symbols with {strike}{opt_type}: {nearby}. "
        f"Scrip size: {len(scrip)}"
    )
    return None, None


# ── Live Trading Engine ───────────────────────────────────────────

class AngelTrader:
    """
    Live options trading engine backed by Angel One Smart API.
    Runs two daemon threads:
      - signal loop  : every 5 min, checks V2 entry signal
      - monitor loop : every 30 s, checks exit conditions when in position
    State is persisted to logs/live_state.json for the dashboard.
    """

    def __init__(self):
        self._lock       = threading.Lock()
        self._obj        = None
        self._auth       = None
        self._api_key    = None
        self._last_login = None
        self._scrip      = []

        # Runtime flags
        self._running         = False
        self._monitoring_only = False   # stop new entries, keep monitoring
        self._sig_thread      = None
        self._mon_thread      = None

        # Config (set by start())
        self.max_trades   = 2
        self.lots         = 1
        self.enabled      = False
        self.paper_mode   = False   # True = simulate orders, no real API calls

        # Daily state
        self.position     = _empty_pos()
        self.trades       = []
        self.daily_pnl    = 0.0
        self.win_count    = 0
        self.trade_count  = 0
        self.last_signal  = None   # "buy" | "sell"  dedup

        # Signal info for display
        self.sig_info     = {"signal": None, "vix": None,
                             "time": None, "next_check": None,
                             "filter_reason": None}
        self.last_error   = None
        self.connected    = False
        self.balance      = 0.0
        self.nifty_ltp    = 0.0
        self._today       = _today()
        self._consec_errors   = 0      # consecutive signal-check failures
        self._last_error_tg   = None   # datetime of last error Telegram sent

    # ── Session management ────────────────────────────────────────

    def login(self):
        from login import login as _do_login
        try:
            obj, auth, _, _ = _do_login()
        except EnvironmentError as e:
            # Missing env vars — very actionable, send specific message
            self.connected  = False
            self.last_error = str(e)
            _tg(f"🔴 <b>Login Failed — Missing Env Vars</b>\n"
                f"Error  : {e}\n"
                f"Fix    : Go to Railway → your project → Variables\n"
                f"         Add: ANGEL_API_KEY, ANGEL_CLIENT_ID,\n"
                f"              ANGEL_PASSWORD, ANGEL_TOTP_SECRET")
            raise
        except Exception as e:
            self.connected  = False
            self.last_error = str(e)
            _tg(f"🔴 <b>Angel One Login Failed</b>\n"
                f"Error  : {e}\n"
                f"Time   : {_now().strftime('%H:%M:%S')}\n"
                f"Causes : Wrong credentials | Railway IP blocked |\n"
                f"         Angel One API down | Clock drift on Railway")
            raise
        self._obj       = obj
        self._auth      = auth
        self._api_key   = os.getenv("ANGEL_API_KEY", "")
        self._last_login = _now()
        self._scrip     = _load_scrip()
        self.connected  = True
        self.last_error = None
        _setup_logfile()
        logger.info("AngelTrader: login OK")

    def _ensure_session(self):
        if self._obj is None:
            self.login()
            return
        if self._last_login and (_now() - self._last_login).total_seconds() > 6.5 * 3600:
            logger.info("AngelTrader: session refresh")
            self.login()

    # ── Market data ───────────────────────────────────────────────

    def get_balance(self):
        try:
            self._ensure_session()
            rms = self._obj.rmsLimit()
            if rms and rms.get("status"):
                d = rms["data"]
                self.balance = round(float(d.get("net", 0)), 2)
                return {
                    "available_cash": self.balance,
                    "used_margin"   : round(float(d.get("utiliseddebits", 0)), 2),
                    "net"           : round(float(d.get("net", 0)), 2),
                }
        except Exception as e:
            logger.warning(f"get_balance: {e}")
        return {"available_cash": self.balance, "used_margin": 0, "net": self.balance}

    @staticmethod
    def _extract_ltp(resp):
        """Extract LTP from SmartAPI response — handles both full and data-only formats."""
        if not resp or not isinstance(resp, dict):
            return None
        # Full format: {"status": true, "data": {"ltp": "..."}}
        if resp.get("data") and isinstance(resp["data"], dict):
            val = resp["data"].get("ltp")
            if val is not None:
                return float(val)
        # Data-only format: {"ltp": "...", "tradingsymbol": "..."}
        if resp.get("ltp") is not None:
            return float(resp["ltp"])
        return None

    def get_nifty_ltp(self):
        try:
            resp = self._obj.ltpData("NSE", "NIFTYBEES-EQ", NIFTYBEES_TOKEN)
            ltp_val = self._extract_ltp(resp)
            if ltp_val:
                ltp = round(ltp_val * NIFTY_MULTIPLIER, 2)
                self.nifty_ltp = ltp
                return ltp
        except Exception as e:
            logger.warning(f"get_nifty_ltp: {e}")
        return self.nifty_ltp

    def get_option_ltp(self, symbol, token):
        try:
            resp = self._obj.ltpData("NFO", symbol, token)
            return self._extract_ltp(resp)
        except Exception as e:
            logger.warning(f"get_option_ltp {symbol}: {e}")
        return None

    # ── Live data fetch for signal ────────────────────────────────

    def _fetch_live_data(self):
        """Fetch last 12 days of 5m candles (enough for EMA20 + buffer)."""
        today    = _today()
        lookback = today - timedelta(days=12)

        df_nbees = _fetch_intraday(self._auth, self._api_key,
                                   NIFTYBEES_TOKEN, lookback, today)
        df_bnf   = _fetch_intraday(self._auth, self._api_key,
                                   BANKBEES_TOKEN, lookback, today)
        df_1d    = _fetch_daily(self._auth, self._api_key,
                                NIFTYBEES_TOKEN, lookback, today)

        # Scale daily data to Nifty proxy
        df_nifty_1d = df_1d.copy()
        if not df_nifty_1d.empty:
            for col in ["Open", "High", "Low", "Close"]:
                df_nifty_1d[col] = (df_nifty_1d[col] * NIFTY_MULTIPLIER).round(2)

        # VIX from NSE public API (live current value)
        df_vix = pd.DataFrame()
        try:
            _sess = requests.Session()
            _sess.get("https://www.nseindia.com",
                      headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                "Referer": "https://www.nseindia.com"}, timeout=5)
            _resp = _sess.get("https://www.nseindia.com/api/allIndices",
                              headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                       "Referer": "https://www.nseindia.com"}, timeout=5)
            _data = _resp.json().get("data", [])
            _vix  = next((x["last"] for x in _data if "VIX" in x.get("indexSymbol", "")), None)
            if _vix is not None:
                import pytz
                _ist = pytz.timezone("Asia/Kolkata")
                _idx = pd.DatetimeIndex([pd.Timestamp(today).tz_localize(_ist)])
                df_vix = pd.DataFrame({"Close": [float(_vix)]}, index=_idx)
        except Exception:
            pass

        return df_nbees, df_nifty_1d, df_bnf, df_vix

    # ── Signal detection (mirrors backtest V2 logic exactly) ─────

    def _check_signal(self, df_nbees, df_1d, df_bnf, df_vix):
        """
        Run V2 indicator logic on latest closed 5m candle.
        Returns ("BUY_CE" | "BUY_PE" | None, vix_value | None)
        Also updates self.sig_info["filter_reason"] with why no signal fired.
        """
        today = _today()
        now   = _now()

        # VIX filter
        vix_val = None
        if (df_vix is not None and not df_vix.empty
                and isinstance(df_vix.index, pd.DatetimeIndex)):
            vix_rows = df_vix[df_vix.index.date <= today]
            if not vix_rows.empty:
                vix_val = round(float(vix_rows.iloc[-1]["Close"]), 2)
                if not (bt.V2_VIX_MIN <= vix_val <= bt.V2_VIX_MAX):
                    self.sig_info["filter_reason"] = f"VIX {vix_val} outside {bt.V2_VIX_MIN}–{bt.V2_VIX_MAX}"
                    return None, vix_val

        # Today's candles (NIFTYBEES for indicators)
        if df_nbees is None or df_nbees.empty or not isinstance(df_nbees.index, pd.DatetimeIndex):
            self.sig_info["filter_reason"] = "Data fetch error — bad index (RangeIndex)"
            return None, vix_val
        # Use all available history (prev days + today) to warm up EMA/RSI/Supertrend
        # so signals can fire from the first candle of the day. VWAP and vol MA
        # are inherently daily and stay today-only.
        all_5m = df_nbees[df_nbees.index.date <= today].between_time("09:15", "15:30")
        sday   = all_5m[all_5m.index.date == today]
        if sday.empty:
            self.sig_info["filter_reason"] = "No candles for today yet"
            return None, vix_val

        # Time window check — Tuesday (expiry): morning only, afternoon blocked (theta decay)
        ts            = now.strftime("%H:%M")
        is_expiry_day = today.weekday() == bt.V2_EXPIRY_WEEKDAY
        in_morning    = bt.V2_NO_ENTRY_BEFORE <= ts <= bt.V2_MORNING_END
        in_afternoon  = (bt.V2_AFTERNOON_START <= ts < bt.NO_ENTRY_AFTER) and not is_expiry_day
        if not (in_morning or in_afternoon):
            reason = "Tuesday expiry — afternoon blocked (theta too high)" if is_expiry_day and ts >= bt.V2_AFTERNOON_START \
                     else f"Outside trading window ({bt.V2_NO_ENTRY_BEFORE}–{bt.V2_MORNING_END} / {bt.V2_AFTERNOON_START}–{bt.NO_ENTRY_AFTER})"
            self.sig_info["filter_reason"] = reason
            return None, vix_val

        # Prev close
        prev_rows = (df_1d[df_1d.index.date < today]
                     if df_1d is not None and not df_1d.empty
                     and isinstance(df_1d.index, pd.DatetimeIndex)
                     else pd.DataFrame())
        if prev_rows.empty:
            self.sig_info["filter_reason"] = "No prev-day data"
            return None, vix_val

        # Compute indicators — EMA/RSI/Supertrend run on multi-day history so
        # they arrive pre-warmed; extract today's slice for signal reads.
        # VWAP and vol MA reset each day and stay today-only.
        vwap_s  = bt._vwap(sday)
        vol_ma  = sday["Volume"].rolling(20, min_periods=5).mean()
        ema_f   = all_5m["Close"].ewm(span=bt.V2_EMA_FAST, adjust=False).mean().loc[sday.index]
        ema_s   = all_5m["Close"].ewm(span=bt.V2_EMA_SLOW, adjust=False).mean().loc[sday.index]
        rsi_s   = bt._rsi(all_5m["Close"], bt.V2_RSI_PERIOD).loc[sday.index]
        st_s    = bt._supertrend(all_5m, bt.V2_ST_PERIOD, bt.V2_ST_MULT).loc[sday.index]

        bnf_day  = (df_bnf[df_bnf.index.date == today].between_time("09:15", "15:30")
                    if df_bnf is not None and not df_bnf.empty
                    and isinstance(df_bnf.index, pd.DatetimeIndex)
                    else pd.DataFrame())
        has_bnf  = not bnf_day.empty and bnf_day["Volume"].sum() > 0
        bnf_vwap = bt._vwap(bnf_day) if has_bnf else None

        i   = len(sday) - 1
        row = sday.iloc[i]
        cl  = float(row["Close"]);  op  = float(row["Open"])
        vol = float(row["Volume"]); vw  = float(vwap_s.iloc[i])
        ef  = float(ema_f.iloc[i]); es  = float(ema_s.iloc[i])
        vm  = float(vol_ma.iloc[i]) if not np.isnan(vol_ma.iloc[i]) else 0.0
        rsi = float(rsi_s.iloc[i])  if not np.isnan(rsi_s.iloc[i])  else 50.0
        st  = int(st_s.iloc[i])

        vol_surge = vm > 0 and vol > vm * bt.V2_VOL_SURGE_MULT

        if has_bnf and bnf_vwap is not None and len(bnf_day) > i:
            bnf_cl   = float(bnf_day.iloc[i]["Close"])
            bnf_vw   = float(bnf_vwap.iloc[i])
            bnf_bull = bnf_cl > bnf_vw;  bnf_bear = bnf_cl < bnf_vw
        else:
            bnf_bull = bnf_bear = True

        raw_buy  = (cl > vw and cl > ef and cl > es and cl > op
                    and vol_surge and rsi > bt.V2_RSI_MIN_CE and bnf_bull and st == 1)
        raw_sell = (cl < vw and cl < ef and cl < es and cl < op
                    and vol_surge and rsi < bt.V2_RSI_MAX_PE and bnf_bear and st == -1)

        # Build human-readable reason for dashboard when no signal fires
        if not raw_buy and not raw_sell:
            reasons = []
            if not (cl > vw):      reasons.append(f"Close({cl:.2f})<VWAP({vw:.2f})")
            if not (cl > ef):      reasons.append(f"Close<EMA9({ef:.2f})")
            if not (cl > es):      reasons.append(f"Close<EMA20({es:.2f})")
            if not vol_surge:      reasons.append(f"Vol {vol/vm:.1f}x<1.5x" if vm > 0 else "Vol N/A")
            if rsi <= bt.V2_RSI_MIN_CE and rsi >= bt.V2_RSI_MAX_PE:
                reasons.append(f"RSI({rsi:.0f}) neutral")
            if st != 1 and st != -1:  reasons.append("ST neutral")
            self.sig_info["filter_reason"] = ", ".join(reasons) if reasons else "Conditions not met"

        signal = None
        if raw_buy  and self.last_signal != "buy":
            signal = "BUY_CE"
            self.sig_info["filter_reason"] = None
        elif raw_sell and self.last_signal != "sell":
            signal = "BUY_PE"
            self.sig_info["filter_reason"] = None
        elif raw_buy or raw_sell:
            self.sig_info["filter_reason"] = "Dedup — same direction already traded"

        return signal, vix_val

    # ── Order placement ───────────────────────────────────────────

    def _order(self, symbol, token, qty, side):
        self._ensure_session()
        params = {
            "variety"         : "NORMAL",
            "tradingsymbol"   : symbol,
            "symboltoken"     : token,
            "transactiontype" : side,
            "exchange"        : "NFO",
            "ordertype"       : "MARKET",
            "producttype"     : "INTRADAY",
            "duration"        : "DAY",
            "quantity"        : str(qty),
            "price"           : "0",
            "squareoff"       : "0",
            "stoploss"        : "0",
        }
        try:
            resp = self._obj.placeOrder(params)
            logger.info(f"ORDER {side} {symbol} qty={qty}: {resp}")
            if resp is None:
                # placeOrder returned None — session likely stale; refresh and retry once
                logger.warning("placeOrder returned None — forcing session refresh and retrying")
                self.login()
                resp = self._obj.placeOrder(params)
                logger.info(f"ORDER retry {side} {symbol}: {resp}")
            if resp is None:
                self.last_error = "placeOrder returned None after session refresh — check Angel One API / network"
                return None
            # Some SmartAPI versions return the order ID string directly on success
            if isinstance(resp, str):
                resp = {"status": True, "data": {"orderid": resp}} if resp else None
                if resp is None:
                    self.last_error = "placeOrder returned empty string"
                    return None
            return resp
        except Exception as e:
            logger.error(f"_order exception {side} {symbol}: {e}", exc_info=True)
            self.last_error = f"Order exception: {e}"
            return None

    # ── Entry ─────────────────────────────────────────────────────

    def _enter(self, signal, force_strike=None):
        opt_type = "CE" if signal == "BUY_CE" else "PE"
        spot     = self.get_nifty_ltp()
        if not spot:
            self.last_error = "_enter: cannot get Nifty LTP"
            logger.warning(self.last_error)
            return False

        strike = force_strike if force_strike else int(round(spot / 50) * 50)
        expiry = _next_thursday()
        token, symbol = _find_option(self._scrip, strike, opt_type, expiry)
        if not token:
            # Scan ±5 strikes (±250 pts) before giving up
            for delta in range(1, 6):
                for s in (strike - delta * 50, strike + delta * 50):
                    token, symbol = _find_option(self._scrip, s, opt_type, expiry)
                    if token:
                        strike = s
                        logger.info(f"_enter: ATM not found, using strike={s}")
                        break
                if token:
                    break
        if not token:
            self.last_error = (f"Option not found near {strike}{opt_type} expiry={expiry} "
                               f"(scrip size={len(self._scrip)})")
            logger.error(self.last_error)
            return False

        qty       = self.lots * bt.LOT_SIZE
        entry_ltp = self.get_option_ltp(symbol, token)
        if not entry_ltp:
            self.last_error = f"LTP fetch failed for {symbol} token={token}"
            logger.error(self.last_error)
            return False

        if self.paper_mode:
            order_id = "PAPER"
        else:
            resp = self._order(symbol, token, qty, "BUY")
            if not (resp and resp.get("status")):
                if not self.last_error:   # don't overwrite exception set in _order()
                    msg = resp.get("message","") if isinstance(resp, dict) else str(resp)
                    self.last_error = f"Buy order failed: {msg} | full={resp}"
                _tg(f"🔴 <b>BUY ORDER FAILED</b>\n"
                    f"Symbol : {symbol}\n"
                    f"Qty    : {qty}\n"
                    f"Error  : {resp}\n"
                    f"Time   : {_now().strftime('%H:%M:%S')}")
                return False
            order_id = resp.get("data", {}).get("orderid", "—")
            # Use actual fill price from Angel One tradeBook
            try:
                import time as _time; _time.sleep(1)
                tb = self._obj.tradeBook()
                if tb and tb.get("status") and tb.get("data"):
                    for row in reversed(tb["data"]):
                        if (row.get("tradingsymbol") == symbol
                                and row.get("transactiontype") == "BUY"
                                and row.get("producttype") == "INTRADAY"):
                            fill = float(row.get("fillprice") or 0)
                            if fill:
                                entry_ltp = fill
                                break
            except Exception as e:
                logger.warning(f"Could not fetch entry fill price: {e}")

        with self._lock:
            self.position = {
                "active"       : True,
                "symbol"       : symbol,
                "token"        : token,
                "side"         : opt_type,
                "strike"       : strike,
                "expiry"       : str(expiry),
                "qty"          : qty,
                "initial_qty"  : qty,
                "entry_price"  : round(entry_ltp, 2),
                "entry_spot"   : round(spot, 2),
                "entry_time"   : _now().strftime("%H:%M"),
                "partial_done" : False,
                "trail_on"     : False,
                "trail_high"   : entry_ltp,
                "live_ltp"     : entry_ltp,
                "live_pnl"     : 0.0,
                "order_id"     : order_id,
                "paper"        : self.paper_mode,
            }
            self.last_signal = "buy" if signal == "BUY_CE" else "sell"

        lots = qty // bt.LOT_SIZE
        tag  = "[PAPER] " if self.paper_mode else ""
        logger.info(f"{tag}Position opened: {symbol} {qty}@{entry_ltp}")
        _tg(f"{'📋' if self.paper_mode else '🟢'} <b>{tag}TRADE ENTRY</b>\n"
            f"Symbol : {symbol}\n"
            f"Type   : {opt_type}\n"
            f"Lots   : {lots} ({qty} qty)\n"
            f"LTP    : ₹{entry_ltp:.2f}\n"
            f"Spot   : ₹{spot:.2f}\n"
            f"Time   : {_now().strftime('%H:%M')}")
        self._save_state()
        return True

    # ── Exit (full or partial) ────────────────────────────────────

    def _exit(self, reason, ltp=None):
        with self._lock:
            if not self.position["active"]:
                return
            self.position["active"] = False  # claim the exit — prevents duplicate from other thread
        pos = dict(self.position)
        pos["active"] = True  # keep local copy consistent for pnl calc below

        if ltp is None:
            ltp = self.get_option_ltp(pos["symbol"], pos["token"]) or pos["entry_price"]

        if not self.paper_mode:
            resp = self._order(pos["symbol"], pos["token"], pos["qty"], "SELL")
            if not (resp and resp.get("status")):
                self.last_error = f"Sell order failed for {pos['symbol']}: {resp}"
                logger.error(self.last_error)
                _tg(f"🔴 <b>SELL ORDER FAILED — MANUAL ACTION NEEDED</b>\n"
                    f"Symbol : {pos['symbol']}\n"
                    f"Qty    : {pos['qty']}\n"
                    f"Reason : {reason}\n"
                    f"Error  : {resp}\n"
                    f"Time   : {_now().strftime('%H:%M:%S')}\n"
                    f"⚠️ Please exit manually on Angel One app!")
                return
            # Use actual fill price from Angel One tradeBook instead of LTP estimate
            try:
                import time as _time; _time.sleep(1)  # brief wait for fill to settle
                tb = self._obj.tradeBook()
                if tb and tb.get("status") and tb.get("data"):
                    for row in reversed(tb["data"]):
                        if (row.get("tradingsymbol") == pos["symbol"]
                                and row.get("transactiontype") == "SELL"
                                and row.get("producttype") == "INTRADAY"):
                            fill = float(row.get("fillprice") or 0)
                            if fill:
                                ltp = fill
                                break
            except Exception as e:
                logger.warning(f"Could not fetch fill price: {e}")

        pnl     = round((ltp - pos["entry_price"]) * pos["qty"], 2)
        pnl_pct = round((ltp - pos["entry_price"]) / pos["entry_price"] * 100, 2) if pos["entry_price"] else 0
        lots    = pos["qty"] // bt.LOT_SIZE
        capital = round(pos["entry_price"] * pos["qty"], 2)
        trade_record = {
            "date"      : _today().isoformat(),
            "time"      : pos["entry_time"],
            "exit_time" : _now().strftime("%H:%M"),
            "symbol"    : pos["symbol"],
            "side"      : pos["side"],
            "strike"    : pos["strike"],
            "entry"     : pos["entry_price"],
            "exit"      : round(ltp, 2),
            "entry_spot": pos["entry_spot"],
            "qty"       : pos["qty"],
            "lots"      : lots,
            "capital"   : capital,
            "pnl"       : pnl,
            "pnl_pct"   : pnl_pct,
            "reason"    : reason,
            "paper"     : self.paper_mode,
        }
        with self._lock:
            self.trades.append(trade_record)
            self.daily_pnl   += pnl
            self.trade_count += 1
            if pnl > 0:
                self.win_count += 1
            self.position  = _empty_pos()
            self.last_signal = None

        _append_trade_log(trade_record)

        tag  = "[PAPER] " if self.paper_mode else ""
        icon = "✅" if pnl >= 0 else "🔴"
        logger.info(f"{tag}Position closed: {reason} ltp={ltp} pnl={pnl}")
        _tg(f"{icon} <b>{tag}TRADE EXIT — {reason}</b>\n"
            f"Symbol : {pos['symbol']}\n"
            f"Entry  : ₹{pos['entry_price']:.2f}  Exit: ₹{ltp:.2f}\n"
            f"Qty    : {pos['qty']}\n"
            f"P&L    : {'+'if pnl>=0 else ''}₹{pnl:,.2f}\n"
            f"Daily  : {'+'if self.daily_pnl>=0 else ''}₹{self.daily_pnl:,.2f}\n"
            f"Time   : {_now().strftime('%H:%M')}")
        self._save_state()

    def _partial_exit(self, ltp):
        pos = self.position
        qty = bt.LOT_SIZE

        if not self.paper_mode:
            resp = self._order(pos["symbol"], pos["token"], qty, "SELL")
            if not (resp and resp.get("status")):
                logger.error(f"Partial sell failed: {resp}")
                return

        pnl     = round((ltp - pos["entry_price"]) * qty, 2)
        pnl_pct = round((ltp - pos["entry_price"]) / pos["entry_price"] * 100, 2) if pos["entry_price"] else 0
        capital = round(pos["entry_price"] * qty, 2)
        partial_record = {
            "date"      : _today().isoformat(),
            "time"      : pos["entry_time"],
            "exit_time" : _now().strftime("%H:%M"),
            "symbol"    : pos["symbol"],
            "side"      : pos["side"],
            "strike"    : pos["strike"],
            "entry"     : pos["entry_price"],
            "exit"      : round(ltp, 2),
            "entry_spot": pos["entry_spot"],
            "qty"       : qty,
            "lots"      : 1,
            "capital"   : capital,
            "pnl"       : pnl,
            "pnl_pct"   : pnl_pct,
            "reason"    : "PARTIAL_TP",
            "paper"     : self.paper_mode,
        }
        with self._lock:
            self.trades.append(partial_record)
            self.daily_pnl         += pnl
            self.position["qty"]   -= qty
            self.position["partial_done"] = True

        _append_trade_log(partial_record)

        tag = "[PAPER] " if self.paper_mode else ""
        logger.info(f"{tag}Partial exit {qty} units @{ltp} pnl={pnl}")
        _tg(f"🟡 <b>{tag}PARTIAL EXIT +10%</b>\n"
            f"Symbol : {pos['symbol']}\n"
            f"Sold   : {qty} qty (1 lot)\n"
            f"LTP    : ₹{ltp:.2f}  P&L: +₹{pnl:,.2f}\n"
            f"Remaining: {pos['qty'] - qty} qty — running to +20% TARGET\n"
            f"Time   : {_now().strftime('%H:%M')}")
        self._save_state()

    # ── Position monitoring ───────────────────────────────────────

    def _manage_position(self):
        pos = self.position
        if not pos["active"]:
            return

        ts = _now().strftime("%H:%M")
        if ts >= SQUAREOFF_TIME:
            self._exit("EOD_SQUAREOFF")
            return

        ltp = self.get_option_ltp(pos["symbol"], pos["token"])
        if ltp is None:
            return

        pnl_pu  = ltp - pos["entry_price"]
        opt_pct = pnl_pu / pos["entry_price"] if pos["entry_price"] > 0 else 0

        # Update live P&L display
        with self._lock:
            self.position["live_ltp"] = round(ltp, 2)
            self.position["live_pnl"] = round(pnl_pu * pos["qty"], 2)

        is_one_lot = pos.get("initial_qty", pos["qty"]) == bt.LOT_SIZE

        # 1-lot: exit at +10% OR ₹1,100 — whichever comes first
        if is_one_lot:
            abs_pnl = (ltp - pos["entry_price"]) * bt.LOT_SIZE
            if opt_pct >= bt.V2_1LOT_TP_PCT or abs_pnl >= bt.V2_1LOT_TP_RUPEES:
                self._exit("TARGET", ltp)
                return

        # 2-lot: partial exit at +10%
        if (not is_one_lot
                and not pos["partial_done"]
                and opt_pct >= bt.V2_PARTIAL_PCT
                and pos["qty"] >= bt.LOT_SIZE * 2):
            self._partial_exit(ltp)

        # Trail activation
        if not pos["trail_on"] and opt_pct >= bt.V2_TRAIL_TRIGGER:
            with self._lock:
                self.position["trail_on"]   = True
                self.position["trail_high"] = ltp

        if pos["trail_on"]:
            with self._lock:
                if ltp > pos["trail_high"]:
                    self.position["trail_high"] = ltp

        # After partial, SL steps to breakeven (trail_floor = 0%)
        trail_floor = bt.V2_TRAIL_FLOOR if pos["partial_done"] else 0.0
        trail_exit  = pos["trail_on"] and opt_pct <= trail_floor

        # ── Spot-based SL (two-tier) ──────────────────────────────────────────
        # Small breach (WARN pts): market may be consolidating — wait 2 polls.
        # Large breach (HARD pts): genuine reversal — exit immediately, no wait.
        current_spot = self.get_nifty_ltp()
        entry_spot   = pos.get("entry_spot", 0.0)
        if current_spot and entry_spot:
            spot_move = abs(current_spot - entry_spot)
            against   = ((pos["side"] == "PE" and current_spot > entry_spot) or
                         (pos["side"] == "CE" and current_spot < entry_spot))
            if against:
                if spot_move >= bt.V2_SPOT_SL_HARD:
                    # Big move — exit right now, no confirmation needed
                    self._exit("SPOT_SL_HARD", ltp)
                    return
                elif spot_move >= bt.V2_SPOT_SL_WARN:
                    # Small breach — need 2 consecutive polls to confirm
                    with self._lock:
                        self.position["spot_sl_warn_count"] = pos.get("spot_sl_warn_count", 0) + 1
                    if pos.get("spot_sl_warn_count", 0) + 1 >= 2:
                        self._exit("SPOT_SL", ltp)
                        return
                else:
                    with self._lock:
                        self.position["spot_sl_warn_count"] = 0
            else:
                with self._lock:
                    self.position["spot_sl_warn_count"] = 0

        # ── Premium backstop (two-tier) ───────────────────────────────────────
        # Hard stop (-20%): immediate exit — no waiting.
        # Warning zone (-13%): 2 polls needed — filters slow theta bleed.
        if opt_pct <= -bt.V2_SL_OPTION_PCT:
            self._exit("SL_HARD", ltp)
            return

        sl_triggered = False
        if opt_pct <= -bt.V2_SL_WARN_PCT:
            with self._lock:
                self.position["sl_warn_count"] = pos["sl_warn_count"] + 1
            if pos["sl_warn_count"] + 1 >= 2:
                sl_triggered = True
        else:
            with self._lock:
                self.position["sl_warn_count"] = 0

        if   opt_pct >= bt.V2_TP_OPTION_PCT: self._exit("TARGET",     ltp)
        elif sl_triggered:                    self._exit("SL",         ltp)
        elif trail_exit:                      self._exit("TRAIL_EXIT", ltp)

    # ── Background loops ──────────────────────────────────────────

    def _signal_loop(self):
        logger.info("Signal loop started")
        while self._running:
            try:
                # Reset daily state at day change
                if _today() != self._today:
                    self._reset_day()

                now = _now()
                if not _market_open(now):
                    time.sleep(60)
                    continue

                self._ensure_session()

                if (not self._monitoring_only
                        and self.enabled
                        and not self.position["active"]
                        and self.trade_count < self.max_trades
                        ):
                    # Always update timing so dashboard shows loop is alive
                    next_t = _next_candle(now)
                    self.sig_info["time"]       = now.strftime("%H:%M:%S")
                    self.sig_info["next_check"] = next_t.strftime("%H:%M")
                    try:
                        self.get_balance()
                        self.get_nifty_ltp()
                        df_nbees, df_1d, df_bnf, df_vix = self._fetch_live_data()
                        signal, vix = self._check_signal(df_nbees, df_1d, df_bnf, df_vix)

                        self.sig_info.update({"signal": signal, "vix": vix})
                        self.last_error = None  # clear old errors on success

                        if signal:
                            logger.info(f"Signal: {signal}")
                            self._enter(signal)
                    except Exception as e:
                        logger.error(f"Signal check error: {e}", exc_info=True)
                        self.last_error = str(e)
                        self._consec_errors += 1
                        # Alert after 3 consecutive failures, then at most once per 10 min
                        now_dt = _now()
                        quiet  = (self._last_error_tg is not None and
                                  (now_dt - self._last_error_tg).total_seconds() < 600)
                        if self._consec_errors >= 3 and not quiet:
                            _tg(f"⚠️ <b>Signal Check Error (×{self._consec_errors})</b>\n"
                                f"Error : {e}\n"
                                f"Time  : {now_dt.strftime('%H:%M:%S')}\n"
                                f"Action: Bot is retrying — check Angel One API / network.")
                            self._last_error_tg = now_dt
                    else:
                        self._consec_errors = 0   # reset on success

                self._save_state()

                # Sleep until 35 seconds after next 5-minute candle close
                sleep_secs = max(30, (_next_candle(now) - _now()).total_seconds())
                time.sleep(min(sleep_secs, 120))

            except Exception as e:
                logger.error(f"Signal loop error: {e}", exc_info=True)
                time.sleep(60)

        logger.info("Signal loop stopped")

    def _monitor_loop(self):
        logger.info("Monitor loop started")
        while self._running:
            try:
                if self.position["active"]:
                    self._ensure_session()
                    self._manage_position()
                    if self._monitoring_only and not self.position["active"]:
                        logger.info("Position closed in monitor-only mode — stopping")
                        self._running = False
                    self._save_state()
            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)
            time.sleep(30)
        logger.info("Monitor loop stopped")

    # ── Public API ────────────────────────────────────────────────

    def start(self, max_trades: int = 2, lots: int = 1, paper_mode: bool = False):
        """Enable trading and launch background threads."""
        if self._obj is None:
            self.login()

        self.max_trades       = max_trades
        self.lots             = lots
        self.paper_mode       = paper_mode
        self.enabled          = True
        self._monitoring_only = False

        if not self._running:
            self._running   = True
            self._sig_thread = threading.Thread(
                target=self._signal_loop, daemon=True, name="SignalLoop")
            self._mon_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="MonitorLoop")
            self._sig_thread.start()
            self._mon_thread.start()

        self._save_state()
        logger.info(f"Trading started: {lots} lot(s), max {max_trades} trades/day")
        tag = "[PAPER] " if paper_mode else ""
        _tg(f"🚀 <b>{tag}Bot Started</b>\n"
            f"Lots   : {lots}\n"
            f"Max    : {max_trades} trades/day\n"
            f"Time   : {_now().strftime('%H:%M:%S IST')}")

    def stop(self):
        """Stop new entries. Keep monitoring if position is open."""
        self.enabled = True  # keep running flag for monitor
        if self.position["active"]:
            self._monitoring_only = True
            logger.info("Trading stopped — monitoring active position until exit")
        else:
            self._monitoring_only = False
            self._running = False
            logger.info("Trading stopped — no open position")
        self.enabled = False
        self._save_state()

    def force_exit(self):
        """Immediately exit any open position and stop."""
        if self.position["active"]:
            self._exit("MANUAL")
        self._running = False
        self.enabled  = False
        self._save_state()

    def exit_position(self):
        """Exit the current position but keep the bot running for new entries."""
        if self.position["active"]:
            self._exit("MANUAL_EXIT")
        self._save_state()

    def get_state(self) -> dict:
        """Return current state for the dashboard (thread-safe snapshot)."""
        pos = dict(self.position)
        wr  = round(self.win_count / self.trade_count * 100) if self.trade_count else 0

        if pos["active"]:
            if self.paper_mode:
                status = "PAPER"
            elif self.enabled and not self._monitoring_only:
                status = "LIVE"
            else:
                status = "MONITORING"
        elif self._running:
            if self.paper_mode:
                status = "PAPER"
            else:
                status = "LIVE" if self.enabled else "MONITORING"
        else:
            status = "STOPPED"

        return {
            "status"      : status,
            "connected"   : self.connected,
            "enabled"     : self.enabled,
            "monitoring"  : self._monitoring_only,
            "paper_mode"  : self.paper_mode,
            "config"      : {"max_trades": self.max_trades, "lots": self.lots, "paper": self.paper_mode},
            "market"      : {"nifty_ltp": self.nifty_ltp, "vix": self.sig_info.get("vix")},
            "signal"      : self.sig_info,
            "position"    : pos,
            "daily_stats" : {
                "pnl"        : round(self.daily_pnl, 2),
                "trade_count": self.trade_count,
                "win_count"  : self.win_count,
                "win_rate"   : wr,
                "balance"    : self.balance,
            },
            "trades"      : list(self.trades),
            "last_error"  : self.last_error,
            "timestamp"   : _now().strftime("%H:%M:%S"),
        }

    # ── Helpers ───────────────────────────────────────────────────

    def _reset_day(self):
        with self._lock:
            self._today      = _today()
            self.position    = _empty_pos()
            self.trades      = []
            self.daily_pnl   = 0.0
            self.win_count   = 0
            self.trade_count = 0
            self.last_signal = None
        _setup_logfile()
        logger.info("Daily state reset")

    def _save_state(self):
        try:
            state = self.get_state()
            os.makedirs("logs", exist_ok=True)
            tmp = LIVE_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, default=str)
            os.replace(tmp, LIVE_STATE_FILE)
        except Exception as e:
            logger.warning(f"State save error: {e}")


# ── Module-level helpers ──────────────────────────────────────────

def _empty_pos():
    return {
        "active"       : False,
        "symbol"       : None,  "token"       : None,
        "side"         : None,  "strike"      : 0,
        "expiry"       : None,  "qty"         : 0,
        "entry_price"  : 0.0,   "entry_spot"  : 0.0,
        "entry_time"   : None,  "partial_done": False,
        "trail_on"     : False, "trail_high"  : 0.0,
        "live_ltp"     : 0.0,   "live_pnl"   : 0.0,
        "order_id"          : None,
        "sl_warn_count"     : 0,   # consecutive polls in premium backstop zone
        "spot_sl_warn_count": 0,   # consecutive polls with spot beyond SL threshold
    }

def _market_open(now: datetime) -> bool:
    ts = now.strftime("%H:%M")
    return MARKET_OPEN <= ts <= MARKET_CLOSE

def _next_candle(now: datetime) -> datetime:
    """Datetime of next 5m candle close + 35 seconds."""
    m = now.minute
    next_m = ((m // 5) + 1) * 5
    if next_m >= 60:
        base = now.replace(minute=0, second=35, microsecond=0) + timedelta(hours=1)
    else:
        base = now.replace(minute=next_m, second=35, microsecond=0)
    return base
