"""
options_confirmation.py — Options Intelligence confirmation-score layer.

EXPERIMENTAL / RESEARCH MODULE. Not wired into live_trader.py, dashboard.py,
or backtest.py. Nothing in the existing Supertrend strategy calls this module
unless a caller explicitly opts in via OPTIONS_CONFIRMATION_ENABLED (default
False) and passes a data provider. Existing entry/exit logic, risk management,
position sizing, and live/paper order execution are completely untouched.

==============================================================================
DATA AVAILABILITY — READ THIS FIRST
==============================================================================
Investigated 2026-08-29 against this project's actual codebase and a live
Angel One SmartAPI session:

  * Whole-repo search for PCR / OI / option-chain: zero hits before this file.
  * `getCandleData` on a real NIFTY weekly option contract returns rows of
    [timestamp, open, high, low, close, volume] — SIX fields, same schema as
    equity candles. No open-interest column. Verified against real data for
    NIFTY01SEP2624150CE, 2026-08-27/28.
  * `SmartConnect.putCallRatio()` and `.oIBuildup()` (Angel SDK source
    inspected directly) take no date/time parameter — they return the
    CURRENT live snapshot only. There is no historical PCR/OI endpoint.
  * NSE does publish a daily F&O bhavcopy with end-of-day OI per contract,
    but that is one data point per day and cannot support intraday
    confirmation scoring — and the task spec explicitly forbids using EOD OI
    for intraday decisions.

CONCLUSION: no historical NIFTY options-chain OI/PCR dataset exists anywhere
this project can reach. This module is therefore built against a clean data
PROVIDER INTERFACE (`OptionsDataProvider`) so real historical data — a paid
vendor, or a feed self-recorded going forward via a live provider — can be
plugged in later without touching the scoring logic below. `HistoricalOptionsDataProvider`
is an explicit stub that raises `NoHistoricalOptionsDataError` rather than
fabricating numbers. Do not replace that stub with synthetic/random data —
that would silently produce fake backtest results.

==============================================================================
METHODOLOGY (documented per the task's requirement — nothing here is an
arbitrary, undocumented choice)
==============================================================================

PCR:
    PCR = sum(put_oi across included strikes) / sum(call_oi across included strikes)
    Strike inclusion is CONFIGURABLE (`OptionsConfirmationConfig.strike_range_method`):
      - "atm_pm_n" (default): ATM ± `atm_strike_count` strikes (default N=10,
        i.e. 21 strikes total) around the spot at signal time. This is the
        conventional PCR methodology — it weights the strikes actually near
        the money, where OI concentration is meaningful, and avoids far-OTM
        noise skewing the ratio.
      - "all": every strike available for the selected expiry.
    Expiry selection is CONFIGURABLE (`expiry_selection`, default "nearest"):
    the nearest weekly expiry that has not yet lapsed as of the signal
    timestamp. Note this is deliberately the NEAREST expiry, not the "one
    cycle out" expiry the live strategy actually trades for theta cushion
    (see live_trader.py::_next_thursday) — sentiment/positioning from the
    option chain is read off the most liquid, highest-OI contract, which is
    always the nearest expiry, regardless of which contract the strategy
    itself buys.

PCR direction (increasing/decreasing):
    Current PCR compared against the PCR reading `pcr_change_window_min`
    minutes ago (CONFIGURABLE: 5 / 15 / 30). Comparison window is a single
    knob, not three separate hardcoded checks, so the backtest runner can
    sweep it like every other threshold.

OI change (Put OI / Call OI increasing/decreasing):
    Same-side sum-of-OI compared against its reading `oi_change_window_min`
    minutes ago (CONFIGURABLE: 5 / 15 / 30).
    Interpretation is NOT assumed to be one-directional. OI increase alone
    is ambiguous (could be fresh longs or fresh shorts on that side) — see
    `classify_oi_action()`, which combines the OI-change sign with the
    change in the option side's own premium (or underlying spot, if premium
    history isn't available) over the same window to distinguish:
        OI up   + price up   -> "long_buildup"    (fresh buying)
        OI up   + price down -> "short_buildup"    (fresh selling/writing)
        OI down + price up   -> "short_covering"   (shorts exiting)
        OI down + price down -> "long_unwinding"   (longs exiting)
    The score itself (per the task's exact spec) only uses the raw
    increasing/decreasing signs for Put OI and Call OI — the buildup/
    unwinding classification is exposed separately for the trade log so the
    interpretation is visible and auditable, not silently assumed.

Support / Resistance:
    Derived from the same strike range used for PCR. Method is CONFIGURABLE
    (`sr_method`):
      - "highest_oi" (default): strike with the largest Put OI -> support;
        strike with the largest Call OI -> resistance.
      - "highest_oi_change": strike with the largest Put OI *increase* over
        `oi_change_window_min` -> support; largest Call OI increase ->
        resistance. (Where fresh OI is being added right now, vs. where it
        has merely accumulated historically.)
      - "top3_oi": average strike of the top-3 Put OI strikes -> support;
        average of the top-3 Call OI strikes -> resistance. Smooths out a
        single-strike outlier.
    Only ever computed from strikes/OI already in the snapshot AT OR BEFORE
    the signal timestamp — see the anti-look-ahead note below.

VWAP:
    Reuses `backtest._vwap()` verbatim (imported, not reimplemented). No
    second VWAP calculation exists in this module.

==============================================================================
ANTI-LOOK-AHEAD DESIGN
==============================================================================
`OptionsDataProvider.get_history(as_of, lookback_minutes)` is the ONLY way
this module reads options data. Its signature makes look-ahead impossible by
construction: callers can only ask for a snapshot AS OF a timestamp plus a
lookback window — there is no way to request a range that extends past
`as_of`. Every score computed by `get_options_confirmation()` receives the
exact signal timestamp and only ever calls `get_history(signal_ts, ...)`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Protocol

import backtest as bt   # reuse only — _vwap() is imported, never reimplemented


# ══════════════════════════════════════════════════════════════════
# Master switches — default OFF. Nothing calls this module unless a
# caller explicitly opts in.
# ══════════════════════════════════════════════════════════════════
OPTIONS_CONFIRMATION_ENABLED = False
OPTIONS_SCORE_THRESHOLD      = 80


# ══════════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════════

@dataclass
class StrikeOI:
    call_oi: float
    put_oi: float
    call_price: Optional[float] = None   # option premium, if the provider has it
    put_price: Optional[float] = None


@dataclass
class OptionChainSnapshot:
    """One point-in-time options-chain reading. `per_strike` keys are strike
    prices (float, e.g. 24150.0). Nothing here should ever be built from data
    timestamped after `timestamp`."""
    timestamp: datetime
    expiry:    datetime
    spot:      float
    per_strike: Dict[float, StrikeOI]


class NoHistoricalOptionsDataError(RuntimeError):
    """Raised by HistoricalOptionsDataProvider — see module docstring."""


class OptionsDataProvider(Protocol):
    def get_history(self, as_of: datetime, lookback_minutes: int = 35) -> List[OptionChainSnapshot]:
        """Return snapshots with timestamp <= as_of, oldest first, covering at
        least `lookback_minutes` back (as many as actually exist — may be
        fewer, including zero). Must never return a snapshot timestamped
        after `as_of`."""
        ...


class HistoricalOptionsDataProvider:
    """STUB. No historical NIFTY options-chain OI/PCR data source exists that
    this project can reach (see module docstring 'DATA AVAILABILITY'). This
    raises rather than fabricating data. To unblock the backtest, implement a
    real provider with this same `get_history` signature — e.g. reading from
    a paid historical-OI vendor's export, or from a feed recorded going
    forward with `LiveAngelOptionsDataProvider.poll()` — and pass it to
    `get_options_confirmation()` in place of this stub."""

    def get_history(self, as_of: datetime, lookback_minutes: int = 35) -> List[OptionChainSnapshot]:
        raise NoHistoricalOptionsDataError(
            "No historical options-chain/OI data source is configured. "
            "Angel One's API exposes no historical PCR/OI (verified — see "
            "module docstring). Plug in a real historical data provider "
            "before running score-filtered backtests."
        )


class LiveAngelOptionsDataProvider:
    """Wired to Angel One's LIVE `putCallRatio()` / `oIBuildup()` endpoints.
    Usable only for FORWARD data collection from the moment `poll()` starts
    being called (e.g. every 1-5 min from a running paper-trading process) —
    never for backtesting past dates, because Angel exposes no historical OI.
    Each `poll()` call appends one real snapshot to an in-memory ring buffer;
    `get_history()` returns whatever has actually been recorded so far, which
    is why it still respects the same anti-look-ahead `as_of` contract as
    every other provider (it just can't be asked about a date before the
    process started recording)."""

    def __init__(self, smart_connect_obj, max_buffer_minutes: int = 24 * 60):
        self._obj = smart_connect_obj
        self._max_age = timedelta(minutes=max_buffer_minutes)
        self._buffer: List[OptionChainSnapshot] = []

    def poll(self) -> Optional[OptionChainSnapshot]:
        """Call periodically from a live/paper process. Builds one snapshot
        from Angel's current PCR + OI-buildup responses. Returns None (and
        appends nothing) if Angel's response can't be parsed — never raises
        into the caller's loop for a single bad poll."""
        try:
            snap = self._fetch_snapshot()
        except Exception:
            return None
        if snap is None:
            return None
        self._buffer.append(snap)
        cutoff = snap.timestamp - self._max_age
        self._buffer = [s for s in self._buffer if s.timestamp >= cutoff]
        return snap

    def _fetch_snapshot(self) -> Optional[OptionChainSnapshot]:
        # Angel's putCallRatio() returns per-symbol PCR, not raw OI, so this
        # combines it with oIBuildup() (which does carry per-symbol OI) to
        # build a per-strike table. Both are live-only — see class docstring.
        pcr_resp = self._obj.putCallRatio()
        oi_resp  = self._obj.oIBuildup({"expirytype": "NEAR", "datatype": "PercOIGainers"})
        if not (pcr_resp and pcr_resp.get("status")):
            return None
        now = datetime.now()
        per_strike: Dict[float, StrikeOI] = {}
        # NOTE: Angel's oIBuildup response shape varies by datatype and isn't
        # strike-indexed the way this module needs — this is intentionally
        # left as a best-effort placeholder rather than guessed-at parsing
        # that could silently misattribute OI to the wrong strike. Treat
        # LiveAngelOptionsDataProvider as a skeleton to finish wiring once
        # you've inspected a live oIBuildup payload, not a finished adapter.
        if not per_strike:
            return None
        return OptionChainSnapshot(timestamp=now, expiry=now, spot=0.0, per_strike=per_strike)

    def get_history(self, as_of: datetime, lookback_minutes: int = 35) -> List[OptionChainSnapshot]:
        cutoff = as_of - timedelta(minutes=lookback_minutes)
        return [s for s in self._buffer if cutoff <= s.timestamp <= as_of]


