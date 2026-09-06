"""
live_trader.py — Angel One Smart API live trading engine
Strategy : V2 (VWAP + EMA9/20 + RSI + Volume + BNF + Supertrend + VIX)
Lot size  : 65 (NSE, effective Oct 28 2025)
"""
import os, json, time, threading, requests, struct, ssl, re, uuid
from collections import deque
import websocket
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

def _trim_forming_candle(df: pd.DataFrame) -> pd.DataFrame:
    """
    Angel's intraday API includes the still-forming 5m candle (Close = latest
    LTP) when queried mid-candle, unlike backtest.py which only ever sees
    fully-closed bars. Drop that last row so EMA/RSI/Supertrend and the
    EMA_EXIT check react to a confirmed candle close, not live tick noise.
    """
    if df is None or df.empty:
        return df
    last_start = df.index[-1]
    if last_start.tzinfo is not None:
        last_start = last_start.tz_localize(None)
    if last_start + timedelta(minutes=5) > _now():
        return df.iloc[:-1]
    return df

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
SCRIP_CACHE       = "logs/scrip_nfo.json"
LIVE_STATE_FILE   = "logs/live_state.json"
TRADE_LOG_FILE    = "logs/trade_history.json"
TEST_ORDER_ID_FILE = "logs/test_order_ids.json"
PORTFOLIO_FILE          = "logs/portfolio.json"
PORTFOLIO_TXN_FILE      = "logs/portfolio_transactions.json"
WATCHLIST_FILE          = "logs/watchlist.json"
NSE_INSTRUMENT_CACHE    = "logs/nse_eq_instruments.json"
NSE_INSTRUMENT_CACHE_TTL_DAYS = 7
INSTRUMENT_MASTER_URL   = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# ── India VIX — shown beside NIFTY 50 in the dashboard header ──────
# (exchange, Angel One symbol token, display name, subtitle)
INDEX_QUOTES = [
    ("NSE", "99926017", "INDIA VIX", "Volatility index · lower = calmer"),
]

# ── Sector index cards on the dashboard's "Index" tab ──────────────
# (display name, exchange, index token, subtitle, [(stock symbol, NSE-EQ token), ...])
#
# The 10 constituent stocks per sector are a manually curated snapshot of
# well-known large/mid caps — Angel's API has no live index-weights endpoint,
# so this isn't pulled from an official "top 10 by weight" feed. Revisit
# periodically; NSE rebalances sector indices a few times a year.
SECTOR_INDICES = [
    ("NIFTY 50", "NSE", "99926000", "50 large-cap stocks", [
        ("HDFCBANK", "1333"), ("RELIANCE", "2885"), ("ICICIBANK", "4963"),
        ("INFY", "1594"), ("TCS", "11536"), ("LT", "11483"), ("SBIN", "3045"),
        ("BHARTIARTL", "10604"), ("ITC", "1660"), ("AXISBANK", "5900"),
    ]),
    ("NIFTY BANK", "NSE", "99926009", "Banking sector", [
        ("HDFCBANK", "1333"), ("ICICIBANK", "4963"), ("KOTAKBANK", "1922"),
        ("AXISBANK", "5900"), ("SBIN", "3045"), ("INDUSINDBK", "5258"),
        ("BANKBARODA", "4668"), ("PNB", "10666"), ("AUBANK", "21238"),
        ("FEDERALBNK", "1023"),
    ]),
    ("NIFTY PSU BANK", "NSE", "99926025", "PSU banks", [
        ("SBIN", "3045"), ("BANKBARODA", "4668"), ("PNB", "10666"),
        ("CANBK", "10794"), ("UNIONBANK", "10753"), ("INDIANB", "14309"),
        ("BANKINDIA", "4745"), ("IOB", "9348"), ("UCOBANK", "11223"),
        ("MAHABANK", "11377"),
    ]),
    ("NIFTY PVT BANK", "NSE", "99926047", "Private banks", [
        ("HDFCBANK", "1333"), ("ICICIBANK", "4963"), ("KOTAKBANK", "1922"),
        ("AXISBANK", "5900"), ("INDUSINDBK", "5258"), ("FEDERALBNK", "1023"),
        ("IDFCFIRSTB", "11184"), ("BANDHANBNK", "2263"), ("RBLBANK", "18391"),
        ("AUBANK", "21238"),
    ]),
    ("NIFTY FMCG", "NSE", "99926021", "FMCG sector", [
        ("ITC", "1660"), ("HINDUNILVR", "1394"), ("NESTLEIND", "17963"),
        ("VBL", "18921"), ("BRITANNIA", "547"), ("TATACONSUM", "3432"),
        ("DABUR", "772"), ("MARICO", "4067"), ("GODREJCP", "10099"),
        ("COLPAL", "15141"),
    ]),
    ("NIFTY IT", "NSE", "99926008", "Technology sector", [
        ("TCS", "11536"), ("INFY", "1594"), ("HCLTECH", "7229"),
        ("WIPRO", "3787"), ("TECHM", "13538"), ("KPITTECH", "9683"),
        ("PERSISTENT", "18365"), ("COFORGE", "11543"), ("MPHASIS", "4503"),
        ("OFSS", "10738"),
    ]),
    ("NIFTY MEDIA", "NSE", "99926031", "Media & entertainment", [
        ("ZEEL", "3812"), ("SUNTV", "13404"), ("PVRINOX", "13147"),
        ("SAREGAMA", "4892"), ("NETWORK18", "14111"), ("TIPSMUSIC", "9117"),
        ("NAZARA", "2987"), ("HATHWAY", "18154"), ("DISHTV", "14537"),
        ("BALAJITELE", "9158"),
    ]),
    ("NIFTY ENERGY", "NSE", "99926020", "Energy sector", [
        ("RELIANCE", "2885"), ("ONGC", "2475"), ("NTPC", "11630"),
        ("POWERGRID", "14977"), ("COALINDIA", "20374"), ("BPCL", "526"),
        ("IOC", "1624"), ("GAIL", "4717"), ("TATAPOWER", "3426"),
        ("ADANIGREEN", "3563"),
    ]),
    ("NIFTY METAL", "NSE", "99926030", "Metal & mining", [
        ("TATASTEEL", "3499"), ("JSWSTEEL", "11723"), ("HINDALCO", "1363"),
        ("VEDL", "3063"), ("JINDALSTEL", "6733"), ("SAIL", "2963"),
        ("NMDC", "15332"), ("NATIONALUM", "6364"), ("HINDZINC", "1424"),
        ("APLAPOLLO", "25780"),
    ]),
    ("NIFTY PHARMA", "NSE", "99926023", "Pharma sector", [
        ("SUNPHARMA", "3351"), ("CIPLA", "694"), ("DRREDDY", "881"),
        ("DIVISLAB", "10940"), ("LUPIN", "10440"), ("AUROPHARMA", "275"),
        ("TORNTPHARM", "3518"), ("ALKEM", "11703"), ("ZYDUSLIFE", "7929"),
        ("BIOCON", "11373"),
    ]),
    ("BSE HEALTHCARE", "BSE", "99919009", "Healthcare (BSE index)", [
        ("SUNPHARMA", "3351"), ("APOLLOHOSP", "157"), ("CIPLA", "694"),
        ("DIVISLAB", "10940"), ("DRREDDY", "881"), ("MAXHEALTH", "22377"),
        ("FORTIS", "14592"), ("LUPIN", "10440"), ("AUROPHARMA", "275"),
        ("TORNTPHARM", "3518"),
    ]),
    ("BSE CONSUMER DURABLES", "BSE", "99919008", "Consumer durables (BSE index)", [
        ("TITAN", "3506"), ("VOLTAS", "3718"), ("HAVELLS", "9819"),
        ("CROMPTON", "17094"), ("DIXON", "21690"), ("BLUESTARCO", "8311"),
        ("WHIRLPOOL", "18011"), ("POLYCAB", "9590"), ("AMBER", "1185"),
        ("VGUARD", "15362"),
    ]),
]

# ── Heavyweights tab: full Nifty 50 weighted constituent set, real-time.
# ~49 tokens, one batched quote call (chunked at 40/req) — still light
# enough to poll on the same cadence as the header VIX feed without
# competing with the bot's own signal-loop candle fetches.
#
# (symbol, NSE-EQ token, Nifty 50 index weight %) — weights are a manually
# refreshed snapshot (source: smart-investing.in, cross-checked against a
# second aggregator, both dated 2026-09-04) since Angel's API has no live
# index-weights endpoint. These 49 named constituents summed to 100.00% of
# index weight in that snapshot; NSE reviews/rebalances Nifty 50 membership
# semi-annually (March/September) — revisit this list after each rebalance.
NIFTY50_CONSTITUENTS = [
    ("RELIANCE",   "2885",  9.35), ("BHARTIARTL", "10604", 6.01),
    ("HDFCBANK",   "1333",  5.74), ("ICICIBANK",  "4963",  5.34),
    ("SBIN",       "3045",  4.91), ("TCS",        "11536", 4.35),
    ("BAJFINANCE", "317",   3.45), ("LT",         "11483", 2.85),
    ("HINDUNILVR", "1394",  2.42), ("INFY",       "1594",  2.40),
    ("SUNPHARMA",  "3351",  2.38), ("TITAN",      "3506",  2.32),
    ("KOTAKBANK",  "1922",  2.21), ("MARUTI",     "10999", 2.09),
    ("ADANIENT",   "25",    2.08), ("AXISBANK",   "5900",  2.07),
    ("M&M",        "2031",  2.06), ("ADANIPORTS", "15083", 2.06),
    ("HCLTECH",    "7229",  1.84), ("ULTRACEMCO", "11532", 1.75),
    ("ITC",        "1660",  1.73), ("BAJAJ-AUTO", "16669", 1.71),
    ("JSWSTEEL",   "11723", 1.69), ("NTPC",       "11630", 1.69),
    ("BAJAJFINSV", "16675", 1.65), ("ETERNAL",    "5097",  1.63),
    ("BEL",        "383",   1.55), ("ONGC",       "2475",  1.54),
    ("COALINDIA",  "20374", 1.33), ("POWERGRID",  "14977", 1.29),
    ("SHRIRAMFIN", "4306",  1.28), ("ASIANPAINT", "236",   1.27),
    ("TATASTEEL",  "3499",  1.23), ("HINDALCO",   "1363",  1.19),
    ("GRASIM",     "1232",  1.18), ("EICHERMOT",  "910",   1.09),
    ("INDIGO",     "11195", 1.01), ("SBILIFE",    "21808", 0.93),
    ("WIPRO",      "3787",  0.92), ("JIOFIN",     "18143", 0.83),
    ("TECHM",      "13538", 0.82), ("TRENT",      "1964",  0.79),
    ("APOLLOHOSP", "157",   0.65), ("HDFCLIFE",   "467",   0.62),
    ("TMPV",       "3456",  0.60), ("CIPLA",      "694",   0.58),
    ("TATACONSUM", "3432",  0.52), ("DRREDDY",    "881",   0.50),
    ("MAXHEALTH",  "22377", 0.50),
]
NIFTY50_INDEX_TOKEN      = "99926000"   # real NIFTY 50 spot index (NSE), for comparison vs the weighted basket
HW_VOL_SPIKE_MULT        = 1.2          # ">1.2x 20-day average" volume threshold
HW_AVG_VOL_LOOKBACK_DAYS = 20
# 4/5 and 3/5 breadth thresholds generalized to weighted-%-of-index terms
HW_BULLISH_WEIGHT_PCT = 80.0
HW_BEARISH_WEIGHT_PCT = 60.0

# ── My Portfolio tab: rule-based profit-booking / re-entry thresholds ──
PORTFOLIO_TAKE_PROFIT_DAY_PCT = 4.0     # single-day pop this big -> consider trimming
PORTFOLIO_STOPLOSS_PCT        = -10.0   # overall loss this deep -> flag for review


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


def _mark_test_order(order_id):
    """Record a broker order ID placed by the ⚡ Test Trade button, so the
    dashboard can exclude its real Angel One fills from trade history/stats."""
    if not order_id or order_id == "—":
        return
    os.makedirs("logs", exist_ok=True)
    try:
        ids = []
        if os.path.exists(TEST_ORDER_ID_FILE):
            with open(TEST_ORDER_ID_FILE) as f:
                ids = json.load(f)
        ids.append(str(order_id))
        tmp = TEST_ORDER_ID_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ids, f)
        os.replace(tmp, TEST_ORDER_ID_FILE)
    except Exception as e:
        logger.warning(f"test_order_id log error: {e}")


# ── My Portfolio tab: manually-tracked holdings + cash, persisted to disk ──
# State shape: {"cash": float, "holdings": [{id, symbol, token, qty, buy_price, buy_date}, ...]}

def _load_portfolio_state() -> dict:
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):   # pre-cash-tracking format: a bare holdings list
                return {"cash": 0.0, "holdings": data}
            data.setdefault("cash", 0.0)
            data.setdefault("holdings", [])
            return data
        except Exception as e:
            logger.warning(f"portfolio load error: {e}")
    return {"cash": 0.0, "holdings": []}


def _save_portfolio_state(state: dict):
    os.makedirs("logs", exist_ok=True)
    tmp = PORTFOLIO_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, PORTFOLIO_FILE)


def _append_portfolio_transaction(record: dict):
    """Buy/sell audit trail — also lets recommendations reference recent activity."""
    os.makedirs("logs", exist_ok=True)
    txns = []
    if os.path.exists(PORTFOLIO_TXN_FILE):
        try:
            with open(PORTFOLIO_TXN_FILE) as f:
                txns = json.load(f)
        except Exception as e:
            logger.warning(f"portfolio txn load error: {e}")
    txns.append(record)
    tmp = PORTFOLIO_TXN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(txns, f, indent=2, default=str)
    os.replace(tmp, PORTFOLIO_TXN_FILE)


# ── Watchlist (left rail on the Portfolio tab): price tracking only,
# no quantity/buy-price — just symbols the user wants to keep an eye on ──

def _load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"watchlist load error: {e}")
    return []


def _save_watchlist(stocks: list):
    os.makedirs("logs", exist_ok=True)
    tmp = WATCHLIST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(stocks, f, indent=2)
    os.replace(tmp, WATCHLIST_FILE)


_nse_instrument_cache = None   # in-memory: {SYMBOL -> token}, lazy-loaded from disk/network


