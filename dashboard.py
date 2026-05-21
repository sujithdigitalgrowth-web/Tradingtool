import json, os, threading
from flask import Flask, render_template_string, jsonify, request
from datetime import datetime, date, timedelta

app = Flask(__name__)

STATE_FILE          = "logs/state.json"
RANGE_CACHE_FILE    = "logs/range_cache.json"
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
        get_trader().login()
    except Exception as e:
        pass   # Dashboard still works without Angel One connection


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
    return {"max_trades": int(cfg.get("max_trades", 2)),
            "lots":       int(cfg.get("lots", 1))}

def _save_trading_config(cfg):
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

@app.route("/api/start-trading", methods=["POST"])
def api_start_trading():
    body       = request.json or {}
    max_trades = max(1, min(10, int(body.get("max_trades", 2))))
    lots       = max(1, min(20, int(body.get("lots", 1))))
    try:
        t = get_trader()
        t.start(max_trades=max_trades, lots=lots)
        _save_trading_config({"max_trades": max_trades, "lots": lots})
        return jsonify({"status": "started", "max_trades": max_trades, "lots": lots})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stop-trading", methods=["POST"])
def api_stop_trading():
    try:
        get_trader().stop()
        return jsonify({"status": "stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/force-exit", methods=["POST"])
def api_force_exit():
    try:
        get_trader().force_exit()
        return jsonify({"status": "exited"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── API: Trading config ───────────────────────────────────────────

@app.route("/api/trading-config", methods=["GET"])
def api_get_config():
    return jsonify(_get_trading_config())

@app.route("/api/trading-config", methods=["POST"])
def api_set_config():
    body       = request.json or {}
    max_trades = max(1, min(10, int(body.get("max_trades", 2))))
    lots       = max(1, min(20, int(body.get("lots", 1))))
    cfg = {"max_trades": max_trades, "lots": lots}
    _save_trading_config(cfg)
    return jsonify(cfg)

# ── API: Backtest (existing) ──────────────────────────────────────

@app.route("/api/state")
def api_state():
    return jsonify(load_json(STATE_FILE))

@app.route("/api/run-today", methods=["POST"])
def api_run_today():
    try:
        import backtest
        from backtest import fetch_range_data_v2, simulate_day, INITIAL_BALANCE
        cfg = _get_trading_config()
        backtest.V2_MAX_TRADES = cfg["max_trades"]
        backtest.QTY           = cfg["lots"] * backtest.LOT_SIZE
        QTY = backtest.QTY
        today = date.today()
        result, target = None, None
        for delta in range(6):
            candidate = today - timedelta(days=delta)
            if candidate.weekday() >= 5:
                continue
            try:
                start = candidate - timedelta(days=40)
                df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_v2(start, candidate)
                r = simulate_day(candidate, df_5m, df_1d,
                                 df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
                if r:
                    result, target = r, candidate
                    break
            except Exception:
                continue
        if not result:
            return jsonify({"error": "No data available"}), 404
        state = {
            "backtest"       : True,
            "backtest_date"  : target.strftime("%d %b %Y"),
            "initial_balance": INITIAL_BALANCE,
            "final_balance"  : result["final_balance"],
            "market"         : result["market"],
            "gann"           : {},
            "risk"           : {"daily_pnl"  : result["daily_pnl"],
                                "trade_count": result["trade_count"],
                                "trades"     : result["trades"]},
            "position"       : {"active": False, "qty": QTY},
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
        return jsonify({"status": "ok", "date": str(target), "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/range-cache")
def api_range_cache():
    return jsonify(load_json(RANGE_CACHE_FILE))

@app.route("/api/run-range", methods=["POST"])
def api_run_range():
    try:
        body  = request.json
        start = date.fromisoformat(body["start"])
        end   = date.fromisoformat(body["end"])
        if (end - start).days > 90:
            return jsonify({"error": "Range too large — max 90 days"}), 400
        import backtest
        from backtest import run_range
        cfg = _get_trading_config()
        backtest.V2_MAX_TRADES = cfg["max_trades"]
        backtest.QTY           = cfg["lots"] * backtest.LOT_SIZE
        results = run_range(start, end)
        cache = {
            "start": str(start), "end": str(end),
            "label": "V2 v2.3 — Live Strategy Parameters",
            "generated": datetime.now().strftime("%d %b %Y %H:%M"),
            "results": results,
        }
        with open(RANGE_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2, default=str)
        return jsonify(cache)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Template ──────────────────────────────────────────────────────

TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Artha Trading Bot</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body{background:#f1f5f9;color:#1e293b;font-family:'Courier New',monospace}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px}
  .tab-a{border-bottom:2px solid #1e293b;color:#1e293b}
  .tab-i{border-bottom:2px solid transparent;color:#94a3b8}
  .tab-i:hover{color:#475569}
  .pulse{animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
  .badge-live{background:#dcfce7;color:#16a34a;border:1px solid #86efac}
  .badge-stop{background:#fee2e2;color:#dc2626;border:1px solid #fca5a5}
  .badge-mon {background:#fef9c3;color:#ca8a04;border:1px solid #fde047}
  #range-chart-wrap{position:relative;height:260px}
  input[type="date"]{color-scheme:light}
  .insight-bullet::before{content:"▸ ";color:#3b82f6}
  /* Modal */
  #modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50;align-items:center;justify-content:center}
  #modal-bg.open{display:flex}
</style>
</head>
<body class="min-h-screen">

<!-- ── Header ── -->
<div class="bg-white border-b border-gray-200 px-6 py-3">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-lg font-bold text-gray-900">Artha Trading Bot</h1>
      <p class="text-xs text-gray-400">Nifty 50 Options &middot; V2 Strategy &middot; Angel One</p>
    </div>
    <div class="flex items-center gap-5">
      <!-- Nifty LTP -->
      <div class="text-right hidden md:block">
        <p class="text-xs text-gray-400">Nifty 50</p>
        <p id="hdr-nifty" class="text-base font-bold text-gray-900">—</p>
      </div>
      <!-- Balance -->
      <div class="text-right hidden md:block">
        <p class="text-xs text-gray-400">Available Cash</p>
        <p id="hdr-cash" class="text-base font-bold text-gray-700">—</p>
      </div>
      <!-- Connection -->
      <span id="conn-badge" class="text-xs px-2 py-1 rounded-full bg-gray-100 text-gray-400">⬤ Connecting</span>
      <!-- Clock -->
      <div id="clock" class="text-xs text-gray-400 text-right min-w-[90px]"></div>
    </div>
  </div>
</div>

<!-- ── Tabs ── -->
<div class="flex px-6 pt-3 border-b border-gray-200 bg-white">
  <button onclick="switchTab('live')"  id="tab-live"  class="tab-a  px-5 py-2 text-sm font-semibold">Live Trading</button>
  <button onclick="switchTab('range')" id="tab-range" class="tab-i  px-5 py-2 text-sm font-semibold">Backtest Analysis</button>
</div>

<!-- ══════════════ LIVE TAB ══════════════ -->
<div id="pane-live" class="p-5 space-y-4">

  <!-- ── Control Card ── -->
  <div class="card p-4">
    <div class="flex flex-wrap items-center justify-between gap-4">
      <!-- Status badge + info -->
      <div class="flex items-center gap-3">
        <span id="status-badge" class="badge-stop text-xs font-bold px-3 py-1 rounded-full">STOPPED</span>
        <div id="session-info" class="text-xs text-gray-400"></div>
      </div>
      <!-- Buttons -->
      <div class="flex gap-2">
        <button id="btn-start" onclick="openStartModal()"
          class="bg-green-600 hover:bg-green-700 text-white text-sm font-bold px-5 py-2 rounded transition">
          ▶ Start Trading
        </button>
        <button id="btn-stop" onclick="stopTrading()" style="display:none"
          class="bg-red-600 hover:bg-red-700 text-white text-sm font-bold px-5 py-2 rounded transition">
          ■ Stop Trading
        </button>
        <button id="btn-force" onclick="forceExit()" style="display:none"
          class="bg-orange-500 hover:bg-orange-600 text-white text-sm font-bold px-5 py-2 rounded transition">
          ⚠ Force Exit
        </button>
      </div>
    </div>
  </div>

  <!-- ── Stats row ── -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">Daily P&amp;L</p>
      <p id="live-pnl" class="text-2xl font-bold text-gray-500">—</p>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">Trades Today</p>
      <p id="live-trades-ct" class="text-2xl font-bold text-gray-900">—</p>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">Win Rate</p>
      <p id="live-wr" class="text-2xl font-bold text-gray-900">—</p>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 mb-1">Cash Available</p>
      <p id="live-cash" class="text-2xl font-bold text-gray-700">—</p>
    </div>
  </div>

  <!-- ── Signal Monitor ── -->
  <div class="card p-4">
    <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold mb-3">Signal Monitor</p>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
      <div>
        <p class="text-xs text-gray-400 mb-1">India VIX</p>
        <p id="sig-vix" class="font-bold text-gray-800">—</p>
      </div>
      <div>
        <p class="text-xs text-gray-400 mb-1">Last Signal</p>
        <p id="sig-last" class="font-bold text-gray-400">No signal</p>
      </div>
      <div>
        <p class="text-xs text-gray-400 mb-1">Checked At</p>
        <p id="sig-time" class="font-bold text-gray-600">—</p>
      </div>
      <div>
        <p class="text-xs text-gray-400 mb-1">Next Check</p>
        <p id="sig-next" class="font-bold text-gray-600">—</p>
      </div>
    </div>
  </div>

  <!-- ── Active Position ── -->
  <div id="pos-card" class="card p-4 hidden">
    <div class="flex items-center justify-between mb-3">
      <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold">Open Position</p>
      <span id="pos-badge" class="text-xs font-bold px-2 py-0.5 rounded bg-blue-100 text-blue-700">CE</span>
    </div>
    <div class="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
      <div><p class="text-xs text-gray-400 mb-0.5">Symbol</p><p id="pos-sym" class="font-bold text-gray-900 text-xs"></p></div>
      <div><p class="text-xs text-gray-400 mb-0.5">Entry Price</p><p id="pos-entry" class="font-bold text-gray-900"></p></div>
      <div><p class="text-xs text-gray-400 mb-0.5">Current LTP</p><p id="pos-ltp" class="font-bold text-gray-900"></p></div>
      <div><p class="text-xs text-gray-400 mb-0.5">Live P&amp;L</p><p id="pos-pnl" class="text-xl font-bold"></p></div>
      <div><p class="text-xs text-gray-400 mb-0.5">Entry Time</p><p id="pos-time" class="font-bold text-gray-600"></p></div>
    </div>
    <div id="pos-tags" class="flex gap-2 mt-2"></div>
  </div>

  <!-- ── Trade Log ── -->
  <div class="card overflow-hidden">
    <div class="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
      <p class="text-sm font-semibold text-gray-700 uppercase tracking-wide">Today's Trades</p>
      <span id="live-trade-badge" class="text-xs text-gray-400"></span>
    </div>
    <div id="live-trades-tbl"></div>
  </div>

</div><!-- /pane-live -->


<!-- ══════════════ BACKTEST TAB ══════════════ -->
<div id="pane-range" class="p-5 hidden space-y-4">

  <!-- Controls -->
  <div class="card p-4">
    <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold mb-3">Run Backtest</p>
    <div class="flex flex-wrap items-end gap-4">
      <div>
        <label class="text-xs text-gray-400 block mb-1">Start Date</label>
        <input id="range-start" type="date" value="2026-05-01"
          class="bg-white border border-gray-300 rounded px-3 py-2 text-sm"/>
      </div>
      <div>
        <label class="text-xs text-gray-400 block mb-1">End Date</label>
        <input id="range-end" type="date" value="2026-05-21"
          class="bg-white border border-gray-300 rounded px-3 py-2 text-sm"/>
      </div>
      <button onclick="runRange()"
        class="bg-gray-900 hover:bg-gray-700 text-white text-sm font-semibold px-5 py-2 rounded transition">
        Run Backtest
      </button>
      <span id="range-status" class="text-xs text-gray-400 self-center"></span>
    </div>
  </div>

  <!-- Summary -->
  <div id="range-summary" class="hidden space-y-4">
    <div class="flex items-center justify-between">
      <p id="rs-label" class="text-xs font-semibold text-gray-500 uppercase tracking-widest"></p>
      <p id="rs-summary" class="text-xs text-gray-400"></p>
    </div>
    <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
      <div class="card p-4"><p class="text-xs text-gray-400 mb-1">Total P&amp;L</p><p id="rs-pnl" class="text-xl font-bold">—</p></div>
      <div class="card p-4"><p class="text-xs text-gray-400 mb-1">Trading Days</p><p id="rs-days" class="text-xl font-bold text-gray-900">—</p></div>
      <div class="card p-4"><p class="text-xs text-gray-400 mb-1">Win Days</p><p id="rs-win" class="text-xl font-bold text-green-600">—</p></div>
      <div class="card p-4"><p class="text-xs text-gray-400 mb-1">Best Day</p><p id="rs-best" class="text-xl font-bold text-green-600">—</p></div>
      <div class="card p-4"><p class="text-xs text-gray-400 mb-1">Worst Day</p><p id="rs-worst" class="text-xl font-bold text-red-500">—</p></div>
    </div>
    <div class="card p-4">
      <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold mb-3">Daily P&amp;L — click a bar for details</p>
      <div id="range-chart-wrap"><canvas id="range-chart"></canvas></div>
    </div>
    <div id="day-detail" class="card p-5 hidden">
      <div class="flex items-center justify-between mb-4">
        <h2 id="dd-title" class="text-base font-bold text-gray-900"></h2>
        <span id="dd-pnl" class="text-lg font-bold"></span>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs mb-4">
        <div class="bg-gray-50 rounded p-3"><p class="text-gray-400 mb-1">Open</p><p id="dd-open" class="font-bold text-gray-900"></p></div>
        <div class="bg-gray-50 rounded p-3"><p class="text-gray-400 mb-1">High</p><p id="dd-high" class="font-bold text-green-600"></p></div>
        <div class="bg-gray-50 rounded p-3"><p class="text-gray-400 mb-1">Low</p><p id="dd-low" class="font-bold text-red-500"></p></div>
        <div class="bg-gray-50 rounded p-3"><p class="text-gray-400 mb-1">Close</p><p id="dd-close" class="font-bold text-gray-900"></p></div>
      </div>
      <div class="mb-4">
        <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold mb-2">Insights</p>
        <ul id="dd-insights" class="space-y-1 text-sm text-gray-600"></ul>
      </div>
      <div>
        <p class="text-xs text-gray-400 uppercase tracking-widest font-semibold mb-2">Trades (<span id="dd-tc">0</span>)</p>
        <div id="dd-trades"></div>
      </div>
    </div>
  </div>
</div><!-- /pane-range -->


<!-- ══════════════ START TRADING MODAL ══════════════ -->
<div id="modal-bg" class="modal-bg">
  <div class="bg-white rounded-2xl shadow-2xl w-full max-w-md mx-4 p-6">
    <h2 class="text-lg font-bold text-gray-900 mb-1">Configure Trading Session</h2>
    <p class="text-xs text-gray-400 mb-5">Set limits before enabling live order placement on Angel One.</p>

    <div class="space-y-4 mb-5">
      <div>
        <label class="text-sm font-semibold text-gray-700 block mb-1">Max Trades per Day</label>
        <div class="flex items-center gap-3">
          <input id="m-max-trades" type="number" min="1" max="5" value="2"
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
    </div>

    <div class="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 mb-5 text-xs text-amber-800">
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
  ['live','range'].forEach(t=>{
    document.getElementById('pane-'+t).classList.toggle('hidden',t!==name);
    document.getElementById('tab-'+t).className=
      (t===name?'tab-a':'tab-i')+' px-5 py-2 text-sm font-semibold';
  });
  if(name==='range') loadRangeCache();
}

// ── Format helpers ─────────────────────────────────────────────
const inr=n=>'₹'+Math.abs(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
const sign=n=>n>=0?'+':'-';
const cls=n=>n>=0?'text-green-600':'text-red-500';

// ── Trade table builder ────────────────────────────────────────
function tradeTable(trades){
  if(!trades||!trades.length)
    return '<div class="px-4 py-8 text-center text-gray-400 text-sm">No trades yet.</div>';
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
      <td class="px-3 py-2 text-gray-400">${t.reason||''}</td>
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
function refreshLive(){
  fetch('/api/live-state').then(r=>r.json()).then(s=>{
    const pos   = s.position  || {};
    const stats = s.daily_stats || {};
    const sig   = s.signal    || {};
    const mkt   = s.market    || {};

    // Status badge
    const status = s.status || 'STOPPED';
    const badge  = document.getElementById('status-badge');
    badge.textContent = status;
    badge.className   = 'text-xs font-bold px-3 py-1 rounded-full '+
      (status==='LIVE'?'badge-live pulse':status==='MONITORING'?'badge-mon':'badge-stop');

    // Buttons
    const isLive = (status==='LIVE');
    const isMon  = (status==='MONITORING');
    document.getElementById('btn-start').style.display = (!isLive&&!isMon)?'':'none';
    document.getElementById('btn-stop') .style.display = isLive?'':'none';
    document.getElementById('btn-force').style.display = isMon ?'':'none';

    // Connection badge
    const cb = document.getElementById('conn-badge');
    cb.textContent = s.connected ? '⬤ Connected' : '⬤ Offline';
    cb.className   = 'text-xs px-2 py-1 rounded-full '+
      (s.connected ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-400');

    // Session info
    let info = '';
    if(isLive||isMon) info=`${s.config?.lots||1} lot(s) · max ${s.config?.max_trades||2} trades`;
    document.getElementById('session-info').textContent = info;

    // Stats
    const pnl = stats.pnl ?? 0;
    const pnlEl = document.getElementById('live-pnl');
    pnlEl.textContent = (pnl>=0?'+':'')+inr(pnl);
    pnlEl.className   = 'text-2xl font-bold '+cls(pnl);
    document.getElementById('live-trades-ct').textContent =
      (stats.trade_count||0)+(s.config?.max_trades?' / '+(s.config.max_trades):'');
    document.getElementById('live-wr').textContent =
      stats.trade_count ? (stats.win_rate||0)+'%' : '—';
    const cash = stats.balance||0;
    document.getElementById('live-cash').textContent = cash ? inr(cash) : '—';

    // Header Nifty + Cash
    if(mkt.nifty_ltp) document.getElementById('hdr-nifty').textContent='₹'+mkt.nifty_ltp.toFixed(0);
    if(cash)          document.getElementById('hdr-cash') .textContent=inr(cash);

    // Signal monitor
    document.getElementById('sig-vix') .textContent = mkt.vix ? mkt.vix+'' : '—';
    const sigEl = document.getElementById('sig-last');
    const sigVal = sig.signal;
    if(sigVal){
      sigEl.textContent  = sigVal;
      sigEl.className    = 'font-bold '+(sigVal.includes('CE')?'text-blue-600':'text-amber-600');
    } else {
      sigEl.textContent  = 'No signal';
      sigEl.className    = 'font-bold text-gray-400';
    }
    document.getElementById('sig-time').textContent = sig.time   || '—';
    document.getElementById('sig-next').textContent = sig.next_check || '—';

    // Active position
    const posCard = document.getElementById('pos-card');
    if(pos.active){
      posCard.classList.remove('hidden');
      document.getElementById('pos-sym')  .textContent = pos.symbol||'—';
      document.getElementById('pos-entry').textContent = pos.entry_price?'₹'+pos.entry_price.toFixed(2):'—';
      document.getElementById('pos-ltp')  .textContent = pos.live_ltp?'₹'+pos.live_ltp.toFixed(2):'—';
      const pp = pos.live_pnl||0;
      const ppEl = document.getElementById('pos-pnl');
      ppEl.textContent = (pp>=0?'+':'')+inr(pp);
      ppEl.className   = 'text-xl font-bold '+cls(pp);
      document.getElementById('pos-time').textContent = pos.entry_time||'—';
      document.getElementById('pos-badge').textContent = pos.side||'';
      document.getElementById('pos-badge').className =
        'text-xs font-bold px-2 py-0.5 rounded '+
        (pos.side==='CE'?'bg-blue-100 text-blue-700':'bg-amber-100 text-amber-700');
      let tags='';
      if(pos.partial_done) tags+='<span class="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded">Partial exited</span>';
      if(pos.trail_on)     tags+='<span class="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded ml-1">Trail active</span>';
      document.getElementById('pos-tags').innerHTML=tags;
    } else {
      posCard.classList.add('hidden');
    }

    // Trade log
    const trades = s.trades||[];
    document.getElementById('live-trades-tbl').innerHTML = tradeTable(trades);
    document.getElementById('live-trade-badge').textContent =
      trades.length ? trades.length+' trade'+(trades.length>1?'s':'') : '';

    // Error banner
    if(s.last_error && !document.getElementById('err-banner')){
      const b=document.createElement('div');
      b.id='err-banner';
      b.className='fixed bottom-4 right-4 bg-red-100 border border-red-300 text-red-800 text-xs px-4 py-2 rounded-lg shadow';
      b.textContent='Error: '+s.last_error;
      document.body.appendChild(b);
      setTimeout(()=>b.remove(),8000);
    }
  }).catch(()=>{});
}
setInterval(refreshLive, 5000); refreshLive();

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

// ── Start Trading Modal ───────────────────────────────────────
function openStartModal(){
  fetch('/api/trading-config').then(r=>r.json()).then(cfg=>{
    document.getElementById('m-max-trades').value=cfg.max_trades||2;
    document.getElementById('m-lots').value=cfg.lots||1;
    updateModalUnits();
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
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('m-lots')      .addEventListener('input',updateModalUnits);
  document.getElementById('m-max-trades').addEventListener('input',updateModalUnits);
});

function confirmStart(){
  const max_trades=parseInt(document.getElementById('m-max-trades').value);
  const lots      =parseInt(document.getElementById('m-lots').value);
  closeModal();
  fetch('/api/start-trading',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({max_trades,lots})
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

// Close modal on background click
document.getElementById('modal-bg').addEventListener('click',function(e){
  if(e.target===this) closeModal();
});

// ── Backtest tab ───────────────────────────────────────────────
let rangeChart=null, rangeResults=[];

function loadRangeCache(){
  fetch('/api/range-cache').then(r=>r.json()).then(cache=>{
    if(cache.results&&cache.results.length){
      document.getElementById('range-status').textContent='Last run: '+(cache.generated||'');
      renderRange(cache.results,cache.label||'');
    }
  }).catch(()=>{});
}

function runRange(){
  const start=document.getElementById('range-start').value;
  const end  =document.getElementById('range-end').value;
  if(!start||!end) return;
  const btn=document.querySelector('button[onclick="runRange()"]');
  btn.disabled=true; btn.textContent='Running…';
  document.getElementById('range-status').textContent='Fetching & simulating…';
  document.getElementById('range-summary').classList.add('hidden');
  fetch('/api/run-range',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({start,end})})
  .then(r=>r.json()).then(cache=>{
    btn.disabled=false; btn.textContent='Run Backtest';
    if(cache.error){document.getElementById('range-status').textContent='Error: '+cache.error;return;}
    document.getElementById('range-status').textContent='Done — '+cache.results.length+' trading days';
    renderRange(cache.results,cache.label||'');
  }).catch(err=>{btn.disabled=false;btn.textContent='Run Backtest';
    document.getElementById('range-status').textContent='Error: '+err;});
}

function renderRange(results,label){
  if(!results||!results.length) return;
  rangeResults=results;
  document.getElementById('range-summary').classList.remove('hidden');
  document.getElementById('day-detail').classList.add('hidden');
  const pnls=results.map(r=>r.daily_pnl);
  const total=pnls.reduce((a,b)=>a+b,0);
  const winDays=pnls.filter(p=>p>0).length;
  const loseDays=pnls.filter(p=>p<0).length;
  const trades=results.reduce((a,r)=>a+(r.trade_count||0),0);
  const wins  =results.reduce((a,r)=>a+(r.win_count  ||0),0);
  document.getElementById('rs-label').textContent=label||'Backtest Results';
  document.getElementById('rs-summary').textContent=
    trades+' trades · '+wins+' winners ('+
    (trades?Math.round(wins/trades*100):0)+'%) · '+winDays+'W / '+loseDays+'L days';
  const tEl=document.getElementById('rs-pnl');
  tEl.textContent=(total>=0?'+':'')+inr(total);
  tEl.className='text-xl font-bold '+cls(total);
  document.getElementById('rs-days') .textContent=results.length;
  document.getElementById('rs-win')  .textContent=winDays+' / '+results.length;
  document.getElementById('rs-best') .textContent='+'+inr(Math.max(...pnls));
  document.getElementById('rs-worst').textContent='-'+inr(Math.abs(Math.min(...pnls)));
  const labels=results.map(r=>{const d=new Date(r.date);
    return d.toLocaleDateString('en-IN',{day:'2-digit',month:'short'});});
  if(rangeChart){rangeChart.destroy();rangeChart=null;}
  rangeChart=new Chart(document.getElementById('range-chart').getContext('2d'),{
    type:'bar',
    data:{labels,datasets:[{label:'Daily P&L (₹)',data:pnls,
      backgroundColor:pnls.map(p=>p>=0?'rgba(34,197,94,.85)':'rgba(239,68,68,.85)'),
      borderColor:pnls.map(p=>p>=0?'rgba(34,197,94,1)':'rgba(239,68,68,1)'),
      borderWidth:1,borderRadius:3}]},
    options:{responsive:true,maintainAspectRatio:false,
      onClick:(e,els)=>{if(els.length)showDay(rangeResults[els[0].index]);},
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>{
        const v=c.raw;return' '+(v>=0?'+':'')+'₹'+Math.abs(v).toLocaleString('en-IN',{minimumFractionDigits:2});
      }}}},
      scales:{x:{ticks:{color:'#6b7280',font:{size:10}},grid:{color:'#f1f5f9'}},
              y:{ticks:{color:'#6b7280',font:{size:10},
                   callback:v=>(v>=0?'+':'')+'₹'+Math.abs(v).toLocaleString('en-IN')},
                 grid:{color:'#f1f5f9'}}}}});
}

function showDay(r){
  const p=document.getElementById('day-detail');
  p.classList.remove('hidden');
  p.scrollIntoView({behavior:'smooth',block:'start'});
  const d=new Date(r.date);
  document.getElementById('dd-title').textContent=
    d.toLocaleDateString('en-IN',{weekday:'long',day:'2-digit',month:'long',year:'numeric'});
  const pEl=document.getElementById('dd-pnl');
  pEl.textContent=(r.daily_pnl>=0?'+':'')+inr(r.daily_pnl);
  pEl.className='text-lg font-bold '+cls(r.daily_pnl);
  const m=r.market||{};
  document.getElementById('dd-open') .textContent='₹'+(m.open ||0).toFixed(2);
  document.getElementById('dd-high') .textContent='₹'+(m.high ||0).toFixed(2);
  document.getElementById('dd-low')  .textContent='₹'+(m.low  ||0).toFixed(2);
  document.getElementById('dd-close').textContent='₹'+(m.close||0).toFixed(2);
  document.getElementById('dd-insights').innerHTML=
    (r.insights||[]).map(i=>`<li class="insight-bullet text-sm text-gray-600">${i}</li>`).join('')||
    '<li class="text-gray-400">No insights.</li>';
  const tds=r.trades||[];
  document.getElementById('dd-tc').textContent=tds.length;
  document.getElementById('dd-trades').innerHTML=tradeTable(tds);
}
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
