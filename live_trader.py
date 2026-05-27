"""
live_trader.py — Angel One Smart API live trading engine
Strategy : V2 (VWAP + EMA9/20 + RSI + Volume + cross-index + Supertrend + VIX)
Instruments: dynamically selected each cycle from instruments.ACTIVE_INSTRUMENTS
"""
import os, json, time, threading, requests
import pandas as pd, numpy as np
from datetime import date, datetime, timedelta, timezone
from logzero import logger

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
from angel_data import _fetch_intraday, _fetch_daily
from instruments import INSTRUMENTS, ACTIVE_INSTRUMENTS

# ── File paths ────────────────────────────────────────────────────
SCRIP_CACHE     = "logs/scrip_nfo.json"
LIVE_STATE_FILE = "logs/live_state.json"

# ── Timing ────────────────────────────────────────────────────────
SQUAREOFF_TIME = "15:15"
MARKET_OPEN    = "09:15"
MARKET_CLOSE   = "15:30"

# ── Scrip master helpers ──────────────────────────────────────────

def _load_scrip():
    """Download NFO scrip master, caching OPTIDX entries for all active instruments."""
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

    active_names = {INSTRUMENTS[k]["nfo_name"] for k in ACTIVE_INSTRUMENTS}
    opts = [
        x for x in resp.json()
        if x.get("exch_seg") == "NFO"
        and x.get("instrumenttype") == "OPTIDX"
        and x.get("name") in active_names
    ]
    os.makedirs("logs", exist_ok=True)
    with open(SCRIP_CACHE, "w") as f:
        json.dump({"date": today, "data": opts}, f)
    logger.info(f"Scrip master cached: {len(opts)} options ({', '.join(active_names)})")
    return opts


def _next_expiry(cfg: dict) -> date:
    """Next expiry date for the given instrument config."""
    today     = _today()
    wday      = cfg["expiry_weekday"]
    skip      = cfg["skip_expiry_day"]
    days_away = (wday - today.weekday()) % 7
    if days_away == 0 and skip:
        days_away = 7
    return today + timedelta(days=days_away)


def _expiry_tag(d: date) -> str:
    """Returns 'DDMMMYY' e.g. '22MAY25' (zero-padded day, upper)."""
    return d.strftime("%d%b%y").upper()


def _find_option(scrip, instr_name: str, strike: int, opt_type: str, expiry: date):
    """Return (token, symbol) for the given option, trying padded and non-padded day forms."""
    for exp in (_expiry_tag(expiry), str(expiry.day) + expiry.strftime("%b%y").upper()):
        target = f"{instr_name}{exp}{strike}{opt_type}"
        for x in scrip:
            if x.get("symbol") == target:
                return x["token"], x["symbol"]
    logger.warning(f"{instr_name} option not found: {strike}{opt_type} exp {expiry}")
    return None, None


# ── Live Trading Engine ───────────────────────────────────────────