# ══════════════════════════════════════════════════════════════════
# Configuration — every threshold the task asked to be sweepable
# ══════════════════════════════════════════════════════════════════

@dataclass
class OptionsConfirmationConfig:
    # PCR level thresholds (tested independently: 1.00 / 1.10 / 1.20 bullish,
    # 1.00 / 0.90 / 0.80 bearish — NOT hardcoded to PCR > 1).
    pcr_bullish_threshold: float = 1.00
    pcr_bearish_threshold: float = 1.00

    # PCR / OI direction comparison windows (minutes) — test 5 / 15 / 30.
    pcr_change_window_min: int = 15
    oi_change_window_min:  int = 15

    # PCR strike-range methodology.
    strike_range_method: str = "atm_pm_n"   # "atm_pm_n" | "all"
    atm_strike_count:    int = 10           # N in ATM ± N (only for "atm_pm_n")
    strike_step:          float = 50.0      # NIFTY strike spacing

    # Support/resistance methodology.
    sr_method: str = "highest_oi"           # "highest_oi" | "highest_oi_change" | "top3_oi"

    # Which expiry's chain to read for PCR/OI/S-R (see docstring).
    expiry_selection: str = "nearest"

    # Score weights — sum to 100 on each side, per the task's exact table.
    weights: dict = field(default_factory=lambda: {
        "pcr_level":            20,
        "pcr_direction":        15,
        "put_oi_direction":     20,
        "call_oi_direction":    15,
        "support_resistance":   15,
        "vwap":                 15,
    })

    entry_threshold: float = OPTIONS_SCORE_THRESHOLD