def _load_nse_instrument_cache() -> dict:
    """SYMBOL -> NSE-EQ token, for resolving manually-typed portfolio symbols.
    Cached to disk for NSE_INSTRUMENT_CACHE_TTL_DAYS since Angel's full
    instrument master is a ~15MB/140k-row download."""
    global _nse_instrument_cache
    if _nse_instrument_cache is not None:
        return _nse_instrument_cache

    if os.path.exists(NSE_INSTRUMENT_CACHE):
        age_days = (time.time() - os.path.getmtime(NSE_INSTRUMENT_CACHE)) / 86400
        if age_days < NSE_INSTRUMENT_CACHE_TTL_DAYS:
            try:
                with open(NSE_INSTRUMENT_CACHE) as f:
                    _nse_instrument_cache = json.load(f)
                    return _nse_instrument_cache
            except Exception as e:
                logger.warning(f"NSE instrument cache read error: {e}")

    logger.info("Downloading Angel One instrument master for symbol resolution...")
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=60)
    data = resp.json()
    mapping = {
        inst["name"].upper(): inst["token"]
        for inst in data
        if inst.get("exch_seg") == "NSE" and inst.get("symbol", "").endswith(("-EQ", "-SM"))
    }
    os.makedirs("logs", exist_ok=True)
    tmp = NSE_INSTRUMENT_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(mapping, f)
    os.replace(tmp, NSE_INSTRUMENT_CACHE)
    _nse_instrument_cache = mapping
    return mapping


def _resolve_nse_token(symbol: str):
    """Best-effort SYMBOL -> NSE-EQ token lookup. Returns None if unknown."""
    return _load_nse_instrument_cache().get(symbol.strip().upper())


# ── Timing ────────────────────────────────────────────────────────
SQUAREOFF_TIME    = "15:15"
NO_NEW_TRADE_TIME = "14:30"
MARKET_OPEN       = "09:15"
MARKET_CLOSE      = "15:30"

# ── Manual position sizing (dashboard "+1 Lot" / "-1 Lot" buttons) ─
MAX_MANUAL_ADD_LOTS = 2   # cap on extra lots addable to one open position

# ── WebSocket real-time feed ──────────────────────────────────────
_WS_V2_URL       = "wss://smartapisocket.angelone.in/smart-stream"
_TICK_STALE_SECS = 10   # treat tick as stale if older than this; fall back to REST


