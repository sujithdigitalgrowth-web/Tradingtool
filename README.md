# Artha Trading Bot — Nifty 50

An automated intraday options trading bot for **Nifty 50 weekly options** on **Angel One (SmartAPI)**, with a live web dashboard for control and monitoring. Runs two selectable strategies — a multi-filter "V2" strategy and a single-indicator Supertrend follower — and supports both **paper trading** (simulated, no real orders) and **live trading** (real Angel One orders).

> ⚠️ **Financial risk disclaimer:** This bot places real orders with real money in live mode. Options trading carries substantial risk of loss. Nothing here is investment advice. Test thoroughly in paper mode before enabling live trading, and only trade capital you can afford to lose.

---

## What it does

- Watches NIFTYBEES (and BANKBEES for confirmation) 5-minute candles during market hours.
- Generates BUY_CE / BUY_PE signals using one of two strategies (configurable per session, no code changes needed):
  - **V2** — VWAP + EMA9/20 + RSI + Volume + Bank Nifty alignment + Supertrend(7,2) + ADX regime filter + VIX band + time-of-day windows.
  - **Supertrend** — a single Supertrend(10,3) flip, with a three-tier profit-lock trailing exit.
- Places market orders for the nearest ATM weekly option via Angel One SmartAPI, sized by configurable lots.
- Manages exits continuously (stop-loss, targets, trailing floors, EMA/Supertrend trend-exits, spot-based stops, end-of-day square-off at 15:15).
- Enforces daily risk caps (max trades/day, daily loss limit, daily profit lock).
- Sends Telegram alerts for entries, exits, errors, and server restarts.
- Serves a live dashboard (Flask) to start/stop the bot, watch positions and P&L in real time, and browse trade history.

Full strategy internals are documented separately:
- **[STRATEGY.md](STRATEGY.md)** — the V2 multi-filter strategy in depth.
- **[SUPERTREND_AND_DASHBOARD.md](SUPERTREND_AND_DASHBOARD.md)** — the Supertrend strategy's indicator math, entry/exit logic, and a full walkthrough of every dashboard panel.

---

## Architecture

```
dashboard.py      Flask web app — control panel + REST API, entry point in production
  └─ live_trader.py   AngelTrader — signal loop + position monitor loop, Angel One orders,
                       WebSocket tick feed, both strategies' logic
       └─ angel_data.py   Historical/intraday candle fetch helpers (Angel One + tokens)
       └─ login.py        Angel One SmartAPI session login (API key + TOTP)
  └─ backtest.py      Shared indicator math (Supertrend/ATR/RSI/ADX/VWAP) + all strategy
                       constants (V2_*, ST6_*) + the validated backtest runner
```

State persists to the `logs/` folder as JSON: `live_state.json` (current status, polled by the dashboard), `trade_history.json` (full trade log), `trading_config.json` (session settings — survives restarts and auto-resumes trading on boot).

`strategy.py`, `bot.py`, and `risk_manager.py` are an earlier Gann Square-of-9 strategy iteration — **not** part of the deployed path, kept for reference only.

---

## Setup

### Requirements

- Python 3.9+
- An Angel One (Angel Broking) trading account with SmartAPI access
- (Optional) A Telegram bot for alerts

### Install

```bash
pip install -r requirements.txt
```

### Configure environment variables

Create a `.env` file in the project root (never commit this file):

```
ANGEL_API_KEY=
ANGEL_SECRET_KEY=
ANGEL_CLIENT_ID=
ANGEL_PASSWORD=
ANGEL_TOTP_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

| Variable | Required | Purpose |
|---|---|---|
| `ANGEL_API_KEY` | Yes | Angel One SmartAPI app key |
| `ANGEL_CLIENT_ID` | Yes | Angel One client/login ID |
| `ANGEL_PASSWORD` | Yes | Angel One account password |
| `ANGEL_TOTP_SECRET` | Yes | TOTP secret for 2FA (from SmartAPI app setup) |
| `ANGEL_SECRET_KEY` | Optional | Used by some SmartAPI flows |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Optional | Enables Telegram alerts for entries, exits, errors, restarts |

### Run locally

```bash
python dashboard.py
```

Then open `http://localhost:5000`. The dashboard starts instantly even before Angel One login completes (login happens in a background thread on first request). Use **▶ Start Bot** to open the session config modal — choose strategy, lots, max trades/day, and **Paper vs Live** mode before enabling anything.

Always test in **Paper Trading** mode first — it runs the full signal/exit pipeline against real market prices without placing real orders.

---

## Deployment

Production runs on a **Vultr** VPS as a systemd service (not Railway, despite some in-code comments/messages referencing it).

```bash
# from your local machine
git add <files> && git commit -m "..." && git push

# on the server
ssh root@<server-ip>
cd ~/tradingbot && git pull
systemctl restart tradingbot.service
systemctl status tradingbot.service --no-pager -l   # confirm "active (running)"
```

`git pull` alone does **not** apply code changes — the running process keeps old code in memory until restarted. **Before restarting, confirm no position is open** on the dashboard's Live tab; this is a live-money bot, and a mid-trade restart can disrupt order/state tracking.

The systemd unit runs `python3 dashboard.py` directly. For a standard Python-hosting platform instead, `Procfile` defines a gunicorn entry point:

```
web: gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT dashboard:app
```

Use `--workers 1` — the bot's trading state (`AngelTrader`) lives in process memory, so multiple workers would each run an independent, uncoordinated copy of the bot.

---

## Project structure

| File | Role |
|---|---|
| `dashboard.py` | Flask control panel — UI, REST API, session config persistence |
| `live_trader.py` | `AngelTrader` — live signal/exit engine for both strategies, order placement, WebSocket feed |
| `backtest.py` | Indicator math, strategy constants, validated backtest runner |
| `angel_data.py` | Angel One candle-data fetch helpers |
| `login.py` | Angel One SmartAPI login (API key + TOTP, with retry) |
| `requirements.txt` | Python dependencies |
| `Procfile` | Gunicorn entry point for platform deploys |
| `STRATEGY.md` | V2 strategy deep-dive |
| `SUPERTREND_AND_DASHBOARD.md` | Supertrend strategy + full dashboard walkthrough |
| `logs/` | Runtime state and trade history (JSON, git-ignored) |
| `strategy.py`, `bot.py`, `risk_manager.py` | Legacy Gann strategy — not deployed |
| `supertrend_*.py`, `compare_*.py`, `ab_test_*.py`, etc. | One-off backtest/comparison scripts used during strategy research |
