"""
instruments.py — Instrument registry for the trading bot.

Each instrument carries two groups of settings:
  1. Market config  — token, lot size, strike interval, expiry day
  2. Strategy params — RSI thresholds, time windows, SL/TP/trail, premium bounds

Nifty strategy params mirror the finalized V2 backtest constants exactly.
BankNifty has its own tuned params suited to its higher volatility.
"""
from angel_data import NIFTYBEES_TOKEN, BANKBEES_TOKEN, NIFTY_MULTIPLIER

BANKBEES_MULTIPLIER = 100.0  # BANKBEES × 100 ≈ BankNifty spot (recalibrate if ETF drifts)

INSTRUMENTS = {
    # ── Nifty 50 ─────────────────────────────────────────────────────
    # Strategy params are locked — finalized via backtest. Do not change.
    "NIFTY": {
        # Market config
        "display_name"     : "Nifty 50",
        "spot_exchange"    : "NSE",
        "spot_symbol"      : "Nifty 50",
        "spot_token"       : "26000",
        "nfo_name"         : "NIFTY",
        "strike_interval"  : 50,
        "lot_size"         : 75,
        "expiry_weekday"   : 3,       # Thursday
        "skip_expiry_day"  : True,
        "signal_token"     : NIFTYBEES_TOKEN,
        "signal_multiplier": NIFTY_MULTIPLIER,
        "confirm_token"    : BANKBEES_TOKEN,

        # Entry signal params (V2 finalized — mirrors backtest.py constants exactly)
        "rsi_min_ce"      : 60,       # RSI > 60 for CE entry
        "rsi_max_pe"      : 40,       # RSI < 40 for PE entry
        "morning_start"   : "09:30",
        "morning_end"     : "11:30",
        "afternoon_start" : "13:30",  # None = no afternoon session
        "no_entry_after"  : "14:50",

        # Exit / risk params (V2 finalized)
        "sl_pct"          : 0.15,     # hard SL at -15% of option entry price
        "sl_warn_pct"     : 0.12,     # warn zone -12% needs 2 polls to confirm
        "tp_pct"          : 0.40,     # take profit at +40%
        "partial_pct"     : 0.20,     # partial exit 1 lot at +20%
        "trail_trigger"   : 0.10,     # activate trailing stop at +10%
        "trail_floor"     : 0.05,     # trail stop floor at +5% (after partial)

        # Premium sanity check
        "min_premium"     : 50,       # skip if ATM option LTP < ₹50 (too thin)
        "max_premium"     : 600,      # skip if ATM option LTP > ₹600 (too expensive)
    },

    # ── Bank Nifty ────────────────────────────────────────────────────
    # BankNifty is more volatile and faster moving than Nifty.
    # Tuned params: looser RSI, morning-only window, wider SL, higher TP.
    "BANKNIFTY": {
        # Market config
        "display_name"     : "Bank Nifty",
        "spot_exchange"    : "NSE",
        "spot_symbol"      : "Nifty Bank",
        "spot_token"       : "26009",
        "nfo_name"         : "BANKNIFTY",
        "strike_interval"  : 100,
        "lot_size"         : 15,
        "expiry_weekday"   : 2,       # Wednesday
        "skip_expiry_day"  : True,
        "signal_token"     : BANKBEES_TOKEN,
        "signal_multiplier": BANKBEES_MULTIPLIER,
        "confirm_token"    : NIFTYBEES_TOKEN,

        # Entry signal params — BankNifty-specific
        "rsi_min_ce"      : 55,       # looser than Nifty — BNF moves fast, strict RSI misses entries
        "rsi_max_pe"      : 45,
        "morning_start"   : "09:30",
        "morning_end"     : "11:30",  # extended — BNF has a second wave 11:00-11:30
        "afternoon_start" : None,     # no afternoon session for BankNifty
        "no_entry_after"  : "11:30",

        # Exit / risk params — wider to match BankNifty's volatility
        "sl_pct"          : 0.20,     # BNF options swing harder — give more room
        "sl_warn_pct"     : 0.15,
        "tp_pct"          : 0.50,     # BNF options can run further
        "partial_pct"     : 0.25,     # partial at +25% (BNF moves in larger chunks)
        "trail_trigger"   : 0.12,     # trail activates at +12%
        "trail_floor"     : 0.06,     # trail floor at +6%

        # Premium sanity check — BankNifty premiums are higher in absolute ₹
        "min_premium"     : 80,       # skip if ATM option LTP < ₹80
        "max_premium"     : 900,      # skip if ATM option LTP > ₹900
    },

    # ── Stock options template (monthly expiry) ───────────────────────
    # "RELIANCE": {
    #     "display_name"     : "Reliance",
    #     "spot_exchange"    : "NSE",
    #     "spot_symbol"      : "RELIANCE-EQ",
    #     "spot_token"       : "<token>",
    #     "nfo_name"         : "RELIANCE",
    #     "strike_interval"  : 20,
    #     "lot_size"         : 250,
    #     "expiry_weekday"   : 3,       # monthly last Thursday — needs special _next_expiry
    #     "skip_expiry_day"  : True,
    #     "signal_token"     : "<equity NSE token>",
    #     "signal_multiplier": 1.0,
    #     "confirm_token"    : NIFTYBEES_TOKEN,
    #     "rsi_min_ce"       : 60,
    #     "rsi_max_pe"       : 40,
    #     "morning_start"    : "09:30",
    #     "morning_end"      : "11:30",
    #     "afternoon_start"  : "13:30",
    #     "no_entry_after"   : "14:50",
    #     "sl_pct"           : 0.20,
    #     "sl_warn_pct"      : 0.15,
    #     "tp_pct"           : 0.50,
    #     "partial_pct"      : 0.25,
    #     "trail_trigger"    : 0.12,
    #     "trail_floor"      : 0.06,
    #     "min_premium"      : 30,
    #     "max_premium"      : 500,
    # },
}

# Instruments evaluated each signal cycle. Comment out to disable one.
ACTIVE_INSTRUMENTS = [
    "NIFTY",
    "BANKNIFTY",
]