# ══════════════════════════════════════════════════════════════════
# Result — one row of the trade-by-trade log, per the task's exact schema
# ══════════════════════════════════════════════════════════════════

@dataclass
class OptionsConfirmationResult:
    timestamp: datetime
    direction: str                 # "CE" | "PE"
    pcr: Optional[float]
    pcr_change: Optional[float]
    put_oi: Optional[float]
    call_oi: Optional[float]
    put_oi_change: Optional[float]
    call_oi_change: Optional[float]
    put_oi_action: Optional[str]   # long_buildup | short_buildup | short_covering | long_unwinding
    call_oi_action: Optional[str]
    support: Optional[float]
    resistance: Optional[float]
    vwap: Optional[float]
    score: Optional[float]         # None = insufficient data, NOT zero
    score_breakdown: Dict[str, Optional[float]]
    decision: str                  # "ENTER" | "SKIP" | "NO_DATA"
    reason: str


# ══════════════════════════════════════════════════════════════════
# PCR
# ══════════════════════════════════════════════════════════════════

def _select_strikes(snapshot: OptionChainSnapshot, config: OptionsConfirmationConfig) -> List[float]:
    strikes = sorted(snapshot.per_strike.keys())
    if config.strike_range_method == "all" or not strikes:
        return strikes
    atm = round(snapshot.spot / config.strike_step) * config.strike_step
    lo  = atm - config.atm_strike_count * config.strike_step
    hi  = atm + config.atm_strike_count * config.strike_step
    return [k for k in strikes if lo <= k <= hi]