def _parse_tick(data: bytes):
    """Parse SmartWebSocketV2 LTP-mode binary frame (51 bytes).

    Layout: mode(1) exchange(1) token(25) seq(8) ts(8) ltp_paise(8)
    Returns (token_str, ltp_float) or (None, None).
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 51:
        return None, None
    try:
        token = data[2:27].rstrip(b'\x00').decode('ascii', errors='ignore').strip()
        ltp   = struct.unpack_from('<q', data, 43)[0] / 100.0
        return (token, ltp) if (token and ltp > 0) else (None, None)
    except Exception:
        return None, None


class _TickFeed:
    """Real-time LTP feed via Angel One SmartWebSocketV2.

    Connects in a daemon thread, subscribes a list of NFO tokens, and calls
    on_tick(token, ltp) for every incoming tick. Auto-reconnects on drop.
    """

    def __init__(self, jwt_token: str, client_code: str, feed_token: str, on_tick):
        self._jwt     = jwt_token
        self._code    = client_code
        self._feed    = feed_token
        self._on_tick = on_tick
        self._groups  = {}   # exchangeType -> [token, ...]
        self._ws_app  = None
        self._active  = False

    def subscribe(self, nfo_tokens: list):
        self._groups = {2: [str(t) for t in nfo_tokens]}   # 2 = NFO

    def subscribe_groups(self, groups: dict):
        """groups: {exchangeType: [token, ...]} — lets one feed span multiple
        exchanges (e.g. 1=NSE indices + 3=BSE indices) in a single connection."""
        self._groups = {ex: [str(t) for t in toks] for ex, toks in groups.items()}

    def _sub_msg(self):
        return json.dumps({
            "action": 1,
            "params": {
                "mode": 1,
                "tokenList": [{"exchangeType": ex, "tokens": toks}
                              for ex, toks in self._groups.items()],
            },
        })

    def connect(self):
        """Blocking — run this in a daemon thread."""
        headers = {
            "Authorization": self._jwt,
            "x-api-version": "3",
            "x-client-code": self._code,
            "x-feed-token": self._feed,
        }
        self._active = True

        def _on_open(ws):
            ws.send(self._sub_msg())
            logger.info(f"TickFeed: subscribed {self._groups}")

        def _on_message(ws, message):
            if isinstance(message, (bytes, bytearray)):
                token, ltp = _parse_tick(message)
                if token and ltp:
                    self._on_tick(token, ltp)

        def _on_error(ws, error):
            logger.warning(f"TickFeed error: {error}")

        def _on_close(ws, code, msg):
            logger.info(f"TickFeed closed (code={code})")

        self._ws_app = websocket.WebSocketApp(
            _WS_V2_URL,
            header=headers,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )
        self._ws_app.run_forever(
            sslopt={"cert_reqs": ssl.CERT_NONE},
            ping_interval=25,
            ping_timeout=10,
        )

    def stop(self):
        self._active = False
        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception:
                pass


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
    """
    NEXT week's Tuesday expiry (Nifty weekly expiry moved to Tuesday, NSE 2025)
    — deliberately one cycle further out than the nearest expiry. Trading the
    current week's contract means as little as 0-4 DTE, maximizing theta decay
    speed; research on why option buyers lose money consistently flags this as
    a top mistake. One extra week of time value costs some leverage but gives
    a real cushion against decay eating a move before it plays out. Not
    backtest-validated — backtest.py's constant-delta P&L model doesn't
    simulate theta decay at all, so this can only be judged from live/paper
    results, not backtest numbers.
    """
    today = _today()
    nearest = today + timedelta(days=2)
    for i in range(8):
        d = today + timedelta(days=i)
        if d.weekday() == 1:   # 1 = Tuesday
            nearest = d
            break
    return nearest + timedelta(days=7)


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
        self._feed_token = None
        self._api_key    = None
        self._last_login = None
        self._scrip      = []

        # Runtime flags
        self._running         = False
        self._monitoring_only = False   # stop new entries, keep monitoring
        self._sig_thread      = None
        self._mon_thread      = None

        # Real-time tick feed
        self._tick_ltp  = {}   # token -> (ltp, monotonic_time)
        self._ws_feed   = None
        self._ws_thread = None

        # Header VIX — real-time, independent of trade state: small WS+REST
        # feed, same pattern as the option tick feed but for one index token.
        self._idx_cache      = {}     # token -> {"ltp":, "close":, "ts": monotonic}
        self._vix_ws_feed    = None
        self._vix_started    = False
        self._idx_epoch      = 0      # bumped on relogin so old feed loops exit

        # Sector index tab — deliberately NOT real-time (96 constituent-stock
        # quotes + historical fetches would otherwise compete with the bot's
        # own signal-loop candle fetches during market hours). Refreshed once
        # per day, only after INDEX_REFRESH_AFTER (market close).
        self._sector_started    = False
        self._sector_fetch_date = None
        self._stock_hist      = {}    # token -> {"c7":, "c90":, "c365":} reference closes
        self._stock_hist_date = None

        # Live NIFTY Put-Call Ratio for the header — current snapshot only,
        # Angel's API has no historical PCR (see options_confirmation.py).
        self._pcr_cache = {"value": None, "ts": None}

        # Heavyweights tab — real-time ltp/close (shares _idx_cache above);
        # 20-day avg volume refreshed once per day.
        self._hw_started    = False
        self._hw_avg_vol      = {}    # token -> 20-day avg daily volume
        self._hw_avg_vol_date = None

        # My Portfolio tab — real-time ltp/close for whatever's in
        # logs/portfolio.json (shares _idx_cache above); token set is
        # re-read from disk each poll so newly-added holdings pick up
        # automatically without a restart.
        self._portfolio_started = False

        # Watchlist (left rail on the Portfolio tab) — same real-time pattern.
        self._watchlist_started = False

        # Underlying EMA9 reference — kept fresh even while a position is
        # open, so _manage_position can check EMA_EXIT (backtest.py's
        # dominant exit mechanism, ported here to match validated results).
        self._ema_ref = {"close": None, "ema9": None, "ts": None}

        # Supertrend(10,3) reference for the "supertrend" strategy — mirrors
        # _ema_ref's role but for Strategy 6's single-indicator flip signal.
        self._st_ref = {"value": None, "prev": None, "ts": None}

        # Daily (prev-close) data never changes intraday — cache per day
        # instead of re-hitting Angel One's historical API every ~2 minutes.
        self._daily_cache = {"date": None, "df": None}

        # Config (set by start())
        self.strategy      = "v2"   # "v2" | "supertrend" — which signal/exit path runs
        self.max_trades   = 2
        self.lots         = 1
        self.enabled      = False
        self.paper_mode   = False   # True = simulate orders, no real API calls
        self.max_daily_loss       = bt.MAX_DAILY_LOSS        # e.g. -8000; block entries once breached
        self.daily_profit_target  = bt.DAILY_PROFIT_TARGET   # e.g. 6000; block entries once hit
        self.loss_cooldown_candles = bt.V2_LOSS_COOLDOWN_CANDLES  # 0 disables
        self.manual_target_pct    = None   # e.g. 0.10 for +10% -- user-set option-price take-
                                            # profit, checked alongside (not instead of) every
                                            # strategy's own SL/trail/flip/reversal logic; None
                                            # disables it. Settable live via /api/manual-target.
        self.carry_overnight      = False  # skip EOD_SQUAREOFF and hold the position into the
                                            # next trading day, UNLESS today is the contract's
                                            # own expiry date (a dying weekly option can't be
                                            # carried past its own expiry regardless of this flag).

        # Daily state
        self.position     = _empty_pos()
        self.trades       = []
        self.daily_pnl    = 0.0
        self.win_count    = 0
        self.trade_count  = 0
        self.last_signal  = None   # "buy" | "sell"  dedup
        self._daily_cap_logged   = False   # so the daily-cap Telegram alert fires once/day
        self._cooldown_remaining = {"buy": 0, "sell": 0}  # closed candles left before re-entry allowed
        self._cooldown_last_ts   = None    # last candle timestamp we decremented cooldowns for

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

        # Recent-activity feed for the dashboard — real events only (scans,
        # entries, exits), newest first, bounded so it can't grow unbounded.
        self._activity = deque(maxlen=20)

    def _log_activity(self, kind: str, text: str):
        self._activity.appendleft({"time": _now().strftime("%H:%M"), "kind": kind, "text": text})

    # ── Session management ────────────────────────────────────────

    def login(self):
        from login import login as _do_login
        try:
            obj, auth, feed_token, _ = _do_login()
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
        self._obj        = obj
        self._auth       = auth
        self._feed_token = feed_token
        self._api_key    = os.getenv("ANGEL_API_KEY", "")
        self._last_login = _now()
        self._scrip     = _load_scrip()
        self.connected  = True
        self.last_error = None
        _setup_logfile()

        # Re-login (initial or 6.5h refresh) invalidates any running feed's
        # auth token — bump the epoch so old feed/REST loops exit instead of
        # piling up, then rebuild fresh.
        self._idx_epoch += 1
        if self._vix_ws_feed:
            self._vix_ws_feed.stop()
            self._vix_ws_feed = None
        self._vix_started   = False
        self._sector_started = False
        self._hw_started     = False
        self._portfolio_started = False
        self._watchlist_started = False
        self._start_vix_feed()
        self._start_sector_feed()
        self._start_heavyweights_feed()
        self._start_portfolio_feed()
        self._start_watchlist_feed()
        logger.info("AngelTrader: login OK")

    def _ensure_session(self):
        # Double-checked locking: many Flask request threads can call this at
        # once (e.g. right after a restart) and race to call login()
        # concurrently — Angel One's session semantics make a losing
        # concurrent login clobber self.connected back to False even though
        # the winning one already has a working session.
        if self._obj is None:
            with self._lock:
                if self._obj is None:
                    self.login()
            return
        if self._last_login and (_now() - self._last_login).total_seconds() > 6.5 * 3600:
            with self._lock:
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

    # ── Header VIX (real-time) + Sector Index tab (once-daily) ──────
    # Two deliberately separate feeds. VIX is a single token, cheap to keep
    # live via WebSocket. The sector-index tab's ~11 indices + ~96
    # constituent stocks are NOT kept real-time — that many quote/historical
    # calls competing with the bot's own signal-loop candle fetches during
    # market hours is exactly the overlap risk flagged and asked to be
    # removed. Instead it refreshes once per day, only after market close.

    INDEX_REFRESH_AFTER = "15:30"   # sector tab only refreshes after this time

    @staticmethod
    def _quote_token_groups(entries):
        """entries: iterable of (exchange, token, ...) tuples (INDEX_QUOTES'
        or SECTOR_INDICES' shape both start this way). Returns {exchange:
        [token,...]}."""
        groups = {}
        for exch, token, *_rest in entries:
            groups.setdefault(exch, set()).add(token)
        return {exch: sorted(toks) for exch, toks in groups.items()}

    @staticmethod
    def _sector_token_groups():
        groups = {}
        for _name, exch, token, _subtitle, stocks in SECTOR_INDICES:
            groups.setdefault(exch, set()).add(token)
            for _sym, stock_token in stocks:
                groups.setdefault("NSE", set()).add(stock_token)
        return {exch: sorted(toks) for exch, toks in groups.items()}

    def _fetch_quote_batch(self, token_groups: dict):
        """Batched quote calls (chunked to stay under Angel's per-request
        cap) — refreshes ltp+close(+volume) in self._idx_cache for every
        token in token_groups."""
        now = time.monotonic()
        for exch, tokens in token_groups.items():
            for i in range(0, len(tokens), 40):
                if i:
                    time.sleep(0.25)   # space out chunked requests, stay under Angel's per-second cap
                chunk = tokens[i:i + 40]
                resp = self._obj.getMarketData("FULL", {exch: chunk})
                if not (resp and resp.get("status") and resp.get("data")):
                    continue
                for row in resp["data"].get("fetched", []):
                    token = str(row.get("symbolToken"))
                    try:
                        ltp   = float(row.get("ltp"))
                        close = float(row.get("close"))
                    except (TypeError, ValueError):
                        continue
                    entry = self._idx_cache.setdefault(token, {})
                    entry["close"] = close
                    entry["ltp"]   = ltp
                    entry["ts"]    = now
                    try:
                        entry["volume"] = float(row.get("tradeVolume"))
                    except (TypeError, ValueError):
                        pass
                    for dst, src in (("high", "high"), ("low", "low"),
                                     ("week52_high", "52WeekHigh"), ("week52_low", "52WeekLow")):
                        try:
                            entry[dst] = float(row.get(src))
                        except (TypeError, ValueError):
                            pass

    @staticmethod
    def _closest_close(closes, target_date):
        """Last daily close at or before target_date, from a Close Series
        indexed by tz-aware timestamp (as returned by angel_data._fetch_daily)."""
        mask = closes.index.date <= target_date
        if not mask.any():
            return None
        return float(closes[mask].iloc[-1])

    def _fetch_stock_history(self):
        """Refresh 7d/90d/1y reference closes for every constituent stock."""
        today = _today()
        from angel_data import _fetch_daily
        api_key = os.getenv("ANGEL_API_KEY", "")
        seen, hist = set(), {}
        for _name, _exch, _idx_token, _subtitle, stocks in SECTOR_INDICES:
            for sym, token in stocks:
                if token in seen:
                    continue
                seen.add(token)
                try:
                    df = _fetch_daily(self._auth, api_key, token,
                                      today - timedelta(days=400), today)
                    if not df.empty:
                        closes = df["Close"]
                        hist[token] = {
                            "c7":   self._closest_close(closes, today - timedelta(days=7)),
                            "c90":  self._closest_close(closes, today - timedelta(days=90)),
                            "c365": self._closest_close(closes, today - timedelta(days=365)),
                        }
                except Exception as e:
                    logger.warning(f"stock history {sym}: {e}")
                time.sleep(0.35)   # stay well under Angel's historical-API rate limit
        # If most fetches failed (e.g. Angel rate-limiting/403s), don't mark
        # today as "done" — that would silently strand the 7D/90D/1Y columns
        # blank until tomorrow. Keep whatever partial data we got and let the
        # caller retry later the same day.
        if len(hist) < len(seen) * 0.5:
            logger.warning(f"Stock history fetch mostly failed ({len(hist)}/{len(seen)}) — will retry later today")
            self._stock_hist.update(hist)
            return False
        self._stock_hist      = hist
        self._stock_hist_date = today
        logger.info(f"Stock history refreshed: {len(hist)} symbols")
        return True

    # ── VIX feed: real-time, WS + REST ───────────────────────────────

    def _start_vix_feed(self):
        if self._vix_started or not (self._auth and self._feed_token):
            return
        self._vix_started = True
        my_epoch = self._idx_epoch
        vix_groups = self._quote_token_groups(INDEX_QUOTES)

        def _rest_loop():
            while self._idx_epoch == my_epoch:
                try:
                    self._fetch_quote_batch(vix_groups)
                except Exception as e:
                    logger.warning(f"VIX REST refresh: {e}")
                time.sleep(20)

        def _on_tick(token, ltp):
            entry = self._idx_cache.setdefault(token, {})
            entry["ltp"] = ltp
            entry["ts"]  = time.monotonic()

        def _ws_loop():
            client_code = os.getenv("ANGEL_CLIENT_ID", "")
            groups = {1 if exch == "NSE" else 3: tokens for exch, tokens in vix_groups.items()}
            while self._idx_epoch == my_epoch:
                try:
                    feed = _TickFeed(self._auth, client_code, self._feed_token, _on_tick)
                    feed.subscribe_groups(groups)
                    self._vix_ws_feed = feed
                    feed.connect()   # blocks until dropped
                except Exception as e:
                    logger.warning(f"VIX TickFeed disconnected: {e}")
                if self._idx_epoch == my_epoch:
                    time.sleep(3)

        try:
            self._fetch_quote_batch(vix_groups)   # seed before the first request lands
        except Exception as e:
            logger.warning(f"VIX REST seed: {e}")

        threading.Thread(target=_rest_loop, daemon=True, name="VixRest").start()
        threading.Thread(target=_ws_loop, daemon=True, name="VixTickFeed").start()

    def get_indices(self):
        try:
            self._ensure_session()
            self._start_vix_feed()
            out = []
            now = time.monotonic()
            for exch, token, name, subtitle in INDEX_QUOTES:
                entry = self._idx_cache.get(token, {})
                ltp, close, ts = entry.get("ltp"), entry.get("close"), entry.get("ts")
                change = pct = None
                if ltp is not None and close:
                    change = round(ltp - close, 2)
                    pct    = round(change / close * 100, 2)
                out.append({
                    "name": name, "subtitle": subtitle, "exchange": exch,
                    "ltp": round(ltp, 2) if ltp is not None else None,
                    "change": change, "pct_change": pct,
                    "live": bool(ts and (now - ts) < 25),   # > the 20s REST refresh cadence
                })
            return out
        except Exception as e:
            logger.warning(f"get_indices: {e}")
            return []

    def get_nifty_pcr(self):
        """Live NIFTY Put-Call Ratio for the header — cached 60s (putCallRatio()
        returns the full market's PCR list, so it isn't polled every request)."""
        now = time.monotonic()
        if self._pcr_cache["ts"] is not None and (now - self._pcr_cache["ts"]) < 60:
            return self._pcr_cache["value"]
        try:
            self._ensure_session()
            resp = self._obj.putCallRatio()
            if resp and resp.get("status"):
                for row in resp.get("data", []):
                    if re.match(r"^NIFTY\d", row.get("tradingSymbol", "")):
                        self._pcr_cache = {"value": round(float(row["pcr"]), 3), "ts": now}
                        return self._pcr_cache["value"]
        except Exception as e:
            logger.warning(f"get_nifty_pcr: {e}")
        return self._pcr_cache["value"]

    # ── Sector Index tab: once-daily snapshot, no WebSocket ──────────

    def _start_sector_feed(self):
        if self._sector_started or not (self._auth and self._feed_token):
            return
        self._sector_started = True
        my_epoch = self._idx_epoch

        def _daily_loop():
            while self._idx_epoch == my_epoch:
                try:
                    self._maybe_refresh_sector_snapshot()
                except Exception as e:
                    logger.warning(f"sector snapshot refresh: {e}")
                time.sleep(300)   # check every 5 min whether today's post-close snapshot is due

        threading.Thread(target=_daily_loop, daemon=True, name="SectorDailySnapshot").start()

    def _maybe_refresh_sector_snapshot(self):
        today = _today()
        if self._sector_fetch_date == today:
            return
        if _now().strftime("%H:%M") < self.INDEX_REFRESH_AFTER:
            return
        self._fetch_quote_batch(self._sector_token_groups())
        if self._fetch_stock_history():
            self._sector_fetch_date = today
            logger.info(f"Sector index snapshot refreshed for {today}")

    def get_sector_indices(self):
        try:
            self._ensure_session()
            self._start_sector_feed()
            is_today = self._sector_fetch_date == _today()
            out = []
            for name, exch, token, subtitle, stocks in SECTOR_INDICES:
                entry = self._idx_cache.get(token, {})
                ltp, close = entry.get("ltp"), entry.get("close")
                change = pct = None
                if ltp is not None and close:
                    change = round(ltp - close, 2)
                    pct    = round(change / close * 100, 2)

                stock_rows = []
                for sym, stock_token in stocks:
                    se   = self._idx_cache.get(stock_token, {})
                    sltp = se.get("ltp")
                    hist = self._stock_hist.get(stock_token, {})

                    def _pct_vs(ref):
                        if sltp is None or not ref:
                            return None
                        return round((sltp - ref) / ref * 100, 2)

                    stock_rows.append({
                        "symbol": sym,
                        "ltp":    round(sltp, 2) if sltp is not None else None,
                        "chg_7d":  _pct_vs(hist.get("c7")),
                        "chg_90d": _pct_vs(hist.get("c90")),
                        "chg_1y":  _pct_vs(hist.get("c365")),
                    })

                out.append({
                    "name": name, "exchange": exch, "subtitle": subtitle,
                    "ltp": round(ltp, 2) if ltp is not None else None,
                    "change": change, "pct_change": pct,
                    "live": is_today,   # "live" here means "today's post-close snapshot", not real-time
                    "stocks": stock_rows,
                })
            return out
        except Exception as e:
            logger.warning(f"get_sector_indices: {e}")
            return []

    # ── Heavyweights tab: full Nifty 50 weighted constituent set, real-time ──

    def _start_heavyweights_feed(self):
        if self._hw_started or not (self._auth and self._feed_token):
            return
        self._hw_started = True
        my_epoch = self._idx_epoch
        tokens = [token for _s, token, _w in NIFTY50_CONSTITUENTS] + [NIFTY50_INDEX_TOKEN]
        hw_groups = {"NSE": tokens}

        def _rest_loop():
            while self._idx_epoch == my_epoch:
                try:
                    self._fetch_quote_batch(hw_groups)
                except Exception as e:
                    logger.warning(f"Heavyweights REST refresh: {e}")
                time.sleep(15)

        try:
            self._fetch_quote_batch(hw_groups)   # seed before the first request lands
        except Exception as e:
            logger.warning(f"Heavyweights REST seed: {e}")

        threading.Thread(target=_rest_loop, daemon=True, name="HeavyweightsRest").start()

        # 49 sequential historical-candle fetches (~20-30s) — must run off the
        # request thread, same reasoning as the sector tab's _fetch_stock_history.
        def _avgvol_loop():
            while self._idx_epoch == my_epoch:
                try:
                    self._maybe_refresh_hw_avg_volumes()
                except Exception as e:
                    logger.warning(f"Heavyweights avg-volume refresh: {e}")
                time.sleep(300)   # cheap re-check; the method itself no-ops once today's done

        threading.Thread(target=_avgvol_loop, daemon=True, name="HeavyweightsAvgVol").start()

    def _maybe_refresh_hw_avg_volumes(self):
        """20-trading-day average daily volume per constituent (excludes
        today) — refreshed once per day, cheap enough to do anytime."""
        today = _today()
        if self._hw_avg_vol_date == today:
            return
        from angel_data import _fetch_daily
        avg_vol = {}
        for sym, token, _w in NIFTY50_CONSTITUENTS:
            try:
                df = _fetch_daily(self._auth, self._api_key, token,
                                  today - timedelta(days=60), today)
                df  = df[df.index.date < today]
                vol = df["Volume"].tail(HW_AVG_VOL_LOOKBACK_DAYS)
                if len(vol):
                    avg_vol[token] = float(vol.mean())
            except Exception as e:
                logger.warning(f"Heavyweights 20d avg volume fetch failed for {sym}: {e}")
            time.sleep(0.35)   # stay under Angel's historical-API rate limit
        if avg_vol:
            self._hw_avg_vol.update(avg_vol)
            self._hw_avg_vol_date = today

    @staticmethod
    def _classify_heavyweights(rows):
        """BULLISH BOOM / FALSE RALLY-WEAK / BEARISH DRAG / SIDEWAYS, using
        index-weight-adjusted breadth (not raw stock counts) and the
        weight-adjusted mean volume-ratio as the "volume" gate. The 4/5 and
        3/5 thresholds from the original top-5 rule generalize to 80%/60%
        of total index weight."""
        weighed = [r for r in rows if r["pct_change"] is not None]
        total_w = sum(r["weight"] for r in weighed) or 1.0
        pos_w = sum(r["weight"] for r in weighed if r["pct_change"] > 0)
        neg_w = sum(r["weight"] for r in weighed if r["pct_change"] < 0)
        pos_pct = pos_w / total_w * 100
        neg_pct = neg_w / total_w * 100

        vol_rows  = [r for r in rows if r["vol_ratio"] is not None]
        vol_w     = sum(r["weight"] for r in vol_rows) or 1.0
        avg_ratio = sum(r["weight"] * r["vol_ratio"] for r in vol_rows) / vol_w
        high_vol  = avg_ratio > HW_VOL_SPIKE_MULT

        if pos_pct >= HW_BULLISH_WEIGHT_PCT and high_vol:
            return {"signal": "BULLISH BOOM", "tone": "bullish",
                    "reason": f"{pos_pct:.0f}% of index weight up on {avg_ratio:.2f}x avg volume"}
        if neg_pct >= HW_BEARISH_WEIGHT_PCT and high_vol:
            return {"signal": "BEARISH DRAG", "tone": "bearish",
                    "reason": f"{neg_pct:.0f}% of index weight down on {avg_ratio:.2f}x avg volume"}
        if pos_pct >= HW_BEARISH_WEIGHT_PCT and not high_vol:
            return {"signal": "FALSE RALLY / WEAK", "tone": "weak",
                    "reason": f"{pos_pct:.0f}% of index weight up but only {avg_ratio:.2f}x avg volume"}
        return {"signal": "SIDEWAYS / NO TREND", "tone": "flat",
                "reason": f"mixed/flat, {avg_ratio:.2f}x avg volume"}

    def get_heavyweights(self):
        try:
            self._ensure_session()
            self._start_heavyweights_feed()   # also kicks off the (async) avg-volume refresh
            now = time.monotonic()
            rows = []
            for sym, token, weight in NIFTY50_CONSTITUENTS:
                entry = self._idx_cache.get(token, {})
                ltp, close, ts = entry.get("ltp"), entry.get("close"), entry.get("ts")
                volume  = entry.get("volume")
                avg_vol = self._hw_avg_vol.get(token)

                pct_change = round((ltp - close) / close * 100, 2) if ltp is not None and close else None
                vol_ratio  = round(volume / avg_vol, 2) if volume is not None and avg_vol else None

                rows.append({
                    "symbol": sym, "weight": weight,
                    "ltp": round(ltp, 2) if ltp is not None else None,
                    "pct_change": pct_change,
                    "volume": volume, "avg_volume": avg_vol, "vol_ratio": vol_ratio,
                    "live": bool(ts and (now - ts) < 25),
                })
            result = self._classify_heavyweights(rows)
            result["pos_weight_pct"] = round(
                sum(r["weight"] for r in rows if r["pct_change"] is not None and r["pct_change"] > 0), 1)
            result["neg_weight_pct"] = round(
                sum(r["weight"] for r in rows if r["pct_change"] is not None and r["pct_change"] < 0), 1)
            result["stocks"] = rows
            result["time"] = _now().strftime("%H:%M:%S")

            # Real NIFTY 50 index (live) vs the weighted-basket-implied change,
            # so the tab can show whether the index move is broad-based or
            # driven by a narrow slice of heavyweights.
            idx_entry = self._idx_cache.get(NIFTY50_INDEX_TOKEN, {})
            idx_ltp, idx_close, idx_ts = idx_entry.get("ltp"), idx_entry.get("close"), idx_entry.get("ts")
            idx_pct = round((idx_ltp - idx_close) / idx_close * 100, 2) if idx_ltp is not None and idx_close else None
            weighed = [r for r in rows if r["pct_change"] is not None]
            basket_pct = (round(sum(r["weight"] * r["pct_change"] for r in weighed) /
                                 (sum(r["weight"] for r in weighed) or 1.0), 2)
                          if weighed else None)
            result["index"] = {
                "ltp": round(idx_ltp, 2) if idx_ltp is not None else None,
                "pct_change": idx_pct,
                "basket_pct_change": basket_pct,
                "live": bool(idx_ts and (now - idx_ts) < 25),
            }
            return result
        except Exception as e:
            logger.warning(f"get_heavyweights: {e}")
            return {"signal": "SIDEWAYS / NO TREND", "tone": "flat", "reason": "data unavailable",
                    "stocks": [], "index": {}, "time": _now().strftime("%H:%M:%S")}

    # ── My Portfolio tab: manually-tracked holdings + cash, real-time ─

    def _start_portfolio_feed(self):
        if self._portfolio_started or not (self._auth and self._feed_token):
            return
        self._portfolio_started = True
        my_epoch = self._idx_epoch

        def _rest_loop():
            while self._idx_epoch == my_epoch:
                try:
                    holdings = _load_portfolio_state()["holdings"]
                    tokens = sorted({h["token"] for h in holdings if h.get("token")})
                    if tokens:
                        self._fetch_quote_batch({"NSE": tokens})
                except Exception as e:
                    logger.warning(f"Portfolio REST refresh: {e}")
                time.sleep(15)

        threading.Thread(target=_rest_loop, daemon=True, name="PortfolioRest").start()

    @staticmethod
    def _classify_holding(day_change_pct, pnl_pct):
        """Rule-based profit-booking / re-entry hint for one holding."""
        if day_change_pct is not None and day_change_pct >= PORTFOLIO_TAKE_PROFIT_DAY_PCT and (pnl_pct or 0) > 0:
            return {"signal": "TAKE PARTIAL PROFIT", "tone": "bullish",
                    "note": f"Up {day_change_pct:.1f}% today — consider booking some quantity here "
                            f"and waiting for a pullback before adding back."}
        if day_change_pct is not None and day_change_pct <= -PORTFOLIO_TAKE_PROFIT_DAY_PCT:
            if (pnl_pct or 0) > 0:
                return {"signal": "WATCH FOR RE-ENTRY", "tone": "weak",
                        "note": f"Down {day_change_pct:.1f}% today but still in profit overall — "
                                f"a pullback worth watching for a possible re-entry if it stabilizes."}
            return {"signal": "WEAK — MONITOR", "tone": "bearish",
                    "note": f"Down {day_change_pct:.1f}% today and currently underwater — watch closely."}
        if pnl_pct is not None and pnl_pct <= PORTFOLIO_STOPLOSS_PCT:
            return {"signal": "REVIEW — DEEP LOSS", "tone": "bearish",
                    "note": f"{pnl_pct:.1f}% below your buy price — reassess the thesis or your stop-loss plan."}
        return {"signal": "HOLD", "tone": "flat", "note": "No unusual move today."}

    def get_portfolio(self):
        try:
            self._ensure_session()
            self._start_portfolio_feed()
            now = time.monotonic()
            state = _load_portfolio_state()
            holdings = state["holdings"]

            rows = []
            for h in holdings:
                token = h.get("token")
                entry = self._idx_cache.get(token, {}) if token else {}
                ltp, close, ts = entry.get("ltp"), entry.get("close"), entry.get("ts")
                qty, buy_price = float(h["qty"]), float(h["buy_price"])

                day_change_pct = round((ltp - close) / close * 100, 2) if ltp is not None and close else None
                invested       = round(qty * buy_price, 2)
                current_value  = round(qty * ltp, 2) if ltp is not None else None
                pnl            = round(current_value - invested, 2) if current_value is not None else None
                pnl_pct        = round((ltp - buy_price) / buy_price * 100, 2) if ltp is not None and buy_price else None
                day_pnl        = round(qty * (ltp - close), 2) if ltp is not None and close else None
                week52_high    = entry.get("week52_high")
                off_52w_high   = round((ltp - week52_high) / week52_high * 100, 2) if ltp is not None and week52_high else None

                verdict = self._classify_holding(day_change_pct, pnl_pct)
                rows.append({
                    "id": h["id"], "symbol": h["symbol"], "qty": qty, "buy_price": buy_price,
                    "buy_date": h.get("buy_date"),
                    "ltp": round(ltp, 2) if ltp is not None else None,
                    "day_change_pct": day_change_pct, "invested": invested,
                    "current_value": current_value, "pnl": pnl, "pnl_pct": pnl_pct,
                    "day_pnl": day_pnl, "off_52w_high_pct": off_52w_high,
                    "signal": verdict["signal"], "tone": verdict["tone"], "note": verdict["note"],
                    "live": bool(ts and (now - ts) < 25),
                })

            priced = [r for r in rows if r["current_value"] is not None]
            cash = round(float(state.get("cash", 0.0)), 2)
            totals = {
                "cash":          cash,
                "invested":      round(sum(r["invested"] for r in rows), 2),
                "current_value": round(sum(r["current_value"] for r in priced), 2),
                "pnl":           round(sum(r["pnl"] for r in priced), 2),
                "day_pnl":       round(sum(r["day_pnl"] for r in priced if r["day_pnl"] is not None), 2),
                "realized_pnl":  round(float(state.get("realized_pnl", 0.0)), 2),
            }
            totals["pnl_pct"] = (round(totals["pnl"] / totals["invested"] * 100, 2)
                                  if totals["invested"] else None)
            totals["total_value"] = round(cash + totals["current_value"], 2)

            return {"holdings": rows, "totals": totals, "time": _now().strftime("%H:%M:%S")}
        except Exception as e:
            logger.warning(f"get_portfolio: {e}")
            return {"holdings": [], "totals": {}, "time": _now().strftime("%H:%M:%S"), "error": str(e)}

    def set_portfolio_cash(self, amount: float):
        self._ensure_session()
        if amount < 0:
            raise ValueError("Funds can't be negative.")
        state = _load_portfolio_state()
        state["cash"] = float(amount)
        _save_portfolio_state(state)
        return state["cash"]

    def add_portfolio_holding(self, symbol: str, qty: float, buy_price: float, buy_date: str = None):
        """Bookkeeping entry for a position you already hold — does NOT
        touch tracked cash (use buy_holding for a purchase funded from it)."""
        self._ensure_session()
        symbol = symbol.strip().upper()
        token = _resolve_nse_token(symbol)
        if not token:
            raise ValueError(f"Couldn't find '{symbol}' on the NSE — check the ticker spelling.")
        if qty <= 0 or buy_price <= 0:
            raise ValueError("Quantity and buy price must both be positive.")

        state = _load_portfolio_state()
        entry = {
            "id": uuid.uuid4().hex[:12], "symbol": symbol, "token": token,
            "qty": qty, "buy_price": buy_price, "buy_date": buy_date,
        }
        state["holdings"].append(entry)
        _save_portfolio_state(state)

        # New token — seed it into the cache immediately so it's not blank
        # until the next 15s poll.
        try:
            self._fetch_quote_batch({"NSE": [token]})
        except Exception as e:
            logger.warning(f"seed quote for new holding {symbol}: {e}")
        return entry

    def remove_portfolio_holding(self, holding_id: str):
        state = _load_portfolio_state()
        remaining = [h for h in state["holdings"] if h["id"] != holding_id]
        if len(remaining) == len(state["holdings"]):
            raise ValueError("Holding not found.")
        state["holdings"] = remaining
        _save_portfolio_state(state)

    def buy_holding(self, symbol: str, qty: float, price: float, buy_date: str = None):
        """A purchase funded from tracked cash — blends into an existing
        position (weighted-average cost) or opens a new one, and deducts
        the cost from available funds."""
        self._ensure_session()
        symbol = symbol.strip().upper()
        token = _resolve_nse_token(symbol)
        if not token:
            raise ValueError(f"Couldn't find '{symbol}' on the NSE — check the ticker spelling.")
        if qty <= 0 or price <= 0:
            raise ValueError("Quantity and price must both be positive.")

        cost = round(qty * price, 2)
        state = _load_portfolio_state()
        if cost > state.get("cash", 0.0) + 1e-6:
            raise ValueError(f"Not enough funds: this buy costs ₹{cost:,.2f} but only "
                              f"₹{state.get('cash', 0.0):,.2f} is available.")

        existing = next((h for h in state["holdings"] if h["symbol"] == symbol), None)
        if existing:
            old_qty, old_price = float(existing["qty"]), float(existing["buy_price"])
            new_qty = old_qty + qty
            existing["buy_price"] = round((old_qty * old_price + qty * price) / new_qty, 4)
            existing["qty"] = new_qty
            entry = existing
        else:
            entry = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "token": token,
                      "qty": qty, "buy_price": price, "buy_date": buy_date}
            state["holdings"].append(entry)

        state["cash"] = round(state.get("cash", 0.0) - cost, 2)
        _save_portfolio_state(state)
        _append_portfolio_transaction({"ts": _now().isoformat(), "type": "buy", "symbol": symbol,
                                        "qty": qty, "price": price, "amount": cost})

        try:
            self._fetch_quote_batch({"NSE": [token]})
        except Exception as e:
            logger.warning(f"seed quote for buy {symbol}: {e}")
        return entry

    def sell_holding(self, holding_id: str, qty: float, price: float):
        """Sells (fully or partially) a tracked holding, crediting the
        proceeds back to available funds and logging realized P&L."""
        self._ensure_session()
        if qty <= 0 or price <= 0:
            raise ValueError("Quantity and price must both be positive.")

        state = _load_portfolio_state()
        holding = next((h for h in state["holdings"] if h["id"] == holding_id), None)
        if not holding:
            raise ValueError("Holding not found.")
        held_qty = float(holding["qty"])
        if qty > held_qty + 1e-6:
            raise ValueError(f"You only hold {held_qty:g} shares of {holding['symbol']}.")

        proceeds = round(qty * price, 2)
        realized = round(qty * (price - float(holding["buy_price"])), 2)

        if qty >= held_qty - 1e-6:
            state["holdings"] = [h for h in state["holdings"] if h["id"] != holding_id]
        else:
            holding["qty"] = round(held_qty - qty, 4)

        state["cash"] = round(state.get("cash", 0.0) + proceeds, 2)
        state["realized_pnl"] = round(state.get("realized_pnl", 0.0) + realized, 2)
        _save_portfolio_state(state)
        _append_portfolio_transaction({"ts": _now().isoformat(), "type": "sell", "symbol": holding["symbol"],
                                        "qty": qty, "price": price, "amount": proceeds, "realized_pnl": realized})
        return {"symbol": holding["symbol"], "qty": qty, "price": price,
                "proceeds": proceeds, "realized_pnl": realized}

    # ── Watchlist tab (left rail): price-only tracking ───────────────

    def _start_watchlist_feed(self):
        if self._watchlist_started or not (self._auth and self._feed_token):
            return
        self._watchlist_started = True
        my_epoch = self._idx_epoch

        def _rest_loop():
            while self._idx_epoch == my_epoch:
                try:
                    stocks = _load_watchlist()
                    tokens = sorted({s["token"] for s in stocks if s.get("token")})
                    if tokens:
                        self._fetch_quote_batch({"NSE": tokens})
                except Exception as e:
                    logger.warning(f"Watchlist REST refresh: {e}")
                time.sleep(15)

        threading.Thread(target=_rest_loop, daemon=True, name="WatchlistRest").start()

    def get_watchlist(self):
        try:
            self._ensure_session()
            self._start_watchlist_feed()
            now = time.monotonic()
            rows = []
            for s in _load_watchlist():
                entry = self._idx_cache.get(s.get("token"), {})
                ltp, close, ts = entry.get("ltp"), entry.get("close"), entry.get("ts")
                pct_change = round((ltp - close) / close * 100, 2) if ltp is not None and close else None
                rows.append({
                    "id": s["id"], "symbol": s["symbol"],
                    "ltp": round(ltp, 2) if ltp is not None else None,
                    "pct_change": pct_change,
                    "live": bool(ts and (now - ts) < 25),
                    "fundamentals": s.get("fundamentals"),
                })
            return {"stocks": rows, "time": _now().strftime("%H:%M:%S")}
        except Exception as e:
            logger.warning(f"get_watchlist: {e}")
            return {"stocks": [], "time": _now().strftime("%H:%M:%S"), "error": str(e)}

    def add_watchlist_symbol(self, symbol: str):
        self._ensure_session()
        symbol = symbol.strip().upper()
        token = _resolve_nse_token(symbol)
        if not token:
            raise ValueError(f"Couldn't find '{symbol}' on the NSE — check the ticker spelling.")

        stocks = _load_watchlist()
        if any(s["symbol"] == symbol for s in stocks):
            raise ValueError(f"{symbol} is already on your watchlist.")
        entry = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "token": token}
        stocks.append(entry)
        _save_watchlist(stocks)

        try:
            self._fetch_quote_batch({"NSE": [token]})   # seed immediately, don't wait for the next 15s poll
        except Exception as e:
            logger.warning(f"seed quote for new watch {symbol}: {e}")
        return entry

    def remove_watchlist_symbol(self, watch_id: str):
        stocks = _load_watchlist()
        remaining = [s for s in stocks if s["id"] != watch_id]
        if len(remaining) == len(stocks):
            raise ValueError("Symbol not found.")
        _save_watchlist(remaining)

    def set_watchlist_fundamentals(self, symbol: str, fundamentals: dict):
        """Attaches a fundamentals snapshot (PE, 3Y profit/sales growth, ROE,
        debt/equity, promoter holding) to a watchlist entry. This is NOT
        live data — Angel's API has no fundamentals, and Yahoo Finance
        (yfinance) is unreliable from this network, so snapshots are
        gathered on request (e.g. from screener.in) and dated with
        `as_of` rather than continuously refreshed."""
        symbol = symbol.strip().upper()
        stocks = _load_watchlist()
        entry = next((s for s in stocks if s["symbol"] == symbol), None)
        if not entry:
            raise ValueError(f"{symbol} is not on your watchlist.")
        entry["fundamentals"] = fundamentals
        _save_watchlist(stocks)
        return entry

    # ── Recommendations: cross-references Heavyweights (index/market
    # trend), My Portfolio (holdings + cash), and Watchlist (buy
    # candidates) into a short list of rule-based hints. Not investment
    # advice — every card traces back to a number already on the dashboard.

    def get_recommendations(self):
        try:
            hw = self.get_heavyweights()
            pf = self.get_portfolio()
            wl = self.get_watchlist()
            cards = []

            tone = hw.get("tone", "flat")
            market_note = {
                "bullish": "Broad-based strength with volume backing — a supportive backdrop for adding exposure.",
                "bearish": "Broad-based weakness with volume backing — a reasonable day to hold cash or trim rather than buy.",
                "weak":    "The move looks thin on volume — don't chase it.",
                "flat":    "No strong directional signal today — stay selective.",
            }.get(tone, "")
            cards.append({"type": "market", "tone": tone,
                          "title": f"Market: {hw.get('signal', 'SIDEWAYS / NO TREND')}",
                          "text": f"{hw.get('reason', '')}. {market_note}"})

            for h in pf.get("holdings", []):
                if h["signal"] != "HOLD":
                    cards.append({"type": "holding", "tone": h["tone"],
                                  "title": f"{h['symbol']}: {h['signal']}", "text": h["note"]})

            cash = pf.get("totals", {}).get("cash") or 0.0
            wl_stocks = wl.get("stocks", [])
            if cash < 1:
                cards.append({"type": "cash", "tone": "flat", "title": "No funds tracked",
                              "text": "Set your available funds on the Portfolio tab so I can suggest where to deploy them."})
            elif tone == "bearish":
                cards.append({"type": "cash", "tone": "weak", "title": f"₹{cash:,.0f} in cash — consider waiting",
                              "text": f"{hw.get('signal')} on the index right now. Holding cash rather than "
                                      f"buying into broad weakness is a reasonable call."})
            else:
                dippers = sorted([s for s in wl_stocks if s.get("pct_change") is not None and s["pct_change"] < 0],
                                  key=lambda s: s["pct_change"])
                if not dippers:
                    cards.append({"type": "cash", "tone": "flat", "title": f"₹{cash:,.0f} in cash",
                                  "text": "Nothing on your watchlist is pulling back today — add candidates there "
                                          "so I can flag dips worth a look."})
                else:
                    best = dippers[0]
                    afford = int(cash // best["ltp"]) if best.get("ltp") else 0
                    if afford > 0:
                        cards.append({"type": "cash", "tone": "bullish",
                                      "title": f"₹{cash:,.0f} available — {best['symbol']} is down {best['pct_change']:.2f}% today",
                                      "text": f"On your watchlist and pulling back while the index is "
                                              f"{hw.get('signal','').lower()}. At ₹{best['ltp']:.2f}/share you "
                                              f"could buy up to {afford} share(s) with your available funds."})
                    else:
                        cards.append({"type": "cash", "tone": "flat", "title": f"₹{cash:,.0f} in cash",
                                      "text": f"{best['symbol']} is down {best['pct_change']:.2f}% today but even "
                                              f"1 share (₹{best['ltp']:.2f}) exceeds your available funds."})

            return {"cards": cards, "time": _now().strftime("%H:%M:%S")}
        except Exception as e:
            logger.warning(f"get_recommendations: {e}")
            return {"cards": [], "time": _now().strftime("%H:%M:%S"), "error": str(e)}

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

        df_nbees = _trim_forming_candle(_fetch_intraday(self._auth, self._api_key,
                                   NIFTYBEES_TOKEN, lookback, today))
        time.sleep(1)   # space out Angel One historical-API calls — avoid rate-limit throttling
        df_bnf   = _trim_forming_candle(_fetch_intraday(self._auth, self._api_key,
                                   BANKBEES_TOKEN, lookback, today))

        # Daily (prev-close) data never changes intraday — fetch once per day
        # and cache, instead of re-hitting the API every ~2-minute cycle.
        if self._daily_cache["date"] == today and self._daily_cache["df"] is not None:
            df_nifty_1d = self._daily_cache["df"]
        else:
            time.sleep(1)
            df_1d = _fetch_daily(self._auth, self._api_key,
                                 NIFTYBEES_TOKEN, lookback, today)
            df_nifty_1d = df_1d.copy()
            if not df_nifty_1d.empty:
                for col in ["Open", "High", "Low", "Close"]:
                    df_nifty_1d[col] = (df_nifty_1d[col] * NIFTY_MULTIPLIER).round(2)
                self._daily_cache = {"date": today, "df": df_nifty_1d}
            # empty result (fetch failed) — leave cache empty, retry next cycle

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

    def _fetch_nbees_only(self):
        """
        Lightweight fetch — just NIFTYBEES 5m candles, used to refresh the
        EMA9 reference while a position is open. Skips BankNifty and daily
        data (not needed for EMA_EXIT), roughly a third of the API calls
        _fetch_live_data would make for the same purpose.
        """
        today    = _today()
        lookback = today - timedelta(days=12)
        return _trim_forming_candle(_fetch_intraday(self._auth, self._api_key, NIFTYBEES_TOKEN, lookback, today))

    # ── Signal detection (mirrors backtest V2 logic exactly) ─────

    def _update_ema_ref(self, df_nbees):
        """
        Refresh the cached underlying (NIFTYBEES) close + EMA9, used by
        _manage_position's EMA_EXIT check. Called from _check_signal (when no
        position is open) and directly from _signal_loop (when a position IS
        open, since _check_signal itself is skipped in that case).
        """
        try:
            if df_nbees is None or df_nbees.empty or not isinstance(df_nbees.index, pd.DatetimeIndex):
                return
            today  = _today()
            all_5m = df_nbees[df_nbees.index.date <= today].between_time("09:15", "15:30")
            sday   = all_5m[all_5m.index.date == today]
            if sday.empty:
                return
            ema_f = all_5m["Close"].ewm(span=bt.V2_EMA_FAST, adjust=False).mean().loc[sday.index]
            self._ema_ref = {"close": float(sday.iloc[-1]["Close"]), "ema9": float(ema_f.iloc[-1]),
                             "ts": sday.index[-1]}
        except Exception as e:
            logger.warning(f"EMA ref update failed: {e}")

    def _update_st_ref(self, df_nbees):
        """
        Refresh the cached Supertrend(10,3) value for the "supertrend" strategy
        (Strategy 6). Computed on the *entire* continuous multi-day candle
        series with no daily reset — matches supertrend_45day_sl_backtest.py's
        core design (the indicator's state carries across the overnight gap;
        only the position itself is flattened at EOD).
        """
        try:
            if df_nbees is None or df_nbees.empty or not isinstance(df_nbees.index, pd.DatetimeIndex):
                return
            all_5m = df_nbees.between_time("09:15", "15:30")
            if len(all_5m) < bt.ST6_PERIOD + 2:
                return
            st_s = bt._supertrend(all_5m, bt.ST6_PERIOD, bt.ST6_MULT)
            self._st_ref = {
                "value": int(st_s.iloc[-1]),
                "prev":  int(st_s.iloc[-2]),
                "ts":    st_s.index[-1],
            }
        except Exception as e:
            logger.warning(f"Supertrend ref update failed: {e}")

    def _check_signal_supertrend(self, df_nbees):
        """
        Strategy 6: Supertrend(10,3) directional follower — single indicator,
        no confirmation stack (no VIX/time-window/RSI/EMA/BNF/ADX), plus a
        global loss cooldown (blocks BOTH directions, unlike V2's same-direction-
        only cooldown) — added after the 2026-08-25 PE-stop-then-CE-whipsaw
        incident, validated via supertrend_45day_trail_backtest.py
        --cooldown-sweep --global. Flip Red(-1)->Green(1) => BUY_CE. Flip
        Green(1)->Red(-1) => BUY_PE. Matches supertrend_45day_sl_backtest.py
        exactly, plus the cooldown.
        """
        self._update_st_ref(df_nbees)
        value, prev = self._st_ref.get("value"), self._st_ref.get("prev")
        if value is None or prev is None:
            self.sig_info["filter_reason"] = "Warming up — not enough candles yet"
            return None

        # Loss cooldown: decrement remaining-candle counters once per newly
        # observed closed candle (not once per poll, which can be more frequent).
        if self.loss_cooldown_candles > 0:
            cur_candle_ts = self._st_ref.get("ts")
            if cur_candle_ts != self._cooldown_last_ts:
                for _d in ("buy", "sell"):
                    if self._cooldown_remaining[_d] > 0:
                        self._cooldown_remaining[_d] -= 1
                self._cooldown_last_ts = cur_candle_ts
        cooling = self.loss_cooldown_candles > 0 and (
            self._cooldown_remaining["buy"] > 0 or self._cooldown_remaining["sell"] > 0)

        if value == 1 and prev == -1:
            if cooling:
                self.sig_info["filter_reason"] = f"Loss cooldown active ({max(self._cooldown_remaining.values())} candle(s) left)"
                return None
            self.sig_info["filter_reason"] = None
            return "BUY_CE"
        if value == -1 and prev == 1:
            if cooling:
                self.sig_info["filter_reason"] = f"Loss cooldown active ({max(self._cooldown_remaining.values())} candle(s) left)"
                return None
            self.sig_info["filter_reason"] = None
            return "BUY_PE"
        self.sig_info["filter_reason"] = f"Supertrend {'up' if value == 1 else 'down'} — no flip"
        return None

    def _check_signal(self, df_nbees, df_1d, df_bnf, df_vix):
        """
        Run V2 indicator logic on latest closed 5m candle.
        Returns ("BUY_CE" | "BUY_PE" | None, vix_value | None)
        Also updates self.sig_info["filter_reason"] with why no signal fired.
        """
        # Global cutoff — no new trades after NO_NEW_TRADE_TIME, either strategy.
        if _now().strftime("%H:%M") >= NO_NEW_TRADE_TIME:
            self.sig_info["filter_reason"] = f"No new trades after {NO_NEW_TRADE_TIME}"
            return None, None

        if self.strategy == "supertrend":
            return self._check_signal_supertrend(df_nbees), None

        self._update_ema_ref(df_nbees)
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

        # Loss cooldown: decrement remaining-candle counters once per newly
        # observed closed candle (not once per poll, which can be more frequent).
        if self.loss_cooldown_candles > 0:
            cur_candle_ts = sday.index[-1]
            if cur_candle_ts != self._cooldown_last_ts:
                for _d in ("buy", "sell"):
                    if self._cooldown_remaining[_d] > 0:
                        self._cooldown_remaining[_d] -= 1
                self._cooldown_last_ts = cur_candle_ts

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
        vwap_s   = bt._vwap(sday)
        _prev_v  = df_nbees[df_nbees.index.date < today].between_time("09:15", "15:30").tail(20)
        if not _prev_v.empty:
            _vbase = pd.concat([_prev_v, sday])
            _vm    = _vbase["Volume"].rolling(20, min_periods=5).mean()
            vol_ma = pd.Series(_vm.values[len(_prev_v):], index=sday.index)
        else:
            vol_ma = sday["Volume"].rolling(20, min_periods=5).mean()
        ema_f   = all_5m["Close"].ewm(span=bt.V2_EMA_FAST, adjust=False).mean().loc[sday.index]
        ema_s   = all_5m["Close"].ewm(span=bt.V2_EMA_SLOW, adjust=False).mean().loc[sday.index]
        rsi_s   = bt._rsi(all_5m["Close"], bt.V2_RSI_PERIOD).loc[sday.index]
        st_s    = bt._supertrend(all_5m, bt.V2_ST_PERIOD, bt.V2_ST_MULT).loc[sday.index]
        adx_s   = bt._adx(all_5m, bt.V2_ADX_PERIOD).loc[sday.index]

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
        adx = float(adx_s.iloc[i])  if not np.isnan(adx_s.iloc[i])  else 0.0

        if has_bnf and bnf_vwap is not None and len(bnf_day) > i:
            bnf_cl   = float(bnf_day.iloc[i]["Close"])
            bnf_vw   = float(bnf_vwap.iloc[i])
            bnf_bull = bnf_cl > bnf_vw;  bnf_bear = bnf_cl < bnf_vw
        else:
            bnf_bull = bnf_bear = True

        # Volume-surge requirement removed (2026-07-14 finding): it disproportionately
        # caught capitulation/climax candles rather than genuine trend starts —
        # validated over 30/60-day backtests to improve trade count and P&L.
        raw_buy  = (cl > vw and cl > ef and cl > es and cl > op
                    and rsi > bt.V2_RSI_MIN_CE and bnf_bull and st == 1)
        raw_sell = (cl < vw and cl < ef and cl < es and cl < op
                    and rsi < bt.V2_RSI_MAX_PE and bnf_bear and st == -1)

        # ADX regime filter: only enter when the market is actually trending
        # (ADX > V2_ADX_MIN) — skips choppy/range-bound conditions where the
        # other 7 conditions can still align by noise.
        adx_blocked = adx <= bt.V2_ADX_MIN and (raw_buy or raw_sell)
        if adx_blocked:
            if raw_buy:
                logger.info(f"Signal check: BUY_CE suppressed — ADX {adx:.1f} <= {bt.V2_ADX_MIN} (choppy regime)")
            if raw_sell:
                logger.info(f"Signal check: BUY_PE suppressed — ADX {adx:.1f} <= {bt.V2_ADX_MIN} (choppy regime)")
            raw_buy  = False
            raw_sell = False

        # Move-from-open filter: skip if the bulk of the move already happened
        day_open_s = float(sday.iloc[0]["Open"]) if not sday.empty else 0.0
        if day_open_s > 0 and bt.V2_MAX_FROM_OPEN_PCT > 0:
            if raw_buy  and (cl - day_open_s) / day_open_s * 100 > bt.V2_MAX_FROM_OPEN_PCT:
                self.sig_info["filter_reason"] = f"Move filter: already up {(cl-day_open_s)/day_open_s*100:.2f}% from open"
                raw_buy = False
            if raw_sell and (day_open_s - cl) / day_open_s * 100 > bt.V2_MAX_FROM_OPEN_PCT:
                self.sig_info["filter_reason"] = f"Move filter: already down {(day_open_s-cl)/day_open_s*100:.2f}% from open"
                raw_sell = False

        # Loss cooldown: after a losing exit, block re-entry in that same
        # direction for loss_cooldown_candles closed candles (0 = disabled).
        cooldown_block_buy  = self.loss_cooldown_candles > 0 and self._cooldown_remaining["buy"]  > 0
        cooldown_block_sell = self.loss_cooldown_candles > 0 and self._cooldown_remaining["sell"] > 0
        if raw_buy and cooldown_block_buy:
            logger.info(f"Signal check: BUY_CE suppressed — loss cooldown "
                        f"({self._cooldown_remaining['buy']} candle(s) remaining)")
            raw_buy = False
        if raw_sell and cooldown_block_sell:
            logger.info(f"Signal check: BUY_PE suppressed — loss cooldown "
                        f"({self._cooldown_remaining['sell']} candle(s) remaining)")
            raw_sell = False

        # Build human-readable reason for dashboard when no signal fires
        if not raw_buy and not raw_sell:
            if cooldown_block_buy or cooldown_block_sell:
                sides = []
                if cooldown_block_buy:  sides.append(f"CE ({self._cooldown_remaining['buy']} left)")
                if cooldown_block_sell: sides.append(f"PE ({self._cooldown_remaining['sell']} left)")
                self.sig_info["filter_reason"] = "Loss cooldown active — " + ", ".join(sides)
            elif adx_blocked:
                self.sig_info["filter_reason"] = f"ADX {adx:.1f} <= {bt.V2_ADX_MIN} (choppy regime)"
            else:
                reasons = []
                if not (cl > vw):      reasons.append(f"Close({cl:.2f})<VWAP({vw:.2f})")
                if not (cl > ef):      reasons.append(f"Close<EMA9({ef:.2f})")
                if not (cl > es):      reasons.append(f"Close<EMA20({es:.2f})")
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

    def _confirm_order(self, order_id, attempts: int = 6, delay: float = 1.0):
        """Poll orderBook() to find out whether `order_id` actually filled,
        was rejected/cancelled, or is still unresolved — placeOrder()
        returning status:true only means the order was ACCEPTED for
        submission, not that it executed. A market order on a liquid NFO
        option normally resolves within 1-2s, so this is a short, bounded
        poll (default ~6s), not a long-running wait.

        Returns (status, fill_price, raw_row):
          "complete" - confirmed filled; fill_price is the average fill price
          "rejected" - confirmed rejected/cancelled by the exchange; fill_price is None
          "unknown"  - could not confirm either way within the poll window
                       (API hiccup, not order ambiguity) — caller should
                       proceed optimistically but alert for manual reconciliation
        """
        for _ in range(attempts):
            try:
                ob = self._obj.orderBook()
                if ob and ob.get("status") and ob.get("data"):
                    for row in ob["data"]:
                        if str(row.get("orderid")) != str(order_id):
                            continue
                        st = str(row.get("orderstatus", "")).strip().lower()
                        if st in ("complete", "executed"):
                            fp = float(row.get("averageprice") or 0) or None
                            return "complete", fp, row
                        if st in ("rejected", "cancelled", "canceled"):
                            return "rejected", None, row
                        break   # open / trigger pending / pending -> keep polling
            except Exception as e:
                logger.warning(f"orderBook poll failed for order {order_id}: {e}")
            time.sleep(delay)
        return "unknown", None, None

    # ── Entry ─────────────────────────────────────────────────────

    def _enter(self, signal, force_strike=None, is_test=False, is_reversal=False):
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

        # Balance check — required capital = full premium (options buying, no margin).
        # If the configured lot count doesn't fit, scale down to whatever whole
        # number of lots the available cash covers instead of skipping outright.
        required = round(entry_ltp * qty, 2)
        if not self.paper_mode and self.balance > 0 and required > self.balance:
            affordable_lots = int(self.balance // (entry_ltp * bt.LOT_SIZE))
            if affordable_lots < 1:
                # Even 1 lot of the ATM strike doesn't fit — walk further OTM
                # (same side, same expiry; premium drops the further OTM you go)
                # instead of skipping the trade outright.
                otm_step = 50 if opt_type == "CE" else -50
                switched = False
                for i in range(1, 11):  # scan up to 500 pts further OTM
                    alt_strike = strike + otm_step * i
                    alt_token, alt_symbol = _find_option(self._scrip, alt_strike, opt_type, expiry)
                    if not alt_token:
                        continue
                    alt_ltp = self.get_option_ltp(alt_symbol, alt_token)
                    if not alt_ltp or alt_ltp * bt.LOT_SIZE > self.balance:
                        continue
                    logger.info(f"ATM {symbol}@{entry_ltp} too expensive for balance "
                                f"₹{self.balance:,.0f} — switching to further OTM "
                                f"{alt_symbol}@{alt_ltp}")
                    strike, token, symbol, entry_ltp = alt_strike, alt_token, alt_symbol, alt_ltp
                    qty      = bt.LOT_SIZE
                    required = round(entry_ltp * qty, 2)
                    switched = True
                    break
                if not switched:
                    msg = (f"Insufficient balance: need ₹{entry_ltp * bt.LOT_SIZE:,.0f} for 1 lot, "
                           f"available ₹{self.balance:,.0f} — no cheaper OTM strike found either — "
                           f"skipping {symbol}")
                    logger.warning(msg)
                    self.last_error = msg
                    _tg(f"⚠️ <b>Trade Skipped — Low Balance</b>\n"
                        f"Need   : ₹{entry_ltp * bt.LOT_SIZE:,.0f} (1 lot)\n"
                        f"Available: ₹{self.balance:,.0f}\n"
                        f"Symbol : {symbol}\n"
                        f"Time   : {_now().strftime('%H:%M:%S')}")
                    return False
            else:
                logger.info(f"Scaling entry down: {qty // bt.LOT_SIZE} lot(s) -> {affordable_lots} lot(s) "
                            f"to fit available balance ₹{self.balance:,.0f}")
                qty      = affordable_lots * bt.LOT_SIZE
                required = round(entry_ltp * qty, 2)

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
            order_status, fill_price, _row = self._confirm_order(order_id)
            if order_status == "rejected":
                msg = f"Buy order for {symbol} qty={qty} was accepted then REJECTED/CANCELLED by the exchange (order {order_id})."
                logger.error(msg)
                self.last_error = msg
                _tg(f"🔴 <b>BUY ORDER REJECTED AFTER ACCEPTANCE</b>\n"
                    f"Symbol : {symbol}\n"
                    f"Qty    : {qty}\n"
                    f"Order  : {order_id}\n"
                    f"No position was opened — nothing to reconcile.\n"
                    f"Time   : {_now().strftime('%H:%M:%S')}")
                return False
            if order_status == "complete" and fill_price:
                entry_ltp = fill_price
            elif order_status == "unknown":
                _tg(f"⚠️ <b>ORDER STATUS UNCONFIRMED</b>\n"
                    f"Buy order for {symbol} qty={qty} (order {order_id}) was accepted, "
                    f"but the fill/reject status couldn't be confirmed in time. Proceeding "
                    f"as if filled — please verify on the Angel One app and reconcile "
                    f"manually if it wasn't.\n"
                    f"Time   : {_now().strftime('%H:%M:%S')}")
            if is_test:
                _mark_test_order(order_id)

        with self._lock:
            self.position = {
                "active"       : True,
                "exiting"      : False,
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
                "is_reversal"  : is_reversal,
                "partial_done" : False,
                "trail_on"     : False,
                "trail_high"   : entry_ltp,
                "live_ltp"     : entry_ltp,
                "live_pnl"     : 0.0,
                "order_id"     : order_id,
                "manual_adds"  : 0,
                "paper"        : self.paper_mode,
                "is_test"      : is_test,
                "realized_pnl" : 0.0,
                "sl_warn_count"     : 0,   # consecutive polls in premium backstop zone
                "spot_sl_warn_count": 0,   # consecutive polls with spot beyond SL threshold
                "ema_warn_count"    : 0,     # consecutive CLOSED candles crossed back through EMA9
                "ema_last_candle_ts": None,  # candle timestamp last counted, so polls within the
                                             # same closed candle don't re-increment ema_warn_count
                "st_last_candle_ts" : self._st_ref.get("ts"),  # supertrend strategy: seed with
                                             # the current candle's ts so the flip check only
                                             # reacts to a candle that closes AFTER entry, not
                                             # the stale pre-entry reading (was None, which made
                                             # the very first poll treat any already-against
                                             # supertrend value as a fresh flip -- instantly
                                             # killing NEG_REVERSAL_EXIT reversals before the trend
                                             # actually turned in their favor).
            }
            self.last_signal = "buy" if signal == "BUY_CE" else "sell"

        lots = qty // bt.LOT_SIZE
        tag  = ("[TEST] " if is_test else "") + ("[PAPER] " if self.paper_mode else "")
        logger.info(f"{tag}Position opened: {symbol} {qty}@{entry_ltp}")
        self._log_activity("entry", f"{tag}BUY {opt_type} — {symbol} @ ₹{entry_ltp:.2f} ({lots} lot{'s' if lots!=1 else ''})")
        _tg(f"{'📋' if self.paper_mode else '🟢'} <b>{tag}TRADE ENTRY</b>\n"
            f"Symbol : {symbol}\n"
            f"Type   : {opt_type}\n"
            f"Lots   : {lots} ({qty} qty)\n"
            f"LTP    : ₹{entry_ltp:.2f}\n"
            f"Spot   : ₹{spot:.2f}\n"
            f"Time   : {_now().strftime('%H:%M')}")
        self._save_state()
        self._start_ws_feed(token)
        return True

    # ── Exit (full or partial) ────────────────────────────────────

    def _exit(self, reason, ltp=None):
        with self._lock:
            if not self.position["active"] or self.position.get("exiting"):
                return
            self.position["exiting"] = True  # claim the exit — prevents duplicate from other thread
        pos = dict(self.position)

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
                with self._lock:
                    # Release the claim — the position is still genuinely open (sell never
                    # went through), so keep managing it instead of orphaning it as "closed".
                    self.position["exiting"] = False
                return
            sell_order_id = resp.get("data", {}).get("orderid")
            order_status, fill_price, _row = self._confirm_order(sell_order_id)
            if order_status == "rejected":
                self.last_error = f"Sell order for {pos['symbol']} was accepted then rejected/cancelled (order {sell_order_id})"
                logger.error(self.last_error)
                _tg(f"🔴 <b>EXIT REJECTED AFTER ACCEPTANCE — MANUAL ACTION NEEDED</b>\n"
                    f"Symbol : {pos['symbol']}\n"
                    f"Qty    : {pos['qty']}\n"
                    f"Reason : {reason}\n"
                    f"Order  : {sell_order_id}\n"
                    f"The position is still OPEN at the broker — the bot will keep "
                    f"managing it, but please check the Angel One app.\n"
                    f"Time   : {_now().strftime('%H:%M:%S')}")
                with self._lock:
                    # Release the claim — the position is still genuinely open (sell
                    # was rejected), so keep managing it instead of orphaning it as "closed".
                    self.position["exiting"] = False
                return
            if order_status == "complete" and fill_price:
                ltp = fill_price
            elif order_status == "unknown":
                _tg(f"⚠️ <b>EXIT STATUS UNCONFIRMED</b>\n"
                    f"Sell order {sell_order_id} for {pos['symbol']} was accepted, but "
                    f"fill/reject status couldn't be confirmed in time. Proceeding as "
                    f"closed — please verify on the Angel One app and reconcile "
                    f"manually if it wasn't actually filled.\n"
                    f"Time   : {_now().strftime('%H:%M:%S')}")
            if pos.get("is_test"):
                _mark_test_order(sell_order_id)

        self._stop_ws_feed()
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
            "is_test"   : pos.get("is_test", False),
        }
        is_test = pos.get("is_test", False)
        total_trade_pnl = pos.get("realized_pnl", 0.0) + pnl
        with self._lock:
            if not is_test:
                self.trades.append(trade_record)
                self.daily_pnl   += pnl
                self.trade_count += 1
                if pnl > 0:
                    self.win_count += 1
            self.position  = _empty_pos()
            self.last_signal = None
            if not is_test and self.loss_cooldown_candles > 0 and total_trade_pnl < 0:
                if self.strategy == "supertrend":
                    # Global cooldown: a loss blocks BOTH directions, since
                    # V2's same-direction-only cooldown wouldn't have stopped
                    # the 2026-08-25 PE-stop-then-CE-whipsaw pattern.
                    self._cooldown_remaining["buy"]  = self.loss_cooldown_candles
                    self._cooldown_remaining["sell"] = self.loss_cooldown_candles
                else:
                    direction = "buy" if pos["side"] == "CE" else "sell"
                    self._cooldown_remaining[direction] = self.loss_cooldown_candles

        if not is_test and self.loss_cooldown_candles > 0 and total_trade_pnl < 0:
            logger.warning(f"Loss cooldown: blocking new {pos['side']} entries for "
                           f"{self.loss_cooldown_candles} closed candle(s) (trade pnl=₹{total_trade_pnl:,.2f})")

        if not is_test:
            _append_trade_log(trade_record)

        tag  = ("[TEST] " if is_test else "") + ("[PAPER] " if self.paper_mode else "")
        icon = "✅" if pnl >= 0 else "🔴"
        logger.info(f"{tag}Position closed: {reason} ltp={ltp} pnl={pnl}")
        self._log_activity("exit" if pnl >= 0 else "exit_loss",
                           f"{tag}SELL {pos['side']} — {pos['symbol']} @ ₹{ltp:.2f} "
                           f"({'+' if pnl>=0 else ''}₹{pnl:,.2f}, {reason})")
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
            self.position["realized_pnl"] = self.position.get("realized_pnl", 0.0) + pnl

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

    def add_lot(self):
        """
        Manually add 1 lot to the active position at the current option LTP
        (dashboard "+1 Lot" button). Recomputes entry_price as the qty-weighted
        average of the old position and the new fill, so downstream SL/target/
        trailing logic (which all key off a single scalar entry_price) keeps
        working unchanged. Capped at MAX_MANUAL_ADD_LOTS manual adds per trade.
        Returns (ok: bool, message: str).
        """
        with self._lock:
            if not self.position["active"]:
                return False, "No active position"
            if self.position.get("exiting"):
                return False, "Position busy — try again in a moment"
            self.position["exiting"] = True   # claim: block concurrent auto-exit while we add
            pos = dict(self.position)

        try:
            if _now().strftime("%H:%M") >= NO_NEW_TRADE_TIME:
                return False, f"No new exposure after {NO_NEW_TRADE_TIME}"

            manual_adds = pos.get("manual_adds", 0)
            if manual_adds >= MAX_MANUAL_ADD_LOTS:
                return False, f"Manual add limit reached (+{MAX_MANUAL_ADD_LOTS} lots max)"

            add_qty = bt.LOT_SIZE
            ltp = self.get_option_ltp(pos["symbol"], pos["token"])
            if not ltp:
                return False, "Could not fetch option LTP"

            if not self.paper_mode and self.balance > 0 and (ltp * add_qty) > self.balance:
                return False, f"Insufficient balance: need ₹{ltp*add_qty:,.0f}, available ₹{self.balance:,.0f}"

            fill = ltp
            if not self.paper_mode:
                resp = self._order(pos["symbol"], pos["token"], add_qty, "BUY")
                if not (resp and resp.get("status")):
                    msg = resp.get("message", "") if isinstance(resp, dict) else str(resp)
                    logger.error(f"Manual add-lot buy failed: {resp}")
                    return False, f"Buy order failed: {msg}"
                order_id = resp.get("data", {}).get("orderid", "—")
                order_status, fill_price, _row = self._confirm_order(order_id)
                if order_status == "rejected":
                    logger.error(f"Manual add-lot order {order_id} was accepted then rejected/cancelled")
                    _tg(f"🔴 <b>ADD-LOT REJECTED AFTER ACCEPTANCE</b>\n"
                        f"Symbol : {pos['symbol']}\n"
                        f"Order  : {order_id}\n"
                        f"No lot was added — bot quantity is unchanged.\n"
                        f"Time   : {_now().strftime('%H:%M:%S')}")
                    return False, "Add-lot order was rejected/cancelled by the exchange — no lot added"
                if order_status == "complete" and fill_price:
                    fill = fill_price
                elif order_status == "unknown":
                    _tg(f"⚠️ <b>ADD-LOT STATUS UNCONFIRMED</b>\n"
                        f"Order {order_id} for {pos['symbol']} was accepted, but fill/reject "
                        f"status couldn't be confirmed in time. Proceeding as if filled — "
                        f"please verify on the Angel One app.\n"
                        f"Time   : {_now().strftime('%H:%M:%S')}")

            with self._lock:
                if not self.position["active"]:
                    _tg(f"⚠️ <b>MANUAL ADD-LOT MISMATCH</b>\n"
                        f"Bought +{add_qty} qty of {pos['symbol']} @ ₹{fill:.2f} but the "
                        f"position closed while the order was in flight — the bot is no "
                        f"longer tracking this lot. Please reconcile on the Angel One app.\n"
                        f"Time: {_now().strftime('%H:%M:%S')}")
                    return False, "Position closed while order was in flight — check Angel One app"

                old_qty   = self.position["qty"]
                old_entry = self.position["entry_price"]
                new_qty   = old_qty + add_qty
                new_entry = round((old_qty * old_entry + add_qty * fill) / new_qty, 2)
                self.position["qty"]         = new_qty
                self.position["initial_qty"] = max(self.position.get("initial_qty", old_qty), new_qty)
                self.position["entry_price"] = new_entry
                self.position["trail_high"]  = max(self.position.get("trail_high", fill), fill)
                self.position["manual_adds"] = manual_adds + 1
                self.position["live_ltp"]    = round(fill, 2)
                self.position["live_pnl"]    = round((fill - new_entry) * new_qty, 2)

            lots = new_qty // bt.LOT_SIZE
            tag  = "[PAPER] " if self.paper_mode else ""
            logger.info(f"{tag}Manual add-lot: +{add_qty} @ {fill}, new avg entry {new_entry}, qty {new_qty}")
            self._log_activity("entry", f"{tag}Manual +1 lot — {pos['symbol']} @ ₹{fill:.2f} "
                                        f"(now {lots} lots, avg ₹{new_entry:.2f})")
            _tg(f"➕ <b>{tag}MANUAL ADD 1 LOT</b>\n"
                f"Symbol : {pos['symbol']}\n"
                f"Fill   : ₹{fill:.2f}\n"
                f"New Qty: {new_qty} ({lots} lots)\n"
                f"New Avg: ₹{new_entry:.2f}\n"
                f"Time   : {_now().strftime('%H:%M:%S')}")
            self._save_state()
            return True, "Lot added"
        finally:
            with self._lock:
                if self.position["active"]:
                    self.position["exiting"] = False

    def sell_lot(self):
        """
        Manually sell 1 lot from the active position at the current option LTP
        (dashboard "-1 Lot" button). Books realized P&L on the sold lot like
        the automatic partial-exit does; entry_price on the remaining qty is
        left unchanged (it's already a blended average). Refuses to sell the
        last lot — use "Exit Trade" to close the position fully.
        Returns (ok: bool, message: str).
        """
        with self._lock:
            if not self.position["active"]:
                return False, "No active position"
            if self.position.get("exiting"):
                return False, "Position busy — try again in a moment"
            if self.position["qty"] <= bt.LOT_SIZE:
                return False, "Only 1 lot left — use Exit Trade to close fully"
            self.position["exiting"] = True   # claim: block concurrent auto-exit while we sell
            pos = dict(self.position)

        try:
            reduce_qty = bt.LOT_SIZE
            ltp = self.get_option_ltp(pos["symbol"], pos["token"])
            if not ltp:
                return False, "Could not fetch option LTP"

            fill = ltp
            if not self.paper_mode:
                resp = self._order(pos["symbol"], pos["token"], reduce_qty, "SELL")
                if not (resp and resp.get("status")):
                    msg = resp.get("message", "") if isinstance(resp, dict) else str(resp)
                    logger.error(f"Manual sell-lot failed: {resp}")
                    return False, f"Sell order failed: {msg}"
                sell_order_id = resp.get("data", {}).get("orderid")
                order_status, fill_price, _row = self._confirm_order(sell_order_id)
                if order_status == "rejected":
                    logger.error(f"Manual sell-lot order {sell_order_id} was accepted then rejected/cancelled")
                    _tg(f"🔴 <b>SELL-LOT REJECTED AFTER ACCEPTANCE</b>\n"
                        f"Symbol : {pos['symbol']}\n"
                        f"Order  : {sell_order_id}\n"
                        f"No lot was sold — bot quantity is unchanged.\n"
                        f"Time   : {_now().strftime('%H:%M:%S')}")
                    return False, "Sell order was rejected/cancelled by the exchange — no lot sold"
                if order_status == "complete" and fill_price:
                    fill = fill_price
                elif order_status == "unknown":
                    _tg(f"⚠️ <b>SELL-LOT STATUS UNCONFIRMED</b>\n"
                        f"Order {sell_order_id} for {pos['symbol']} was accepted, but fill/reject "
                        f"status couldn't be confirmed in time. Proceeding as if filled — "
                        f"please verify on the Angel One app.\n"
                        f"Time   : {_now().strftime('%H:%M:%S')}")

            pnl     = round((fill - pos["entry_price"]) * reduce_qty, 2)
            pnl_pct = round((fill - pos["entry_price"]) / pos["entry_price"] * 100, 2) if pos["entry_price"] else 0
            capital = round(pos["entry_price"] * reduce_qty, 2)
            record = {
                "date"      : _today().isoformat(),
                "time"      : pos["entry_time"],
                "exit_time" : _now().strftime("%H:%M"),
                "symbol"    : pos["symbol"],
                "side"      : pos["side"],
                "strike"    : pos["strike"],
                "entry"     : pos["entry_price"],
                "exit"      : round(fill, 2),
                "entry_spot": pos["entry_spot"],
                "qty"       : reduce_qty,
                "lots"      : 1,
                "capital"   : capital,
                "pnl"       : pnl,
                "pnl_pct"   : pnl_pct,
                "reason"    : "MANUAL_SELL_LOT",
                "paper"     : self.paper_mode,
            }

            with self._lock:
                remaining = pos["qty"] - reduce_qty
                if self.position["active"]:
                    self.trades.append(record)
                    self.daily_pnl += pnl
                    self.position["qty"] = remaining
                    self.position["realized_pnl"] = self.position.get("realized_pnl", 0.0) + pnl
                    self.position["live_ltp"] = round(fill, 2)
                    self.position["live_pnl"] = round((fill - pos["entry_price"]) * remaining, 2)

            _append_trade_log(record)

            tag = "[PAPER] " if self.paper_mode else ""
            logger.info(f"{tag}Manual sell-lot: -{reduce_qty} @ {fill} pnl={pnl}")
            self._log_activity("exit" if pnl >= 0 else "exit_loss",
                               f"{tag}Manual -1 lot — {pos['symbol']} @ ₹{fill:.2f} "
                               f"({'+' if pnl>=0 else ''}₹{pnl:,.2f})")
            _tg(f"➖ <b>{tag}MANUAL SELL 1 LOT</b>\n"
                f"Symbol : {pos['symbol']}\n"
                f"Fill   : ₹{fill:.2f}  P&L: {'+' if pnl>=0 else ''}₹{pnl:,.2f}\n"
                f"Remaining: {remaining} qty ({remaining // bt.LOT_SIZE} lot(s))\n"
                f"Time   : {_now().strftime('%H:%M:%S')}")
            self._save_state()
            return True, "Lot sold"
        finally:
            with self._lock:
                if self.position["active"]:
                    self.position["exiting"] = False

    # ── Real-time tick feed management ───────────────────────────

    def _start_ws_feed(self, token: str):
        """Start WebSocket V2 real-time feed for the open position's option token."""
        if self.paper_mode:
            return
        if not (self._auth and self._feed_token):
            logger.warning("TickFeed: missing auth/feed_token — WebSocket skipped")
            return

        self._stop_ws_feed()  # clean up any previous feed
        self._tick_ltp.clear()

        client_code = os.getenv("ANGEL_CLIENT_ID", "")
        feed_obj = _TickFeed(self._auth, client_code, self._feed_token,
                             lambda tok, ltp: self._tick_ltp.__setitem__(tok, (ltp, time.monotonic())))
        feed_obj.subscribe([token])
        self._ws_feed = feed_obj

        def _run():
            while self.position.get("active"):
                try:
                    feed_obj.connect()   # blocks until WebSocket closes
                except Exception as e:
                    logger.warning(f"TickFeed disconnected: {e}")
                if self.position.get("active"):
                    time.sleep(2)        # brief pause before reconnect
            logger.info("TickFeed: position closed, stopping")

        self._ws_thread = threading.Thread(target=_run, daemon=True, name="TickFeed")
        self._ws_thread.start()
        logger.info(f"TickFeed: started for NFO|{token}")

    def _stop_ws_feed(self):
        if self._ws_feed:
            self._ws_feed.stop()
            self._ws_feed = None

    # ── Position monitoring ───────────────────────────────────────

    def _manage_position(self):
        pos = self.position
        if not pos["active"]:
            return

        ts = _now().strftime("%H:%M")
        # carry_overnight skips the daily square-off so the position rolls into
        # the next trading day -- EXCEPT on the contract's own expiry date,
        # since a weekly option can't be carried past its own expiry.
        expiry_today = pos.get("expiry") == _today().isoformat()
        if ts >= SQUAREOFF_TIME and (not self.carry_overnight or expiry_today):
            self._exit("EOD_SQUAREOFF")
            return

        if self.strategy == "supertrend":
            self._manage_position_supertrend(pos)
            return

        tok  = pos["token"]
        tick = self._tick_ltp.get(tok)
        if tick and (time.monotonic() - tick[1]) < _TICK_STALE_SECS:
            ltp = tick[0]
        else:
            ltp = self.get_option_ltp(pos["symbol"], tok)
        if ltp is None:
            return

        pnl_pu  = ltp - pos["entry_price"]
        opt_pct = pnl_pu / pos["entry_price"] if pos["entry_price"] > 0 else 0

        # Update live P&L display
        with self._lock:
            self.position["live_ltp"] = round(ltp, 2)
            self.position["live_pnl"] = round(pnl_pu * pos["qty"], 2)

        # User-set manual target — checked alongside every other exit rule
        # below, not instead of them; whichever condition hits first wins.
        if self.manual_target_pct and opt_pct >= self.manual_target_pct:
            self._exit("MANUAL_TARGET", ltp)
            return

        is_one_lot  = pos.get("initial_qty", pos["qty"]) == bt.LOT_SIZE
        entry_time  = pos.get("entry_time", "00:00") or "00:00"
        late_entry  = entry_time >= "14:30"

        # 1-lot: exit at +10% OR ₹1,100 — whichever comes first (only if V2_1LOT_HARD_TP).
        # Validated default (False): skip the hard cap and let the trailing stop
        # below (activates @V2_TRAIL_TRIGGER, floor @breakeven) manage the exit
        # instead — backtested to turn -Rs.34,059 into +Rs.1,046 over 138 days.
        if is_one_lot and bt.V2_1LOT_HARD_TP:
            abs_pnl = (ltp - pos["entry_price"]) * bt.LOT_SIZE
            if opt_pct >= bt.V2_1LOT_TP_PCT or abs_pnl >= bt.V2_1LOT_TP_RUPEES:
                self._exit("TARGET", ltp)
                return

        # 2-lot late entry (after 14:30): full exit at +10% — no time to run to +20%
        if (not is_one_lot
                and late_entry
                and opt_pct >= bt.V2_PARTIAL_PCT):
            self._exit("TARGET_LATE", ltp)
            return

        # 2-lot normal: partial exit at +10%
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

        # After partial, SL steps to breakeven (trail_floor = 0%).
        # Big-winner lock: once peak gain reaches V2_TRAIL_LOCK_TRIGGER, the floor
        # ratchets up with the peak instead of sitting flat at breakeven — stops a
        # large spike from fully round-tripping back to a loss before EMA9 flips.
        base_floor  = bt.V2_TRAIL_FLOOR if pos["partial_done"] else 0.0
        peak_pct    = (pos["trail_high"] - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] > 0 else 0.0
        if peak_pct >= bt.V2_TRAIL_LOCK_TRIGGER:
            trail_floor = max(base_floor, peak_pct - bt.V2_TRAIL_LOCK_GIVEBACK)
        else:
            trail_floor = base_floor
        trail_exit  = pos["trail_on"] and opt_pct <= trail_floor

        # ── Spot-based SL (two-tier) ──────────────────────────────────────────
        # Small breach (WARN pts): market may be consolidating — wait 2 polls.
        # Large breach (HARD pts): genuine reversal — exit immediately, no wait.
        current_spot = self.get_nifty_ltp()
        entry_spot   = pos.get("entry_spot", 0.0)
        spot_move    = None   # populated below; read later by the EMA-confirm backstop check
        against      = False
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

        # EMA9 exit: underlying closed back through EMA9 against the position,
        # confirmed over bt.V2_EMA_EXIT_CONFIRM_CANDLES consecutive CLOSED
        # candles (matches backtest.py's ema_exit_confirm mechanism). A candle
        # that moves back in the position's favor resets the counter to 0 —
        # this is a streak, not a rolling window. Counted once per newly
        # closed candle (via the "ts" on self._ema_ref), not once per poll —
        # _manage_position runs every 5s but the underlying candle only
        # advances once every 5 minutes.
        ema_exit = False
        ema_ts   = self._ema_ref.get("ts")
        if self._ema_ref.get("ema9") is not None and ema_ts is not None:
            ema_breach = ((pos["side"] == "CE" and self._ema_ref["close"] < self._ema_ref["ema9"]) or
                          (pos["side"] == "PE" and self._ema_ref["close"] > self._ema_ref["ema9"]))
            if ema_ts != pos.get("ema_last_candle_ts"):
                with self._lock:
                    self.position["ema_warn_count"] = (pos.get("ema_warn_count", 0) + 1) if ema_breach else 0
                    self.position["ema_last_candle_ts"] = ema_ts
            ema_exit = pos.get("ema_warn_count", 0) >= bt.V2_EMA_EXIT_CONFIRM_CANDLES

        # EMA-confirm hard backstop: while inside the confirm waiting window
        # (breach counter > 0 but hasn't reached V2_EMA_EXIT_CONFIRM_CANDLES
        # yet), a fast adverse spot move overrides the wait and exits
        # immediately — checked every 5s poll via the same live spot read
        # above, since the whole point is reacting faster than a candle close.
        ema_warn_count = pos.get("ema_warn_count", 0)
        ema_backstop = (bt.V2_EMA_CONFIRM_BACKSTOP_PTS > 0
                        and 0 < ema_warn_count < bt.V2_EMA_EXIT_CONFIRM_CANDLES
                        and against and spot_move is not None
                        and spot_move > bt.V2_EMA_CONFIRM_BACKSTOP_PTS)
        if ema_backstop:
            logger.info(f"EMA-confirm backstop: spot moved {spot_move:.1f}pts against {pos['side']} "
                       f"while awaiting confirmation ({ema_warn_count}/{bt.V2_EMA_EXIT_CONFIRM_CANDLES})")

        if   opt_pct >= bt.V2_TP_OPTION_PCT and not is_one_lot: self._exit("TARGET",     ltp)
        elif sl_triggered:                                       self._exit("SL",         ltp)
        elif trail_exit:                                         self._exit("TRAIL_EXIT", ltp)
        elif ema_exit:                                           self._exit("EMA_EXIT",   ltp)
        elif ema_backstop:                                       self._exit("EMA_EXIT_BACKSTOP", ltp)

    def _manage_position_supertrend(self, pos):
        """
        Strategy 6 exit logic — matches supertrend_45day_trail_backtest.py:
        (1) 50-point adverse spot move -> immediate stop, no confirmation;
        (2) 3-tier profit floor, always the max of whichever tier applies —
            never steps back down as peak_pct rises past a tier boundary:
              peak >= 15% -> floor = breakeven (a trade that got this far
                              can never close as a real loss for the day)
              peak >= 25% -> floor = +10%
              peak >= 32% -> floor = peak - GIVEBACK(3pts), continuous, no cap
            Chosen over the higher-EV "32%-only" version for the loss
            guarantee below 32% — backtested 45d (Aug 2026): Rs.43,905 vs
            Rs.33,562 doing nothing (Rs.51,905 for 32%-only, no guarantee);
        (3) Supertrend flip against the position;
        (4) Still down at least ST6_NEG_REVERSAL_LOSS_PCT, ST6_NEG_REVERSAL_AGE_MIN
            minutes after entry -> NEG_REVERSAL_EXIT, cut and immediately flip
            into the opposite side at the same strike.
            Original 10min/any-negative rule backtested Aug19-Sep1 2026 (18 real
            trades, real option prices): every trade still negative at +10min
            went on to lose (8/8 and 6/7 across two windows, never recovered)
            and the reversal -- managed by these same rules -- turned
            -Rs.11,482 into +Rs.6,825 on the subset that triggered it.
            45d grid search (Sep 2026) across age x loss-pct settled on
            20min/5%: matches 10min/5%'s net P&L (Rs.54,101 vs Rs.55,059) while
            roughly halving max drawdown (Rs.-6,357 vs Rs.-11,294) -- see
            ST6_NEG_REVERSAL_AGE_MIN in backtest.py. Never chains a second
            reversal, and skips the flip (still cuts, just no re-entry) within
            30min of square-off or once the day's trade/loss caps are already hit.
        EOD square-off is handled by the shared check in _manage_position
        before this is called.
        """
        tok  = pos["token"]
        tick = self._tick_ltp.get(tok)
        if tick and (time.monotonic() - tick[1]) < _TICK_STALE_SECS:
            ltp = tick[0]
        else:
            ltp = self.get_option_ltp(pos["symbol"], tok)
        if ltp is not None:
            with self._lock:
                self.position["live_ltp"] = round(ltp, 2)
                self.position["live_pnl"] = round((ltp - pos["entry_price"]) * pos["qty"], 2)
                if ltp > pos["trail_high"]:
                    self.position["trail_high"] = ltp

            # User-set manual target — checked alongside every other exit rule
            # below, not instead of them; whichever condition hits first wins.
            if (self.manual_target_pct and pos["entry_price"] > 0 and
                    (ltp - pos["entry_price"]) / pos["entry_price"] >= self.manual_target_pct):
                self._exit("MANUAL_TARGET", ltp)
                return

        current_spot = self.get_nifty_ltp()
        entry_spot   = pos.get("entry_spot", 0.0)
        if current_spot and entry_spot:
            adverse = (entry_spot - current_spot if pos["side"] == "CE"
                      else current_spot - entry_spot)
            if adverse >= bt.ST6_SPOT_SL:
                self._exit("ST_SPOT_SL", ltp)
                return

        entry_time_str = pos.get("entry_time")
        if not pos.get("is_reversal") and ltp is not None and entry_time_str:
            entry_dt = datetime.combine(_today(), datetime.strptime(entry_time_str, "%H:%M").time())
            age_min  = (_now() - entry_dt).total_seconds() / 60
            if (age_min >= bt.ST6_NEG_REVERSAL_AGE_MIN and
                    ltp <= pos["entry_price"] * (1 - bt.ST6_NEG_REVERSAL_LOSS_PCT)):
                reversal_signal = "BUY_PE" if pos["side"] == "CE" else "BUY_CE"
                strike = pos["strike"]
                self._exit("NEG_REVERSAL_EXIT", ltp)
                too_late = _now().strftime("%H:%M") >= "14:45"
                caps_hit = (self.trade_count >= self.max_trades or
                           self.daily_pnl <= self.max_daily_loss)
                if not too_late and not caps_hit:
                    self._enter(reversal_signal, force_strike=strike, is_reversal=True)
                return

        entry = pos["entry_price"]
        if ltp is not None and entry > 0:
            peak_pct = (pos["trail_high"] - entry) / entry
            floor_pct = None
            if peak_pct >= bt.ST6_TRAIL_LOCK_TRIGGER:
                floor_pct = peak_pct - bt.ST6_TRAIL_GIVEBACK
            elif peak_pct >= bt.ST6_STEP2_TRIGGER:
                floor_pct = bt.ST6_STEP2_FLOOR
            elif peak_pct >= bt.ST6_STEP1_TRIGGER:
                floor_pct = bt.ST6_STEP1_FLOOR
            if floor_pct is not None:
                opt_pct = (ltp - entry) / entry
                if opt_pct <= floor_pct:
                    self._exit("ST_TRAIL_EXIT", ltp)
                    return

        st_ts = self._st_ref.get("ts")
        if st_ts is not None and st_ts != pos.get("st_last_candle_ts"):
            with self._lock:
                self.position["st_last_candle_ts"] = st_ts
            st_val, st_prev = self._st_ref.get("value"), self._st_ref.get("prev")
            # Require an ACTUAL flip on this candle (value != prev), matching
            # neg10min_45day_backtest.py's st_prev/st_now transition check --
            # not just "current reading happens to be against me". A reversal
            # is entered deliberately against the still-prevailing Supertrend
            # direction (that's the whole premise: price already moved, the
            # indicator hasn't caught up yet), so checking raw current value
            # alone would treat that pre-existing, unchanged reading as a
            # fresh flip and kill the reversal before the trend ever turns.
            flipped = st_val is not None and st_prev is not None and st_val != st_prev
            flip_against = flipped and ((pos["side"] == "CE" and st_val == -1) or
                            (pos["side"] == "PE" and st_val == 1))
            if flip_against:
                self._exit("ST_FLIP", ltp)
                return

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

                    # Daily loss cap / profit lock — ported from backtest.py's
                    # entry gate (MAX_DAILY_LOSS / DAILY_PROFIT_TARGET), which
                    # was never enforced live before this check existed.
                    daily_cap_hit    = self.daily_pnl <= self.max_daily_loss
                    daily_target_hit = self.daily_pnl >= self.daily_profit_target
                    if daily_cap_hit or daily_target_hit:
                        reason = (f"Daily loss cap hit (₹{self.daily_pnl:,.2f} ≤ ₹{self.max_daily_loss:,.2f}) "
                                  f"— no new entries today" if daily_cap_hit else
                                  f"Daily profit target reached (₹{self.daily_pnl:,.2f} ≥ "
                                  f"₹{self.daily_profit_target:,.2f}) — locked in, no new entries")
                        self.sig_info["filter_reason"] = reason
                        if not self._daily_cap_logged:
                            logger.warning(reason)
                            _tg(f"🛑 <b>Trading Halted For Today</b>\n{reason}\n"
                                f"Time: {_now().strftime('%H:%M:%S')}")
                            self._daily_cap_logged = True
                    else:
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
                            else:
                                self._log_activity("scan", "Market scan completed — no signal")
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
                elif self.position["active"]:
                    # Position open — full signal check is skipped, but keep the
                    # relevant indicator reference fresh so _manage_position's
                    # exit check has current data to compare against. Still log
                    # an activity entry each poll (trend + PCR) so "Recent
                    # Activity" doesn't go silent for the whole life of a trade.
                    try:
                        df_nbees = self._fetch_nbees_only()
                        trend = None
                        if self.strategy == "supertrend":
                            self._update_st_ref(df_nbees)
                            st_val = self._st_ref.get("value")
                            trend = "Uptrend" if st_val == 1 else "Downtrend" if st_val == -1 else None
                        else:
                            self._update_ema_ref(df_nbees)
                            ema = self._ema_ref
                            if ema.get("close") is not None and ema.get("ema9") is not None:
                                trend = "Uptrend" if ema["close"] > ema["ema9"] else "Downtrend"

                        pcr = self.get_nifty_pcr()
                        parts = [p for p in (trend, f"PCR {pcr:.2f}" if pcr is not None else None) if p]
                        detail = " · ".join(parts) if parts else "monitoring"
                        self._log_activity("scan", f"Position open ({self.position['side']}) — {detail}")
                    except Exception as e:
                        logger.warning(f"Indicator ref refresh (position open) failed: {e}")

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
            time.sleep(5)
        logger.info("Monitor loop stopped")

    # ── Public API ────────────────────────────────────────────────

    def start(self, max_trades: int = 2, lots: int = 1, paper_mode: bool = False,
             max_daily_loss: float = None, daily_profit_target: float = None,
             strategy: str = "v2", manual_target_pct: float = None,
             carry_overnight: bool = False):
        """Enable trading and launch background threads."""
        if self._obj is None:
            self.login()

        self.strategy          = strategy if strategy in ("v2", "supertrend") else "v2"
        self.max_trades       = max_trades
        self.lots             = lots
        self.paper_mode       = paper_mode
        self.max_daily_loss   = max_daily_loss if max_daily_loss is not None else bt.MAX_DAILY_LOSS
        self.daily_profit_target = (daily_profit_target if daily_profit_target is not None
                                    else bt.DAILY_PROFIT_TARGET)
        self.manual_target_pct = manual_target_pct
        self.carry_overnight   = carry_overnight
        # Supertrend uses its own global (either-direction) cooldown constant;
        # V2 uses its validated same-direction-only cooldown.
        self.loss_cooldown_candles = (bt.ST6_LOSS_COOLDOWN_CANDLES if self.strategy == "supertrend"
                                      else bt.V2_LOSS_COOLDOWN_CANDLES)
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
        logger.info(f"Trading started: {lots} lot(s), max {max_trades} trades/day, "
                    f"daily loss cap ₹{self.max_daily_loss:,.0f}, profit target ₹{self.daily_profit_target:,.0f}")
        tag = "[PAPER] " if paper_mode else ""
        _tg(f"🚀 <b>{tag}Bot Started</b>\n"
            f"Lots   : {lots}\n"
            f"Max    : {max_trades} trades/day\n"
            f"Daily cap: ₹{self.max_daily_loss:,.0f} loss / ₹{self.daily_profit_target:,.0f} profit\n"
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

        if pos["active"]:
            side, ep, es = pos.get("side"), pos.get("entry_price") or 0, pos.get("entry_spot") or 0
            if self.strategy == "supertrend":
                spot_stop = (es - bt.ST6_SPOT_SL) if side == "CE" else (es + bt.ST6_SPOT_SL)
                pos["stop_desc"]   = f"Spot {spot_stop:.0f} ({bt.ST6_SPOT_SL}pt)" if es else "—"
                pos["target_desc"] = "Exit on trend flip"
            else:
                pos["stop_desc"]   = f"₹{ep*(1-bt.V2_SL_OPTION_PCT):.2f} (−{bt.V2_SL_OPTION_PCT*100:.0f}%)" if ep else "—"
                pos["target_desc"] = f"₹{ep*(1+bt.V2_PARTIAL_PCT):.2f} (+{bt.V2_PARTIAL_PCT*100:.0f}%)" if ep else "—"

        return {
            "status"      : status,
            "connected"   : self.connected,
            "enabled"     : self.enabled,
            "monitoring"  : self._monitoring_only,
            "paper_mode"  : self.paper_mode,
            "config"      : {"max_trades": self.max_trades, "lots": self.lots, "paper": self.paper_mode,
                             "strategy": self.strategy, "lot_size": bt.LOT_SIZE,
                             "max_manual_add_lots": MAX_MANUAL_ADD_LOTS},
            "market"      : {"nifty_ltp": self.nifty_ltp, "vix": self.sig_info.get("vix"),
                             "st_trend": self._st_ref.get("value")},
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
            "activity"    : list(self._activity),
            "last_error"  : self.last_error,
            "timestamp"   : _now().strftime("%H:%M:%S"),
        }

    # ── Helpers ───────────────────────────────────────────────────

    def _reset_day(self):
        with self._lock:
            self._today      = _today()
            # A carry_overnight position surviving into today must NOT be
            # wiped here -- it's still genuinely open at the broker. Only
            # reset the position slot when nothing real is holding it.
            if not self.position.get("active"):
                self.position = _empty_pos()
            self.trades      = []
            self.daily_pnl   = 0.0
            self.win_count   = 0
            self.trade_count = 0
            self.last_signal = None
            self._daily_cap_logged   = False
            self._cooldown_remaining = {"buy": 0, "sell": 0}
            self._cooldown_last_ts   = None
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
        "exiting"      : False,
        "symbol"       : None,  "token"       : None,
        "side"         : None,  "strike"      : 0,
        "expiry"       : None,  "qty"         : 0,
        "entry_price"  : 0.0,   "entry_spot"  : 0.0,
        "entry_time"   : None,  "partial_done": False,
        "trail_on"     : False, "trail_high"  : 0.0,
        "live_ltp"     : 0.0,   "live_pnl"   : 0.0,
        "order_id"          : None,
        "manual_adds"       : 0,   # count of manual "+1 Lot" clicks this trade
        "sl_warn_count"     : 0,   # consecutive polls in premium backstop zone
        "spot_sl_warn_count": 0,   # consecutive polls with spot beyond SL threshold
        "realized_pnl"      : 0.0,  # running pnl for this trade cycle (partial + final)
        "ema_warn_count"    : 0,     # consecutive CLOSED candles crossed back through EMA9
        "ema_last_candle_ts": None,  # candle timestamp last counted for ema_warn_count
        "st_last_candle_ts" : None,  # supertrend strategy: candle timestamp last checked
        "is_reversal"       : False, # opened by NEG_REVERSAL_EXIT flipping to the opposite side
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
