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
            manual_target_pct   = cfg.get("manual_target_pct")
            carry_overnight     = bool(cfg.get("carry_overnight", False))
            t.start(max_trades=max_trades, lots=lots, paper_mode=paper,
                   max_daily_loss=max_daily_loss, daily_profit_target=daily_profit_target,
                   manual_target_pct=(float(manual_target_pct) / 100.0 if manual_target_pct else None),
                   carry_overnight=carry_overnight)
            from logzero import logger
            logger.info(f"Auto-resumed trading: {lots} lot(s), "
                       f"max {max_trades} trades, paper={paper}")
            mode = "📋 PAPER" if paper else "🟢 LIVE"
            _tg(f"🔄 <b>Server Restarted — Trading Auto-Resumed</b>\n"
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
    manual_target_pct = _parse_manual_target_pct(body)
    if manual_target_pct == "unset":
        manual_target_pct = None
    carry_overnight = bool(body.get("carry_overnight", False))
    try:
        t = get_trader()
        t.start(max_trades=max_trades, lots=lots, paper_mode=paper,
               max_daily_loss=max_daily_loss, daily_profit_target=daily_profit_target,
               manual_target_pct=(manual_target_pct / 100.0 if manual_target_pct else None),
               carry_overnight=carry_overnight)
        _save_trading_config({"max_trades": max_trades, "lots": lots, "paper": paper, "active": True,
                              "max_daily_loss": max_daily_loss, "daily_profit_target": daily_profit_target,
                              "manual_target_pct": manual_target_pct,
                              "carry_overnight": carry_overnight})
        return jsonify({"status": "started", "max_trades": max_trades, "lots": lots, "paper": paper,
                        "max_daily_loss": max_daily_loss, "daily_profit_target": daily_profit_target,
                        "manual_target_pct": manual_target_pct,
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
        expiry = _next_thursday(scrip)
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

TRADE_LOG_FILE     = os.getenv("ARTHA_TRADE_LOG", "logs/trade_history.json")   # env override: local previews
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
  #holding-modal-bg{display:none;position:fixed;inset:0;background:rgba(30,27,46,.5);z-index:50;align-items:center;justify-content:center}
  #holding-modal-bg.open{display:flex}
  #watch-modal-bg{display:none;position:fixed;inset:0;background:rgba(30,27,46,.5);z-index:50;align-items:center;justify-content:center}
  #watch-modal-bg.open{display:flex}
  #sell-modal-bg{display:none;position:fixed;inset:0;background:rgba(30,27,46,.5);z-index:50;align-items:center;justify-content:center}
  #sell-modal-bg.open{display:flex}
  #funds-modal-bg{display:none;position:fixed;inset:0;background:rgba(30,27,46,.5);z-index:50;align-items:center;justify-content:center}
  #funds-modal-bg.open{display:flex}
  /* ── Artha Scanner ── */
  .scan-card{--accent:#7c3aed;--accent-soft:rgba(124,58,237,.14);transition:box-shadow .4s}
  .scan-card[data-mode="scanning"]{box-shadow:0 1px 2px rgba(76,29,149,.05),0 8px 28px rgba(124,58,237,.12)}
  .scan-card[data-mode="found"]{--accent:#16a34a;--accent-soft:rgba(22,163,74,.16);
    box-shadow:0 0 0 2px #86efac,0 10px 30px rgba(22,163,74,.18)}
  .scan-card[data-mode="skipped"]{--accent:#d97706;--accent-soft:rgba(217,119,6,.14)}
  .scan-card[data-mode="halted"]{--accent:#dc2626;--accent-soft:rgba(220,38,38,.12)}
  .scan-card[data-mode="idle"],.scan-card[data-mode="closed"]{--accent:#a39fc0;--accent-soft:rgba(163,159,192,.12)}
  .scan-chip{font-size:11px;font-weight:800;letter-spacing:.08em;padding:3px 10px;border-radius:9999px;
    color:var(--accent);background:var(--accent-soft)}
  .scan-card[data-mode="scanning"] .scan-chip,.scan-card[data-mode="found"] .scan-chip{animation:pulse 1.6s infinite}
  .radar{position:relative;width:168px;height:168px;border-radius:9999px;overflow:hidden;
    background:radial-gradient(circle,#fff 0%,#faf8ff 55%,#f1ecfe 100%);border:1px solid #e9e4fb}
  .radar-ring{position:absolute;border:1px solid var(--accent-soft);border-radius:9999px;inset:0;margin:auto}
  .radar-ring.r1{width:33%;height:33%}.radar-ring.r2{width:66%;height:66%}.radar-ring.r3{width:96%;height:96%}
  .radar-cross{position:absolute;inset:0;background:
    linear-gradient(var(--accent-soft),var(--accent-soft)) center/1px 100% no-repeat,
    linear-gradient(var(--accent-soft),var(--accent-soft)) center/100% 1px no-repeat}
  .radar-sweep{position:absolute;inset:0;border-radius:9999px;opacity:0;
    background:conic-gradient(from 0deg,transparent 0deg,transparent 290deg,var(--accent-soft) 330deg,var(--accent) 360deg);
    -webkit-mask:radial-gradient(circle,#000 69%,transparent 70%);mask:radial-gradient(circle,#000 69%,transparent 70%)}
  .scan-card[data-mode="scanning"] .radar-sweep,.scan-card[data-mode="skipped"] .radar-sweep{opacity:.55;animation:sweep 3s linear infinite}
  .scan-card[data-mode="found"] .radar-sweep{opacity:.7;animation:sweep .8s linear infinite}
  @keyframes sweep{to{transform:rotate(360deg)}}
  .radar-core{position:absolute;inset:0;margin:auto;width:42px;height:42px;border-radius:9999px;background:#fff;
    border:2px solid var(--accent);display:flex;align-items:center;justify-content:center;
    color:var(--accent);font-size:18px;font-weight:800;box-shadow:0 0 0 6px var(--accent-soft)}
  .scan-card[data-mode="found"] .radar-core{animation:corePing 1s ease-out infinite}
  @keyframes corePing{0%{box-shadow:0 0 0 0 rgba(22,163,74,.45)}100%{box-shadow:0 0 0 22px rgba(22,163,74,0)}}
  .blip{position:absolute;width:7px;height:7px;border-radius:9999px;background:var(--accent);
    transform:translate(-50%,-50%);animation:blip 2.4s ease-out forwards}
  @keyframes blip{0%{opacity:0;transform:translate(-50%,-50%) scale(.3)}15%{opacity:1;transform:translate(-50%,-50%) scale(1.4)}100%{opacity:0;transform:translate(-50%,-50%) scale(.8)}}
  .scan-step{display:flex;align-items:center;gap:10px;font-size:13px;padding:7px 10px;border-radius:10px;background:#faf9ff;border:1px solid #f0ecfb}
  .scan-step .dot{width:9px;height:9px;border-radius:9999px;background:#d6d1ea;flex-shrink:0}
  .scan-step .lbl{color:#5b5578;font-weight:600}
  .scan-step .val{margin-left:auto;font-weight:700;color:#1e1b2e;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .scan-step.ok .dot{background:#22c55e}
  .scan-step.wait .dot{background:#7c3aed;animation:pulse 1.2s infinite}
  .scan-step.warn{background:#fffbeb;border-color:#fde68a}.scan-step.warn .dot{background:#f59e0b}
  .scan-step.bad{background:#fef2f2;border-color:#fecaca}.scan-step.bad .dot{background:#ef4444}
  .scan-step.hit{background:#f0fdf4;border-color:#86efac}.scan-step.hit .dot{background:#16a34a;animation:pulse .8s infinite}
  .scan-ticker::before{content:'›_ ';color:var(--accent);font-weight:800}
  .exec-banner{position:absolute;inset:0;z-index:5;display:flex;flex-direction:column;align-items:center;justify-content:center;
    text-align:center;background:linear-gradient(135deg,#f0fdf4 0%,#ffffff 60%);animation:execOut 4.2s ease forwards}
  .exec-burst{position:absolute;width:90px;height:90px;border-radius:9999px;border:3px solid #22c55e;animation:burst 1.2s ease-out 3}
  @keyframes burst{0%{transform:scale(.4);opacity:1}100%{transform:scale(3.4);opacity:0}}
  @keyframes execOut{0%,80%{opacity:1}100%{opacity:0;visibility:hidden}}
  @media (prefers-reduced-motion:reduce){.radar-sweep,.blip,.exec-burst,.radar-core,.scan-chip{animation:none!important}}
  /* ── Live tab: session strip, cockpit, daily gauge ── */
  .sess-track{position:relative;height:10px;border-radius:9999px;background:#f1effa;overflow:visible}
  .sess-fill{position:absolute;left:0;top:0;bottom:0;border-radius:9999px;width:0;
    background:linear-gradient(90deg,#c4b5fd,#7c3aed);transition:width .8s ease}
  .sess-zone{position:absolute;top:0;bottom:0;background:repeating-linear-gradient(45deg,rgba(217,119,6,.18) 0 4px,transparent 4px 8px);border-radius:0 9999px 9999px 0}
  .sess-now{position:absolute;top:-4px;width:18px;height:18px;margin-left:-9px;border-radius:9999px;background:#fff;
    border:3px solid #7c3aed;box-shadow:0 0 0 0 rgba(124,58,237,.5);animation:nowPing 2s infinite}
  @keyframes nowPing{0%{box-shadow:0 0 0 0 rgba(124,58,237,.45)}100%{box-shadow:0 0 0 12px rgba(124,58,237,0)}}
  .sess-mark{position:absolute;top:-3px;width:16px;height:16px;margin-left:-8px;border-radius:9999px;border:2px solid #fff;
    font-size:9px;font-weight:800;color:#fff;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 4px rgba(0,0,0,.15)}
  .sess-tick{position:absolute;transform:translateX(-50%);white-space:nowrap}
  .ladder{position:relative;height:14px;border-radius:9999px;overflow:visible}
  .lad-zone{position:absolute;top:0;bottom:0}
  .lad-floor{position:absolute;top:-5px;bottom:-5px;width:0;border-left:2px dashed #7c3aed}
  .lad-peak{position:absolute;top:-9px;margin-left:-5px;width:0;height:0;border-left:5px solid transparent;
    border-right:5px solid transparent;border-top:7px solid #16a34a}
  .lad-now{position:absolute;top:50%;width:20px;height:20px;margin:-10px 0 0 -10px;border-radius:9999px;background:#fff;
    border:3px solid #7c3aed;transition:left .6s ease;box-shadow:0 2px 8px rgba(124,58,237,.35)}
  .lad-now span{position:absolute;bottom:22px;left:50%;transform:translateX(-50%);font-size:11px;font-weight:800;
    background:#1e1b2e;color:#fff;padding:1px 6px;border-radius:6px;white-space:nowrap}
  .slbar{position:relative;height:10px;border-radius:9999px;
    background:linear-gradient(90deg,#fecaca 0%,#fef3c7 50%,#bbf7d0 100%)}
  .slbar-mid{position:absolute;left:50%;top:-3px;bottom:-3px;border-left:2px solid #a39fc0}
  .slbar-dot{position:absolute;top:50%;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:9999px;background:#1e1b2e;
    border:3px solid #fff;box-shadow:0 1px 5px rgba(0,0,0,.25);transition:left .6s ease;left:50%}
  .dgauge{position:relative;height:8px;border-radius:9999px;background:linear-gradient(90deg,#fee2e2,#f1effa 50%,#dcfce7)}
  .dgauge-zero{position:absolute;top:-3px;bottom:-3px;border-left:2px solid #a39fc0}
  .dgauge-fill{position:absolute;top:0;bottom:0;border-radius:9999px;transition:all .8s ease}

  /* ── Trade History: story, tiles, timeline ── */
  .hx-story{background:linear-gradient(135deg,#ffffff 0%,#f6f3ff 100%)}
  .hx-curve-wrap{position:relative}
  .hx-tip{position:absolute;pointer-events:none;background:#1e1b2e;color:#fff;font-size:11px;line-height:1.35;
    padding:6px 9px;border-radius:8px;white-space:nowrap;transform:translate(-50%,-115%);box-shadow:0 4px 14px rgba(0,0,0,.18)}
  .hx-badge{font-size:11px;font-weight:700;padding:4px 10px;border-radius:9999px;background:#f5f3fc;border:1px solid #ece7f9;color:#5b5578}
  .hx-ring{transition:stroke-dasharray 1s ease}
  .hx-bar{flex:1;height:8px;border-radius:9999px;background:#f1effa;overflow:hidden}
  .hx-bar span{display:block;height:100%;border-radius:9999px;width:0;transition:width .9s ease}
  .hx-chip{font-size:12px;font-weight:600;padding:4px 12px;border-radius:9999px;background:#f5f3fc;color:#6b6588;border:1px solid #ece7f9}
  .hx-chip.active{background:#7c3aed;color:#fff;border-color:#7c3aed}
  .hx-trade{display:grid;grid-template-columns:4px 1fr auto;gap:12px;align-items:center;padding:10px 14px 10px 0;
    border-radius:14px;background:#fff;border:1px solid #f0ecfb;overflow:hidden;animation:hxIn .35s ease both;cursor:pointer}
  .hx-trade:hover{border-color:#ddd6fe;box-shadow:0 4px 14px rgba(124,58,237,.08)}
  .hx-trade .edge{align-self:stretch}
  .hx-ret{height:6px;border-radius:9999px;background:#f1effa;position:relative;overflow:hidden}
  .hx-ret span{position:absolute;top:0;bottom:0;border-radius:9999px}
  .hx-details{grid-column:2/4;display:none;font-size:12px;color:#5b5578}
  .hx-trade.open .hx-details{display:grid}
  @keyframes hxIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  @media (prefers-reduced-motion:reduce){.hx-trade{animation:none}.hx-ring,.hx-bar span{transition:none}}
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

  <!-- ── Market session timeline ── -->
  <div class="card px-5 py-3">
    <div class="flex items-center justify-between gap-3 mb-2">
      <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Market Session</p>
      <p id="sess-label" class="text-xs font-semibold text-gray-500">—</p>
    </div>
    <div class="sess-track" id="sess-track">
      <div class="sess-fill" id="sess-fill"></div>
      <div class="sess-zone" id="sess-nonew" title="No new entries after cutoff"></div>
      <div id="sess-marks"></div>
      <div class="sess-now hidden" id="sess-now"></div>
    </div>
    <div class="relative h-4 mt-1 text-[10px] text-gray-400" id="sess-ticks"></div>
  </div>

  <!-- ── Top row: live trade panel (left, empty until a trade is on) + Bot card (right, square) ── -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">

    <!-- ═══ LEFT: live trade details ═══ -->
    <div class="lg:col-span-2">

      <!-- ── Live scanner (shown while no trade is open) ── -->
      <div id="pos-empty" class="card scan-card p-5 h-full min-h-[260px]" data-mode="idle">
        <div class="flex items-center justify-between mb-3">
          <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Artha Scanner</p>
          <span id="scan-chip" class="scan-chip">IDLE</span>
        </div>
        <div class="flex flex-col md:flex-row gap-6 items-center md:items-stretch">
          <!-- Radar -->
          <div class="radar shrink-0" aria-hidden="true">
            <div class="radar-ring r1"></div><div class="radar-ring r2"></div><div class="radar-ring r3"></div>
            <div class="radar-cross"></div>
            <div class="radar-sweep"></div>
            <div id="radar-blips"></div>
            <div class="radar-core"><span id="radar-icon">◉</span></div>
          </div>
          <!-- Status + checklist -->
          <div class="flex-1 w-full min-w-0">
            <p id="scan-title" class="text-lg font-extrabold text-gray-900">Scanner idle</p>
            <p id="scan-sub" class="text-xs text-gray-500 mt-0.5">Start the bot to begin scanning.</p>
            <ul class="mt-4 space-y-2" id="scan-steps">
              <li class="scan-step" id="step-data"><span class="dot"></span><span class="lbl">Market data</span><span class="val" id="val-data">—</span></li>
              <li class="scan-step" id="step-trend"><span class="dot"></span><span class="lbl">Artha Trend</span><span class="val" id="val-trend">—</span></li>
              <li class="scan-step" id="step-flip"><span class="dot"></span><span class="lbl">Trend flip</span><span class="val" id="val-flip">—</span></li>
              <li class="scan-step" id="step-move"><span class="dot"></span><span class="lbl">Pre-move filter</span><span class="val" id="val-move">—</span></li>
              <li class="scan-step" id="step-risk"><span class="dot"></span><span class="lbl">Risk limits</span><span class="val" id="val-risk">—</span></li>
            </ul>
            <div class="mt-4 flex items-center justify-between gap-3">
              <p id="scan-ticker" class="scan-ticker text-xs text-gray-500 truncate"></p>
              <p class="text-xs text-gray-400 shrink-0">Next scan <span id="scan-next" class="font-bold text-violet-600 tabular-nums">—</span></p>
            </div>
          </div>
        </div>
      </div>

      <!-- ── Active Position ── -->
      <div id="pos-card" class="card p-5 hidden h-full relative overflow-hidden">
        <div id="exec-banner" class="exec-banner hidden">
          <div class="exec-burst"></div>
          <p class="text-xs font-bold tracking-widest uppercase text-green-700">Signal confirmed</p>
          <p class="text-2xl font-extrabold text-gray-900 mt-1">✓ Trade executed</p>
          <p id="exec-detail" class="text-sm text-gray-600 mt-1"></p>
        </div>
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

        <!-- ── Trade cockpit ── -->
        <div class="mt-4 pt-4 border-t border-gray-100">
          <div class="flex items-center justify-between gap-3">
            <p class="text-xs font-semibold text-gray-500">Profit ladder</p>
            <p id="lad-status" class="text-xs font-bold text-gray-500">—</p>
          </div>
          <div class="ladder mt-8" id="ladder">
            <div id="lad-zones"></div>
            <div class="lad-floor hidden" id="lad-floor"></div>
            <div class="lad-peak hidden" id="lad-peak" title="Peak reached"></div>
            <div class="lad-now" id="lad-now"><span id="lad-now-lbl">0%</span></div>
          </div>
          <div class="relative h-4 mt-1 text-[10px] text-gray-400" id="lad-ticks"></div>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-5 mt-3">
            <div>
              <div class="flex items-center justify-between">
                <p class="text-xs font-semibold text-gray-500">Nifty vs stop-loss</p>
                <p id="sl-dist" class="text-xs font-bold text-gray-700">—</p>
              </div>
              <div class="slbar mt-2"><div class="slbar-mid"></div><div class="slbar-dot" id="sl-dot"></div></div>
              <div class="flex justify-between text-[10px] text-gray-400 mt-1">
                <span id="sl-lo">stop</span><span id="sl-mid">entry</span><span id="sl-hi">+50</span></div>
            </div>
            <div>
              <div class="flex items-center justify-between">
                <p class="text-xs font-semibold text-gray-500">Option price since entry</p>
                <p class="text-xs text-gray-500">⏱ <span id="pos-age" class="font-bold text-gray-700 tabular-nums">—</span></p>
              </div>
              <svg id="pos-spark" class="w-full mt-1" height="44" preserveAspectRatio="none"></svg>
            </div>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-4 mt-4 pt-4 border-t border-gray-100">
          <div class="flex items-center gap-2">
            <label class="text-xs font-semibold text-gray-500" for="live-manual-target">Manual Target %</label>
            <select id="live-manual-target" onchange="setManualTarget()"
              class="border border-gray-300 rounded px-2 py-1.5 text-xs text-gray-800">
              <option value="0">Off — hardcoded exit logic only</option>
              <option value="5">5%</option>
              <option value="10">10%</option>
              <option value="15">15%</option>
              <option value="20">20%</option>
            </select>
          </div>
          <label class="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" id="live-carry-overnight" onchange="setCarryOvernight()" class="accent-purple-600"/>
            <span class="text-xs font-semibold text-gray-500">Carry position overnight</span>
          </label>
          <span id="live-target-status" class="text-xs text-gray-400"></span>
        </div>
      </div>

    </div>

    <!-- ═══ RIGHT: Bot Card (square) ═══ -->
    <div class="hero-card p-5 flex flex-col">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <h1 class="text-xl font-extrabold text-gray-900 tracking-tight">ARTHA BOT</h1>
        <span id="status-badge" class="badge-stop text-xs font-bold px-3 py-1 rounded-full">STOPPED</span>
      </div>
      <p id="hero-subtitle" class="text-xs text-gray-500 mt-1">Artha Trend Strategy &middot; Nifty 50 Options</p>

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
        <span id="strategy-badge" class="text-xs font-bold px-3 py-1 rounded-full bg-violet-100 text-violet-700">ARTHA TREND</span>
      </div>

      <div class="mt-4">
        <p class="text-xs text-gray-400">Today's P&amp;L</p>
        <p id="live-pnl" class="text-3xl font-extrabold text-gray-300">—</p>
        <p class="text-xs text-gray-400 mt-0.5">Real-time performance</p>
        <!-- Daily limit gauge: loss cap ← 0 → profit target -->
        <div class="dgauge mt-3"><div class="dgauge-zero"></div><div class="dgauge-fill" id="dg-fill"></div></div>
        <div class="flex justify-between text-[10px] text-gray-400 mt-1">
          <span id="dg-lo">loss limit</span><span id="dg-note" class="font-semibold text-gray-500"></span><span id="dg-hi">target</span></div>
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

      <!-- ── Month summary (full story lives in the Trade History tab) ── -->
      <button onclick="switchTab('history')"
        class="card w-full px-5 py-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-left hover:border-violet-300 transition">
        <span id="perf14-month" class="text-xs text-gray-400 uppercase tracking-widest font-semibold">This month</span>
        <span id="perf14-pnl" class="text-base font-extrabold text-gray-300">—</span>
        <span id="perf14-wl" class="text-xs text-gray-500"></span>
        <span class="ml-auto text-xs font-semibold text-violet-600">View history →</span>
      </button>

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
          <div class="flex justify-between gap-2"><span class="text-gray-400 shrink-0" id="bs-indicator-label">Artha Trend</span>
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

  <!-- ── Performance story: total + equity curve ── -->
  <div id="hx-story" class="card hx-story p-5 hidden">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Performance Story</p>
        <p id="hx-range" class="text-xs text-gray-400 mt-0.5">—</p>
        <p id="hx-total" class="text-4xl font-extrabold mt-2 tabular-nums">₹0.00</p>
        <p id="hx-headline" class="text-sm text-gray-600 mt-1">—</p>
      </div>
      <div class="flex gap-2 flex-wrap" id="hx-badges"></div>
    </div>
    <div class="hx-curve-wrap mt-4">
      <svg id="hx-curve" class="w-full" height="190" preserveAspectRatio="none"></svg>
      <div id="hx-tip" class="hx-tip hidden"></div>
    </div>
  </div>

  <!-- ── Visual stat tiles ── -->
  <div id="hx-tiles" class="hidden grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
    <div class="card p-4 flex items-center gap-3">
      <svg width="54" height="54" viewBox="0 0 36 36" class="shrink-0 -rotate-90">
        <circle cx="18" cy="18" r="15.5" fill="none" stroke="#fee2e2" stroke-width="4"/>
        <circle id="hx-ring" cx="18" cy="18" r="15.5" fill="none" stroke="#22c55e" stroke-width="4"
          stroke-linecap="round" stroke-dasharray="0 100" pathLength="100" class="hx-ring"/>
      </svg>
      <div><p class="text-xs text-gray-400">Win rate</p><p id="hx-wr" class="text-xl font-extrabold text-gray-900">—</p>
        <p id="hx-wl" class="text-xs text-gray-400">—</p></div>
    </div>
    <div class="card p-4"><p class="text-xs text-gray-400">Current streak</p>
      <p id="hx-streak" class="text-xl font-extrabold text-gray-900 mt-1">—</p>
      <p id="hx-streak-best" class="text-xs text-gray-400">—</p></div>
    <div class="card p-4"><p class="text-xs text-gray-400">Best trade 🏆</p>
      <p id="hx-best" class="text-xl font-extrabold text-green-600 mt-1">—</p>
      <p id="hx-best-d" class="text-xs text-gray-400 truncate">—</p></div>
    <div class="card p-4"><p class="text-xs text-gray-400">Worst trade</p>
      <p id="hx-worst" class="text-xl font-extrabold text-red-500 mt-1">—</p>
      <p id="hx-worst-d" class="text-xs text-gray-400 truncate">—</p></div>
    <div class="card p-4"><p class="text-xs text-gray-400">Avg win vs avg loss</p>
      <div class="mt-2 space-y-1.5">
        <div class="flex items-center gap-2"><div class="hx-bar"><span id="hx-aw-bar" class="bg-green-500"></span></div><span id="hx-aw" class="text-xs font-bold text-green-600 w-16 text-right">—</span></div>
        <div class="flex items-center gap-2"><div class="hx-bar"><span id="hx-al-bar" class="bg-red-400"></span></div><span id="hx-al" class="text-xs font-bold text-red-500 w-16 text-right">—</span></div>
      </div></div>
    <div class="card p-4"><p class="text-xs text-gray-400">Profit factor</p>
      <p id="hx-pf" class="text-xl font-extrabold text-gray-900 mt-1">—</p>
      <p id="hx-pf-note" class="text-xs text-gray-400">₹ won per ₹1 lost</p></div>
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

  <!-- ── Trade timeline ── -->
  <div id="hx-timeline-card" class="card p-5 hidden">
    <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
      <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Trade Timeline</p>
      <div class="flex gap-1.5 flex-wrap" id="hx-filters">
        <button class="hx-chip active" data-f="all">All</button>
        <button class="hx-chip" data-f="win">Wins</button>
        <button class="hx-chip" data-f="loss">Losses</button>
        <button class="hx-chip" data-f="CE">CE</button>
        <button class="hx-chip" data-f="PE">PE</button>
      </div>
    </div>
    <div id="hx-timeline" class="hx-timeline space-y-2.5"></div>
    <button id="hx-more" class="hidden mt-4 w-full text-xs font-semibold text-violet-600 hover:underline">Show more</button>
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

    <p class="text-xs text-gray-400 mb-5">Manual Target % and Carry Overnight are set from the Open Position card
      once a trade is live, so you can decide based on how the trade is actually going.</p>

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
  if(name==='history')      renderCalendar();
  if(name==='index')        loadIndices();
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
let calReqSeq = 0, statsReqSeq = 0;

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

  const reqId = ++calReqSeq;   // ignore responses for a month the user already moved past
  fetch(`/api/trade-history?from=${from}&to=${to}`)
    .then(r=>r.json())
    .then(d=>{
      if(reqId !== calReqSeq) return;
      const trades = d.trades||[];
      calTradesByDate = {};
      trades.forEach(t=>{ (calTradesByDate[t.date] = calTradesByDate[t.date]||[]).push(t); });

      // Heat scale: colour strength follows the size of the day's P&L
      const dayTotals = {};
      Object.entries(calTradesByDate).forEach(([ds,ts])=>{
        if(new Date(ds+'T00:00:00').getMonth()!==month) return;
        const v = ts.filter(t=>t.pnl!=null).reduce((a,t)=>a+t.pnl,0); if(ts.length) dayTotals[ds]=v; });
      const maxAbs = Math.max(1, ...Object.values(dayTotals).map(Math.abs));
      const bestDay = Object.entries(dayTotals).filter(([,v])=>v>0).sort((a,b)=>b[1]-a[1])[0]?.[0];

      // Day cells
      let html = '';
      let cur = new Date(gridStart);
      while(cur <= gridEnd){
        const ds = calFmt(cur);
        const inMonth   = cur.getMonth()===month;
        if(!inMonth){   // days of the neighbouring months: keep the grid aligned, show nothing
          html += '<div class="min-h-[92px]" aria-hidden="true"></div>';
          cur.setDate(cur.getDate()+1);
          continue;
        }
        const dayTrades = (calTradesByDate[ds]||[]).filter(t=>t.pnl!=null);
        // Overflow cells (previous/next month filling out the week grid) show
        // their own month's abbreviation so e.g. "31" isn't mistaken for a
        // day in the displayed month.
        const dayLabel = inMonth ? cur.getDate() : `${monthNames[cur.getMonth()].slice(0,3)} ${cur.getDate()}`;
        let inner = `<p class="text-xs font-semibold ${inMonth?'text-gray-700':'text-gray-300'} mb-1.5">${dayLabel}</p>`;
        let cellTone = 'bg-white border-gray-100', cellStyle = '';

        if(dayTrades.length){
          const dayPnl = dayTrades.reduce((a,t)=>a+t.pnl,0);
          const dayCap = dayTrades.reduce((a,t)=>a+(t.capital||0),0);
          const dayPct = dayCap ? (dayPnl/dayCap*100) : 0;
          cellTone = dayPnl>=0 ? 'border-green-200' : 'border-red-200';
          const heat = inMonth ? (0.08 + 0.32*Math.min(1, Math.abs(dayPnl)/maxAbs)) : 0.06;
          cellStyle = ` style="background:${dayPnl>=0?`rgba(34,197,94,${heat})`:`rgba(239,68,68,${heat})`}"`;
          if(ds===bestDay) inner = inner.replace('</p>', ' <span title="Best day of the month">🏆</span></p>');
          inner += `<p class="text-[10px] text-gray-500 mb-0.5">${dayTrades.length} trade${dayTrades.length>1?'s':''}</p>`;
          inner += `<div class="space-y-0.5">
              <div class="flex justify-between text-xs"><span class="text-gray-500 font-medium">Total P&amp;L</span>`+
              `<span class="${cls(dayPnl)} font-bold">${dayPnl>=0?'+':''}${inr(dayPnl)}</span></div>
              <div class="flex justify-between text-xs"><span class="text-gray-400">P&amp;L %</span>`+
              `<span class="${cls(dayPnl)} font-semibold">${dayPnl>=0?'+':''}${dayPct.toFixed(1)}%</span></div>
            </div>`;
        }

        const cellClass  = `cal-cell rounded-lg border p-2 min-h-[92px] ${cellTone}` + (dayTrades.length ? ' has-trades' : '');
        const onclickAttr = dayTrades.length ? ` onclick="openDayModal('${ds}')"` : '';
        html += `<div class="${cellClass}"${cellStyle}${onclickAttr}>${inner}</div>`;
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
  const reqId = ++statsReqSeq;
  fetch(`/api/trade-history?from=${from}&to=${to}`)
    .then(r=>r.json())
    .then(d=>{ if(reqId===statsReqSeq) renderHistoryStory((d.trades||[]).filter(t=>t.pnl!=null), from, to); })
    .catch(()=>{});
}

// ── Trade History: story / tiles / timeline ──────────────────────
const REASONS = {
  ST_TRAIL_EXIT:['🔒','Profit locked'], TRAIL_EXIT:['🔒','Profit locked'],
  ST_SPOT_SL:['🛑','Stop-loss hit'], SL:['🛑','Stop-loss hit'], SL_HARD:['🛑','Hard stop-loss'],
  ST_FLIP:['↺','Trend flipped'], EMA_EXIT:['〰','EMA exit (old strategy)'],
  EOD_SQUAREOFF:['🕒','Day-end square-off'], MANUAL:['✋','Manual exit'], MANUAL_EXIT:['✋','Manual exit'],
  MANUAL_TARGET:['🎯','Target hit'], TARGET:['🎯','Target hit'], ANGEL_FILL:['✓','Filled today'],
};
const reasonOf = r => REASONS[r] || ['•', (r||'—').replace(/_/g,' ').toLowerCase()];
const fmtDay = ds => new Date(ds+'T00:00:00').toLocaleDateString('en-IN',{day:'numeric',month:'short'});
const fmtDayLong = ds => new Date(ds+'T00:00:00').toLocaleDateString('en-IN',{weekday:'short',day:'numeric',month:'short'});
const mins = (a,b) => { if(!a||!b) return null; const [h1,m1]=a.split(':').map(Number),[h2,m2]=b.split(':').map(Number);
  const d=(h2*60+m2)-(h1*60+m1); return d>=0?d:null; };
const fmtDur = m => m==null ? '' : m<60 ? m+'m' : Math.floor(m/60)+'h '+(m%60)+'m';
const retPct = t => t.pnl_pct!=null ? t.pnl_pct : (t.entry ? (t.exit-t.entry)/t.entry*100 : 0);
let hxTrades = [], hxFilter = 'all', hxShown = 12;

function countUp(el, target, ms=900){
  const start = performance.now();
  const step = now => {
    const p = Math.min(1,(now-start)/ms), v = target*(1-Math.pow(1-p,3));
    el.textContent = (v>=0?'+':'-')+inr(v);
    if(p<1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderHistoryStory(trades, from, to){
  trades = [...trades].sort((a,b)=>(a.date+(a.time||'')).localeCompare(b.date+(b.time||'')));
  hxTrades = trades;
  const show = !!trades.length;
  ['hx-story','hx-tiles','hx-timeline-card'].forEach(id=>document.getElementById(id).classList.toggle('hidden', !show));
  document.getElementById('hx-range').textContent = fmtDay(from)+' – '+fmtDay(to)+(show?'':' · no trades');
  if(!show) return;

  const total = trades.reduce((a,t)=>a+t.pnl,0);
  const wins = trades.filter(t=>t.pnl>0), losses = trades.filter(t=>t.pnl<0);
  const best = trades.reduce((a,t)=>t.pnl>a.pnl?t:a), worst = trades.reduce((a,t)=>t.pnl<a.pnl?t:a);
  const totEl = document.getElementById('hx-total');
  totEl.className = 'text-4xl font-extrabold mt-2 tabular-nums '+cls(total);
  countUp(totEl, total);
  document.getElementById('hx-headline').textContent =
    `${trades.length} trade${trades.length!==1?'s':''} · ${wins.length} win${wins.length!==1?'s':''} · `+
    (best.pnl>0 ? `biggest win ${inr(best.pnl)} on ${fmtDay(best.date)}` : 'no winning trade yet');

  // Streaks (by trade, newest backwards)
  let cur = 0, curWin = null;
  for(let i=trades.length-1;i>=0;i--){ const w = trades[i].pnl>0;
    if(curWin===null){ curWin=w; cur=1; } else if(w===curWin) cur++; else break; }
  let bestRun=0, run=0; trades.forEach(t=>{ run = t.pnl>0 ? run+1 : 0; bestRun=Math.max(bestRun,run); });

  // Badges
  const ce = trades.filter(t=>t.side==='CE'), pe = trades.filter(t=>t.side==='PE');
  const wr = arr => arr.length ? Math.round(arr.filter(t=>t.pnl>0).length/arr.length*100)+'%' : '—';
  const badges = [];
  if(curWin && cur>=2) badges.push(`🔥 ${cur}-win streak`);
  badges.push(`CE ${wr(ce)} win · ${ce.length}`, `PE ${wr(pe)} win · ${pe.length}`);
  if(trades.every(t=>t.paper)) badges.push('📋 Paper trades');
  document.getElementById('hx-badges').innerHTML = badges.map(b=>`<span class="hx-badge">${b}</span>`).join('');

  // Tiles
  const wrPct = Math.round(wins.length/trades.length*100);
  document.getElementById('hx-wr').textContent = wrPct+'%';
  document.getElementById('hx-wl').textContent = wins.length+' W · '+losses.length+' L';
  requestAnimationFrame(()=>document.getElementById('hx-ring').setAttribute('stroke-dasharray', wrPct+' 100'));
  document.getElementById('hx-streak').textContent = curWin===null ? '—' : (curWin ? '🔥 '+cur+' win'+(cur>1?'s':'') : '❄️ '+cur+' loss'+(cur>1?'es':''));
  document.getElementById('hx-streak-best').textContent = 'Best run: '+bestRun+' win'+(bestRun!==1?'s':'');
  const signed = v => (v>=0?'+':'-')+inr(v);
  const bestEl = document.getElementById('hx-best'), worstEl = document.getElementById('hx-worst');
  bestEl.textContent = signed(best.pnl);   bestEl.className  = 'text-xl font-extrabold mt-1 '+cls(best.pnl);
  worstEl.textContent = signed(worst.pnl); worstEl.className = 'text-xl font-extrabold mt-1 '+cls(worst.pnl);
  document.getElementById('hx-best-d').textContent = fmtDay(best.date)+' · '+best.side+' '+(best.strike||'');
  document.getElementById('hx-worst-d').textContent = fmtDay(worst.date)+' · '+worst.side+' '+(worst.strike||'');
  const aw = wins.length ? wins.reduce((a,t)=>a+t.pnl,0)/wins.length : 0;
  const al = losses.length ? Math.abs(losses.reduce((a,t)=>a+t.pnl,0)/losses.length) : 0;
  const mx = Math.max(aw,al)||1;
  document.getElementById('hx-aw').textContent = inr(aw).replace('.00','');
  document.getElementById('hx-al').textContent = inr(al).replace('.00','');
  requestAnimationFrame(()=>{ document.getElementById('hx-aw-bar').style.width=(aw/mx*100)+'%';
                              document.getElementById('hx-al-bar').style.width=(al/mx*100)+'%'; });
  const gw = wins.reduce((a,t)=>a+t.pnl,0), gl = Math.abs(losses.reduce((a,t)=>a+t.pnl,0));
  const pf = gl ? gw/gl : null;
  const pfEl = document.getElementById('hx-pf');
  pfEl.textContent = pf==null ? (gw?'∞':'—') : pf.toFixed(2);
  pfEl.className = 'text-xl font-extrabold mt-1 '+(pf==null||pf>=1?'text-green-600':'text-red-500');

  drawEquityCurve(trades);
  hxShown = 12; renderTimeline();
}

function drawEquityCurve(trades){
  const svg = document.getElementById('hx-curve');
  const W = svg.clientWidth || 800, H = 190, P = {l:8, r:8, t:14, b:14};
  let cum = 0; const pts = [{v:0}].concat(trades.map(t=>({v:(cum+=t.pnl), t})));
  const vals = pts.map(p=>p.v), lo = Math.min(0,...vals), hi = Math.max(0,...vals), span = (hi-lo)||1;
  const x = i => P.l + i*(W-P.l-P.r)/Math.max(1,pts.length-1);
  const y = v => P.t + (hi-v)*(H-P.t-P.b)/span;
  const line = pts.map((p,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(p.v).toFixed(1)).join(' ');
  const area = line+` L${x(pts.length-1).toFixed(1)} ${y(lo)} L${x(0)} ${y(lo)} Z`;
  const up = cum>=0, col = up?'#16a34a':'#ef4444';
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svg.innerHTML = `
    <defs><linearGradient id="hxg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${col}" stop-opacity=".22"/><stop offset="100%" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    <line x1="${P.l}" x2="${W-P.r}" y1="${y(0)}" y2="${y(0)}" stroke="#d6d1ea" stroke-dasharray="4 4"/>
    <path d="${area}" fill="url(#hxg)"/>
    <path id="hx-line" d="${line}" fill="none" stroke="${col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    ${pts.slice(1).map((p,i)=>`<circle cx="${x(i+1)}" cy="${y(p.v)}" r="3.5" fill="${p.t.pnl>=0?'#22c55e':'#ef4444'}" stroke="#fff" stroke-width="1.5"/>
      <circle class="hx-hit" data-i="${i}" cx="${x(i+1)}" cy="${y(p.v)}" r="11" fill="transparent"/>`).join('')}`;
  try {   // draw-in animation; getTotalLength can throw while the tab is hidden
    const path = document.getElementById('hx-line'), len = path.getTotalLength();
    path.style.strokeDasharray = len; path.style.strokeDashoffset = len;
    path.getBoundingClientRect(); path.style.transition = 'stroke-dashoffset 1.2s ease'; path.style.strokeDashoffset = 0;
  } catch(e) {}
  const tip = document.getElementById('hx-tip');
  svg.querySelectorAll('.hx-hit').forEach(c=>{
    c.addEventListener('mouseenter',()=>{
      const p = pts[+c.dataset.i+1], t = p.t, r = svg.getBoundingClientRect(), s = r.width/W;
      tip.innerHTML = `<b>${fmtDayLong(t.date)}</b> · ${t.side} ${t.strike||''}<br>`+
        `<span style="color:${t.pnl>=0?'#86efac':'#fca5a5'}">${t.pnl>=0?'+':'-'}${inr(t.pnl)}</span> · total ${p.v>=0?'+':'-'}${inr(p.v)}`;
      tip.style.left = (+c.getAttribute('cx')*s)+'px'; tip.style.top = (+c.getAttribute('cy')*(r.height/H))+'px';
      tip.classList.remove('hidden');
    });
    c.addEventListener('mouseleave',()=>tip.classList.add('hidden'));
  });
}

function renderTimeline(){
  const f = hxFilter;
  const list = [...hxTrades].reverse().filter(t => f==='all' ? true : f==='win' ? t.pnl>0 : f==='loss' ? t.pnl<0 : t.side===f);
  const maxRet = Math.max(30, ...list.map(t=>Math.abs(retPct(t))));
  document.getElementById('hx-timeline').innerHTML = list.slice(0, hxShown).map((t,i)=>{
    const ret = retPct(t), win = t.pnl>=0, [ico,label] = reasonOf(t.reason);
    const w = Math.min(50, Math.abs(ret)/maxRet*50);
    const bar = win ? `left:50%;width:${w}%;background:#22c55e` : `right:50%;width:${w}%;background:#f87171`;
    const dur = mins(t.time, t.exit_time);
    return `<div class="hx-trade" style="animation-delay:${Math.min(i,12)*35}ms" onclick="this.classList.toggle('open')">
      <div class="edge" style="background:${win?'#22c55e':'#f87171'}"></div>
      <div class="min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-sm font-bold text-gray-900">${fmtDayLong(t.date)}</span>
          <span class="text-xs px-2 py-0.5 rounded-full font-bold ${t.side==='CE'?'bg-blue-100 text-blue-700':'bg-amber-100 text-amber-700'}">${t.side} ${t.strike||''}</span>
          <span class="text-xs text-gray-400">${t.time||''} → ${t.exit_time||''}${dur!=null?' · '+fmtDur(dur):''}</span>
          ${t.paper?'<span class="text-xs bg-blue-50 text-blue-600 px-1.5 rounded">Paper</span>':''}
        </div>
        <div class="flex items-center gap-3 mt-2">
          <div class="hx-ret flex-1"><span style="${bar}"></span></div>
          <span class="text-xs text-gray-500 whitespace-nowrap">${ico} ${label}</span>
        </div>
      </div>
      <div class="text-right">
        <p class="text-base font-extrabold ${cls(t.pnl)} tabular-nums">${win?'+':'-'}${inr(t.pnl)}</p>
        <p class="text-xs font-semibold ${cls(t.pnl)}">${ret>=0?'+':''}${ret.toFixed(1)}%</p>
      </div>
      <div class="hx-details grid-cols-2 md:grid-cols-5 gap-2 pt-2 mt-1 border-t border-violet-50">
        <span>Option: <b>₹${(t.entry||0).toFixed(2)} → ₹${(t.exit||0).toFixed(2)}</b></span>
        <span>Nifty at entry: <b>${t.entry_spot?'₹'+Number(t.entry_spot).toFixed(0):'—'}</b></span>
        <span>Qty: <b>${t.qty||'—'}${t.lots?' ('+t.lots+' lot'+(t.lots>1?'s':'')+')':''}</b></span>
        <span>Capital: <b>${t.capital?inr(t.capital):'—'}</b></span>
        <span class="truncate">${t.symbol||''}</span>
      </div>
    </div>`;
  }).join('') || '<p class="text-sm text-gray-400 text-center py-6">No trades match this filter.</p>';
  const more = document.getElementById('hx-more');
  more.classList.toggle('hidden', list.length <= hxShown);
  more.textContent = `Show more (${list.length-hxShown} left)`;
}
document.getElementById('hx-filters').addEventListener('click', e=>{
  const b = e.target.closest('.hx-chip'); if(!b) return;
  document.querySelectorAll('#hx-filters .hx-chip').forEach(c=>c.classList.toggle('active', c===b));
  hxFilter = b.dataset.f; hxShown = 12; renderTimeline();
});
document.getElementById('hx-more').addEventListener('click', ()=>{ hxShown += 12; renderTimeline(); });
window.addEventListener('resize', ()=>{ if(hxTrades.length && !document.getElementById('hx-story').classList.contains('hidden')) drawEquityCurve(hxTrades); });

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
    if(DEMO) s = demoState(s);
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

    // Paper badge
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
    bsLabel.textContent = 'Artha Trend';
    if(mkt.st_trend === 1)       { bsInd.textContent = 'Uptrend ▲';   bsInd.className = 'font-semibold text-green-600 text-right'; }
    else if(mkt.st_trend === -1) { bsInd.textContent = 'Downtrend ▼'; bsInd.className = 'font-semibold text-red-600 text-right'; }
    else                         { bsInd.textContent = '—';           bsInd.className = 'font-semibold text-gray-700 text-right'; }
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

      // Manual Target % / Carry Overnight — only reflect server state when the
      // user isn't mid-edit (avoids clobbering a change they just made before
      // the next poll lands).
      const cfg = s.config||{};
      const mtEl = document.getElementById('live-manual-target');
      const coEl = document.getElementById('live-carry-overnight');
      if(document.activeElement !== mtEl) mtEl.value = cfg.manual_target_pct ? String(cfg.manual_target_pct) : '0';
      if(document.activeElement !== coEl) coEl.checked = !!cfg.carry_overnight;

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

    renderScanner(s);
    renderSession(s);
    renderCockpit(s);
    renderDayGauge(s);
  }).catch(e=>console.error('refreshLive', e));
}
setInterval(refreshLive, 5000); refreshLive();

// ── Artha Scanner panel ──────────────────────────────────────────
// Visualises the bot's real entry checks (data → trend → flip → pre-move →
// risk limits) from /api/live-state; nothing here makes trading decisions.
let scanMode = 'idle', lastPosActive = null, tickerLines = [], tickerIdx = 0;
const SCAN_TITLES = {
  idle:     ['IDLE',     'Scanner idle'],
  closed:   ['CLOSED',   'Market closed'],
  scanning: ['SCANNING', 'Scanning the market'],
  skipped:  ['FILTERED', 'Signal found — skipped'],
  found:    ['SIGNAL',   'Signal found — executing'],
  halted:   ['DONE',     'Done for today'],
};
function setStep(id, state, text){
  const li = document.getElementById('step-'+id);
  li.className = 'scan-step'+(state?' '+state:'');
  document.getElementById('val-'+id).textContent = text;
}
// Preview only: ?demo=scanning|skipped|found|executed|position|halted fakes the
// live state client-side (with a fake 11:05 clock) so the Live tab can be
// checked outside market hours. Never used unless the URL asks for it.
const DEMO = new URLSearchParams(location.search).get('demo');
const nowHM = () => DEMO ? '11:05' : new Date().toTimeString().slice(0,5);
let demoTick = 0, demoPeak = 0;
function demoState(s){
  const d = JSON.parse(JSON.stringify(s));
  const now = nowHM();
  d.status='PAPER'; d.connected=true; d.paper_mode=true;
  d.config={...(d.config||{}), max_trades:3, lots:5, spot_sl:50, squareoff_time:'15:15',
            trail_steps:[{trigger:.15,floor:0},{trigger:.25,floor:.10}], trail_giveback:null, trail_lock_trigger:.32};
  d.market={...(d.market||{}), market_open:true, nifty_ltp:25382.45, st_trend:1, st_prev:1, pre_move:4.2,
            max_pre_move:15, candle_close:now, no_new_trade_time:'14:30', max_daily_loss:-8000, daily_profit_target:6000};
  d.signal={...(d.signal||{}), signal:null, filter_reason:'Artha Trend up — no flip',
            next_check:new Date(Date.now()+180000).toTimeString().slice(0,5)};
  d.daily_stats={...(d.daily_stats||{}), trade_count:0, pnl:0};
  d.position={...(d.position||{}), active:false};
  d.trades=[];
  d.activity=[{time:now, kind:'scan', text:'Market scan completed — no signal'}];
  const openPos = () => {
    const t = Date.now()/1000, ep = 198.6;
    const ltp = +(ep*(1.21 + 0.09*Math.sin(t/9) + 0.03*Math.sin(t/2.3))).toFixed(2);
    demoPeak = Math.max(demoPeak, ltp);
    d.position={...d.position, active:true, side:'CE', strike:25350, symbol:'NIFTY27OCT2625350CE', qty:325,
      entry_price:ep, entry_spot:25352.10, entry_time:'10:48', live_ltp:ltp, trail_high:demoPeak,
      live_pnl:+((ltp-ep)*325).toFixed(2), stop_desc:'Spot 25302 (50pt)', target_desc:'Exit on trend flip'};
    d.market.nifty_ltp = +(25352.10 + (ltp-ep)*2.1).toFixed(2);
    d.daily_stats.trade_count = 1;
  };
  if(DEMO==='skipped'){ d.market.st_prev=-1; d.market.pre_move=38.4;
    d.signal.filter_reason='Skipped — Nifty already moved +38.4pt in the entry candle (>15pt cap)'; }
  if(DEMO==='found'){ d.market.st_prev=-1; d.signal.signal='BUY_CE'; d.signal.filter_reason=null; }
  if(DEMO==='position') openPos();
  if(DEMO==='executed'){            // scanning → found → executed, looping every 20s
    const phase = demoTick++ % 4;
    if(phase===1){ d.market.st_prev=-1; d.signal.signal='BUY_CE'; d.signal.filter_reason=null; }
    if(phase>=2) openPos();
  }
  if(DEMO==='halted'){ d.daily_stats.trade_count=1; d.daily_stats.pnl=25203.75;
    d.trades=[{date:'', time:'09:20', exit_time:'09:55', side:'CE', strike:22700, symbol:'NIFTY27OCT2622700CE',
               entry:235, exit:312.55, qty:325, pnl:25203.75, reason:'ST_TRAIL_EXIT', paper:true}];
    d.signal.filter_reason='Daily profit target reached (₹25,203.75 ≥ ₹6,000.00) — locked in, no new entries'; }
  return d;
}
function signalConsumed(s){
  const sigT = ((s.signal||{}).time||'').slice(0,5);
  const lastExit = (s.trades||[]).map(t=>t.exit_time||'').sort().pop()||'';
  return !!(sigT && lastExit && lastExit >= sigT);
}
function renderScanner(s){
  const pos = s.position||{}, sig = s.signal||{}, mkt = s.market||{}, st = s.daily_stats||{}, cfg = s.config||{};
  const status = s.status||'STOPPED';
  const running = status==='LIVE'||status==='PAPER';
  const reason = sig.filter_reason||'';
  const trades = st.trade_count||0, maxT = cfg.max_trades||0, pnl = st.pnl||0;
  const flip = mkt.st_trend!=null && mkt.st_prev!=null && mkt.st_trend!==mkt.st_prev;
  const flipSide = mkt.st_trend===1 ? 'BUY CE' : 'BUY PE';
  const halted = /Daily (loss cap|profit target)/.test(reason) || (maxT && trades>=maxT);

  // "Trade executed" banner on the not-active → active transition
  if(lastPosActive===false && pos.active){
    document.getElementById('exec-detail').textContent =
      [pos.side, pos.strike||'', pos.qty?'· '+pos.qty+' qty':'', pos.entry_price?'@ ₹'+Number(pos.entry_price).toFixed(2):'']
        .join(' ').replace(/\s+/g,' ');
    const b = document.getElementById('exec-banner');
    b.classList.remove('hidden'); b.style.animation='none'; void b.offsetWidth; b.style.animation='';
    setTimeout(()=>b.classList.add('hidden'), 4300);
  }
  lastPosActive = !!pos.active;

  let mode, sub;
  if(!running && status!=='MONITORING'){ mode='idle'; sub='Start the bot to begin scanning.'; }
  else if(!mkt.market_open){ mode='closed'; sub='Scanning resumes at 09:15 on the next trading day.'; }
  else if(halted){ mode='halted';
    sub = /profit target/.test(reason) ? 'Daily profit target reached — gains locked in.'
        : /loss cap/.test(reason) ? 'Daily loss limit reached — protecting capital.'
        : 'Maximum trades for today taken.'; }
  // sig.signal is only refreshed on the next scan, so ignore it once a trade
  // has closed since that scan (otherwise "executing" lingers after the exit).
  else if(sig.signal && !pos.active && !signalConsumed(s)){
    if(s.last_error){ mode='skipped'; sub='Entry failed — '+s.last_error; }
    else { mode='found'; sub=sig.signal.replace('_',' ')+' — placing order…'; } }
  else if(/^Skipped/.test(reason)){ mode='skipped'; sub=reason.replace(/^Skipped — /,''); }
  else if(/cooldown/i.test(reason)){ mode='skipped'; sub='Cooling down after a loss — '+reason.replace(/^.*\(/,'(').replace(/\)$/,')'); }
  else { mode='scanning'; sub = /Warming/.test(reason) ? 'Warming up — loading candles.'
        : 'Watching for an Artha Trend flip on the 5-minute chart.'; }
  scanMode = mode;
  const card = document.getElementById('pos-empty');
  card.dataset.mode = mode;
  document.getElementById('scan-chip').textContent = SCAN_TITLES[mode][0];
  document.getElementById('scan-title').textContent = SCAN_TITLES[mode][1];
  document.getElementById('scan-sub').textContent = sub;
  document.getElementById('radar-icon').textContent =
    mode==='found' ? '✓' : mode==='halted' ? '■' : mode==='skipped' ? '!' : mkt.st_trend===1 ? '▲' : mkt.st_trend===-1 ? '▼' : '◉';

  // Checklist
  const live = running && mkt.market_open;
  if(!live){ ['data','trend','flip','move','risk'].forEach(k=>setStep(k,'', '—')); }
  else {
    setStep('data', s.connected&&mkt.nifty_ltp ? 'ok':'wait',
      s.connected&&mkt.nifty_ltp ? 'Nifty ₹'+Number(mkt.nifty_ltp).toLocaleString('en-IN',{maximumFractionDigits:2})
        +(mkt.candle_close?' · candle '+mkt.candle_close:'') : 'connecting…');
    setStep('trend', mkt.st_trend==null?'wait':'ok',
      mkt.st_trend===1?'Uptrend ▲':mkt.st_trend===-1?'Downtrend ▼':'warming up…');
    setStep('flip', flip?'hit':'wait', flip ? 'FLIP → '+flipSide : 'watching… no flip yet');
    const pm = mkt.pre_move, cap = mkt.max_pre_move;
    if(pm==null) setStep('move', flip?'ok':'', 'first candle · n/a');
    else {
      const over = cap!=null && Math.abs(pm)>cap;
      setStep('move', over ? (flip?'bad':'warn') : 'ok',
        (pm>=0?'+':'')+pm.toFixed(1)+' pt '+(over?'> ':'≤ ')+cap);
    }
    const late = nowHM() > (mkt.no_new_trade_time||'14:30');
    setStep('risk', halted?'bad': late?'warn':'ok',
      halted ? 'limit reached' : late ? 'no new entries after '+(mkt.no_new_trade_time||'14:30')
        : trades+'/'+maxT+' trades · '+(pnl>=0?'+':'')+inr(pnl));
  }

  // Ticker: latest real activity first, then the scan pipeline
  const latest = (s.activity||[])[0];
  tickerLines = [];
  if(latest) tickerLines.push(latest.time+'  '+latest.text);
  if(mode==='scanning') tickerLines.push('Fetching NIFTY 5-minute candles…','Computing Artha Trend (10, 3)…',
    'Checking for a trend flip…','Checking pre-move filter (≤ '+(mkt.max_pre_move??15)+' pt)…','Verifying risk limits…');
  if(mode==='found') tickerLines = ['Signal confirmed: '+(sig.signal||'').replace('_',' '),'Selecting ATM strike & expiry…','Placing order…'];
  if(!tickerLines.length) tickerLines = [sub];
}
function tickTicker(){
  const el = document.getElementById('scan-ticker');
  if(!el || !tickerLines.length) return;
  tickerIdx = (tickerIdx+1) % tickerLines.length;
  el.textContent = tickerLines[tickerIdx];
}
setInterval(tickTicker, 2200);
function spawnBlip(){
  if(!['scanning','skipped','found'].includes(scanMode) || document.hidden) return;
  const box = document.getElementById('radar-blips');
  const a = Math.random()*Math.PI*2, r = 18+Math.random()*58;
  const b = document.createElement('span');
  b.className='blip'; b.style.left=(84+Math.cos(a)*r)+'px'; b.style.top=(84+Math.sin(a)*r)+'px';
  box.appendChild(b); setTimeout(()=>b.remove(), 2500);
}
setInterval(spawnBlip, 900);

// ── Market session strip ─────────────────────────────────────────
const hm2m = hm => { const [h,m] = (hm||'0:0').split(':').map(Number); return h*60+m; };
const S_OPEN = hm2m('09:15'), S_CLOSE = hm2m('15:30');
const sessPct = hm => Math.max(0, Math.min(100, (hm2m(hm)-S_OPEN)/(S_CLOSE-S_OPEN)*100));
function renderSession(s){
  const mkt = s.market||{}, cfg = s.config||{}, pos = s.position||{};
  const cut = mkt.no_new_trade_time||'14:30', sq = cfg.squareoff_time||'15:15';
  const now = nowHM(), nm = hm2m(now);
  const day = new Date().getDay(), weekday = DEMO || (day>=1 && day<=5);
  const open = DEMO ? true : (mkt.market_open ?? (weekday && nm>=S_OPEN && nm<=S_CLOSE));
  const nowEl = document.getElementById('sess-now'), lab = document.getElementById('sess-label');
  document.getElementById('sess-nonew').style.cssText = `left:${sessPct(cut)}%;right:0`;
  let fill;
  if(open){
    fill = sessPct(now);
    nowEl.classList.remove('hidden'); nowEl.style.left = fill+'%';
    const left = S_CLOSE-nm, h = Math.floor(left/60), m = left%60;
    lab.textContent = nm < hm2m(cut) ? `● Open · ${h?h+'h ':''}${m}m to close · new entries until ${cut}`
                    : nm < hm2m(sq) ? `● Open · no new entries · square-off ${sq}` : '● Closing auction';
    lab.className = 'text-xs font-semibold '+(nm < hm2m(cut) ? 'text-green-600' : 'text-amber-600');
  } else {
    fill = (weekday && nm > S_CLOSE) ? 100 : 0;
    nowEl.classList.add('hidden');
    const d = new Date(); d.setHours(9,15,0,0);
    if(!(weekday && nm < S_OPEN)) do { d.setDate(d.getDate()+1); } while(d.getDay()===0 || d.getDay()===6);
    const ms = d - new Date(), hrs = Math.floor(ms/3.6e6), mins = Math.floor(ms%3.6e6/6e4);
    lab.textContent = `Market closed · opens ${d.toLocaleDateString('en-IN',{weekday:'short'})} 09:15 (in ${hrs>=24?Math.floor(hrs/24)+'d ':''}${hrs%24}h ${mins}m)`;
    lab.className = 'text-xs font-semibold text-gray-400';
  }
  document.getElementById('sess-fill').style.width = fill+'%';
  // Entry / exit dots for today's trades (+ the open position's entry)
  const marks = [];
  (s.trades||[]).forEach(t=>{
    if(t.time) marks.push({at:t.time, bg:t.side==='CE'?'#2563eb':'#d97706', txt:'↗', tip:`${t.side} ${t.strike||''} entry ${t.time}`});
    if(t.exit_time) marks.push({at:t.exit_time, bg:t.pnl>=0?'#16a34a':'#ef4444', txt:'✓', tip:`exit ${t.exit_time} · ${t.pnl>=0?'+':'-'}${inr(t.pnl)}`});
  });
  if(pos.active && pos.entry_time) marks.push({at:pos.entry_time.slice(0,5), bg:pos.side==='CE'?'#2563eb':'#d97706', txt:'↗', tip:`${pos.side} open since ${pos.entry_time}`});
  document.getElementById('sess-marks').innerHTML = marks.map(m=>
    `<span class="sess-mark" style="left:${sessPct(m.at)}%;background:${m.bg}" title="${m.tip}">${m.txt}</span>`).join('');
  document.getElementById('sess-ticks').innerHTML = [['09:15','09:15'],['11:00','11:00'],['13:00','13:00'],[cut,cut+' cutoff'],[sq,sq+' sq-off']]
    .map(([t,l])=>`<span class="sess-tick" style="left:${sessPct(t)}%">${l}</span>`).join('');
}

// ── Trade cockpit (open position) ────────────────────────────────
function renderCockpit(s){
  const pos = s.position||{}, cfg = s.config||{}, mkt = s.market||{};
  if(!pos.active || !pos.entry_price) return;
  const ep = pos.entry_price, ltp = pos.live_ltp || ep;
  const cur = (ltp-ep)/ep, peak = Math.max(cur, ((pos.trail_high||ep)-ep)/ep);
  const steps = cfg.trail_steps || [{trigger:.15,floor:0},{trigger:.25,floor:.10}];
  let floor = null; steps.forEach(st=>{ if(peak>=st.trigger) floor = st.floor; });
  if(cfg.trail_giveback!=null && peak >= (cfg.trail_lock_trigger??.32)) floor = Math.max(floor??-1, peak-cfg.trail_giveback);

  // Ladder
  const lo = Math.min(-0.20, cur-0.05), hi = Math.max(0.40, peak+0.08, cur+0.08);
  const P = v => (v-lo)/(hi-lo)*100;
  const t1 = steps[0]?.trigger ?? .15, t2 = steps[1]?.trigger ?? .25;
  document.getElementById('lad-zones').innerHTML =
    `<div style="position:absolute;inset:0;border-radius:9999px;overflow:hidden">`+
    [[lo,0,'#fee2e2'],[0,t1,'#f1effa'],[t1,t2,'#dcfce7'],[t2,hi,'#bbf7d0']].map(([a,b,c])=>
      `<div class="lad-zone" style="left:${P(a)}%;width:${P(b)-P(a)}%;background:${c}"></div>`).join('')+`</div>`;
  const now = document.getElementById('lad-now');
  now.style.left = P(cur)+'%'; now.style.borderColor = cur>=0 ? '#16a34a' : '#ef4444';
  document.getElementById('lad-now-lbl').textContent = (cur>=0?'+':'')+(cur*100).toFixed(1)+'%';
  const pk = document.getElementById('lad-peak');
  pk.classList.toggle('hidden', peak<=0.005); pk.style.left = P(peak)+'%';
  const fl = document.getElementById('lad-floor');
  fl.classList.toggle('hidden', floor==null); if(floor!=null) fl.style.left = P(floor)+'%';
  document.getElementById('lad-ticks').innerHTML = [[0,'entry'],[t1,'+'+Math.round(t1*100)+'% → lock 0%'],[t2,'+'+Math.round(t2*100)+'% → lock +10%']]
    .map(([v,l])=>`<span class="sess-tick" style="left:${P(v)}%">${l}</span>`).join('');
  const st = document.getElementById('lad-status');
  if(floor==null){ st.textContent = `Next: +${Math.round(t1*100)}% locks breakeven (${((t1-cur)*100).toFixed(1)}% away)`; st.className='text-xs font-bold text-gray-500'; }
  else { st.textContent = `🔒 ${floor<=0.0001 ? 'Breakeven' : '+'+(floor*100).toFixed(0)+'%'} locked · peak +${(peak*100).toFixed(1)}%`; st.className='text-xs font-bold text-green-600'; }

  // Nifty vs spot stop-loss
  const es = pos.entry_spot, n = mkt.nifty_ltp, sl = cfg.spot_sl || 50;
  if(es && n){
    const fav = pos.side==='CE' ? n-es : es-n;
    document.getElementById('sl-dot').style.left = (50 + Math.max(-1,Math.min(1,fav/sl))*50)+'%';
    const away = sl + fav;
    const d = document.getElementById('sl-dist');
    d.textContent = away<=0 ? 'Stop reached' : `${away.toFixed(0)} pts from stop`;
    d.className = 'text-xs font-bold '+(away < sl*0.4 ? 'text-red-500' : away < sl ? 'text-amber-600' : 'text-green-600');
    const stop = pos.side==='CE' ? es-sl : es+sl;
    document.getElementById('sl-lo').textContent = 'stop '+stop.toFixed(0);
    document.getElementById('sl-mid').textContent = 'entry '+es.toFixed(0);
    document.getElementById('sl-hi').textContent = (pos.side==='CE'?'+':'−')+sl+' pts';
  }

  // Time in trade
  if(pos.entry_time){ const m = hm2m(nowHM()) - hm2m(pos.entry_time.slice(0,5));
    document.getElementById('pos-age').textContent = m>=0 ? fmtDur(m) || '0m' : '—'; }

  // Option price line since entry (kept per trade in sessionStorage)
  const key = 'spark:'+(pos.symbol||'')+(pos.entry_time||'');
  let pts = []; try { pts = JSON.parse(sessionStorage.getItem(key)||'[]'); } catch(e){}
  pts.push(ltp); if(pts.length>500) pts = pts.slice(-500);
  try { sessionStorage.setItem(key, JSON.stringify(pts)); } catch(e){}
  const svg = document.getElementById('pos-spark'), W = svg.clientWidth||300, H = 44;
  const all = pts.concat([ep]), mn = Math.min(...all), mx = Math.max(...all), sp = (mx-mn)||1;
  const X = i => pts.length>1 ? i*(W-4)/(pts.length-1)+2 : W/2, Y = v => 4+(mx-v)*(H-8)/sp;
  const col = ltp>=ep ? '#16a34a' : '#ef4444';
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svg.innerHTML = `<line x1="0" x2="${W}" y1="${Y(ep)}" y2="${Y(ep)}" stroke="#d6d1ea" stroke-dasharray="3 3"/>`+
    `<polyline fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" points="${pts.map((v,i)=>X(i).toFixed(1)+','+Y(v).toFixed(1)).join(' ')}"/>`+
    `<circle cx="${X(pts.length-1)}" cy="${Y(ltp)}" r="3.5" fill="${col}"/>`;
}

// ── Daily limit gauge (bot card) ─────────────────────────────────
function renderDayGauge(s){
  const mkt = s.market||{}, pnl = (s.daily_stats||{}).pnl||0;
  const lo = mkt.max_daily_loss ?? -8000, hi = mkt.daily_profit_target ?? 6000;
  const zero = Math.abs(lo)/(Math.abs(lo)+hi)*100;
  const at = Math.max(0, Math.min(100, (pnl-lo)/(hi-lo)*100));
  document.querySelector('.dgauge-zero').style.left = zero+'%';
  const f = document.getElementById('dg-fill');
  f.style.left = Math.min(zero,at)+'%'; f.style.width = Math.abs(at-zero)+'%';
  f.style.background = pnl>=0 ? '#22c55e' : '#ef4444';
  document.getElementById('dg-lo').textContent = '−'+inr(lo).replace('.00','')+' limit';
  document.getElementById('dg-hi').textContent = '+'+inr(hi).replace('.00','')+' target';
  document.getElementById('dg-note').textContent =
    pnl>=hi ? '🎯 Target reached' : pnl<=lo ? '🛑 Loss limit hit' :
    pnl>0 ? inr(hi-pnl).replace('.00','')+' to target' : pnl<0 ? inr(pnl-lo).replace('.00','')+' before limit' : '';
}

// ── Bot Status countdown (ticks every second between 5s polls) ──
function updateCountdown(){
  const el = document.getElementById('bs-countdown');
  const scanEl = document.getElementById('scan-next');
  const set = t => { el.textContent = t; if(scanEl) scanEl.textContent = (scanMode==='scanning'||scanMode==='skipped') ? t : '—'; };
  if(!nextCheckStr){ set('—'); return; }
  const now = new Date();
  const [hh,mm] = nextCheckStr.split(':').map(Number);
  const target = new Date(now); target.setHours(hh,mm,0,0);
  let diff = Math.floor((target-now)/1000);
  if(diff < 0){ set('Checking…'); return; }
  const m = Math.floor(diff/60), sec = diff%60;
  set(String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0'));
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

// Month-to-date summary line + win-rate pills (refresh every 60s)
function refreshPerfMonth(){
  const to   = new Date();
  const from = new Date(to.getFullYear(), to.getMonth(), 1);   // 1st of current month
  // Local-date formatting (not toISOString, which shifts by the UTC offset
  // and would send e.g. IST midnight as the previous day) so the query
  // range and each chart point line up with real calendar days.
  const fmt  = dt => `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
  const monthName = to.toLocaleDateString('en-IN',{month:'long'});

  fetch(`/api/trade-history?from=${fmt(from)}&to=${fmt(to)}`).then(r=>r.json()).then(d=>{
    const s      = d.summary || {};
    const wins   = s.wins   || 0;
    const losses = s.losses || 0;
    const total  = s.total_trades || 0;
    const winRate = total ? s.win_rate : 0;

    document.getElementById('perf14-month').textContent = monthName;
    const pnlEl = document.getElementById('perf14-pnl');
    pnlEl.textContent = total ? (s.total_pnl>=0?'+':'')+inr(s.total_pnl) : 'No trades yet';
    pnlEl.className   = 'text-base font-extrabold '+(total?cls(s.total_pnl):'text-gray-300');
    document.getElementById('perf14-wl').textContent = total
      ? `${total} trade${total===1?'':'s'} · ${wins}W ${losses}L · ${winRate}% win rate` : '';

    // Month-to-date headline pills
    const wr14a = document.getElementById('hero-wr14');
    const wr14b = document.getElementById('mini-wr14');
    wr14a.textContent = total ? winRate+'%' : '—';
    wr14b.textContent = total ? winRate+'%' : '—';

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
  closeModal();
  fetch('/api/start-trading',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({max_trades,lots,paper})
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
function setManualTarget(){
  const pct = parseFloat(document.getElementById('live-manual-target').value) || 0;
  const statusEl = document.getElementById('live-target-status');
  statusEl.textContent = 'Saving…';
  fetch('/api/manual-target',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pct})
  }).then(r=>r.json()).then(d=>{
    statusEl.textContent = d.error ? 'Error: '+d.error : (pct ? 'Target set to '+pct+'%' : 'Manual target off');
    setTimeout(()=>{ if(statusEl.textContent.indexOf('Error')!==0) statusEl.textContent=''; },3000);
  }).catch(e=>{ statusEl.textContent='Error: '+e; });
}
function setCarryOvernight(){
  const enabled = document.getElementById('live-carry-overnight').checked;
  const statusEl = document.getElementById('live-target-status');
  statusEl.textContent = 'Saving…';
  fetch('/api/carry-overnight',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled})
  }).then(r=>r.json()).then(d=>{
    statusEl.textContent = d.error ? 'Error: '+d.error : (enabled ? 'Will carry overnight' : 'Overnight carry off');
    setTimeout(()=>{ if(statusEl.textContent.indexOf('Error')!==0) statusEl.textContent=''; },3000);
  }).catch(e=>{ statusEl.textContent='Error: '+e; });
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