def calculate_pcr(snapshot: Optional[OptionChainSnapshot],
                   config: OptionsConfirmationConfig) -> Optional[float]:
    """PCR = sum(put OI) / sum(call OI) over the configured strike range."""
    if snapshot is None or not snapshot.per_strike:
        return None
    strikes = _select_strikes(snapshot, config)
    if not strikes:
        return None
    put_oi  = sum(snapshot.per_strike[k].put_oi  for k in strikes)
    call_oi = sum(snapshot.per_strike[k].call_oi for k in strikes)
    if call_oi <= 0:
        return None
    return put_oi / call_oi


def calculate_pcr_change(history: List[OptionChainSnapshot],
                          as_of: datetime,
                          config: OptionsConfirmationConfig) -> Optional[float]:
    """Current PCR minus the PCR reading `pcr_change_window_min` minutes ago.
    Positive = increasing, negative = decreasing. None if either side of the
    comparison isn't available in `history`."""
    now_snap  = _snapshot_at_or_before(history, as_of)
    past_snap = _snapshot_at_or_before(
        history, as_of - timedelta(minutes=config.pcr_change_window_min))
    if now_snap is None or past_snap is None or now_snap is past_snap:
        return None
    pcr_now  = calculate_pcr(now_snap, config)
    pcr_past = calculate_pcr(past_snap, config)
    if pcr_now is None or pcr_past is None:
        return None
    return pcr_now - pcr_past


def _snapshot_at_or_before(history: List[OptionChainSnapshot],
                            ts: datetime) -> Optional[OptionChainSnapshot]:
    candidates = [s for s in history if s.timestamp <= ts]
    return max(candidates, key=lambda s: s.timestamp) if candidates else None


# ══════════════════════════════════════════════════════════════════
# OI changes
# ══════════════════════════════════════════════════════════════════

