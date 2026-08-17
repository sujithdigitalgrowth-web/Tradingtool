# Artha Trading Bot — Strategy Documentation (V2)

Snapshot of the strategy exactly as coded today (2026-07-24), for your own review. Pulled directly from `backtest.py` (the validated reference implementation) and `live_trader.py` (the live execution engine that's actually deployed on Vultr via `dashboard.py`).

> **Note on legacy files:** `bot.py`, `strategy.py`, and `risk_manager.py` exist in the repo but are **not** part of the deployed path — the systemd service runs `dashboard.py`, which drives `live_trader.py`. Those three files look like an earlier iteration and are ignored below.

---

## 1. Instrument & Data

- **Traded instrument:** Nifty 50 weekly options (CE/PE), NFO, `INTRADAY` product type, market orders only.
- **Signal source:** NIFTYBEES (Nifty ETF, real volume) 5-minute candles — used as a liquid proxy for Nifty 50 since the index itself has no tradeable volume.
- **Confirmation source:** BANKBEES (Bank Nifty ETF) 5-minute candles — direction must agree with the Nifty signal.
- **VIX filter source:** India VIX, live value (NSE public API in live trading; Yahoo daily in backtest).
- **Expiry chosen:** next Thursday (`_next_thursday()`), i.e. always the nearest weekly expiry.
- **Strike chosen:** nearest ATM strike to current spot, rounded to 50 — recalculated fresh on every entry (`round(spot/50)*50`). It is *not* sticky to a previously-traded strike; it just comes out the same when spot hasn't moved 50+ points since the last trade.
- **Candle timing (fixed 2026-07-24):** Angel's historical API returns the still-forming 5-minute candle (Close = latest tick) when queried mid-bar. `_trim_forming_candle()` now drops that row everywhere data is fetched, so both entries and `EMA_EXIT` only ever act on a fully-closed candle — matching what the backtest has always assumed.

---

## 2. Entry Logic

All of the following must be true simultaneously on the **latest closed 5-min candle**:

| Condition | CE (bullish) | PE (bearish) |
|---|---|---|
| Price vs VWAP | `Close > VWAP` | `Close < VWAP` |
| Price vs EMA9 | `Close > EMA9` | `Close < EMA9` |
| Price vs EMA20 | `Close > EMA20` | `Close < EMA20` |
| Candle color | `Close > Open` | `Close < Open` |
| RSI(14) | `RSI > 60` | `RSI < 40` |
| Supertrend(7, ×2) | uptrend (1) | downtrend (-1) |
| Bank Nifty alignment | BANKBEES close > its VWAP | BANKBEES close < its VWAP |

**Gates applied on top of the raw signal:**
- **VIX filter:** only trade if `13 ≤ India VIX ≤ 30` (below 13 = premiums too thin; above 30 = deemed too wild).
- **Time window:** `10:15–12:00` (morning) or `13:30–14:50` (afternoon), lunch (`12:00–13:30`) always blocked.
- **Tuesday (weekly expiry) special case:** afternoon session fully blocked — theta decay too steep on expiry day itself.
- **Move-from-open filter:** skip if price has already moved >0.5% from today's open in the signal's direction (avoids chasing a move that's already played out).
- **Same-direction dedup (`last_signal`):** won't fire the *same* direction twice **while a position from that direction is still logically "active" in the dedup sense** — but see the caveat below, this resets on every exit.
- **Daily trade cap:** configurable, 1–6 trades/day (dashboard setting; today was set to 4).

**⚠️ Known gap — no cooldown after a losing exit:** `last_signal` resets to `None` the instant any position closes, regardless of exit reason. If the raw condition is still true on the very next closed candle (e.g. a choppy, range-bound market sitting right on VWAP/EMA9), the bot will immediately re-enter the same direction — and since spot usually hasn't moved 50 points, it lands on the *same* ATM strike. This is present in both live and backtest logic (not a live-only bug), and was directly demonstrated in the 2026-07-24 backtest: two separate PE losses stacked back-to-back on the same setup. **Not yet fixed** — flagged for a future cooldown/loss-streak guard if you want it.

---

## 3. Position Sizing

- Base size: `lots × 65` (NSE lot size, effective Oct 28 2025), `lots` configurable via dashboard (currently 1).
- **Dynamic scale-down (fixed 2026-07-24):** if available cash can't cover the configured lot count, the bot now scales down to the largest whole number of lots that *does* fit (minimum 1 lot) instead of skipping the trade outright. Still skips if even 1 lot doesn't fit.
- Required capital = full option premium × qty (options buying, no margin).

---

## 4. Exit Logic

Checked continuously while a position is open (every 5 seconds via the monitor loop), in this priority order:

1. **1-lot hard take-profit** *(disabled by default — `V2_1LOT_HARD_TP = False`)*: would exit at +10% or ₹1,100 absolute, whichever first. Currently skipped in favor of the trailing stop below (backtested: turns -₹34,059 into +₹1,046 over 138 days by letting winners run instead of capping early).
2. **2-lot late-entry full exit:** if entered after 14:30, take full profit at +10% (no time left to reach +20%).
3. **2-lot partial exit:** 1 lot booked at +10% gain, remainder keeps running.
4. **Trailing stop:** activates once unrealized gain hits +10%; floor steps up to breakeven after a partial exit, or stays at 0% floor for 1-lot trades. Exits if price falls back to the floor from the peak.
5. **Big-winner profit lock:** once peak gain reaches +15%, the trailing floor ratchets up to `peak − 8%` instead of sitting flat at breakeven — protects a large spike from fully round-tripping into a loss.
6. **Spot-based stop-loss (two-tier):** if Nifty spot moves against the position — 80 points = immediate exit (`SPOT_SL_HARD`); 50 points = needs 2 consecutive 5-second polls to confirm (`SPOT_SL`), filtering brief noise.
7. **Premium stop-loss (two-tier):** -20% = immediate exit (`SL_HARD`); -17% = needs 2 consecutive polls to confirm (`SL`).
8. **EMA9 trend-exit (`EMA_EXIT`):** if the underlying's closed-candle price crosses back through EMA9 against the position (Close < EMA9 while long PE-equivalent bearish stance, etc.) — the dominant exit mechanism in backtest, and now (as of the candle-timing fix) only evaluated on confirmed closes, not live tick noise.
9. **Hard take-profit:** +20% option gain (2-lot only).
10. **End-of-day square-off:** forced exit at 15:15 regardless of P&L.

---

## 5. Risk Controls — Live vs. Backtest Gap

| Control | Backtest (`backtest.py`) | Live (`live_trader.py`) |
|---|---|---|
| Max trades/day | `V2_MAX_TRADES = 2` (constant) | Configurable 1–6 via dashboard (default 2, today set to 4) |
| Daily loss cap | `MAX_DAILY_LOSS = -8000` — blocks new entries once breached | **Not enforced.** `daily_pnl` is tracked but never checked before allowing a new entry. |
| Daily profit lock | `DAILY_PROFIT_TARGET = 6000` — blocks new entries once hit | **Not enforced**, same as above. |
| Starting capital | Fresh `₹30,000` every simulated day | Real Angel One account cash balance, carries over intra-day losses |

**This is the most consequential gap for live risk management**: right now, the only things that can stop the bot from taking more trades on a bad day are (a) hitting the configured trade-count cap, or (b) running out of cash. There's no live equivalent of the backtest's `-₹8,000` daily-loss circuit breaker or `+₹6,000` profit lock. Worth deciding if you want that ported over — happy to implement if so.

---

## 6. Quick Parameter Reference

| Parameter | Value | Meaning |
|---|---|---|
| `LOT_SIZE` | 65 | NSE Nifty lot size |
| `V2_EMA_FAST` / `V2_EMA_SLOW` | 9 / 20 | Trend EMAs |
| `V2_RSI_PERIOD` | 14 | RSI lookback |
| `V2_RSI_MIN_CE` / `V2_RSI_MAX_PE` | 60 / 40 | RSI entry thresholds |
| `V2_ST_PERIOD` / `V2_ST_MULT` | 7 / 2.0 | Supertrend settings |
| `V2_VIX_MIN` / `V2_VIX_MAX` | 13 / 30 | Tradeable VIX band |
| `V2_NO_ENTRY_BEFORE` / `V2_MORNING_END` | 10:15 / 12:00 | Morning session |
| `V2_AFTERNOON_START` / `NO_ENTRY_AFTER` | 13:30 / 14:50 | Afternoon session |
| `SQUAREOFF_TIME` | 15:15 | Forced EOD exit |
| `V2_MAX_FROM_OPEN_PCT` | 0.5% | Move-from-open filter |
| `V2_PARTIAL_PCT` / `V2_TRAIL_TRIGGER` | 10% / 10% | Partial exit / trail activation |
| `V2_TRAIL_LOCK_TRIGGER` / `_GIVEBACK` | 15% / 8% | Big-winner profit lock |
| `V2_SL_WARN_PCT` / `V2_SL_OPTION_PCT` | 17% / 20% | Premium SL (warn / hard) |
| `V2_SPOT_SL_WARN` / `V2_SPOT_SL_HARD` | 50 / 80 pts | Spot SL (warn / hard) |
| `V2_TP_OPTION_PCT` | 20% | 2-lot hard take-profit |
| `V2_1LOT_HARD_TP` | `False` | 1-lot hard-cap disabled (validated default) |

---

## 7. Recent Changes (this investigation, 2026-07-24)

1. **Fixed:** live entry/exit signals now only evaluate fully-closed 5-min candles (`_trim_forming_candle`), eliminating 1-2 minute noise-driven `EMA_EXIT`s that didn't match backtested behavior.
2. **Fixed:** insufficient-balance skips now scale the trade down to whatever whole lot count fits, instead of skipping entirely.
3. **Not fixed (flagged, your call):** no cooldown after a losing exit — can re-enter the same direction/strike immediately in a choppy market. Backtest confirmed this cost a second stacked loss on 2026-07-24.
4. **Not fixed (flagged, your call):** live has no daily-loss circuit breaker or profit-lock, unlike the backtest's `-₹8,000` / `+₹6,000` caps.
