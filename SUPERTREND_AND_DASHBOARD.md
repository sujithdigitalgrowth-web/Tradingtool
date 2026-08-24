# Supertrend Strategy & Dashboard — Complete Reference

Snapshot as coded today (2026-08-24), pulled directly from `backtest.py` (reference implementation), `live_trader.py` (the live execution engine deployed on Vultr), and `dashboard.py` (the Flask control panel). For the older V2 multi-filter strategy, see `STRATEGY.md` — this file covers the **Supertrend strategy** (selectable in the dashboard as "Strategy 6") and the dashboard UI end-to-end.

---

## Part 1 — The Supertrend Indicator (the math)

Defined once in `backtest.py::_supertrend()` and reused by both strategies (V2 uses it as one of seven filters; the Supertrend strategy uses it as the *only* signal).

```
atr = ATR(period)                          # Wilder-style EMA of true range
hl2 = (High + Low) / 2
basic_upper = hl2 + multiplier * atr
basic_lower = hl2 - multiplier * atr

final_upper[i] = basic_upper[i]  if basic_upper[i] < final_upper[i-1] or Close[i-1] > final_upper[i-1]
                 else final_upper[i-1]
final_lower[i] = basic_lower[i]  if basic_lower[i] > final_lower[i-1] or Close[i-1] < final_lower[i-1]
                 else final_lower[i-1]

direction[i] =  +1   if Close[i] > final_upper[i]      (bullish / "green")
             =  -1   if Close[i] < final_lower[i]      (bearish / "red")
             =  direction[i-1]   otherwise (inside the band — trend holds)
```

Output is a single integer per candle: **+1 (uptrend/green)** or **-1 (downtrend/red)**. A "flip" is when this value changes from one candle to the next — that flip *is* the signal.

Two different parameter sets are used depending on which strategy is running:

| | Period | Multiplier |
|---|---|---|
| V2 strategy's Supertrend filter | 7 | 2.0 |
| **Supertrend strategy (Strategy 6)** | **10** | **3.0** |

---

## Part 2 — Supertrend Strategy ("Strategy 6") Logic

Code: `live_trader.py::_check_signal_supertrend()` (entry) and `_manage_position_supertrend()` (exit), selected by setting `strategy="supertrend"`. Mirrors `supertrend_45day_trail_backtest.py` exactly — that's the validated reference backtest.

### Key design difference from V2

The Supertrend value is computed on the **entire continuous multi-day 5-minute candle series, with no daily reset**. The indicator's internal state (final_upper/final_lower bands, direction) carries across the overnight gap — only the *position* is force-flattened at end of day. This is deliberate: resetting the bands every morning would cause spurious flips right at the open that have nothing to do with the actual trend.

### Entry — dead simple, no confirmation stack

Unlike V2 (7 conditions + VIX + time window + ADX + cooldown), Strategy 6 is a **single-indicator directional follower**:

| Signal | Condition |
|---|---|
| `BUY_CE` | Supertrend flips **Red(-1) → Green(1)** on the latest closed 5-min candle |
| `BUY_PE` | Supertrend flips **Green(1) → Red(-1)** on the latest closed 5-min candle |

No VIX filter, no time-of-day window, no RSI, no EMA, no Bank Nifty confirmation, no ADX regime filter, no loss cooldown. It trades every flip, whenever it happens during market hours. Entry sizing, strike selection (ATM, rounded to nearest 50), expiry (next weekly, one cycle out), and balance-fit scaling are all shared with V2 via the common `_enter()` code path.

### Exit — three-tier profit lock + hard stops, in priority order

Checked every ~5 seconds (tick feed when available, else polled LTP) while a position is open:

1. **EOD square-off** — forced exit at **15:15**, regardless of P&L (shared gate, checked before either strategy's exit logic runs).

2. **Spot-based stop-loss** — if Nifty spot moves **50 points** against the position (adverse to CE if spot falls, adverse to PE if spot rises), exit immediately as `ST_SPOT_SL`. No confirmation wait — one clean threshold, unlike V2's two-tier warn/hard version.

3. **Three-tier profit floor** — tracks `trail_high` (the best LTP seen since entry) and computes `peak_pct = (trail_high − entry) / entry`. The floor is **always the max of whichever tier currently applies** — it only ratchets up, never back down, as `peak_pct` climbs past each boundary:

   | Peak gain reaches... | Floor becomes... | What it guarantees |
   |---|---|---|
   | **+15%** (`ST6_STEP1_TRIGGER`) | **breakeven** (0%) | A trade that got this far can never close as a real loss for the day |
   | **+25%** (`ST6_STEP2_TRIGGER`) | **+10%** (`ST6_STEP2_FLOOR`) | Locks in at least 10% once the move is clearly working |
   | **+32%** (`ST6_TRAIL_LOCK_TRIGGER`) | **`peak − 3%`**, continuous, no cap | Lets a genuine trend run, giving back only 3 points off the peak |

   If current unrealized % drops to or below whichever floor is active, exit as `ST_TRAIL_EXIT`.

   *Why this specific shape (from the code's own design note):* backtested over 45 days (Aug 2026) this returned **₹43,905** vs **₹33,562** for doing nothing extra (no profit lock at all), and vs **₹51,905** for a simpler "only lock in at +32%, no guarantee below that" version. The 3-tier version was chosen over the higher-EV 32%-only version specifically **for the loss guarantee below 32%** — trading some upside for the assurance that a trade which ever reached +15% can't turn into a losing day.

4. **Supertrend flip against the position** — if the Supertrend value flips against the held side on a newly closed candle (Green→Red while holding CE, or Red→Green while holding PE), exit as `ST_FLIP`. This is the strategy's core trend-following exit — get out the moment the indicator that got you in reverses.

There is no fixed take-profit and no premium-percentage stop-loss in this strategy — the profit floor and the Supertrend flip are the entire exit model, backed only by the 50-point spot hard-stop as a tail-risk backstop.

### Parameter reference (Strategy 6)

| Constant (in `backtest.py`) | Value | Meaning |
|---|---|---|
| `ST6_PERIOD` | 10 | Supertrend ATR period |
| `ST6_MULT` | 3.0 | Supertrend ATR multiplier |
| `ST6_SPOT_SL` | 50 pts | Adverse spot move → immediate stop |
| `ST6_STEP1_TRIGGER` / `_FLOOR` | 15% / 0% | Tier 1: breakeven lock |
| `ST6_STEP2_TRIGGER` / `_FLOOR` | 25% / 10% | Tier 2: +10% lock |
| `ST6_TRAIL_LOCK_TRIGGER` | 32% | Tier 3: switches to trailing |
| `ST6_TRAIL_GIVEBACK` | 3% | Tier 3: floor = peak − 3% |
| `LOT_SIZE` | 65 | NSE Nifty lot size (shared with V2) |

### What it shares with V2

Strike selection (ATM ± scan on miss), expiry choice (next weekly Tuesday, one cycle out for theta cushion), balance-based lot scaling / OTM fallback, order placement, WebSocket tick feed for live LTP, trade logging to `logs/trade_history.json`, and the daily loss-cap / profit-target entry gate (`MAX_DAILY_LOSS` / `DAILY_PROFIT_TARGET`, configurable per session from the dashboard) all run through the same shared code in `AngelTrader`, regardless of which strategy is active.

---

## Part 3 — Dashboard Preview (`dashboard.py`)

Single-file Flask app (Tailwind CSS via CDN, vanilla JS, no build step) serving one HTML page with two tabs, polling the live trader's state every few seconds via `/api/live-state`. Theme: white/lavender cards on a soft violet background ("violet/lavender" redesign, latest commit).

### Layout at a glance

```
┌──────────────────────────────────────────────────────────────────────┐
│  NIFTY 50  24,850.30      Available Cash ₹48,230   ⬤ Connected  12:41│  ← sticky header
├──────────────────────────────────────────────────────────────────────┤
│  [ Live Trading ]  [ Trade History ]                                 │  ← tabs
├──────────────────────────────────────────────────────────────────────┤
│  ┌───────────────────────────────────┐  ┌─────────────────────────┐ │
│  │  Open Position  [CE] [✕ Exit]      │  │  ARTHA BOT   [RUNNING]  │ │
│  │  NIFTY24AUG24850CE                 │  │  Supertrend Strategy    │ │
│  │  Qty 65 · Entry 12:35    +₹412.50  │  │  [▶/■/⚠/⚡ buttons]     │ │
│  │  LTP | SL | Target | Invested      │  │  [PAPER] [Supertrend]   │ │
│  │  (or "No active trade" empty state)│  │  Today's P&L  +₹412.50 │ │
│  │                                     │  │  🤖 Lots·Trades·WR14   │ │
│  │                                     │  │  ⬤ Bot is running      │ │
│  └───────────────────────────────────┘  └─────────────────────────┘ │
│  ┌───────────────────────────┐  ┌────────────────────────────────┐  │
│  │ 14-Day Performance         │  │ Bot Status  🤖                 │  │
│  │ P&L / Wins / Losses / WR   │  │ Last Signal · Supertrend · ⏱  │  │
│  │ [bar chart]                │  ├────────────────────────────────┤  │
│  ├─────────────────────────────┤  │ Cash │ Max Trades              │  │
│  │ Today's Trades (table)     │  │ Daily P&L │ Win Rate 14D        │  │
│  │                             │  ├────────────────────────────────┤  │
│  │                             │  │ Recent Activity (live feed)    │  │
│  └─────────────────────────────┘  └────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### Header (sticky)

Live Nifty 50 LTP, available cash balance, connection status pill (`⬤ Connecting` / `⬤ Connected`, gray → green), and a live IST clock ticking every second.

### Tab 1 — Live Trading

**Row 1, left — Open Position card** (2/3 width)
- Empty state by default: "No active trade — Trade details will appear here once the bot enters a position."
- When a position is open: symbol, side badge (CE/PE), manual **✕ Exit Trade** button, live P&L (₹ and %), LTP / Stop Loss / Target / Invested-capital grid, and status tag chips.

**Row 1, right — "ARTHA BOT" hero card** (1/3 width, the main control surface)
- Title + status badge: `STOPPED` (gray) / `RUNNING` (green) / etc.
- Subtitle dynamically reads **"Supertrend Strategy · Nifty 50 Options"** or **"V2 Strategy · Nifty 50 Options"** depending on which strategy is configured for the active session.
- Action buttons, shown/hidden by state: **▶ Start Bot** → opens the config modal · **■ Stop Bot** · **✕ Exit Trade** · **⚠ Force Exit** · **⚡ Test Trade**.
- Badges: `PAPER` (blue, shown only in paper mode) and a strategy badge (`V2` or `Supertrend`, violet pill).
- Today's P&L, large.
- Three stat pills: Position Size (lots), Trades Today, 14-Day Win Rate.
- Bottom status line with a colored dot: *"Bot is stopped"* / *"Bot is running — scanning for signals"* etc.

**Row 2, left column**
- **14-Day Performance**: total P&L, win count, loss count, win rate, and a small bar chart (`perf14-chart`) of daily results.
- **Today's Trades**: live table of the current day's fills as they happen.

**Row 2, right column**
- **Bot Status card**: status dot + label, "Last Signal", an indicator readout labeled dynamically (`Supertrend` value, or the V2 filter reason) with a countdown to the next scan, plus amber/red banners for active filter reasons or errors, and a "👁 View Logs" link that scrolls to the activity feed.
- **Mini stat tiles** (2×2 grid): Cash Available, Max Trades, Daily P&L, Win Rate (14D).
- **Recent Activity**: a live, newest-first feed of real bot events — market scans, entries, exits (win/loss color-coded) — capped at the last 20 (`AngelTrader._activity`, a `deque(maxlen=20)`), with a "View All" toggle.

### Tab 2 — Trade History

- Date-range picker (**From** / **To**) + **Load History** button, backed by `/api/trade-history`.
- Summary cards: Total Trades, Total P&L, Win Rate, Wins/Losses, Capital Deployed.
- Full trade table for the selected range, populated after loading.

### Start Trading modal (opened via ▶ Start Bot)

The one place session risk parameters are set before enabling order placement:

| Field | Range / options | Notes |
|---|---|---|
| Max Trades per Day | 1–6 (default 2) | Hard cap — bot won't exceed it even if more signals fire |
| Number of Lots | 1–20 (default 1) | × 65 units/lot shown live |
| **Strategy** | `V2` (multi-filter: VWAP/EMA/RSI/ADX/VIX) or `Supertrend` (10,3) follower + 50pt SL | Radio choice — this is what selects the logic documented in Part 2 |
| Mode | Paper Trading (default, simulated, no real orders) or Live Trading (real Angel One orders) | Amber warning banner shown for live mode |

Submitting posts `{max_trades, lots, paper, strategy}` to `/api/start-trading`, which calls `AngelTrader.start(...)` and persists the config to `logs/trading_config.json` so it survives a server restart (auto-resumed by `_init_trader()` on boot, with a Telegram notification either way).

### API surface (`dashboard.py`)

| Route | Purpose |
|---|---|
| `GET /api/balance` | Angel One account cash/margin |
| `GET /api/nifty-ltp` | Current Nifty 50 LTP |
| `GET /api/live-state` | Full polling payload: position, P&L, bot status, sig_info, activity feed |
| `POST /api/start-trading` | Start the bot with `{max_trades, lots, paper, strategy, max_daily_loss, daily_profit_target}` |
| `POST /api/stop-trading` | Stop new entries (keeps monitoring any open position) |
| `POST /api/force-exit` | Immediately flatten the current position |
| `POST /api/exit-position` | Manual exit (same effect, triggered by the "✕ Exit Trade" button) |
| `POST /api/test-trade` | Fire a synthetic trade to sanity-check the pipeline |
| `GET /api/debug-scrip` | Inspect the cached Angel One NFO scrip master |
| `GET/POST /api/trading-config` | Read/write session config (persisted to `logs/trading_config.json`) |
| `GET /api/trade-history` | Historical trades for the Trade History tab, filtered by date range |

### Notes

- The dashboard talks to `live_trader.AngelTrader`, which is lazily instantiated on first request (`get_trader()`) so Flask itself starts up instantly even before Angel One login succeeds.
- All persistent state lives under `logs/`: `live_state.json` (current status for polling), `trade_history.json` (full trade log), `trading_config.json` (session settings, survives restarts), `trading_enabled.json`.
- An older **Backtest / Date Range Analysis tab** was removed (commit `be135d5`) because it depended on Yahoo Finance data that started rate-limiting — the dashboard is now Live Trading + Trade History only. `take_screenshot.py` still references the removed `#tab-range` button and is stale.
- Bot is hosted on **Vultr** (not Railway, despite some in-code comments/messages still saying "Railway" — see project memory), deployed via SSH + `git pull` + service restart.

---

## File Map

| File | Role |
|---|---|
| `backtest.py` | Reference implementation — indicators (`_supertrend`, `_atr`, `_rsi`, `_adx`, `_vwap`), all strategy constants (`V2_*`, `ST6_*`), validated backtest runner |
| `live_trader.py` | `AngelTrader` — live execution engine, both strategies' signal/exit logic, Angel One order placement, WebSocket tick feed |
| `dashboard.py` | Flask control panel — UI, REST API, session config persistence |
| `supertrend_45day_trail_backtest.py` | The validated backtest Strategy 6's live logic mirrors exactly |
| `STRATEGY.md` | Deep-dive on the V2 strategy specifically (entry filters, exit priority, live/backtest risk-control gaps) |
| `strategy.py`, `bot.py`, `risk_manager.py` | Legacy Gann Square-of-9 strategy — **not** part of the deployed path (kept for reference only) |