def calculate_oi_changes(history: List[OptionChainSnapshot],
                          as_of: datetime,
                          config: OptionsConfirmationConfig) -> dict:
    """Returns {"put_oi", "call_oi", "put_oi_change", "call_oi_change"} — the
    current aggregate OI on each side (over the configured strike range) and
    its change over `oi_change_window_min`. Any field is None if the
    underlying snapshot data isn't available — never defaulted to 0, since 0
    would be indistinguishable from "genuinely unchanged"."""
    now_snap  = _snapshot_at_or_before(history, as_of)
    past_snap = _snapshot_at_or_before(
        history, as_of - timedelta(minutes=config.oi_change_window_min))
    out = {"put_oi": None, "call_oi": None, "put_oi_change": None, "call_oi_change": None}
    if now_snap is None:
        return out
    strikes_now = _select_strikes(now_snap, config)
    if not strikes_now:
        return out
    put_now  = sum(now_snap.per_strike[k].put_oi  for k in strikes_now)
    call_now = sum(now_snap.per_strike[k].call_oi for k in strikes_now)
    out["put_oi"], out["call_oi"] = put_now, call_now
    if past_snap is None or past_snap is now_snap:
        return out
    strikes_past = _select_strikes(past_snap, config)
    if not strikes_past:
        return out
    put_past  = sum(past_snap.per_strike[k].put_oi  for k in strikes_past)
    call_past = sum(past_snap.per_strike[k].call_oi for k in strikes_past)
    out["put_oi_change"]  = put_now  - put_past
    out["call_oi_change"] = call_now - call_past
    return out


def classify_oi_action(oi_change: Optional[float],
                        price_change: Optional[float]) -> Optional[str]:
    """Standard OI-buildup interpretation table (documented in the module
    docstring). `price_change` should be the same side's own premium change
    where available, else the underlying spot's change as an approximation —
    caller is responsible for documenting which one it passed. This is a
    genuinely separate signal from `oi_change` — passing the same value for
    both collapses the four-way classification to two outcomes and is a
    caller bug, not a valid use of this function."""
    if oi_change is None or price_change is None:
        return None
    if oi_change > 0 and price_change > 0:
        return "long_buildup"
    if oi_change > 0 and price_change <= 0:
        return "short_buildup"
    if oi_change <= 0 and price_change > 0:
        return "short_covering"
    return "long_unwinding"


def _spot_change(history: List[OptionChainSnapshot], as_of: datetime,
                  window_min: int) -> Optional[float]:
    """Underlying spot change over `window_min`, from the snapshots
    themselves — the price-change signal `classify_oi_action` uses as an
    approximation when per-side premium history isn't available."""
    now_snap  = _snapshot_at_or_before(history, as_of)
    past_snap = _snapshot_at_or_before(history, as_of - timedelta(minutes=window_min))
    if now_snap is None or past_snap is None or now_snap is past_snap:
        return None
    return now_snap.spot - past_snap.spot


# ══════════════════════════════════════════════════════════════════
# Support / Resistance
# ══════════════════════════════════════════════════════════════════

def identify_support_resistance(history: List[OptionChainSnapshot],
                                  as_of: datetime,
                                  config: OptionsConfirmationConfig) -> dict:
    """Returns {"support", "resistance"} using only snapshots at/before
    `as_of` — see `OptionsConfirmationConfig.sr_method` for the three
    selectable methodologies."""
    now_snap = _snapshot_at_or_before(history, as_of)
    out = {"support": None, "resistance": None}
    if now_snap is None:
        return out
    strikes = _select_strikes(now_snap, config)
    if not strikes:
        return out

    if config.sr_method == "highest_oi":
        support    = max(strikes, key=lambda k: now_snap.per_strike[k].put_oi)
        resistance = max(strikes, key=lambda k: now_snap.per_strike[k].call_oi)
        out["support"], out["resistance"] = support, resistance

    elif config.sr_method == "highest_oi_change":
        past_snap = _snapshot_at_or_before(
            history, as_of - timedelta(minutes=config.oi_change_window_min))
        if past_snap is None or past_snap is now_snap:
            return out
        past_strikes = set(_select_strikes(past_snap, config))

        def delta(k, side):
            if k not in past_strikes:
                return None
            now_v  = getattr(now_snap.per_strike[k],  side)
            past_v = getattr(past_snap.per_strike[k], side)
            return now_v - past_v

        put_deltas  = {k: delta(k, "put_oi")  for k in strikes}
        call_deltas = {k: delta(k, "call_oi") for k in strikes}
        put_deltas  = {k: v for k, v in put_deltas.items()  if v is not None}
        call_deltas = {k: v for k, v in call_deltas.items() if v is not None}
        if put_deltas:
            out["support"] = max(put_deltas, key=put_deltas.get)
        if call_deltas:
            out["resistance"] = max(call_deltas, key=call_deltas.get)

    elif config.sr_method == "top3_oi":
        top3_put  = sorted(strikes, key=lambda k: now_snap.per_strike[k].put_oi,  reverse=True)[:3]
        top3_call = sorted(strikes, key=lambda k: now_snap.per_strike[k].call_oi, reverse=True)[:3]
        if top3_put:
            out["support"] = statistics.mean(top3_put)
        if top3_call:
            out["resistance"] = statistics.mean(top3_call)

    return out


