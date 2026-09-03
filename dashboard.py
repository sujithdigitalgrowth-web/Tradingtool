import json, os, threading, requests as _requests
from flask import Flask, render_template_string, jsonify, request
from datetime import datetime, timezone, timedelta
import backtest as bt

# Cloud hosts (Vultr included) typically run system clocks in UTC — this
# project's users and market hours are IST, so timestamps shown in the UI
# must be converted explicitly rather than trusting datetime.now().
_IST = timezone(timedelta(hours=5, minutes=30))
def _now_ist():
    return datetime.now(_IST).replace(tzinfo=None)

def _tg(msg: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat  = os.getenv("TELEGRAM_CHAT_ID",   "")
    if not token or not chat:
        return
    try:
        _requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass

app = Flask(__name__)

TRADING_FLAG_FILE   = "logs/trading_enabled.json"
TRADING_CONFIG_FILE = "logs/trading_config.json"
LIVE_STATE_FILE     = "logs/live_state.json"

os.makedirs("logs", exist_ok=True)

# ── Lazy-load trader (avoids blocking Flask startup) ─────────────
_trader = None
_trader_lock = threading.Lock()

def get_trader():
    global _trader
    if _trader is None:
        with _trader_lock:
            if _trader is None:
                from live_trader import AngelTrader
                _trader = AngelTrader()
                threading.Thread(target=_init_trader, daemon=True).start()
    return _trader

def _init_trader():
    try:
        t = get_trader()
        t.login()
        # Auto-resume trading if it was active before the server restarted
        cfg = load_json(TRADING_CONFIG_FILE)
        if cfg.get("active"):
            max_trades          = int(cfg.get("max_trades", 2))
            lots                = int(cfg.get("lots", 1))
            paper               = bool(cfg.get("paper", False))
            max_daily_loss      = float(cfg.get("max_daily_loss", bt.MAX_DAILY_LOSS))
            daily_profit_target = float(cfg.get("daily_profit_target", bt.DAILY_PROFIT_TARGET))
            strategy            = cfg.get("strategy", "v2")
            manual_target_pct   = cfg.get("manual_target_pct")
            carry_overnight     = bool(cfg.get("carry_overnight", False))
            t.start(max_trades=max_trades, lots=lots, paper_mode=paper,
                   max_daily_loss=max_daily_loss, daily_profit_target=daily_profit_target,
                   strategy=strategy,
                   manual_target_pct=(float(manual_target_pct) / 100.0 if manual_target_pct else None),
                   carry_overnight=carry_overnight)
            from logzero import logger
            logger.info(f"Auto-resumed trading: strategy={strategy}, {lots} lot(s), "
                       f"max {max_trades} trades, paper={paper}")
            mode = "📋 PAPER" if paper else "🟢 LIVE"
            _tg(f"🔄 <b>Server Restarted — Trading Auto-Resumed</b>\n"
                f"Strategy: {strategy}\n"
                f"Mode   : {mode}\n"
                f"Lots   : {lots}  |  Max trades: {max_trades}\n"
                f"Time   : {datetime.now().strftime('%d %b %Y %H:%M:%S')}\n"
                f"Status : Connected to Angel One ✅")
        else:
            _tg(f"🔄 <b>Server Restarted</b>\n"
                f"Time   : {datetime.now().strftime('%d %b %Y %H:%M:%S')}\n"
                f"Status : Connected ✅ — Trading is STOPPED\n"
                f"Action : Open dashboard and click ▶ Start Trading")
    except Exception as e:
        _tg(f"🔴 <b>Server Restart FAILED</b>\n"
            f"Error  : {e}\n"
            f"Time   : {datetime.now().strftime('%d %b %Y %H:%M:%S')}\n"
            f"Action : Check Angel One credentials / network on Railway")


# ── Generic helpers ───────────────────────────────────────────────

def load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _get_trading_config():
    cfg = load_json(TRADING_CONFIG_FILE)
    manual_target_pct = cfg.get("manual_target_pct")
    return {
        "max_trades":          int(cfg.get("max_trades", 2)),
        "lots":                int(cfg.get("lots", 1)),
        "paper":               bool(cfg.get("paper", True)),
        "active":              bool(cfg.get("active", False)),
        "max_daily_loss":      float(cfg.get("max_daily_loss", bt.MAX_DAILY_LOSS)),
        "daily_profit_target": float(cfg.get("daily_profit_target", bt.DAILY_PROFIT_TARGET)),
        "strategy":            cfg.get("strategy", "v2"),
        "manual_target_pct":   float(manual_target_pct) if manual_target_pct is not None else None,
        "carry_overnight":     bool(cfg.get("carry_overnight", False)),
    }

def _save_trading_config(cfg):
    os.makedirs("logs", exist_ok=True)
    with open(TRADING_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── API: Balance & market data ────────────────────────────────────

@app.route("/api/balance")
def api_balance():
    try:
        return jsonify(get_trader().get_balance())
    except Exception as e:
        return jsonify({"available_cash": 0, "error": str(e)})

@app.route("/api/nifty-ltp")
def api_nifty_ltp():
    try:
        ltp = get_trader().get_nifty_ltp()
        return jsonify({"ltp": ltp})
    except Exception as e:
        return jsonify({"ltp": 0, "error": str(e)})

@app.route("/api/indices")
def api_indices():
    try:
        return jsonify({"indices": get_trader().get_indices(),
                         "pcr": get_trader().get_nifty_pcr(),
                         "time": _now_ist().strftime("%H:%M:%S")})
    except Exception as e:
        return jsonify({"indices": [], "pcr": None, "error": str(e)})

@app.route("/api/sector-indices")
def api_sector_indices():
    try:
        return jsonify({"indices": get_trader().get_sector_indices(),
                         "time": _now_ist().strftime("%H:%M:%S")})
    except Exception as e:
        return jsonify({"indices": [], "error": str(e)})

# ── API: Live state ───────────────────────────────────────────────

@app.route("/api/live-state")
def api_live_state():
    # Try in-memory first (trader running), fall back to file
    try:
        t = get_trader()
        if t.connected or t._running:
            return jsonify(t.get_state())
    except Exception:
        pass
    return jsonify(load_json(LIVE_STATE_FILE))

# ── API: Trading control ──────────────────────────────────────────

def _parse_manual_target_pct(body, key="manual_target_pct"):
    """Manual take-profit %, given/stored as a plain percent number (10 = 10%),
    converted to the 0-1 fraction live_trader.py's checks expect. None/0/""
    disables it. Clamped to (0, 100] to reject nonsense input."""
    if key not in body:
        return "unset"   # sentinel: caller should keep the existing value
    raw = body.get(key)
    if raw in (None, "", 0, "0"):
        return None
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return None
    if pct <= 0:
        return None
    return round(min(100.0, pct), 2)

@app.route("/api/start-trading", methods=["POST"])
def api_start_trading():
    body                = request.json or {}
    max_trades          = max(1, min(6, int(body.get("max_trades", 2))))
    lots                = max(1, min(20, int(body.get("lots", 1))))
    paper               = bool(body.get("paper", False))
    max_daily_loss      = min(-500, max(-50000, float(body.get("max_daily_loss", bt.MAX_DAILY_LOSS))))
    daily_profit_target = max(500, min(50000, float(body.get("daily_profit_target", bt.DAILY_PROFIT_TARGET))))
    strategy            = body.get("strategy", "v2")
    if strategy not in ("v2", "supertrend"):
        strategy = "v2"
    manual_target_pct = _parse_manual_target_pct(body)
    if manual_target_pct == "unset":
        manual_target_pct = None
    carry_overnight = bool(body.get("carry_overnight", False))
    try:
        t = get_trader()
        t.start(max_trades=max_trades, lots=lots, paper_mode=paper,
               max_daily_loss=max_daily_loss, daily_profit_target=daily_profit_target,
               strategy=strategy,
               manual_target_pct=(manual_target_pct / 100.0 if manual_target_pct else None),
               carry_overnight=carry_overnight)
        _save_trading_config({"max_trades": max_trades, "lots": lots, "paper": paper, "active": True,
                              "max_daily_loss": max_daily_loss, "daily_profit_target": daily_profit_target,
                              "strategy": strategy, "manual_target_pct": manual_target_pct,
                              "carry_overnight": carry_overnight})
        return jsonify({"status": "started", "max_trades": max_trades, "lots": lots, "paper": paper,
                        "max_daily_loss": max_daily_loss, "daily_profit_target": daily_profit_target,
                        "strategy": strategy, "manual_target_pct": manual_target_pct,
                        "carry_overnight": carry_overnight})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stop-trading", methods=["POST"])
def api_stop_trading():
    try:
        get_trader().stop()
        cfg = _get_trading_config()
        cfg["active"] = False
        _save_trading_config(cfg)
        return jsonify({"status": "stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/force-exit", methods=["POST"])
def api_force_exit():
    try:
        get_trader().force_exit()
        cfg = _get_trading_config()
        cfg["active"] = False
        _save_trading_config(cfg)
        return jsonify({"status": "exited"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/exit-position", methods=["POST"])
def api_exit_position():
    try:
        get_trader().exit_position()
        return jsonify({"status": "exited"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/add-lot", methods=["POST"])
def api_add_lot():
    try:
        ok, msg = get_trader().add_lot()
        return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 400)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sell-lot", methods=["POST"])
def api_sell_lot():
    try:
        ok, msg = get_trader().sell_lot()
        return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 400)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/test-trade", methods=["POST"])
def api_test_trade():
    """Force a real CE buy then auto-exit after 5 seconds — for connectivity testing only."""
    try:
        t = get_trader()
        if not t.connected:
            return jsonify({"error": "Not connected to Angel One"}), 400
        if not t._running:
            return jsonify({"error": "Start Trading first before running test"}), 400
        if t.position["active"]:
            return jsonify({"error": "Already in a position — exit it first"}), 400

        body = request.get_json(force=True, silent=True) or {}
        force_strike = int(body["strike"]) if body.get("strike") else None
        entered = t._enter("BUY_CE", force_strike=force_strike, is_test=True)
        if not entered:
            err = t.last_error or "Entry failed — check logs"
            return jsonify({"error": err}), 500

        def _auto_exit():
            import time as _t
            _t.sleep(5)
            t._exit("TEST_EXIT")
        threading.Thread(target=_auto_exit, daemon=True).start()

        return jsonify({"status": "ok", "message": "Test CE order placed — auto-exiting in 5 seconds"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── API: Scrip debug ─────────────────────────────────────────────

@app.route("/api/debug-scrip")
def api_debug_scrip():
    """Dump scrip cache entries near a strike to diagnose symbol format issues."""
    try:
        t = get_trader()
        scrip = t._scrip
        if not scrip:
            return jsonify({"error": "Scrip cache empty — start trading first", "total": 0})

        strike = request.args.get("strike", "")
        # Show sample of any NIFTY entries, or filter by strike
        if strike:
            matches = [x for x in scrip if strike in x.get("symbol", "")]
        else:
            matches = [x for x in scrip if "NIFTY" in x.get("symbol", "")][:20]

        from live_trader import _next_thursday, _expiry_tag
        expiry = _next_thursday()
        tag    = _expiry_tag(expiry)

        return jsonify({
            "total_nifty_options": len(scrip),
            "expiry_date": str(expiry),
            "expiry_tag_tried": tag,
            "sample_symbols": [x.get("symbol") for x in matches[:30]],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── API: Trading config ───────────────────────────────────────────

@app.route("/api/trading-config", methods=["GET"])
def api_get_config():
    return jsonify(_get_trading_config())

@app.route("/api/trading-config", methods=["POST"])
def api_set_config():
    body                = request.json or {}
    max_trades          = max(1, min(6, int(body.get("max_trades", 2))))
    lots                = max(1, min(20, int(body.get("lots", 1))))
    existing            = _get_trading_config()
    max_daily_loss      = min(-500, max(-50000, float(body.get("max_daily_loss", existing["max_daily_loss"]))))
    daily_profit_target = max(500, min(50000, float(body.get("daily_profit_target", existing["daily_profit_target"]))))
    manual_target_pct   = _parse_manual_target_pct(body)
    if manual_target_pct == "unset":
        manual_target_pct = existing.get("manual_target_pct")
    carry_overnight     = bool(body.get("carry_overnight", existing.get("carry_overnight", False)))
    cfg = {"max_trades": max_trades, "lots": lots,
           "paper": existing.get("paper", True), "active": existing.get("active", False),
           "max_daily_loss": max_daily_loss, "daily_profit_target": daily_profit_target,
           "strategy": existing.get("strategy", "v2"),
           "manual_target_pct": manual_target_pct, "carry_overnight": carry_overnight}
    _save_trading_config(cfg)
    return jsonify(cfg)

@app.route("/api/manual-target", methods=["POST"])
def api_set_manual_target():
    """Apply a take-profit % (or clear it) to the currently RUNNING trader
    immediately, without needing to stop/restart trading. Persists too, so it
    survives a server restart's auto-resume."""
    body = request.json or {}
    pct = _parse_manual_target_pct(body, key="pct")
    if pct == "unset":
        return jsonify({"error": "Missing 'pct' (a percent number, or null/0 to disable)"}), 400
    try:
        t = get_trader()
        t.manual_target_pct = (pct / 100.0) if pct else None
        cfg = _get_trading_config()
        cfg["manual_target_pct"] = pct
        _save_trading_config(cfg)
        return jsonify({"status": "ok", "manual_target_pct": pct})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/carry-overnight", methods=["POST"])
def api_set_carry_overnight():
    """Toggle carry_overnight on the currently RUNNING trader immediately."""
    body    = request.json or {}
    enabled = bool(body.get("enabled", False))
    try:
        t = get_trader()
        t.carry_overnight = enabled
        cfg = _get_trading_config()
        cfg["carry_overnight"] = enabled
        _save_trading_config(cfg)
        return jsonify({"status": "ok", "carry_overnight": enabled})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── API: Trade History ───────────────────────────────────────────

TRADE_LOG_FILE     = "logs/trade_history.json"
TEST_ORDER_ID_FILE = "logs/test_order_ids.json"

def _load_test_order_ids():
    """Order IDs placed by the ⚡ Test Trade button — excluded from trade history/stats."""
    try:
        if os.path.exists(TEST_ORDER_ID_FILE):
            with open(TEST_ORDER_ID_FILE) as f:
                return set(str(x) for x in json.load(f))
    except Exception:
        pass
    return set()

@app.route("/api/trade-history")
def api_trade_history():
    from_date = request.args.get("from", "")
    to_date   = request.args.get("to",   "")
    today_str = _now_ist().strftime("%Y-%m-%d")

    records = []
    test_order_ids = _load_test_order_ids()

    # ── Today: pull directly from Angel One tradeBook (fill prices) ──
    today_in_range = (not from_date or from_date <= today_str) and (not to_date or to_date >= today_str)
    if today_in_range:
        try:
            t = get_trader()
            if t.connected:
                resp = t._obj.tradeBook()
                if resp and resp.get("status") and resp.get("data"):
                    fills = [r for r in resp["data"] if "NIFTY" in r.get("tradingsymbol", "")
                             and r.get("producttype") == "INTRADAY"
                             and str(r.get("orderid")) not in test_order_ids]
                    # Pair each SELL with the oldest still-open BUY of the same symbol
                    # (FIFO) instead of one dict entry per symbol — the same strike can
                    # be round-tripped more than once a day, and a last-wins dict silently
                    # drops/misattributes the earlier trades in that case.
                    fills.sort(key=lambda r: r.get("filltime", ""))
                    open_buys = {}
                    for r in fills:
                        sym = r.get("tradingsymbol")
                        if r.get("transactiontype") == "BUY":
                            open_buys.setdefault(sym, []).append(r)
                        elif r.get("transactiontype") == "SELL":
                            queue = open_buys.get(sym)
                            buy   = queue.pop(0) if queue else None
                            qty        = int(r.get("fillsize") or r.get("quantity") or 0)
                            exit_fill  = float(r.get("fillprice") or 0)
                            entry_fill = float(buy.get("fillprice") or 0) if buy else 0.0
                            if not qty or not exit_fill:
                                continue
                            pnl     = round((exit_fill - entry_fill) * qty, 2) if entry_fill else None
                            pnl_pct = round((exit_fill - entry_fill) / entry_fill * 100, 2) if entry_fill else None
                            capital = round(entry_fill * qty, 2) if entry_fill else None
                            ftime   = r.get("filltime", "")[:5]
                            btime   = buy.get("filltime", "")[:5] if buy else ""
                            records.append({
                                "date"      : today_str,
                                "time"      : btime,
                                "exit_time" : ftime,
                                "symbol"    : sym,
                                "side"      : "CE" if sym.endswith("CE") else "PE",
                                "strike"    : int(r.get("strikeprice") or 0),
                                "entry"     : entry_fill,
                                "exit"      : exit_fill,
                                "qty"       : qty,
                                "lots"      : qty // 65,
                                "capital"   : capital,
                                "pnl"       : pnl,
                                "pnl_pct"   : pnl_pct,
                                "reason"    : "ANGEL_FILL",
                                "paper"     : False,
                            })
        except Exception:
            pass

    # ── Historical: load from persisted trade log (past days only) ──
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE) as f:
                all_trades = json.load(f)
            for tr in all_trades:
                # reason check covers old records logged before is_test existed
                if tr.get("is_test") or tr.get("reason") == "TEST_EXIT":
                    continue
                d = tr.get("date", "")
                if d == today_str:
                    continue   # today is covered by Angel One above
                if (not from_date or d >= from_date) and (not to_date or d <= to_date):
                    records.append(tr)
        except Exception:
            pass

    # Trades with P&L between ₹10 and ₹50 (either direction) are treated as
    # test trades and dropped from history/stats entirely.
    records = [r for r in records if r.get("pnl") is None or not (10 < abs(r["pnl"]) < 50)]

    records.sort(key=lambda r: (r.get("date",""), r.get("time","")))

    completed     = [r for r in records if r.get("pnl") is not None]
    total_pnl     = round(sum(r["pnl"] for r in completed), 2)
    wins          = sum(1 for r in completed if r["pnl"] > 0)
    win_rate      = round(wins / len(completed) * 100) if completed else 0
    total_capital = round(sum(r.get("capital") or 0 for r in completed), 2)

    return jsonify({
        "trades"  : records,
        "summary" : {
            "total_trades" : len(completed),
            "total_pnl"    : total_pnl,
            "wins"         : wins,
            "losses"       : len(completed) - wins,
            "win_rate"     : win_rate,
            "total_capital": total_capital,
        }
    })

# ── Template ──────────────────────────────────────────────────────

TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Artha Trading Bot</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body{background:linear-gradient(180deg,#f8f7fd 0%,#f4f2fb 100%);color:#1e1b2e;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif}
  .card{background:#fff;border:1px solid #ece9f7;border-radius:18px;
    box-shadow:0 1px 2px rgba(76,29,149,.04),0 4px 16px rgba(76,29,149,.05)}
  .hero-card{background:linear-gradient(135deg,#faf9ff 0%,#f3effe 100%);
    border:1px solid #e9e4fb;border-radius:20px;
    box-shadow:0 1px 2px rgba(76,29,149,.05),0 8px 24px rgba(124,58,237,.07)}
  .tab-a{border-bottom:2px solid #7c3aed;color:#1e1b2e}
  .tab-i{border-bottom:2px solid transparent;color:#a39fc0}
  .tab-i:hover{color:#5b5578}
  .pulse{animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
  .idx-flash{animation:idxFlash 0.6s ease-out}
  @keyframes idxFlash{0%{color:#7c3aed}100%{color:inherit}}
  .badge-live{background:#dcfce7;color:#16a34a;border:1px solid #86efac}
  .badge-stop{background:#f1effa;color:#6b6588;border:1px solid #ded8f2}
  .badge-mon {background:#fef9c3;color:#ca8a04;border:1px solid #fde047}
  .badge-paper{background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd}
  .pill{background:#f5f3fc;border:1px solid #ece7f9;border-radius:14px}
  .icon-chip{width:28px;height:28px;border-radius:9999px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .orb{width:56px;height:56px;border-radius:9999px;flex-shrink:0;
    background:conic-gradient(from 180deg,#c4b5fd,#7c3aed,#a78bfa,#c4b5fd);
    display:flex;align-items:center;justify-content:center;padding:3px}
  .orb-inner{width:100%;height:100%;border-radius:9999px;background:#fff;
    display:flex;align-items:center;justify-content:center;font-size:22px}
  .btn-primary{background:#7c3aed}
  .btn-primary:hover{background:#6d28d9}
  input[type="date"]{color-scheme:light}
  /* Modal */
  #modal-bg{display:none;position:fixed;inset:0;background:rgba(30,27,46,.5);z-index:50;align-items:center;justify-content:center}
  #modal-bg.open{display:flex}
  #day-modal-bg{display:none;position:fixed;inset:0;background:rgba(30,27,46,.5);z-index:50;align-items:center;justify-content:center}
  #day-modal-bg.open{display:flex}
  .cal-cell{cursor:default}
  .cal-cell.has-trades{cursor:pointer}
  .cal-cell.has-trades:hover{box-shadow:0 2px 8px rgba(0,0,0,.08)}
</style>
</head>
<body class="min-h-screen">

<!-- ── Header ── -->
<div class="bg-white/80 backdrop-blur border-b border-violet-100 px-6 py-3 sticky top-0 z-10">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <div class="flex items-center gap-3">
      <p class="text-sm font-bold text-gray-900">NIFTY 50</p>
      <p id="hdr-nifty" class="text-lg font-bold text-gray-900">—</p>
      <span class="w-px h-5 bg-gray-200"></span>
      <p class="text-sm font-bold text-gray-900">VIX</p>
      <p id="hdr-vix" class="text-lg font-bold text-gray-900">—</p>
      <span class="w-px h-5 bg-gray-200"></span>
      <p class="text-sm font-bold text-gray-900">PCR</p>
      <p id="hdr-pcr" class="text-lg font-bold text-gray-900">—</p>
    </div>
    <div class="flex items-center gap-5">
      <!-- Balance -->
      <div class="text-right hidden md:block">
        <p class="text-xs text-gray-400">Available Cash</p>
        <p id="hdr-cash" class="text-sm font-bold text-gray-700">—</p>
      </div>
      <!-- Connection -->
      <span id="conn-badge" class="text-xs px-2.5 py-1 rounded-full bg-gray-100 text-gray-400">⬤ Connecting</span>
      <!-- Clock -->
      <div id="clock" class="text-xs text-gray-400 text-right min-w-[90px]"></div>
    </div>
  </div>
</div>

<!-- ── Tabs ── -->
<div class="flex px-6 pt-3 border-b border-violet-100 bg-white/60">
  <button onclick="switchTab('live')"    id="tab-live"    class="tab-a  px-5 py-2 text-sm font-semibold">Live Trading</button>
  <button onclick="switchTab('history')" id="tab-history" class="tab-i  px-5 py-2 text-sm font-semibold">Trade History</button>
  <button onclick="switchTab('index')"   id="tab-index"   class="tab-i  px-5 py-2 text-sm font-semibold">Index</button>
</div>

<!-- ══════════════ LIVE TAB ══════════════ -->
<div id="pane-live" class="p-5 space-y-4">

  <!-- ── Top row: live trade panel (left, empty until a trade is on) + Bot card (right, square) ── -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">

    <!-- ═══ LEFT: live trade details ═══ -->
    <div class="lg:col-span-2">

      <!-- ── Empty state (shown until the bot takes a trade) ── -->
      <div id="pos-empty" class="card p-5 h-full min-h-[260px] flex items-center justify-center text-center">
        <div>
          <p class="text-sm font-semibold text-gray-400">No active trade</p>
          <p class="text-xs text-gray-400 mt-1">Trade details will appear here once the bot enters a position.</p>
        </div>
      </div>

      <!-- ── Active Position ── -->
      <div id="pos-card" class="card p-5 hidden h-full">
        <div class="flex items-center justify-between mb-4">
          <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Open Position</p>
          <div class="flex items-center gap-2">
            <span id="pos-badge" class="text-xs font-bold px-2.5 py-0.5 rounded-full bg-blue-100 text-blue-700">CE</span>
            <button id="btn-add-lot" onclick="addLot()"
              class="text-xs font-bold px-3 py-1 rounded-full bg-green-100 text-green-700 hover:bg-green-200 transition">
              + 1 Lot
            </button>
            <button id="btn-sell-lot" onclick="sellLot()"
              class="text-xs font-bold px-3 py-1 rounded-full bg-amber-100 text-amber-700 hover:bg-amber-200 transition">
              − 1 Lot
            </button>
            <button onclick="manualExitPosition()"
              class="text-xs font-bold px-3 py-1 rounded-full bg-red-100 text-red-700 hover:bg-red-200 transition">
              ✕ Exit Trade
            </button>
          </div>
        </div>
        <div class="flex items-center justify-between flex-wrap gap-3 pb-4 border-b border-gray-100">
          <div>
            <p id="pos-sym" class="text-base font-bold text-gray-900"></p>
            <p class="text-xs text-gray-400">Qty <span id="pos-qty" class="font-semibold text-gray-600"></span>
              &middot; Entry <span id="pos-time" class="font-semibold text-gray-600"></span></p>
          </div>
          <div class="text-right">
            <p id="pos-pnl" class="text-2xl font-extrabold"></p>
            <p id="pos-pnl-pct" class="text-xs font-semibold"></p>
          </div>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm pt-3">
          <div><p class="text-xs text-gray-400 mb-0.5">LTP</p><p id="pos-ltp" class="font-bold text-gray-900"></p></div>
          <div><p class="text-xs text-gray-400 mb-0.5">Stop Loss</p><p id="pos-sl" class="font-bold text-red-600"></p></div>
          <div><p class="text-xs text-gray-400 mb-0.5">Target</p><p id="pos-target" class="font-bold text-green-600"></p></div>
          <div><p class="text-xs text-gray-400 mb-0.5">Invested</p><p id="pos-invested" class="font-bold text-gray-700"></p></div>
        </div>
        <div id="pos-tags" class="flex gap-2 mt-3"></div>
      </div>

    </div>

    <!-- ═══ RIGHT: Bot Card (square) ═══ -->
    <div class="hero-card p-5 flex flex-col">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <h1 class="text-xl font-extrabold text-gray-900 tracking-tight">ARTHA BOT</h1>
        <span id="status-badge" class="badge-stop text-xs font-bold px-3 py-1 rounded-full">STOPPED</span>
      </div>
      <p id="hero-subtitle" class="text-xs text-gray-500 mt-1">Supertrend Strategy &middot; Nifty 50 Options</p>

      <div class="flex gap-2 mt-4 flex-wrap">
        <button id="btn-start" onclick="openStartModal()"
          class="btn-primary text-white text-sm font-bold px-5 py-2.5 rounded-xl transition">
          ▶ Start Bot
        </button>
        <button id="btn-exit" onclick="manualExitPosition()" style="display:none"
          class="bg-white border border-red-200 text-red-600 hover:bg-red-50 text-sm font-bold px-5 py-2.5 rounded-xl transition">
          ✕ Exit Trade
        </button>
        <button id="btn-stop" onclick="stopTrading()" style="display:none"
          class="bg-red-600 hover:bg-red-700 text-white text-sm font-bold px-5 py-2.5 rounded-xl transition">
          ■ Stop Bot
        </button>
        <button id="btn-force" onclick="forceExit()" style="display:none"
          class="bg-orange-500 hover:bg-orange-600 text-white text-sm font-bold px-5 py-2.5 rounded-xl transition">
          ⚠ Force Exit
        </button>
        <button id="btn-test" onclick="testTrade()" style="display:none"
          class="bg-white border border-violet-200 text-violet-700 hover:bg-violet-50 text-sm font-bold px-5 py-2.5 rounded-xl transition">
          ⚡ Test Trade
        </button>
      </div>
      <div class="flex items-center gap-2 mt-2">
        <span id="paper-badge" class="hidden text-xs font-bold px-3 py-1 rounded-full bg-blue-100 text-blue-700">PAPER</span>
        <span id="strategy-badge" class="text-xs font-bold px-3 py-1 rounded-full bg-violet-100 text-violet-700">V2</span>
      </div>

      <div class="mt-4">
        <p class="text-xs text-gray-400">Today's P&amp;L</p>
        <p id="live-pnl" class="text-3xl font-extrabold text-gray-300">—</p>
        <p class="text-xs text-gray-400 mt-0.5">Real-time performance</p>
      </div>

      <div class="flex flex-wrap gap-2 mt-4">
        <div class="pill px-3 py-2 flex items-center gap-2">
          <span class="icon-chip bg-blue-100 text-blue-600"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18"/><path d="M6 20v-6"/><path d="M12 20V9"/><path d="M18 20V4"/></svg></span>
          <div><p id="live-lots" class="text-sm font-bold text-gray-900 leading-none">—</p>
          <p class="text-[10px] text-gray-400 mt-0.5">Position Size</p></div>
        </div>
        <div class="pill px-3 py-2 flex items-center gap-2">
          <span class="icon-chip bg-violet-100 text-violet-600"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2.1l4 4-4 4"/><path d="M3 12.2v-2a4 4 0 0 1 4-4h14"/><path d="M7 21.9l-4-4 4-4"/><path d="M21 11.8v2a4 4 0 0 1-4 4H3"/></svg></span>
          <div><p id="live-trades-ct" class="text-sm font-bold text-gray-900 leading-none">—</p>
          <p class="text-[10px] text-gray-400 mt-0.5">Trades Today</p></div>
        </div>
        <div class="pill px-3 py-2 flex items-center gap-2">
          <span class="icon-chip bg-green-100 text-green-600"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1" fill="currentColor"/></svg></span>
          <div><p id="hero-wr14" class="text-sm font-bold text-gray-900 leading-none">—</p>
          <p class="text-[10px] text-gray-400 mt-0.5">Month Win Rate</p></div>
        </div>
      </div>

      <div class="flex items-center gap-2 mt-4 pt-4 border-t border-violet-100">
        <span id="hero-status-dot" class="w-2 h-2 rounded-full bg-gray-300 shrink-0"></span>
        <p id="hero-status-line" class="text-xs text-gray-500">Bot is stopped</p>
      </div>
    </div>

  </div>

  <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
    <!-- ═══ LEFT column ═══ -->
    <div class="lg:col-span-2 space-y-4">

      <!-- ── This Month's Performance ── -->
      <div class="card p-5">
        <div class="flex items-center justify-between mb-4">
          <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">This Month's Performance</p>
          <span id="perf14-total" class="text-xs text-gray-400 pill px-2 py-0.5">— trades</span>
        </div>
        <div class="flex flex-wrap gap-8 mb-2">
          <div>
            <p id="perf14-pnl" class="text-2xl font-extrabold text-gray-300">—</p>
            <p class="text-xs text-gray-400">Total P&amp;L</p>
          </div>
          <div>
            <p id="perf14-wins-n" class="text-2xl font-extrabold text-green-600">—</p>
            <p class="text-xs text-gray-400">Wins</p>
          </div>
          <div>
            <p id="perf14-losses-n" class="text-2xl font-extrabold text-red-500">—</p>
            <p class="text-xs text-gray-400">Losses</p>
          </div>
          <div>
            <p id="perf14-winrate" class="text-2xl font-extrabold text-gray-900">—</p>
            <p class="text-xs text-gray-400">Win Rate</p>
          </div>
        </div>
        <div id="perf14-chart" class="mt-3"></div>
      </div>

      <!-- ── Trade Log ── -->
      <div class="card overflow-hidden">
        <div class="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
          <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Today's Trades</p>
          <span id="live-trade-badge" class="text-xs text-gray-400"></span>
        </div>
        <div id="live-trades-tbl"></div>
      </div>

    </div>

    <!-- ═══ RIGHT column ═══ -->
    <div class="space-y-4">

      <!-- ── Bot Status ── -->
      <div class="card p-5">
        <div class="flex items-start justify-between gap-3">
          <div class="flex-1 min-w-0">
            <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold mb-3">Bot Status</p>
            <div class="flex items-center gap-2 mb-3">
              <span id="bs-dot" class="w-2 h-2 rounded-full bg-gray-300"></span>
              <span id="bs-status" class="text-sm font-bold text-gray-800">Stopped</span>
            </div>
          </div>
          <div class="orb"><div class="orb-inner">🤖</div></div>
        </div>
        <div class="space-y-2 text-xs">
          <div class="flex justify-between gap-2"><span class="text-gray-400 shrink-0">Last Signal</span>
            <span id="bs-last-signal" class="font-semibold text-gray-700 text-right">—</span></div>
          <div class="flex justify-between gap-2"><span class="text-gray-400 shrink-0" id="bs-indicator-label">Supertrend</span>
            <span id="bs-indicator" class="font-semibold text-gray-700 text-right">—</span></div>
          <div class="flex justify-between gap-2"><span class="text-gray-400 shrink-0">Next Check</span>
            <span id="bs-countdown" class="font-mono font-semibold text-violet-700">—</span></div>
        </div>
        <div id="sig-filter" class="hidden mt-3 bg-amber-50 border border-amber-200 text-amber-800 text-xs rounded-lg px-3 py-2"></div>
        <div id="sig-error"  class="hidden mt-3 bg-red-50  border border-red-200  text-red-700  text-xs rounded-lg px-3 py-2"></div>
        <button onclick="document.getElementById('activity-card').scrollIntoView({behavior:'smooth'})"
          class="text-xs text-violet-600 font-semibold mt-3 hover:underline">👁 View Logs</button>
      </div>

      <!-- ── Mini stats ── -->
      <div class="grid grid-cols-2 gap-3">
        <div class="card p-3">
          <span class="icon-chip bg-blue-100 text-blue-600 mb-2"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/></svg></span>
          <p class="text-xs text-gray-400">Cash Available</p>
          <p id="live-cash" class="text-sm font-bold text-gray-800">—</p>
        </div>
        <div class="card p-3">
          <span class="icon-chip bg-violet-100 text-violet-600 mb-2"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2.1l4 4-4 4"/><path d="M3 12.2v-2a4 4 0 0 1 4-4h14"/><path d="M7 21.9l-4-4 4-4"/><path d="M21 11.8v2a4 4 0 0 1-4 4H3"/></svg></span>
          <p class="text-xs text-gray-400">Max Trades</p>
          <p id="mini-maxtrades" class="text-sm font-bold text-gray-800">—</p>
        </div>
        <div class="card p-3">
          <span class="icon-chip bg-amber-100 text-amber-600 mb-2"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 16l4-6 4 3 5-7"/></svg></span>
          <p class="text-xs text-gray-400">Daily P&amp;L</p>
          <p id="mini-dailypnl" class="text-sm font-bold text-gray-800">—</p>
        </div>
        <div class="card p-3">
          <span class="icon-chip bg-green-100 text-green-600 mb-2"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 21h8"/><path d="M12 17v4"/><path d="M7 4h10v5a5 5 0 0 1-10 0V4Z"/><path d="M17 5h2a2 2 0 0 1 0 4h-1"/><path d="M7 5H5a2 2 0 0 0 0 4h1"/></svg></span>
          <p class="text-xs text-gray-400">Win Rate (Month)</p>
          <p id="mini-wr14" class="text-sm font-bold text-gray-800">—</p>
        </div>
      </div>

      <!-- ── Recent Activity ── -->
      <div id="activity-card" class="card p-5">
        <div class="flex items-center justify-between mb-3">
          <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Recent Activity</p>
          <button id="activity-toggle" onclick="toggleActivity()" class="text-xs text-violet-600 font-semibold hover:underline">View All</button>
        </div>
        <div id="activity-list" class="space-y-3"></div>
      </div>

    </div>
  </div>

</div><!-- /pane-live -->


<!-- ══════════════ TRADE HISTORY TAB ══════════════ -->
<div id="pane-history" class="p-5 hidden space-y-4">

  <!-- Date range -->
  <div class="card p-4">
    <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold mb-3">Trade History</p>
    <div class="flex flex-wrap items-end gap-4">
      <div>
        <label class="text-xs text-gray-400 block mb-1">From</label>
        <input id="hist-from" type="date" class="bg-white border border-gray-300 rounded px-3 py-2 text-sm"/>
      </div>
      <div>
        <label class="text-xs text-gray-400 block mb-1">To</label>
        <input id="hist-to" type="date" class="bg-white border border-gray-300 rounded px-3 py-2 text-sm"/>
      </div>
      <button onclick="loadHistoryRange()"
        class="bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-bold px-5 py-2 rounded transition">
        Load History
      </button>
    </div>
  </div>

  <!-- Summary cards (for the date range above) -->
  <div id="hist-summary" class="hidden grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">Total Trades</p>
      <p id="hs-trades" class="text-2xl font-bold text-gray-900">—</p>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">Total P&amp;L</p>
      <p id="hs-pnl" class="text-2xl font-bold">—</p>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">Win Rate</p>
      <p id="hs-wr" class="text-2xl font-bold text-gray-900">—</p>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">Wins / Losses</p>
      <p id="hs-wl" class="text-2xl font-bold text-gray-900">—</p>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">CE Win %</p>
      <p id="hs-ce-wr" class="text-2xl font-bold text-blue-700">—</p>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">PE Win %</p>
      <p id="hs-pe-wr" class="text-2xl font-bold text-amber-700">—</p>
    </div>
  </div>

  <!-- Month navigation -->
  <div class="card p-4 flex items-center justify-between">
    <button onclick="calShiftMonth(-1)"
      class="w-9 h-9 rounded-full bg-gray-100 hover:bg-gray-200 text-gray-600 font-bold transition">‹</button>
    <p id="cal-month-label" class="text-sm font-bold text-gray-900 uppercase tracking-widest">—</p>
    <button onclick="calShiftMonth(1)"
      class="w-9 h-9 rounded-full bg-gray-100 hover:bg-gray-200 text-gray-600 font-bold transition">›</button>
  </div>

  <!-- Calendar -->
  <div class="card p-4">
    <div class="grid grid-cols-7 gap-2 text-center text-xs font-bold text-gray-400 mb-2">
      <div>MON</div><div>TUE</div><div>WED</div><div>THU</div><div>FRI</div><div>SAT</div><div>SUN</div>
    </div>
    <div id="cal-grid" class="grid grid-cols-7 gap-2"></div>
  </div>

  <!-- Legend -->
  <div class="flex items-center gap-5 text-xs text-gray-400 px-1">
    <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-green-500 inline-block"></span>Profit Day</span>
    <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-red-500 inline-block"></span>Loss Day</span>
    <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-gray-300 inline-block"></span>No Trades</span>
    <span class="ml-auto">All amounts in INR</span>
  </div>

</div><!-- /pane-history -->

<!-- ══════════════ INDEX TAB ══════════════ -->
<div id="pane-index" class="p-5 hidden space-y-4">
  <div class="flex items-center justify-between px-1">
    <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Market Indices</p>
    <p id="idx-updated" class="text-xs text-gray-400">NSE &amp; BSE &middot; Last updated —</p>
  </div>
  <div id="idx-grid" class="grid grid-cols-1 lg:grid-cols-3 gap-4">
    <!-- filled by renderIndices() -->
  </div>
</div><!-- /pane-index -->


<!-- ══════════════ START TRADING MODAL ══════════════ -->
<div id="modal-bg" class="modal-bg">
  <div class="bg-white rounded-2xl shadow-2xl w-full max-w-md mx-4 p-6">
    <h2 class="text-lg font-bold text-gray-900 mb-1">Configure Trading Session</h2>
    <p class="text-xs text-gray-400 mb-5">Set limits before enabling live order placement on Angel One.</p>

    <div class="space-y-4 mb-5">
      <div>
        <label class="text-sm font-semibold text-gray-700 block mb-1">Max Trades per Day</label>
        <div class="flex items-center gap-3">
          <input id="m-max-trades" type="number" min="1" max="6" value="2"
            class="w-20 border border-gray-300 rounded px-3 py-2 text-sm text-gray-800"/>
          <span class="text-xs text-gray-400">Bot won't exceed this even if signals fire more</span>
        </div>
      </div>
      <div>
        <label class="text-sm font-semibold text-gray-700 block mb-1">Number of Lots</label>
        <div class="flex items-center gap-3">
          <input id="m-lots" type="number" min="1" max="20" value="1"
            class="w-20 border border-gray-300 rounded px-3 py-2 text-sm text-gray-800"/>
          <span id="m-units" class="text-xs text-gray-400">= 65 units per trade</span>
        </div>
      </div>
      <div>
        <label class="text-sm font-semibold text-gray-700 block mb-2">Strategy</label>
        <div class="flex gap-3">
          <label class="flex items-center gap-2 cursor-pointer">
            <input type="radio" name="m-strategy" id="m-strategy-v2" value="v2" checked
              class="accent-blue-600"/>
            <span class="text-sm text-gray-700">
              <span class="font-semibold text-blue-700">V2</span>
              <span class="text-xs text-gray-400 ml-1">— multi-filter (VWAP/EMA/RSI/ADX/VIX)</span>
            </span>
          </label>
          <label class="flex items-center gap-2 cursor-pointer">
            <input type="radio" name="m-strategy" id="m-strategy-supertrend" value="supertrend"
              class="accent-purple-600"/>
            <span class="text-sm text-gray-700">
              <span class="font-semibold text-purple-700">Supertrend</span>
              <span class="text-xs text-gray-400 ml-1">— (10,3) follower + 50pt SL</span>
            </span>
          </label>
        </div>
      </div>
      <div>
        <label class="text-sm font-semibold text-gray-700 block mb-1">Manual Target %</label>
        <div class="flex items-center gap-3">
          <select id="m-manual-target" class="border border-gray-300 rounded px-3 py-2 text-sm text-gray-800">
            <option value="0">Off — hardcoded exit logic only</option>
            <option value="5">5%</option>
            <option value="10">10%</option>
            <option value="15">15%</option>
            <option value="20">20%</option>
          </select>
          <span class="text-xs text-gray-400">Extra take-profit, alongside SL/trail/flip — whichever hits first wins</span>
        </div>
      </div>
      <div>
        <label class="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" id="m-carry-overnight" class="accent-purple-600"/>
          <span class="text-sm font-semibold text-gray-700">Carry position overnight</span>
        </label>
        <p class="text-xs text-gray-400 mt-1 ml-6">Skip end-of-day square-off and hold into the next trading day
          (still force-closes on the contract's own expiry date). Off by default.</p>
      </div>
      <div>
        <label class="text-sm font-semibold text-gray-700 block mb-2">Mode</label>
        <div class="flex gap-3">
          <label class="flex items-center gap-2 cursor-pointer">
            <input type="radio" name="m-mode" id="m-mode-paper" value="paper" checked
              class="accent-blue-600"/>
            <span class="text-sm text-gray-700">
              <span class="font-semibold text-blue-700">Paper Trading</span>
              <span class="text-xs text-gray-400 ml-1">— simulate only, no real orders</span>
            </span>
          </label>
          <label class="flex items-center gap-2 cursor-pointer">
            <input type="radio" name="m-mode" id="m-mode-live" value="live"
              class="accent-green-600"/>
            <span class="text-sm text-gray-700">
              <span class="font-semibold text-green-700">Live Trading</span>
              <span class="text-xs text-gray-400 ml-1">— real Angel One orders</span>
            </span>
          </label>
        </div>
      </div>
    </div>

    <div id="m-warning-paper" class="bg-blue-50 border border-blue-200 rounded-lg px-4 py-3 mb-5 text-xs text-blue-800">
      📋 <strong>Paper mode:</strong> Signals fire and P&amp;L is tracked using real market prices,
      but <strong>no orders are placed</strong> on Angel One. Safe to run anytime.
    </div>
    <div id="m-warning-live" class="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 mb-5 text-xs text-amber-800 hidden">
      ⚠ <strong>Live mode:</strong> Real orders will be placed on your Angel One account.
      The bot trades only when a signal fires — it does not guarantee
      <span id="m-max-display">2</span> trades per day.
    </div>

    <div class="flex gap-3 justify-end">
      <button onclick="closeModal()"
        class="px-5 py-2 rounded text-sm font-semibold border border-gray-300 text-gray-700 hover:bg-gray-50 transition">
        Cancel
      </button>
      <button onclick="confirmStart()"
        class="px-5 py-2 rounded text-sm font-bold bg-green-600 text-white hover:bg-green-700 transition">
        Start Trading
      </button>
    </div>
  </div>
</div>

<div id="day-modal-bg" class="modal-bg">
  <div class="bg-white rounded-2xl shadow-2xl w-full max-w-5xl mx-4 p-6 max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between mb-4">
      <div>
        <h2 id="day-modal-title" class="text-lg font-bold text-gray-900">—</h2>
        <p id="day-modal-subtitle" class="text-xs text-gray-400 mt-0.5">—</p>
      </div>
      <button onclick="closeDayModal()"
        class="w-8 h-8 rounded-full bg-gray-100 hover:bg-gray-200 text-gray-500 font-bold transition">✕</button>
    </div>
    <div id="day-modal-table" class="card overflow-hidden"></div>
  </div>
</div>

<script>
// ── Clock ──────────────────────────────────────────────────────
function tick(){
  const n=new Date();
  document.getElementById('clock').textContent=
    n.toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'})+
    '  '+n.toLocaleTimeString('en-IN');
}
setInterval(tick,1000); tick();

// ── Tabs ───────────────────────────────────────────────────────
function switchTab(name){
  ['live','history','index'].forEach(t=>{
    document.getElementById('pane-'+t).classList.toggle('hidden',t!==name);
    document.getElementById('tab-'+t).className=
      (t===name?'tab-a':'tab-i')+' px-5 py-2 text-sm font-semibold';
  });
  if(name==='history') renderCalendar();
  if(name==='index')   loadIndices();
}

// ── Index tab ──────────────────────────────────────────────────
// Deliberately NOT real-time — refreshes once/day server-side, after market
// close (15:30), so it never competes with the bot's own signal-loop candle
// fetches during trading hours. This polls a cheap in-memory cache, not the
// broker, so a slow client-side interval is just to catch that daily update
// if the tab's left open across the close.
let idxTimer = null;
let idxPrevLtp = {};   // "IndexName" or "IndexName|SYMBOL" -> last ltp, so we can flash on change
async function loadIndices(){
  try{
    const r = await fetch('/api/sector-indices');
    const d = await r.json();
    renderIndices(d.indices || []);
    const isToday = (d.indices || []).some(i=>i.live);
    document.getElementById('idx-updated').innerHTML =
      (isToday ? '<span class="w-1.5 h-1.5 rounded-full bg-green-500 inline-block mr-1"></span>Today\'s close &middot; '
                : '<span class="w-1.5 h-1.5 rounded-full bg-gray-300 inline-block mr-1"></span>Showing a previous day\'s close &middot; ') +
      'NSE & BSE · Refreshes daily after 3:30pm · Last checked ' + (d.time || '—');
  }catch(e){ /* keep last render on transient failure */ }
  if(!idxTimer) idxTimer = setInterval(loadIndices, 300000);
}
function pctCell(v){
  if(v==null) return '<span class="text-gray-300">—</span>';
  return `<span class="${cls(v)}">${v>=0?'+':''}${v.toFixed(2)}%</span>`;
}
function renderIndices(indices){
  const grid = document.getElementById('idx-grid');
  if(!indices.length){
    grid.innerHTML = '<p class="text-sm text-gray-400 col-span-full text-center py-8">Index data unavailable — is the broker session connected?</p>';
    return;
  }
  grid.innerHTML = indices.map(idx=>{
    const up      = (idx.change ?? 0) >= 0;
    const arrow    = up ? '▲' : '▼';
    const ltp      = idx.ltp!=null ? idx.ltp.toLocaleString('en-IN',{maximumFractionDigits:2}) : '—';
    const chg      = idx.change!=null ? `${up?'+':''}${idx.change.toLocaleString('en-IN',{maximumFractionDigits:2})} pts` : '—';
    const pct      = idx.pct_change!=null ? `(${up?'+':''}${idx.pct_change.toFixed(2)}%)` : '';
    const flashKey = idx.name;
    const flash    = (idxPrevLtp[flashKey]!=null && idxPrevLtp[flashKey]!==idx.ltp) ? 'idx-flash' : '';
    idxPrevLtp[flashKey] = idx.ltp;

    // Best/worst 1-year performer in this sector, so they can be highlighted —
    // only among stocks that actually have a 1Y figure (chg_1y != null).
    const stocks = idx.stocks || [];
    const scored1y = stocks.map((s,i)=>({i, v:s.chg_1y})).filter(x=>x.v!=null);
    let bestI = null, worstI = null;
    if(scored1y.length){
      bestI  = scored1y.reduce((a,b)=>b.v>a.v?b:a).i;
      worstI = scored1y.reduce((a,b)=>b.v<a.v?b:a).i;
      if(bestI===worstI){ bestI=null; worstI=null; }   // only one scoreable stock — nothing to contrast
    }

    const rows = stocks.map((s,i)=>{
      const sKey  = idx.name+'|'+s.symbol;
      const sFlash = (idxPrevLtp[sKey]!=null && idxPrevLtp[sKey]!==s.ltp) ? 'idx-flash' : '';
      idxPrevLtp[sKey] = s.ltp;
      const sLtp = s.ltp!=null ? s.ltp.toLocaleString('en-IN',{maximumFractionDigits:2}) : '—';
      const rowTone = i===bestI ? 'bg-green-50' : i===worstI ? 'bg-red-50' : '';
      const badge   = i===bestI ? ' 🏆' : i===worstI ? ' 📉' : '';
      return `<tr class="border-t border-gray-100 ${rowTone}">
        <td class="py-1.5 pr-2 font-semibold text-gray-700">${s.symbol}${badge}</td>
        <td class="py-1.5 pr-2 text-right font-semibold ${sFlash}">${sLtp}</td>
        <td class="py-1.5 pr-2 text-right">${pctCell(s.chg_7d)}</td>
        <td class="py-1.5 pr-2 text-right">${pctCell(s.chg_90d)}</td>
        <td class="py-1.5 text-right">${pctCell(s.chg_1y)}</td>
      </tr>`;
    }).join('');

    return `<div class="card p-4" style="border-left:3px solid ${up?'#22c55e':'#ef4444'}">
      <div class="flex items-center justify-between">
        <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">${idx.name}</p>
        ${idx.live ? '<span class="w-1.5 h-1.5 rounded-full bg-green-500" title="Today\'s close"></span>' : ''}
      </div>
      <p class="text-2xl font-extrabold text-gray-900 mt-1 ${flash}">${ltp}</p>
      <p class="text-xs font-semibold ${cls(idx.change ?? 0)} mt-1">${arrow} ${chg} ${pct}</p>
      <p class="text-xs text-gray-400 mt-1 mb-3">${idx.exchange} &middot; ${idx.subtitle}</p>
      <table class="w-full text-xs">
        <thead>
          <tr class="text-gray-400">
            <th class="text-left font-semibold pb-1">Stock</th>
            <th class="text-right font-semibold pb-1">LTP</th>
            <th class="text-right font-semibold pb-1">7D</th>
            <th class="text-right font-semibold pb-1">90D</th>
            <th class="text-right font-semibold pb-1">1Y</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }).join('');
}

// ── Trade History (calendar view) ────────────────────────────────
let calDate = new Date(); calDate.setDate(1);
let calTradesByDate = {};

// Local-date formatting (not toISOString, which shifts by the UTC offset —
// in IST, local midnight is 18:30 UTC the previous day, so toISOString()
// would file a day's trades one cell earlier than the day they occurred).
function calFmt(d){ return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }

function calShiftMonth(delta){
  calDate.setMonth(calDate.getMonth()+delta);
  renderCalendar();
}

function renderCalendar(){
  const year = calDate.getFullYear(), month = calDate.getMonth();
  const monthNames = ['January','February','March','April','May','June','July',
                       'August','September','October','November','December'];
  document.getElementById('cal-month-label').textContent = monthNames[month]+' '+year;

  const firstOfMonth = new Date(year, month, 1);
  const lastOfMonth  = new Date(year, month+1, 0);
  const startOffset  = (firstOfMonth.getDay()+6)%7;           // Mon=0 .. Sun=6
  const endOffset    = (lastOfMonth.getDay()+6)%7;
  const gridStart = new Date(firstOfMonth); gridStart.setDate(gridStart.getDate()-startOffset);
  const gridEnd   = new Date(lastOfMonth);  gridEnd.setDate(gridEnd.getDate()+(6-endOffset));

  const from = calFmt(gridStart), to = calFmt(gridEnd);
  const grid = document.getElementById('cal-grid');
  grid.innerHTML = '<p class="col-span-7 text-sm text-gray-400 text-center py-8">Loading…</p>';

  // Keep the date-range boxes (and the stats they drive) in sync with the
  // month currently shown, unless the user is mid-edit on a custom range.
  const monthFirst = calFmt(firstOfMonth), monthLast = calFmt(lastOfMonth);
  document.getElementById('hist-from').value = monthFirst;
  document.getElementById('hist-to').value   = monthLast;
  loadStatsForRange(monthFirst, monthLast);

  fetch(`/api/trade-history?from=${from}&to=${to}`)
    .then(r=>r.json())
    .then(d=>{
      const trades = d.trades||[];
      calTradesByDate = {};
      trades.forEach(t=>{ (calTradesByDate[t.date] = calTradesByDate[t.date]||[]).push(t); });

      // Day cells
      let html = '';
      let cur = new Date(gridStart);
      while(cur <= gridEnd){
        const ds = calFmt(cur);
        const inMonth   = cur.getMonth()===month;
        const dayTrades = (calTradesByDate[ds]||[]).filter(t=>t.pnl!=null);
        // Overflow cells (previous/next month filling out the week grid) show
        // their own month's abbreviation so e.g. "31" isn't mistaken for a
        // day in the displayed month.
        const dayLabel = inMonth ? cur.getDate() : `${monthNames[cur.getMonth()].slice(0,3)} ${cur.getDate()}`;
        let inner = `<p class="text-xs font-semibold ${inMonth?'text-gray-700':'text-gray-300'} mb-1.5">${dayLabel}</p>`;
        let cellTone = 'bg-white border-gray-100';

        if(dayTrades.length){
          const dayPnl = dayTrades.reduce((a,t)=>a+t.pnl,0);
          const dayCap = dayTrades.reduce((a,t)=>a+(t.capital||0),0);
          const dayPct = dayCap ? (dayPnl/dayCap*100) : 0;
          cellTone = dayPnl>=0 ? 'bg-green-50 border-green-100' : 'bg-red-50 border-red-100';
          inner += `<div class="space-y-0.5">
              <div class="flex justify-between text-xs"><span class="text-gray-500 font-medium">Total P&amp;L</span>`+
              `<span class="${cls(dayPnl)} font-bold">${dayPnl>=0?'+':''}${inr(dayPnl)}</span></div>
              <div class="flex justify-between text-xs"><span class="text-gray-400">P&amp;L %</span>`+
              `<span class="${cls(dayPnl)} font-semibold">${dayPnl>=0?'+':''}${dayPct.toFixed(1)}%</span></div>
            </div>`;
        }

        const cellClass  = `cal-cell rounded-lg border p-2 min-h-[92px] ${cellTone}` + (dayTrades.length ? ' has-trades' : '');
        const onclickAttr = dayTrades.length ? ` onclick="openDayModal('${ds}')"` : '';
        html += `<div class="${cellClass}"${onclickAttr}>${inner}</div>`;
        cur.setDate(cur.getDate()+1);
      }
      grid.innerHTML = html;
    })
    .catch(()=>{ grid.innerHTML = '<p class="col-span-7 text-sm text-red-500 text-center py-8">Failed to load.</p>'; });
}

function loadHistoryRange(){
  const from = document.getElementById('hist-from').value;
  const to   = document.getElementById('hist-to').value;
  if(!from || !to) return;
  loadStatsForRange(from, to);
}

function loadStatsForRange(from, to){
  fetch(`/api/trade-history?from=${from}&to=${to}`)
    .then(r=>r.json())
    .then(d=>{
      const trades   = (d.trades||[]).filter(t=>t.pnl!=null);
      const totalPnl = trades.reduce((a,t)=>a+t.pnl,0);
      const wins     = trades.filter(t=>t.pnl>0).length;
      const ceTrades = trades.filter(t=>t.side==='CE');
      const peTrades = trades.filter(t=>t.side==='PE');
      const ceWins   = ceTrades.filter(t=>t.pnl>0).length;
      const peWins   = peTrades.filter(t=>t.pnl>0).length;

      document.getElementById('hist-summary').classList.remove('hidden');
      const pnlEl = document.getElementById('hs-pnl');
      pnlEl.textContent = (totalPnl>=0?'+':'')+inr(totalPnl);
      pnlEl.className   = 'text-2xl font-bold '+cls(totalPnl);
      document.getElementById('hs-trades') .textContent = trades.length;
      document.getElementById('hs-wr')     .textContent = trades.length ? Math.round(wins/trades.length*100)+'%' : '—';
      document.getElementById('hs-wl')     .textContent = wins+' / '+(trades.length-wins);
      document.getElementById('hs-ce-wr')  .textContent = ceTrades.length ? Math.round(ceWins/ceTrades.length*100)+'% ('+ceTrades.length+')' : '—';
      document.getElementById('hs-pe-wr')  .textContent = peTrades.length ? Math.round(peWins/peTrades.length*100)+'% ('+peTrades.length+')' : '—';
    })
    .catch(()=>{});
}

function openDayModal(ds){
  const trades = calTradesByDate[ds] || [];
  const d = new Date(ds+'T00:00:00');
  document.getElementById('day-modal-title').textContent =
    d.toLocaleDateString('en-IN',{weekday:'long', day:'numeric', month:'long', year:'numeric'});
  const dayPnl = trades.filter(t=>t.pnl!=null).reduce((a,t)=>a+t.pnl,0);
  document.getElementById('day-modal-subtitle').textContent =
    trades.length+' trade'+(trades.length!==1?'s':'')+' · Total P&L '+(dayPnl>=0?'+':'')+inr(dayPnl);
  document.getElementById('day-modal-table').innerHTML = tradeTable(trades);
  document.getElementById('day-modal-bg').classList.add('open');
}
function closeDayModal(){
  document.getElementById('day-modal-bg').classList.remove('open');
}

// ── Format helpers ─────────────────────────────────────────────
const inr=n=>'₹'+Math.abs(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
const sign=n=>n>=0?'+':'-';
const cls=n=>n>=0?'text-green-600':'text-red-500';

// ── Trade table builder ────────────────────────────────────────
function tradeTable(trades){
  if(!trades||!trades.length)
    return `<div class="px-4 py-10 text-center">
      <div class="mx-auto w-10 h-10 rounded-full bg-violet-100 flex items-center justify-center text-violet-500 mb-3">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="4" rx="1"/><path d="M9 4H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-3"/><path d="M9 12h6"/><path d="M9 16h6"/></svg>
      </div>
      <p class="text-sm text-gray-500 font-semibold">No trades yet today.</p>
      <p class="text-xs text-gray-400 mt-1">Sit back and let Artha do the magic.</p>
    </div>`;
  const LOT=65;
  const rows=[...trades].reverse().map(t=>{
    const boughtUnits = t.qty_bought || t.qty || 0;
    const soldUnits   = t.qty || 0;
    const boughtLots  = Math.round(boughtUnits / LOT);
    const soldLots    = Math.round(soldUnits   / LOT);
    const isPartial   = t.reason === 'PARTIAL_TP';

    const boughtCell =
      `<span class="font-bold text-blue-700">${boughtLots} lot${boughtLots!==1?'s':''}</span>`+
      `<span class="text-gray-400 text-xs ml-1">(${boughtUnits} qty)</span>`;

    const soldCell =
      `<span class="font-bold ${isPartial?'text-amber-600':'text-red-600'}">${soldLots} lot${soldLots!==1?'s':''}</span>`+
      `<span class="text-gray-400 text-xs ml-1">(${soldUnits} qty)</span>`;

    return`
    <tr class="border-b border-gray-100 hover:bg-gray-50 text-xs">
      <td class="px-3 py-2 text-gray-500">${t.time||''}</td>
      <td class="px-3 py-2 text-gray-500">${t.exit_time||''}</td>
      <td class="px-3 py-2 font-semibold text-gray-800">${t.symbol||'—'}</td>
      <td class="px-3 py-2">
        <span class="px-2 py-0.5 rounded ${t.side==='CE'?'bg-blue-100 text-blue-700':'bg-amber-100 text-amber-700'}">${t.side||''}</span>
      </td>
      <td class="px-3 py-2 text-right text-gray-600">${t.entry_spot?'₹'+t.entry_spot.toFixed(0):''}</td>
      <td class="px-3 py-2 text-right text-gray-600">₹${(t.entry||0).toFixed(2)}</td>
      <td class="px-3 py-2 text-right text-gray-600">₹${(t.exit||0).toFixed(2)}</td>
      <td class="px-3 py-2">${boughtCell}</td>
      <td class="px-3 py-2">${soldCell}</td>
      <td class="px-3 py-2 text-right font-bold ${cls(t.pnl)}">${sign(t.pnl)}${inr(t.pnl)}</td>
      <td class="px-3 py-2 text-gray-400">${t.reason||''}${t.paper?'<span class="ml-1 text-xs bg-blue-100 text-blue-600 px-1 rounded">P</span>':''}</td>
    </tr>`}).join('');
  return`<div class="overflow-x-auto"><table class="w-full">
    <thead><tr class="text-gray-400 border-b border-gray-100 text-xs">
      <th class="text-left  px-3 py-2">Entry</th>
      <th class="text-left  px-3 py-2">Exit</th>
      <th class="text-left  px-3 py-2">Symbol</th>
      <th class="text-left  px-3 py-2">Type</th>
      <th class="text-right px-3 py-2">Spot</th>
      <th class="text-right px-3 py-2">Opt In</th>
      <th class="text-right px-3 py-2">Opt Out</th>
      <th class="text-left  px-3 py-2">Bought</th>
      <th class="text-left  px-3 py-2">Sold</th>
      <th class="text-right px-3 py-2">P&amp;L</th>
      <th class="text-left  px-3 py-2">Reason</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
}

// ── Live state polling ─────────────────────────────────────────
let nextCheckStr = null;
let activityItems = [], activityShowAll = false;

function refreshLive(){
  fetch('/api/live-state').then(r=>r.json()).then(s=>{
    const pos   = s.position  || {};
    const stats = s.daily_stats || {};
    const sig   = s.signal    || {};
    const mkt   = s.market    || {};

    // Status badge + hero status line
    const status = s.status || 'STOPPED';
    const badge  = document.getElementById('status-badge');
    badge.textContent = status;
    badge.className   = 'text-xs font-bold px-3 py-1 rounded-full '+
      (status==='LIVE'?'badge-live pulse':status==='PAPER'?'badge-paper pulse':status==='MONITORING'?'badge-mon':'badge-stop');
    const dot  = document.getElementById('hero-status-dot');
    const line = document.getElementById('hero-status-line');
    const bsDot = document.getElementById('bs-dot');
    const bsStatus = document.getElementById('bs-status');
    if(status==='LIVE'||status==='PAPER'){
      dot.className='w-2 h-2 rounded-full bg-green-500 shrink-0 pulse';
      line.textContent = s.last_error ? 'Bot hit an error — check logs' : 'Bot is running normally';
      bsDot.className='w-2 h-2 rounded-full bg-green-500 pulse';
      bsStatus.textContent = 'Running smoothly';
    } else if(status==='MONITORING'){
      dot.className='w-2 h-2 rounded-full bg-amber-500 shrink-0 pulse';
      line.textContent='Monitoring open position — not taking new entries';
      bsDot.className='w-2 h-2 rounded-full bg-amber-500 pulse';
      bsStatus.textContent='Monitoring only';
    } else {
      dot.className='w-2 h-2 rounded-full bg-gray-300 shrink-0';
      line.textContent='Bot is stopped';
      bsDot.className='w-2 h-2 rounded-full bg-gray-300';
      bsStatus.textContent='Stopped';
    }

    // Strategy + paper badges, hero subtitle
    const strategy = (s.config && s.config.strategy) || 'v2';
    const isST     = strategy === 'supertrend';
    const stBadge  = document.getElementById('strategy-badge');
    stBadge.textContent = isST ? 'SUPERTREND' : 'V2';
    stBadge.className   = 'text-xs font-bold px-3 py-1 rounded-full '+
      (isST ? 'bg-violet-100 text-violet-700' : 'bg-blue-100 text-blue-700');
    document.getElementById('hero-subtitle').textContent =
      (isST ? 'Supertrend' : 'V2')+' Strategy · Nifty 50 Options';
    document.getElementById('paper-badge').classList.toggle('hidden', !(status==='PAPER'||s.paper_mode));

    // Buttons
    const isLive  = (status==='LIVE');
    const isPaper = (status==='PAPER');
    const isMon   = (status==='MONITORING');
    const isActive = isLive || isPaper;
    document.getElementById('btn-start').style.display = (!isActive&&!isMon)?'':'none';
    document.getElementById('btn-stop') .style.display = isActive?'':'none';
    document.getElementById('btn-force').style.display = isMon ?'':'none';
    document.getElementById('btn-test') .style.display = (isActive && !pos.active)?'':'none';
    document.getElementById('btn-exit') .style.display = pos.active?'':'none';

    // Connection badge
    const cb = document.getElementById('conn-badge');
    cb.textContent = s.connected ? '⬤ Connected' : '⬤ Offline';
    cb.className   = 'text-xs px-2.5 py-1 rounded-full '+
      (s.connected ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-400');

    // Hero pills + stats
    const pnl = stats.pnl ?? 0;
    const pnlEl = document.getElementById('live-pnl');
    pnlEl.textContent = (pnl>=0?'+':'')+inr(pnl);
    pnlEl.className   = 'text-3xl font-extrabold '+cls(pnl);
    document.getElementById('live-lots').textContent = (s.config?.lots||1)+' Lot'+((s.config?.lots||1)!==1?'s':'');
    document.getElementById('live-trades-ct').textContent =
      (stats.trade_count||0)+' / '+(s.config?.max_trades||2);
    const cash = stats.balance||0;
    document.getElementById('live-cash').textContent = cash ? inr(cash) : '—';
    document.getElementById('mini-maxtrades').textContent =
      (stats.trade_count||0)+' / '+(s.config?.max_trades||2)+' trades';
    const dpnlEl = document.getElementById('mini-dailypnl');
    dpnlEl.textContent = (pnl>=0?'+':'')+inr(pnl);
    dpnlEl.className   = 'text-sm font-bold '+cls(pnl);

    // Header Nifty + Cash
    if(mkt.nifty_ltp) document.getElementById('hdr-nifty').textContent='₹'+mkt.nifty_ltp.toLocaleString('en-IN',{minimumFractionDigits:2});
    if(cash)          document.getElementById('hdr-cash') .textContent=inr(cash);

    // Bot Status: last signal, indicator, filter/error
    const bsLabel = document.getElementById('bs-indicator-label');
    const bsInd   = document.getElementById('bs-indicator');
    if(isST){
      bsLabel.textContent = 'Supertrend';
      if(mkt.st_trend === 1)       { bsInd.textContent = 'Uptrend ▲';   bsInd.className = 'font-semibold text-green-600 text-right'; }
      else if(mkt.st_trend === -1) { bsInd.textContent = 'Downtrend ▼'; bsInd.className = 'font-semibold text-red-600 text-right'; }
      else                         { bsInd.textContent = '—';           bsInd.className = 'font-semibold text-gray-700 text-right'; }
    } else {
      bsLabel.textContent = 'India VIX';
      bsInd.textContent   = mkt.vix ? mkt.vix+'' : '—';
      bsInd.className     = 'font-semibold text-gray-700 text-right';
    }
    const lastSigEl = document.getElementById('bs-last-signal');
    if(sig.signal){
      lastSigEl.textContent = sig.signal+(sig.time?' · '+sig.time:'');
      lastSigEl.className   = 'font-semibold text-right '+(sig.signal.includes('CE')?'text-blue-600':'text-amber-600');
    } else {
      lastSigEl.textContent = 'None yet';
      lastSigEl.className   = 'font-semibold text-gray-400 text-right';
    }
    nextCheckStr = sig.next_check || null;
    updateCountdown();

    // Active position
    const posCard = document.getElementById('pos-card');
    const posEmpty = document.getElementById('pos-empty');
    if(pos.active){
      posCard.classList.remove('hidden');
      posEmpty.classList.add('hidden');
      document.getElementById('pos-sym').textContent = pos.symbol||'—';
      const _lotSz = (s.config && s.config.lot_size) || 65;
      document.getElementById('pos-qty').textContent  = pos.qty ? pos.qty+' ('+Math.round(pos.qty/_lotSz)+' lot'+(pos.qty/_lotSz!==1?'s':'')+')' : '—';
      document.getElementById('pos-ltp').textContent  = pos.live_ltp?'₹'+pos.live_ltp.toFixed(2):'—';
      document.getElementById('pos-time').textContent = pos.entry_time||'—';
      const pp = pos.live_pnl||0;
      const ppEl = document.getElementById('pos-pnl');
      ppEl.textContent = (pp>=0?'+':'')+inr(pp);
      ppEl.className   = 'text-2xl font-extrabold '+cls(pp);
      const ep = pos.entry_price||0;
      const pctEl = document.getElementById('pos-pnl-pct');
      const pct = ep ? (pp/(ep*(pos.qty||1))*100) : 0;
      pctEl.textContent = ep ? (pp>=0?'+':'')+pct.toFixed(2)+'%' : '';
      pctEl.className   = 'text-xs font-semibold '+cls(pp);
      document.getElementById('pos-sl')      .textContent = pos.stop_desc   || '—';
      document.getElementById('pos-target')  .textContent = pos.target_desc || '—';
      document.getElementById('pos-invested').textContent = ep && pos.qty ? inr(ep*pos.qty) : '—';
      document.getElementById('pos-badge').textContent = pos.side||'';
      document.getElementById('pos-badge').className =
        'text-xs font-bold px-2.5 py-0.5 rounded-full '+
        (pos.side==='CE'?'bg-blue-100 text-blue-700':'bg-amber-100 text-amber-700');
      let tags='';
      if(pos.partial_done) tags+='<span class="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full">Partial exited</span>';
      if(pos.trail_on)     tags+='<span class="text-xs bg-violet-100 text-violet-700 px-2 py-0.5 rounded-full ml-1">Trail active</span>';
      document.getElementById('pos-tags').innerHTML=tags;

      // +1 Lot / -1 Lot buttons
      const lotSize   = (s.config && s.config.lot_size) || 65;
      const maxAdds   = (s.config && s.config.max_manual_add_lots) || 2;
      const addBtn    = document.getElementById('btn-add-lot');
      const sellBtn   = document.getElementById('btn-sell-lot');
      const addsUsed  = pos.manual_adds || 0;
      const addMaxed  = addsUsed >= maxAdds;
      addBtn.disabled = addMaxed;
      addBtn.classList.toggle('opacity-40', addMaxed);
      addBtn.classList.toggle('cursor-not-allowed', addMaxed);
      addBtn.title = addMaxed ? 'Manual add limit reached ('+maxAdds+' max)' : '';
      const onlyOneLot = (pos.qty||0) <= lotSize;
      sellBtn.disabled = onlyOneLot;
      sellBtn.classList.toggle('opacity-40', onlyOneLot);
      sellBtn.classList.toggle('cursor-not-allowed', onlyOneLot);
      sellBtn.title = onlyOneLot ? 'Only 1 lot left — use Exit Trade' : '';
    } else {
      posCard.classList.add('hidden');
      posEmpty.classList.remove('hidden');
    }

    // Trade log
    const trades = s.trades||[];
    document.getElementById('live-trades-tbl').innerHTML = tradeTable(trades);
    document.getElementById('live-trade-badge').textContent =
      trades.length ? trades.length+' trade'+(trades.length>1?'s':'') : '';

    // Filter reason
    const filterBox = document.getElementById('sig-filter');
    const filterReason = sig.filter_reason;
    if(filterReason && !pos.active){
      filterBox.textContent = '⚙ '+filterReason;
      filterBox.classList.remove('hidden');
    } else {
      filterBox.classList.add('hidden');
    }

    // Error (persistent until cleared)
    const errBox = document.getElementById('sig-error');
    if(s.last_error){
      errBox.textContent = 'Error: '+s.last_error;
      errBox.classList.remove('hidden');
    } else {
      errBox.classList.add('hidden');
    }

    // Recent activity
    activityItems = s.activity || [];
    document.getElementById('activity-list').innerHTML = renderActivity(activityItems, activityShowAll);
  }).catch(()=>{});
}
setInterval(refreshLive, 5000); refreshLive();

// ── Bot Status countdown (ticks every second between 5s polls) ──
function updateCountdown(){
  const el = document.getElementById('bs-countdown');
  if(!nextCheckStr){ el.textContent='—'; return; }
  const now = new Date();
  const [hh,mm] = nextCheckStr.split(':').map(Number);
  const target = new Date(now); target.setHours(hh,mm,0,0);
  let diff = Math.floor((target-now)/1000);
  if(diff < 0){ el.textContent='Checking…'; return; }
  const m = Math.floor(diff/60), sec = diff%60;
  el.textContent = String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0');
}
setInterval(updateCountdown, 1000);

// ── Recent Activity feed ─────────────────────────────────────────
function renderActivity(items, showAll){
  if(!items || !items.length)
    return '<p class="text-xs text-gray-400 text-center py-6">No activity yet today.</p>';
  const shown = showAll ? items : items.slice(0,4);
  const iconFor = k => k==='entry'     ? ['bg-blue-100 text-blue-600','↗'] :
                        k==='exit'     ? ['bg-green-100 text-green-600','✓'] :
                        k==='exit_loss'? ['bg-red-100 text-red-600','✕'] :
                                         ['bg-violet-100 text-violet-600','◎'];
  return shown.map(a=>{
    const [css,icon] = iconFor(a.kind);
    return `<div class="flex items-start gap-3">
      <span class="w-6 h-6 rounded-full flex items-center justify-center text-xs shrink-0 ${css}">${icon}</span>
      <div class="min-w-0">
        <p class="text-xs text-gray-700 leading-snug">${a.text}</p>
        <p class="text-[10px] text-gray-400">${a.time}</p>
      </div>
    </div>`;
  }).join('');
}
function toggleActivity(){
  activityShowAll = !activityShowAll;
  document.getElementById('activity-toggle').textContent = activityShowAll?'Show Less':'View All';
  document.getElementById('activity-list').innerHTML = renderActivity(activityItems, activityShowAll);
}

// Also refresh balance separately every 30s
function refreshBalance(){
  fetch('/api/balance').then(r=>r.json()).then(d=>{
    if(d.available_cash){
      document.getElementById('hdr-cash').textContent=inr(d.available_cash);
      document.getElementById('live-cash').textContent=inr(d.available_cash);
    }
  }).catch(()=>{});
}
setInterval(refreshBalance,30000); refreshBalance();

// VIX + PCR beside NIFTY 50 in the header — visible on every tab, not just Index
function refreshHeaderVix(){
  fetch('/api/indices').then(r=>r.json()).then(d=>{
    const vix = (d.indices||[]).find(i=>i.name==='INDIA VIX');
    if(vix && vix.ltp!=null){
      const el = document.getElementById('hdr-vix');
      el.textContent = vix.ltp.toLocaleString('en-IN',{maximumFractionDigits:2});
      el.className = 'text-lg font-bold ' + cls(vix.change ?? 0);
    }
    if(d.pcr!=null){
      document.getElementById('hdr-pcr').textContent = d.pcr.toFixed(2);
    }
  }).catch(()=>{});
}
setInterval(refreshHeaderVix,5000); refreshHeaderVix();

// 14-day win/loss performance (slow-moving — refresh every 60s)
function renderSparkline(dayLabels, cumValues, dayDeltas, dayPercents){
  if(!cumValues.length || cumValues.every(v=>v===0))
    return '<p class="text-xs text-gray-400 text-center py-10">No trades in this window yet.</p>';
  const w=600, h=140, pad=6, topPad=26, botPad=26;
  const min=Math.min(0,...cumValues), max=Math.max(0,...cumValues);
  const range=(max-min)||1;
  const x = i => pad + i*(w-2*pad)/Math.max(1,cumValues.length-1);
  const y = v => h-botPad - (v-min)/range*(h-topPad-botPad);
  const pts = cumValues.map((v,i)=>[x(i),y(v)]);
  const line = pts.map((p,i)=>(i===0?'M':'L')+p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ');
  const area = line+` L${pts[pts.length-1][0].toFixed(1)},${(h-botPad).toFixed(1)} L${pts[0][0].toFixed(1)},${(h-botPad).toFixed(1)} Z`;
  const zeroY = y(0).toFixed(1);
  const last  = pts[pts.length-1];
  const lastVal = cumValues[cumValues.length-1];
  const lastLabel = (lastVal>=0?'+':'-')+'₹'+Math.abs(lastVal).toLocaleString('en-IN',{maximumFractionDigits:0});

  // Per-day P&L% labels — rendered as normal HTML overlaid on the SVG (not as
  // SVG <text>) so they stay crisp; the chart itself uses preserveAspectRatio
  // "none" to stretch edge-to-edge, which would otherwise squash/stretch glyphs.
  // Always placed above the dot (never below), per design.
  const deltas  = dayDeltas   || [];
  const percents = dayPercents || [];
  const n = pts.length;
  const labels = pts.map((p,i)=>{
    const dv = deltas[i] || 0;
    if(!dv) return '';
    const pv     = percents[i] || 0;
    const txt    = (pv>=0?'+':'-')+Math.abs(pv).toFixed(1)+'%';
    const color  = dv>=0 ? 'text-green-600' : 'text-red-500';
    const leftPct = p[0]/w*100;
    const topPct  = p[1]/h*100;
    const xAlign  = i===0 ? 'translate(0,' : (i===n-1 ? 'translate(-100%,' : 'translate(-50%,');
    return `<span class="absolute text-[10px] font-bold ${color} whitespace-nowrap bg-white/80 px-0.5 rounded"
        style="left:${leftPct.toFixed(2)}%; top:${topPct.toFixed(2)}%; transform:${xAlign}-100%); margin-top:-3px">${txt}</span>`;
  }).join('');

  return `<div class="relative" style="height:9rem">
    <svg viewBox="0 0 ${w} ${h}" class="w-full h-full absolute inset-0" preserveAspectRatio="none">
      <line x1="${pad}" y1="${zeroY}" x2="${w-pad}" y2="${zeroY}" stroke="#e9e4fb" stroke-width="1"/>
      <path d="${area}" fill="#7c3aed" fill-opacity="0.08" stroke="none"/>
      <path d="${line}" fill="none" stroke="#7c3aed" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="4.5" fill="#7c3aed" stroke="#fff" stroke-width="2"/>
    </svg>
    <div class="absolute inset-0">${labels}</div>
    </div>
    <div class="flex justify-between text-[10px] text-gray-400 mt-1 px-0.5">
      <span>${dayLabels[0]}</span><span class="font-semibold text-violet-700">${lastLabel}</span><span>${dayLabels[dayLabels.length-1]}</span>
    </div>`;
}

function refreshPerfMonth(){
  const to   = new Date();
  const from = new Date(to.getFullYear(), to.getMonth(), 1);   // 1st of current month
  // Local-date formatting (not toISOString, which shifts by the UTC offset
  // and would send e.g. IST midnight as the previous day) so the query
  // range and each chart point line up with real calendar days.
  const fmt  = dt => `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
  const monthName = to.toLocaleDateString('en-IN',{month:'long'});
  const daysSoFar = to.getDate();   // 1st .. today, inclusive

  fetch(`/api/trade-history?from=${fmt(from)}&to=${fmt(to)}`).then(r=>r.json()).then(d=>{
    const s      = d.summary || {};
    const wins   = s.wins   || 0;
    const losses = s.losses || 0;
    const total  = s.total_trades || 0;
    const winRate = total ? s.win_rate : 0;

    document.getElementById('perf14-total').textContent = total+' trade'+(total===1?'':'s')+' · '+monthName;
    const pnlEl = document.getElementById('perf14-pnl');
    pnlEl.textContent = total ? (s.total_pnl>=0?'+':'')+inr(s.total_pnl) : '—';
    pnlEl.className   = 'text-2xl font-extrabold '+(total?cls(s.total_pnl):'text-gray-300');
    document.getElementById('perf14-wins-n').textContent   = wins;
    document.getElementById('perf14-losses-n').textContent = losses;
    document.getElementById('perf14-winrate').textContent  = total ? winRate+'%' : '—';

    // Month-to-date headline pills
    const wr14a = document.getElementById('hero-wr14');
    const wr14b = document.getElementById('mini-wr14');
    wr14a.textContent = total ? winRate+'%' : '—';
    wr14b.textContent = total ? winRate+'%' : '—';

    // Build a daily cumulative P&L series across every day of the month so
    // far (including zero-trade days, so the line reflects real gaps).
    const byDate = {}, byDateCap = {};
    (d.trades||[]).forEach(t=>{
      if(t.pnl!=null){
        byDate[t.date]    = (byDate[t.date]||0)    + t.pnl;
        byDateCap[t.date] = (byDateCap[t.date]||0) + (t.capital||0);
      }
    });
    const dayLabels=[], cumValues=[], dayDeltas=[], dayPercents=[];
    let running=0;
    for(let day=1; day<=daysSoFar; day++){
      const dt = new Date(to.getFullYear(), to.getMonth(), day);
      const key = fmt(dt);
      const delta = byDate[key]||0;
      const cap   = byDateCap[key]||0;
      running += delta;
      dayLabels.push(dt.toLocaleDateString('en-IN',{day:'2-digit',month:'short'}));
      cumValues.push(Math.round(running));
      dayDeltas.push(Math.round(delta));
      dayPercents.push(cap ? (delta/cap*100) : 0);
    }
    document.getElementById('perf14-chart').innerHTML = renderSparkline(dayLabels, cumValues, dayDeltas, dayPercents);
  }).catch(()=>{});
}
setInterval(refreshPerfMonth,60000); refreshPerfMonth();

// ── Start Trading Modal ───────────────────────────────────────
function openStartModal(){
  fetch('/api/trading-config').then(r=>r.json()).then(cfg=>{
    document.getElementById('m-max-trades').value=cfg.max_trades||2;
    document.getElementById('m-lots').value=cfg.lots||1;
    const isPaper = cfg.paper !== false; // default to paper
    document.getElementById('m-mode-paper').checked = isPaper;
    document.getElementById('m-mode-live') .checked = !isPaper;
    const isSupertrend = cfg.strategy === 'supertrend';
    document.getElementById('m-strategy-v2')        .checked = !isSupertrend;
    document.getElementById('m-strategy-supertrend').checked = isSupertrend;
    document.getElementById('m-manual-target').value = cfg.manual_target_pct ? String(cfg.manual_target_pct) : '0';
    document.getElementById('m-carry-overnight').checked = !!cfg.carry_overnight;
    updateModalUnits();
    updateModeWarning();
  }).catch(()=>{});
  document.getElementById('modal-bg').classList.add('open');
}
function closeModal(){
  document.getElementById('modal-bg').classList.remove('open');
}
function updateModalUnits(){
  const lots=parseInt(document.getElementById('m-lots').value||1);
  const mt  =parseInt(document.getElementById('m-max-trades').value||2);
  document.getElementById('m-units')      .textContent='= '+(lots*65)+' units per trade';
  document.getElementById('m-max-display').textContent=mt;
}
function updateModeWarning(){
  const isPaper=document.getElementById('m-mode-paper').checked;
  document.getElementById('m-warning-paper').classList.toggle('hidden',!isPaper);
  document.getElementById('m-warning-live') .classList.toggle('hidden', isPaper);
}
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('m-lots')      .addEventListener('input',updateModalUnits);
  document.getElementById('m-max-trades').addEventListener('input',updateModalUnits);
  document.getElementById('m-mode-paper').addEventListener('change',updateModeWarning);
  document.getElementById('m-mode-live') .addEventListener('change',updateModeWarning);
});

function confirmStart(){
  const max_trades=parseInt(document.getElementById('m-max-trades').value);
  const lots      =parseInt(document.getElementById('m-lots').value);
  const paper     =document.getElementById('m-mode-paper').checked;
  const strategy  =document.getElementById('m-strategy-supertrend').checked ? 'supertrend' : 'v2';
  const manual_target_pct = parseFloat(document.getElementById('m-manual-target').value) || 0;
  const carry_overnight   = document.getElementById('m-carry-overnight').checked;
  closeModal();
  fetch('/api/start-trading',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({max_trades,lots,paper,strategy,manual_target_pct,carry_overnight})
  }).then(r=>r.json()).then(d=>{
    if(d.error) alert('Error: '+d.error);
    else setTimeout(refreshLive,1000);
  });
}

function stopTrading(){
  fetch('/api/stop-trading',{method:'POST'})
    .then(r=>r.json())
    .then(()=>setTimeout(refreshLive,500));
}
function forceExit(){
  if(!confirm('Exit open position immediately?')) return;
  fetch('/api/force-exit',{method:'POST'})
    .then(r=>r.json())
    .then(()=>setTimeout(refreshLive,500));
}
function manualExitPosition(){
  if(!confirm('Exit this trade now? The bot will stay running and can take new entries.')) return;
  fetch('/api/exit-position',{method:'POST'})
    .then(r=>r.json())
    .then(()=>setTimeout(refreshLive,500));
}
function addLot(){
  if(!confirm('Buy 1 more lot at the current market price and add it to this position?')) return;
  const btn=document.getElementById('btn-add-lot');
  btn.disabled=true;
  fetch('/api/add-lot',{method:'POST'})
    .then(r=>r.json())
    .then(d=>{
      if(d.status==='error') alert('Error: '+d.message);
      setTimeout(refreshLive,500);
    }).catch(e=>alert('Error: '+e));
}
function sellLot(){
  if(!confirm('Sell 1 lot from this position at the current market price?')) return;
  const btn=document.getElementById('btn-sell-lot');
  btn.disabled=true;
  fetch('/api/sell-lot',{method:'POST'})
    .then(r=>r.json())
    .then(d=>{
      if(d.status==='error') alert('Error: '+d.message);
      setTimeout(refreshLive,500);
    }).catch(e=>alert('Error: '+e));
}
function testTrade(){
  if(!confirm('Place a REAL CE order on Angel One and auto-exit in 5 seconds?\n\nThis is for connectivity testing only — a real order will be placed.')) return;
  const btn=document.getElementById('btn-test');
  btn.disabled=true; btn.textContent='Placing…';
  fetch('/api/test-trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})
    .then(r=>r.json())
    .then(d=>{
      btn.disabled=false; btn.textContent='⚡ Test Trade';
      if(d.error){alert('Error: '+d.error);}
      else{alert('✅ '+d.message+'\n\nWatch the Trades section — entry and exit will appear.');}
      setTimeout(refreshLive,500);
      setTimeout(refreshLive,6000);
    }).catch(e=>{btn.disabled=false;btn.textContent='⚡ Test Trade';alert('Error: '+e);});
}

// Close modal on background click
document.getElementById('modal-bg').addEventListener('click',function(e){
  if(e.target===this) closeModal();
});
document.getElementById('day-modal-bg').addEventListener('click',function(e){
  if(e.target===this) closeDayModal();
});

</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(TEMPLATE)

@app.route("/health")
def health():
    try:
        t = get_trader()
        return jsonify({
            "status"   : "ok",
            "connected": t.connected,
            "running"  : t._running,
            "time"     : datetime.now().strftime("%H:%M:%S"),
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


if __name__ == "__main__":
    get_trader()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Dashboard : http://localhost:{port}")
    print("  Live tab  : start/stop trading, see live P&L")
    print("  Backtest  : run range analysis\n")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