class AngelTrader:
    """
    Live options trading engine backed by Angel One Smart API.
    Each signal cycle the bot evaluates all ACTIVE_INSTRUMENTS,
    scores them, and trades the one with the strongest setup.

    Two daemon threads:
      - signal loop  : every 5 min, selects best instrument and checks entry
      - monitor loop : every 30 s, manages exit when in position
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
        self._monitoring_only = False
        self._sig_thread      = None
        self._mon_thread      = None

        # Config (set by start())
        self.max_trades   = 2
        self.lots         = 1
        self.enabled      = False
        self.paper_mode   = False

        # Daily state
        self.position     = _empty_pos()
        self.trades       = []
        self.daily_pnl    = 0.0
        self.win_count    = 0
        self.trade_count  = 0
        self.last_signal  = None   # "buy" | "sell" — dedup across instruments

        # Signal + instrument info for display
        self.sig_info     = {
            "signal"       : None,
            "vix"          : None,
            "time"         : None,
            "next_check"   : None,
            "filter_reason": None,
            "instrument"   : None,   # which instrument was selected
            "audit"        : [],     # per-instrument evaluation results
        }
        self.last_error   = None
        self.connected    = False
        self.balance      = 0.0
        self.spot_ltps    = {}        # {"NIFTY": 24500, "BANKNIFTY": 52000}
        self.nifty_ltp    = 0.0      # kept for dashboard backward compat
        self._today       = _today()
        self._consec_errors   = 0
        self._last_error_tg   = None

    # ── Session management ────────────────────────────────────────

    def login(self):
        from login import login as _do_login
        try:
            obj, auth, _, _ = _do_login()
        except EnvironmentError as e:
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
        self._obj        = obj
        self._auth       = auth
        self._api_key    = os.getenv("ANGEL_API_KEY", "")
        self._last_login = _now()
        self._scrip      = _load_scrip()
        self.connected   = True
        self.last_error  = None
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
                self.balance = round(float(d.get("availablecash", 0)), 2)
                return {
                    "available_cash": self.balance,
                    "used_margin"   : round(float(d.get("utiliseddebits", 0)), 2),
                    "net"           : round(float(d.get("net", 0)), 2),
                }
        except Exception as e:
            logger.warning(f"get_balance: {e}")
        return {"available_cash": self.balance, "used_margin": 0, "net": self.balance}

    def _get_spot_ltp(self, cfg: dict) -> float:
        """Fetch current spot price for an instrument using its direct index token."""
        try:
            resp = self._obj.ltpData(cfg["spot_exchange"], cfg["spot_symbol"], cfg["spot_token"])
            if resp and resp.get("status"):
                return float(resp["data"]["ltp"])
        except Exception as e:
            logger.warning(f"get_spot_ltp {cfg['nfo_name']}: {e}")
        return 0.0

    def get_nifty_ltp(self):
        """Fetch Nifty 50 spot LTP (kept for dashboard compat)."""
        ltp = self._get_spot_ltp(INSTRUMENTS["NIFTY"])
        if ltp:
            self.nifty_ltp = ltp
            self.spot_ltps["NIFTY"] = ltp
        return ltp

    def _refresh_all_spot_ltps(self):
        """Update spot_ltps dict for all active instruments."""
        for key in ACTIVE_INSTRUMENTS:
            ltp = self._get_spot_ltp(INSTRUMENTS[key])
            if ltp:
                self.spot_ltps[key] = ltp
                if key == "NIFTY":
                    self.nifty_ltp = ltp

    def get_option_ltp(self, symbol, token):
        try:
            resp = self._obj.ltpData("NFO", symbol, token)
            if resp and resp.get("status"):
                return float(resp["data"]["ltp"])
        except Exception as e:
            logger.warning(f"get_option_ltp {symbol}: {e}")
        return None

    # ── Live data fetch ───────────────────────────────────────────

    def _fetch_all_data(self):
        """
        Fetch 5m + 1d candles for all unique signal/confirm tokens across active
        instruments, then build a per-instrument data dict.

        Returns (data_dict, df_vix) where data_dict[instr_key] = {
            "df_signal": DataFrame,   # 5m ETF candles for indicator computation
            "df_1d"    : DataFrame,   # daily ETF candles for prev-close check
            "df_confirm": DataFrame,  # 5m cross-index ETF candles
        }
        """
        today    = _today()
        lookback = today - timedelta(days=12)

        # Collect unique tokens needed
        tokens_5m = {}   # token -> multiplier (we fetch raw, no multiplier needed for signals)
        tokens_1d = {}
        for key in ACTIVE_INSTRUMENTS:
            cfg = INSTRUMENTS[key]
            tokens_5m[cfg["signal_token"]]  = 1.0
            tokens_5m[cfg["confirm_token"]] = 1.0
            tokens_1d[cfg["signal_token"]]  = 1.0

        # Fetch each unique token once
        raw_5m = {}
        for tok in tokens_5m:
            raw_5m[tok] = _fetch_intraday(self._auth, self._api_key, tok, lookback, today)

        raw_1d = {}
        for tok in tokens_1d:
            raw_1d[tok] = _fetch_daily(self._auth, self._api_key, tok, lookback, today)

        # Build per-instrument dict
        data_dict = {}
        for key in ACTIVE_INSTRUMENTS:
            cfg = INSTRUMENTS[key]
            data_dict[key] = {
                "df_signal" : raw_5m.get(cfg["signal_token"],  pd.DataFrame()),
                "df_1d"     : raw_1d.get(cfg["signal_token"],  pd.DataFrame()),
                "df_confirm": raw_5m.get(cfg["confirm_token"], pd.DataFrame()),
            }

        # VIX from Yahoo Finance (daily, global filter)
        df_vix = pd.DataFrame()
        try:
            import yfinance as yf
            vix    = yf.Ticker("^INDIAVIX")
            df_vix = vix.history(start=lookback, end=today + timedelta(days=1), interval="1d")
            if not df_vix.empty and df_vix.index.tz is not None:
                df_vix.index = df_vix.index.tz_convert("Asia/Kolkata")
        except Exception:
            pass

        return data_dict, df_vix

    # ── Per-instrument signal computation ─────────────────────────

    def _compute_signal(self, instr_key: str, df_signal, df_1d, df_confirm):
        """
        Run V2 indicator logic for one instrument using that instrument's own params.
        Returns (raw_signal, score, reason):
          raw_signal : "BUY_CE" | "BUY_PE" | None
          score      : float — higher means stronger setup (used to rank instruments)
          reason     : human-readable string explaining why no signal / what drove it
        Note: VIX filter and dedup are handled by the caller.
        """
        cfg   = INSTRUMENTS[instr_key]
        today = _today()
        now   = _now()

        if df_signal is None or df_signal.empty:
            return None, 0.0, "No 5m data"
        if not isinstance(df_signal.index, pd.DatetimeIndex):
            return None, 0.0, "Data fetch error — bad index (RangeIndex)"

        sday = df_signal[df_signal.index.date == today].between_time("09:15", "15:30")
        if len(sday) < bt.V2_EMA_SLOW + 2:
            return None, 0.0, f"Not enough candles ({len(sday)}, need {bt.V2_EMA_SLOW + 2})"

        # Per-instrument time window
        ts              = now.strftime("%H:%M")
        morning_start   = cfg.get("morning_start",   bt.V2_NO_ENTRY_BEFORE)
        morning_end     = cfg.get("morning_end",      bt.V2_MORNING_END)
        afternoon_start = cfg.get("afternoon_start",  bt.V2_AFTERNOON_START)
        no_entry_after  = cfg.get("no_entry_after",   bt.NO_ENTRY_AFTER)

        in_morning   = morning_start <= ts <= morning_end
        in_afternoon = (afternoon_start is not None and
                        afternoon_start <= ts < no_entry_after)
        if not (in_morning or in_afternoon):
            if afternoon_start:
                window_str = f"{morning_start}–{morning_end} / {afternoon_start}–{no_entry_after}"
            else:
                window_str = f"{morning_start}–{morning_end}"
            return None, 0.0, f"Outside window ({window_str})"

        prev_rows = (df_1d[df_1d.index.date < today]
                     if df_1d is not None and not df_1d.empty
                     and isinstance(df_1d.index, pd.DatetimeIndex)
                     else pd.DataFrame())
        if prev_rows.empty:
            return None, 0.0, "No prev-day data"

        # Indicators (on raw ETF prices — ratios are scale-invariant)
        vwap_s = bt._vwap(sday)
        ema_f  = sday["Close"].ewm(span=bt.V2_EMA_FAST, adjust=False).mean()
        ema_s  = sday["Close"].ewm(span=bt.V2_EMA_SLOW, adjust=False).mean()
        vol_ma = sday["Volume"].rolling(20).mean()
        rsi_s  = bt._rsi(sday["Close"], bt.V2_RSI_PERIOD)
        st_s   = bt._supertrend(sday, bt.V2_ST_PERIOD, bt.V2_ST_MULT)

        conf_day = (df_confirm[df_confirm.index.date == today].between_time("09:15", "15:30")
                    if df_confirm is not None and not df_confirm.empty
                    and isinstance(df_confirm.index, pd.DatetimeIndex)
                    else pd.DataFrame())
        has_conf  = not conf_day.empty and conf_day["Volume"].sum() > 0
        conf_vwap = bt._vwap(conf_day) if has_conf else None

        i   = len(sday) - 1
        row = sday.iloc[i]
        cl  = float(row["Close"]);  op  = float(row["Open"])
        vol = float(row["Volume"]); vw  = float(vwap_s.iloc[i])
        ef  = float(ema_f.iloc[i]); es  = float(ema_s.iloc[i])
        vm  = float(vol_ma.iloc[i]) if not np.isnan(vol_ma.iloc[i]) else 0.0
        rsi = float(rsi_s.iloc[i])  if not np.isnan(rsi_s.iloc[i])  else 50.0
        st  = int(st_s.iloc[i])

        vol_surge = vm > 0 and vol > vm * bt.V2_VOL_SURGE_MULT

        if has_conf and conf_vwap is not None and len(conf_day) > i:
            conf_cl   = float(conf_day.iloc[i]["Close"])
            conf_vw   = float(conf_vwap.iloc[i])
            conf_bull = conf_cl > conf_vw
            conf_bear = conf_cl < conf_vw
        else:
            conf_bull = conf_bear = True

        # Per-instrument RSI thresholds
        rsi_min_ce = cfg.get("rsi_min_ce", bt.V2_RSI_MIN_CE)
        rsi_max_pe = cfg.get("rsi_max_pe", bt.V2_RSI_MAX_PE)

        raw_buy  = (cl > vw and cl > ef and cl > es and cl > op
                    and vol_surge and rsi > rsi_min_ce and conf_bull and st == 1)
        raw_sell = (cl < vw and cl < ef and cl < es and cl < op
                    and vol_surge and rsi < rsi_max_pe and conf_bear and st == -1)

        if not raw_buy and not raw_sell:
            reasons = []
            if not (cl > vw):           reasons.append(f"Close<VWAP({vw:.4f})")
            if not (cl > ef):           reasons.append(f"Close<EMA9({ef:.4f})")
            if not (cl > es):           reasons.append(f"Close<EMA20({es:.4f})")
            if not vol_surge:           reasons.append(f"Vol {vol/vm:.1f}x<1.5x" if vm > 0 else "Vol N/A")
            if rsi <= rsi_min_ce and rsi >= rsi_max_pe:
                                        reasons.append(f"RSI({rsi:.0f}) neutral (need >{rsi_min_ce} or <{rsi_max_pe})")
            if st not in (1, -1):      reasons.append("ST neutral")
            return None, 0.0, ", ".join(reasons) if reasons else "Conditions not met"

        # Signal strength score — higher = better setup (used to rank instruments)
        vol_ratio = vol / vm if vm > 0 else 0.0
        if raw_buy:
            vwap_dist = max(0.0, cl / vw - 1) * 100
            rsi_str   = max(0.0, rsi - rsi_min_ce) / (100 - rsi_min_ce) * 10
            score     = vwap_dist + (vol_ratio - 1.5) * 2 + rsi_str
            return "BUY_CE", round(score, 3), None
        else:
            vwap_dist = max(0.0, 1 - cl / vw) * 100
            rsi_str   = max(0.0, rsi_max_pe - rsi) / rsi_max_pe * 10
            score     = vwap_dist + (vol_ratio - 1.5) * 2 + rsi_str
            return "BUY_PE", round(score, 3), None

    # ── Instrument selection ──────────────────────────────────────

    def _select_instrument(self, data_dict: dict, df_vix):
        """
        Evaluate all active instruments, apply global VIX filter, then pick the
        instrument with the highest signal score.

        Returns (instr_key, signal, vix_val, filter_reason, audit_list).
        instr_key and signal are None when no tradeable setup is found.
        """
        today = _today()

        # Global VIX filter
        vix_val = None
        if (df_vix is not None and not df_vix.empty
                and isinstance(df_vix.index, pd.DatetimeIndex)):
            vix_rows = df_vix[df_vix.index.date <= today]
            if not vix_rows.empty:
                vix_val = round(float(vix_rows.iloc[-1]["Close"]), 2)
                if not (bt.V2_VIX_MIN <= vix_val <= bt.V2_VIX_MAX):
                    reason = f"VIX {vix_val} outside {bt.V2_VIX_MIN}–{bt.V2_VIX_MAX}"
                    return None, None, vix_val, reason, []

        # Per-instrument evaluation
        audit      = []
        candidates = []
        for key in ACTIVE_INSTRUMENTS:
            cfg = INSTRUMENTS[key]
            # Skip if today is expiry day and skip_expiry_day is set
            expiry_wd = cfg["expiry_weekday"]
            if cfg["skip_expiry_day"] and today.weekday() == expiry_wd:
                entry = {"instrument": key, "display": cfg["display_name"],
                         "signal": None, "score": 0.0, "reason": "Expiry day — skipped"}
                audit.append(entry)
                continue

            d = data_dict.get(key, {})
            raw_sig, score, reason = self._compute_signal(
                key,
                d.get("df_signal"),
                d.get("df_1d"),
                d.get("df_confirm"),
            )
            entry = {
                "instrument": key,
                "display"   : cfg["display_name"],
                "signal"    : raw_sig,
                "score"     : score,
                "reason"    : reason,
            }
            audit.append(entry)
            if raw_sig:
                candidates.append((key, raw_sig, score))

        if not candidates:
            best_reason = " | ".join(
                f"{a['display']}: {a['reason']}" for a in audit if a.get("reason")
            )
            return None, None, vix_val, best_reason or "No signal on any instrument", audit

        # Highest score wins
        best_key, best_sig, best_score = max(candidates, key=lambda x: x[2])
        logger.info(
            f"Instrument selected: {best_key} ({best_sig}) score={best_score:.3f} "
            f"| others: {[(c[0], c[1], c[2]) for c in candidates if c[0] != best_key]}"
        )
        return best_key, best_sig, vix_val, None, audit

    # ── Order placement ───────────────────────────────────────────

    def _order(self, symbol, token, qty, side):
        resp = self._obj.placeOrder({
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
        })
        logger.info(f"ORDER {side} {symbol} qty={qty}: {resp}")
        return resp

    # ── Entry ─────────────────────────────────────────────────────

    def _enter(self, signal: str, instr_key: str):
        cfg      = INSTRUMENTS[instr_key]
        opt_type = "CE" if signal == "BUY_CE" else "PE"
        spot     = self._get_spot_ltp(cfg)
        if not spot:
            logger.warning(f"_enter: cannot get spot LTP for {instr_key}")
            return False

        interval = cfg["strike_interval"]
        strike   = int(round(spot / interval) * interval)
        expiry   = _next_expiry(cfg)
        token, symbol = _find_option(self._scrip, cfg["nfo_name"], strike, opt_type, expiry)
        if not token:
            logger.error(f"_enter: option not found {instr_key} {strike}{opt_type} {expiry}")
            return False

        lot_size  = cfg["lot_size"]
        qty       = self.lots * lot_size
        entry_ltp = self.get_option_ltp(symbol, token)
        if not entry_ltp:
            logger.error(f"_enter: cannot get LTP for {symbol}")
            return False

        # Premium sanity check — skip if option is too cheap (illiquid) or too expensive (IV spike)
        min_prem = cfg.get("min_premium", 30)
        max_prem = cfg.get("max_premium", 700)
        if not (min_prem <= entry_ltp <= max_prem):
            reason = f"Premium ₹{entry_ltp:.0f} outside ₹{min_prem}–₹{max_prem}"
            logger.info(f"_enter: {instr_key} {reason} — skip")
            with self._lock:
                self.sig_info["filter_reason"] = reason
            return False

        if self.paper_mode:
            order_id = "PAPER"
        else:
            resp = self._order(symbol, token, qty, "BUY")
            if not (resp and resp.get("status")):
                self.last_error = f"Buy order failed: {resp}"
                _tg(f"🔴 <b>BUY ORDER FAILED</b>\n"
                    f"Symbol : {symbol}\n"
                    f"Qty    : {qty}\n"
                    f"Error  : {resp}\n"
                    f"Time   : {_now().strftime('%H:%M:%S')}")
                return False
            order_id = resp.get("data", {}).get("orderid", "—")

        with self._lock:
            self.position = {
                "active"       : True,
                "instrument"   : instr_key,
                "display_name" : cfg["display_name"],
                "symbol"       : symbol,
                "token"        : token,
                "side"         : opt_type,
                "strike"       : strike,
                "expiry"       : str(expiry),
                "qty"          : qty,
                "lot_size"     : lot_size,
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
                "sl_warn_count": 0,
                # Per-instrument exit params (locked in at entry)
                "sl_pct"       : cfg.get("sl_pct",         bt.V2_SL_OPTION_PCT),
                "sl_warn_pct"  : cfg.get("sl_warn_pct",    bt.V2_SL_WARN_PCT),
                "tp_pct"       : cfg.get("tp_pct",         bt.V2_TP_OPTION_PCT),
                "partial_pct"  : cfg.get("partial_pct",    bt.V2_PARTIAL_PCT),
                "trail_trigger": cfg.get("trail_trigger",  bt.V2_TRAIL_TRIGGER),
                "trail_floor"  : cfg.get("trail_floor",    bt.V2_TRAIL_FLOOR),
            }
            self.last_signal = "buy" if signal == "BUY_CE" else "sell"

        lots = qty // lot_size
        tag  = "[PAPER] " if self.paper_mode else ""
        logger.info(f"{tag}Position opened: {instr_key} {symbol} {qty}@{entry_ltp}")
        _tg(f"{'📋' if self.paper_mode else '🟢'} <b>{tag}TRADE ENTRY — {cfg['display_name']}</b>\n"
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
        pos = self.position
        if not pos["active"]:
            return

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

        pnl = round((ltp - pos["entry_price"]) * pos["qty"], 2)
        with self._lock:
            self.trades.append({
                "time"       : pos["entry_time"],
                "exit_time"  : _now().strftime("%H:%M"),
                "instrument" : pos.get("instrument", "NIFTY"),
                "symbol"     : pos["symbol"],
                "side"       : pos["side"],
                "strike"     : pos["strike"],
                "entry"      : pos["entry_price"],
                "exit"       : round(ltp, 2),
                "entry_spot" : pos["entry_spot"],
                "qty"        : pos["qty"],
                "pnl"        : pnl,
                "reason"     : reason,
                "paper"      : self.paper_mode,
            })
            self.daily_pnl   += pnl
            self.trade_count += 1
            if pnl > 0:
                self.win_count += 1
            self.position    = _empty_pos()
            self.last_signal = None

        tag  = "[PAPER] " if self.paper_mode else ""
        icon = "✅" if pnl >= 0 else "🔴"
        instr_label = pos.get("display_name", pos.get("instrument", ""))
        logger.info(f"{tag}Position closed: {reason} ltp={ltp} pnl={pnl}")
        _tg(f"{icon} <b>{tag}TRADE EXIT — {reason}</b>\n"
            f"Instrument: {instr_label}\n"
            f"Symbol : {pos['symbol']}\n"
            f"Entry  : ₹{pos['entry_price']:.2f}  Exit: ₹{ltp:.2f}\n"
            f"Qty    : {pos['qty']}\n"
            f"P&L    : {'+'if pnl>=0 else ''}₹{pnl:,.2f}\n"
            f"Daily  : {'+'if self.daily_pnl>=0 else ''}₹{self.daily_pnl:,.2f}\n"
            f"Time   : {_now().strftime('%H:%M')}")
        self._save_state()

    def _partial_exit(self, ltp):
        pos      = self.position
        lot_size = pos.get("lot_size", bt.LOT_SIZE)
        qty      = lot_size

        if not self.paper_mode:
            resp = self._order(pos["symbol"], pos["token"], qty, "SELL")
            if not (resp and resp.get("status")):
                logger.error(f"Partial sell failed: {resp}")
                return

        pnl = round((ltp - pos["entry_price"]) * qty, 2)
        with self._lock:
            self.trades.append({
                "time"       : pos["entry_time"],
                "exit_time"  : _now().strftime("%H:%M"),
                "instrument" : pos.get("instrument", "NIFTY"),
                "symbol"     : pos["symbol"],
                "side"       : pos["side"],
                "strike"     : pos["strike"],
                "entry"      : pos["entry_price"],
                "exit"       : round(ltp, 2),
                "entry_spot" : pos["entry_spot"],
                "qty"        : qty,
                "pnl"        : pnl,
                "reason"     : "PARTIAL_TP",
                "paper"      : self.paper_mode,
            })
            self.daily_pnl              += pnl
            self.position["qty"]        -= qty
            self.position["partial_done"] = True

        tag = "[PAPER] " if self.paper_mode else ""
        logger.info(f"{tag}Partial exit {qty} units @{ltp} pnl={pnl}")
        _tg(f"🟡 <b>{tag}PARTIAL EXIT +20%</b>\n"
            f"Symbol : {pos['symbol']}\n"
            f"Sold   : {qty} qty (1 lot)\n"
            f"LTP    : ₹{ltp:.2f}  P&L: +₹{pnl:,.2f}\n"
            f"Remaining: {pos['qty'] - qty} qty — running to TARGET\n"
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

        with self._lock:
            self.position["live_ltp"] = round(ltp, 2)
            self.position["live_pnl"] = round(pnl_pu * pos["qty"], 2)

        lot_size = pos.get("lot_size", bt.LOT_SIZE)

        # Per-instrument exit params (stored in position at entry time)
        sl_pct        = pos.get("sl_pct",         bt.V2_SL_OPTION_PCT)
        sl_warn_pct   = pos.get("sl_warn_pct",    bt.V2_SL_WARN_PCT)
        tp_pct        = pos.get("tp_pct",         bt.V2_TP_OPTION_PCT)
        partial_pct   = pos.get("partial_pct",    bt.V2_PARTIAL_PCT)
        trail_trigger = pos.get("trail_trigger",  bt.V2_TRAIL_TRIGGER)
        trail_floor_v = pos.get("trail_floor",    bt.V2_TRAIL_FLOOR)

        # Partial exit (only when 2+ lots)
        if (not pos["partial_done"]
                and opt_pct >= partial_pct
                and pos["qty"] >= lot_size * 2):
            self._partial_exit(ltp)

        # Trail activation
        if not pos["trail_on"] and opt_pct >= trail_trigger:
            with self._lock:
                self.position["trail_on"]   = True
                self.position["trail_high"] = ltp

        if pos["trail_on"]:
            with self._lock:
                if ltp > pos["trail_high"]:
                    self.position["trail_high"] = ltp

        trail_floor = trail_floor_v if pos["partial_done"] else 0.0
        trail_exit  = pos["trail_on"] and opt_pct <= trail_floor

        sl_triggered = False
        if opt_pct <= -sl_pct:
            sl_triggered = True
            with self._lock:
                self.position["sl_warn_count"] = 0
        elif opt_pct <= -sl_warn_pct:
            with self._lock:
                self.position["sl_warn_count"] = pos["sl_warn_count"] + 1
            if pos["sl_warn_count"] + 1 >= 2:
                sl_triggered = True
        else:
            with self._lock:
                self.position["sl_warn_count"] = 0

        if   opt_pct >= tp_pct:   self._exit("TARGET",     ltp)
        elif sl_triggered:         self._exit("SL",         ltp)
        elif trail_exit:           self._exit("TRAIL_EXIT", ltp)

    # ── Background loops ──────────────────────────────────────────

    def _signal_loop(self):
        logger.info("Signal loop started")
        while self._running:
            try:
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
                        and self.trade_count < self.max_trades):

                    next_t = _next_candle(now)
                    self.sig_info["time"]       = now.strftime("%H:%M:%S")
                    self.sig_info["next_check"] = next_t.strftime("%H:%M")
                    try:
                        self.get_balance()
                        self._refresh_all_spot_ltps()

                        data_dict, df_vix = self._fetch_all_data()
                        instr_key, raw_signal, vix, reason, audit = self._select_instrument(
                            data_dict, df_vix
                        )

                        # Dedup: don't re-enter same direction as last trade
                        signal = None
                        if raw_signal:
                            direction = "buy" if raw_signal == "BUY_CE" else "sell"
                            if direction == self.last_signal:
                                reason = "Dedup — same direction already traded"
                            else:
                                signal = raw_signal

                        with self._lock:
                            self.sig_info.update({
                                "signal"       : signal,
                                "vix"          : vix,
                                "instrument"   : instr_key if signal else None,
                                "audit"        : audit,
                                "filter_reason": reason if not signal else None,
                            })
                        self.last_error = None

                        if signal:
                            logger.info(f"Signal: {signal} on {instr_key}")
                            self._enter(signal, instr_key)

                    except Exception as e:
                        logger.error(f"Signal check error: {e}", exc_info=True)
                        self.last_error = str(e)
                        self._consec_errors += 1
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
                        self._consec_errors = 0

                self._save_state()

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
        if self._obj is None:
            self.login()

        self.max_trades       = max_trades
        self.lots             = lots
        self.paper_mode       = paper_mode
        self.enabled          = True
        self._monitoring_only = False

        if not self._running:
            self._running    = True
            self._sig_thread = threading.Thread(
                target=self._signal_loop, daemon=True, name="SignalLoop")
            self._mon_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="MonitorLoop")
            self._sig_thread.start()
            self._mon_thread.start()

        self._save_state()
        active_str = ", ".join(ACTIVE_INSTRUMENTS)
        logger.info(f"Trading started: {lots} lot(s), max {max_trades} trades/day | instruments: {active_str}")

    def stop(self):
        self.enabled = True
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
        if self.position["active"]:
            self._exit("MANUAL")
        self._running = False
        self.enabled  = False
        self._save_state()

    def get_state(self) -> dict:
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
            "config"      : {
                "max_trades"          : self.max_trades,
                "lots"                : self.lots,
                "paper"               : self.paper_mode,
                "active_instruments"  : ACTIVE_INSTRUMENTS,
            },
            "market"      : {
                "nifty_ltp"  : self.spot_ltps.get("NIFTY", self.nifty_ltp),
                "vix"        : self.sig_info.get("vix"),
                "spot_ltps"  : dict(self.spot_ltps),
            },
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
        "instrument"   : None,   "display_name" : None,
        "symbol"       : None,   "token"        : None,
        "side"         : None,   "strike"       : 0,
        "expiry"       : None,   "qty"          : 0,
        "lot_size"     : 0,
        "entry_price"  : 0.0,    "entry_spot"   : 0.0,
        "entry_time"   : None,   "partial_done" : False,
        "trail_on"     : False,  "trail_high"   : 0.0,
        "live_ltp"     : 0.0,    "live_pnl"     : 0.0,
        "order_id"     : None,
        "sl_warn_count": 0,
        "paper"        : False,
    }

def _market_open(now: datetime) -> bool:
    ts = now.strftime("%H:%M")
    return MARKET_OPEN <= ts <= MARKET_CLOSE

def _next_candle(now: datetime) -> datetime:
    """Datetime of next 5m candle close + 35 seconds."""
    m      = now.minute
    next_m = ((m // 5) + 1) * 5
    if next_m >= 60:
        base = now.replace(minute=0, second=35, microsecond=0) + timedelta(hours=1)
    else:
        base = now.replace(minute=next_m, second=35, microsecond=0)
    return base