# ══════════════════════════════════════════════════════════════════
# VWAP — reused, not reimplemented
# ══════════════════════════════════════════════════════════════════

def get_vwap_series(df_5m):
    """Thin pass-through to backtest._vwap so callers don't need to import
    backtest directly just to compute VWAP. Same calculation, same object."""
    return bt._vwap(df_5m)


# ══════════════════════════════════════════════════════════════════
# Scoring
# ══════════════════════════════════════════════════════════════════

def calculate_options_score(direction: str,
                              pcr: Optional[float],
                              pcr_change: Optional[float],
                              put_oi_change: Optional[float],
                              call_oi_change: Optional[float],
                              price: Optional[float],
                              support: Optional[float],
                              resistance: Optional[float],
                              vwap: Optional[float],
                              config: OptionsConfirmationConfig) -> tuple:
    """Implements the exact scoring table from the task spec, symmetric for
    CE (bullish) and PE (bearish). Returns (score, breakdown).

    `score` is None — not 0 — if EVERY component is undecidable (no data at
    all), so a missing-data signal can never masquerade as "0/100, skip."
    Each component that *can* be evaluated contributes its points or 0;
    components that individually can't be evaluated (that one input is None)
    contribute None in the breakdown (visible in the log) and 0 to the total.
    """
    bullish = direction == "CE"
    w = config.weights
    breakdown: Dict[str, Optional[float]] = {}

    # 1. PCR level
    if pcr is None:
        breakdown["pcr_level"] = None
    elif bullish:
        breakdown["pcr_level"] = w["pcr_level"] if pcr >= config.pcr_bullish_threshold else 0
    else:
        breakdown["pcr_level"] = w["pcr_level"] if pcr <= config.pcr_bearish_threshold else 0

    # 2. PCR direction
    if pcr_change is None:
        breakdown["pcr_direction"] = None
    elif bullish:
        breakdown["pcr_direction"] = w["pcr_direction"] if pcr_change > 0 else 0
    else:
        breakdown["pcr_direction"] = w["pcr_direction"] if pcr_change < 0 else 0

    # 3. Put OI direction
    if put_oi_change is None:
        breakdown["put_oi_direction"] = None
    elif bullish:
        breakdown["put_oi_direction"] = w["put_oi_direction"] if put_oi_change > 0 else 0
    else:
        breakdown["put_oi_direction"] = w["put_oi_direction"] if put_oi_change < 0 else 0

    # 4. Call OI direction
    if call_oi_change is None:
        breakdown["call_oi_direction"] = None
    elif bullish:
        breakdown["call_oi_direction"] = w["call_oi_direction"] if call_oi_change < 0 else 0
    else:
        breakdown["call_oi_direction"] = w["call_oi_direction"] if call_oi_change > 0 else 0

    # 5. Support / resistance
    level = support if bullish else resistance
    if price is None or level is None:
        breakdown["support_resistance"] = None
    elif bullish:
        breakdown["support_resistance"] = w["support_resistance"] if price > level else 0
    else:
        breakdown["support_resistance"] = w["support_resistance"] if price < level else 0

    # 6. VWAP
    if price is None or vwap is None:
        breakdown["vwap"] = None
    elif bullish:
        breakdown["vwap"] = w["vwap"] if price > vwap else 0
    else:
        breakdown["vwap"] = w["vwap"] if price < vwap else 0

    evaluated = [v for v in breakdown.values() if v is not None]
    if not evaluated:
        return None, breakdown
    return float(sum(evaluated)), breakdown


# ══════════════════════════════════════════════════════════════════
# Orchestration — the one entry point strategy code should call
# ══════════════════════════════════════════════════════════════════

def get_options_confirmation(direction: str,
                              timestamp: datetime,
                              spot: float,
                              vwap: Optional[float],
                              provider: OptionsDataProvider,
                              config: Optional[OptionsConfirmationConfig] = None) -> OptionsConfirmationResult:
    """The single call site strategy/backtest code should use. Fetches only
    data at-or-before `timestamp` from `provider` (see anti-look-ahead note
    in the module docstring), computes the full score breakdown, and returns
    a result ready to both gate the entry and log as one CSV/JSON row.

    If `provider` has no data for this timestamp (including the stub
    `HistoricalOptionsDataProvider`, which always raises), this returns a
    NO_DATA result rather than propagating the exception — a single
    unscoreable signal must not crash a multi-day backtest loop.
    """
    config = config or OptionsConfirmationConfig()
    lookback = max(config.pcr_change_window_min, config.oi_change_window_min) + 5

    try:
        history = provider.get_history(timestamp, lookback_minutes=lookback)
    except NoHistoricalOptionsDataError as e:
        return _no_data_result(direction, timestamp, vwap, reason=str(e))

    now_snap = _snapshot_at_or_before(history, timestamp)
    if now_snap is None:
        return _no_data_result(direction, timestamp, vwap,
                               reason="No options-chain snapshot at or before signal timestamp.")

    pcr        = calculate_pcr(now_snap, config)
    pcr_change = calculate_pcr_change(history, timestamp, config)
    oi         = calculate_oi_changes(history, timestamp, config)
    sr         = identify_support_resistance(history, timestamp, config)

    spot_change = _spot_change(history, timestamp, config.oi_change_window_min)
    put_action  = classify_oi_action(oi["put_oi_change"],  spot_change)
    call_action = classify_oi_action(oi["call_oi_change"], spot_change)

    score, breakdown = calculate_options_score(
        direction=direction, pcr=pcr, pcr_change=pcr_change,
        put_oi_change=oi["put_oi_change"], call_oi_change=oi["call_oi_change"],
        price=spot, support=sr["support"], resistance=sr["resistance"],
        vwap=vwap, config=config,
    )

    if score is None:
        decision, reason = "NO_DATA", "No component of the score could be evaluated."
    elif score >= config.entry_threshold:
        decision, reason = "ENTER", f"score {score:.0f} >= threshold {config.entry_threshold:.0f}"
    else:
        decision, reason = "SKIP", f"score {score:.0f} < threshold {config.entry_threshold:.0f}"

    return OptionsConfirmationResult(
        timestamp=timestamp, direction=direction,
        pcr=pcr, pcr_change=pcr_change,
        put_oi=oi["put_oi"], call_oi=oi["call_oi"],
        put_oi_change=oi["put_oi_change"], call_oi_change=oi["call_oi_change"],
        put_oi_action=put_action, call_oi_action=call_action,
        support=sr["support"], resistance=sr["resistance"], vwap=vwap,
        score=score, score_breakdown=breakdown,
        decision=decision, reason=reason,
    )


def _no_data_result(direction, timestamp, vwap, reason) -> OptionsConfirmationResult:
    empty = {"pcr_level": None, "pcr_direction": None, "put_oi_direction": None,
             "call_oi_direction": None, "support_resistance": None, "vwap": None}
    return OptionsConfirmationResult(
        timestamp=timestamp, direction=direction,
        pcr=None, pcr_change=None, put_oi=None, call_oi=None,
        put_oi_change=None, call_oi_change=None,
        put_oi_action=None, call_oi_action=None,
        support=None, resistance=None, vwap=vwap,
        score=None, score_breakdown=empty,
        decision="NO_DATA", reason=reason,
    )
