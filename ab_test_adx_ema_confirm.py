"""
Three-way A/B/C comparison over the last 45 calendar days:
  A) Baseline  — VIX fixed (NSE source), sl_warn_count fix in live_trader.py
                 doesn't affect backtest, loss_cooldown_candles=0, ADX off,
                 EMA_EXIT instant (confirm=1) — i.e. logic as it stood BEFORE
                 today's ADX / EMA-confirm changes.
  B) ADX ON    — require_adx=True (V2_ADX_MIN), EMA_EXIT still instant.
  C) ADX + EMA-confirm ON — require_adx=True, ema_exit_confirm=V2_EMA_EXIT_CONFIRM_CANDLES.

loss_cooldown_candles stays 0 in all three runs (kept OFF per instruction, to
isolate today's changes cleanly).

Run: python3 ab_test_adx_ema_confirm.py
"""
from datetime import date, timedelta
import backtest as bt
from backtest import fetch_range_data_angel, simulate_day

TARGET      = date.today()
RANGE_DAYS  = 45
WARMUP_DAYS = 40

report_start = TARGET - timedelta(days=RANGE_DAYS)
fetch_start  = report_start - timedelta(days=WARMUP_DAYS)

print(f"Fetching Angel One + NSE VIX data: {fetch_start} -> {TARGET} "
      f"(reporting window: {report_start} -> {TARGET})")

# Angel's historical-candle endpoint intermittently 403s a chunk (transient,
# not IP/auth related — a plain retry usually gets a full response). Retry
# the whole fetch a few times until the expected ~85-day span comes back.
MIN_EXPECTED_ROWS = 4000
for attempt in range(4):
    df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_angel(fetch_start, TARGET)
    if len(df_5m) >= MIN_EXPECTED_ROWS:
        break
    print(f"  Incomplete fetch (attempt {attempt+1}: {len(df_5m)} rows) — retrying...")
else:
    print(f"  WARNING: proceeding with incomplete data ({len(df_5m)} rows) after 4 attempts.")

print(f"5m rows: {len(df_5m)} | nbees rows: {len(df_nbees)} | bnf rows: {len(df_bnf)} | vix rows: {len(df_vix)}")
if not df_vix.empty:
    print(f"VIX date range: {df_vix.index.min().date()} -> {df_vix.index.max().date()}  "
          f"min={df_vix['Close'].min():.2f}  max={df_vix['Close'].max():.2f}")
else:
    print("WARNING: VIX data is empty — filter will not apply this run.")
print()

if df_5m.empty:
    print("ERROR: no data returned — aborting.")
    raise SystemExit(1)


def run_variant(require_adx: bool, ema_exit_confirm: int) -> list:
    results = []
    current = report_start
    while current <= TARGET:
        if current.weekday() < 5:
            result = simulate_day(current, df_5m, df_1d,
                                  df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix,
                                  loss_cooldown_candles=0,
                                  require_adx=require_adx,
                                  ema_exit_confirm=ema_exit_confirm)
            if result:
                results.append(result)
        current += timedelta(days=1)
    return results


def summarize(label: str, results: list) -> dict:
    total_trades = sum(r["trade_count"] for r in results)
    total_wins   = sum(r["win_count"]   for r in results)
    total_pnl    = sum(r["daily_pnl"]   for r in results)
    daily_pnls   = [r["daily_pnl"] for r in results]
    return {
        "label"            : label,
        "days_simulated"   : len(results),
        "total_trades"     : total_trades,
        "win_rate_pct"     : round(100 * total_wins / total_trades, 1) if total_trades else 0.0,
        "total_pnl"        : round(total_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / total_trades, 2) if total_trades else 0.0,
        "max_day_loss"     : round(min(daily_pnls), 2) if daily_pnls else 0.0,
        "max_day_win"      : round(max(daily_pnls), 2) if daily_pnls else 0.0,
        "adx_skips"        : sum(r.get("adx_skips", 0) for r in results),
        "ema_unconfirms"   : sum(r.get("ema_unconfirm_count", 0) for r in results),
    }


print("=" * 70)
print("RUN A — baseline (ADX off, EMA_EXIT instant, cooldown off)")
print("=" * 70)
results_a = run_variant(require_adx=False, ema_exit_confirm=1)
a = summarize("A: Baseline", results_a)

print("\n" + "=" * 70)
print(f"RUN B — ADX ON (V2_ADX_MIN={bt.V2_ADX_MIN}), EMA_EXIT still instant")
print("=" * 70)
results_b = run_variant(require_adx=True, ema_exit_confirm=1)
b = summarize("B: ADX ON", results_b)

print("\n" + "=" * 70)
print(f"RUN C — ADX ON + EMA_EXIT confirm ON (N={bt.V2_EMA_EXIT_CONFIRM_CANDLES})")
print("=" * 70)
results_c = run_variant(require_adx=True, ema_exit_confirm=bt.V2_EMA_EXIT_CONFIRM_CANDLES)
c = summarize("C: ADX + EMA-confirm", results_c)

print("\n" + "=" * 70)
print("COMPARISON")
print("=" * 70)
fields = [
    ("Days simulated",                 "days_simulated"),
    ("Total trades",                   "total_trades"),
    ("Win rate %",                     "win_rate_pct"),
    ("Total P&L (Rs.)",                "total_pnl"),
    ("Avg P&L / trade (Rs.)",          "avg_pnl_per_trade"),
    ("Max single-day loss (Rs.)",      "max_day_loss"),
    ("Max single-day win (Rs.)",       "max_day_win"),
    ("Entries skipped by ADX",         "adx_skips"),
    ("EMA_EXIT unconfirm/reset events","ema_unconfirms"),
]
print(f"{'Metric':<34}{'A: Baseline':<16}{'B: ADX ON':<16}{'C: ADX+EMA':<16}")
for label, key in fields:
    print(f"{label:<34}{str(a[key]):<16}{str(b[key]):<16}{str(c[key]):<16}")

# ── Which run-A trades did ADX remove in run B? ───────────────────
print("\n--- Trades ADX removed (present in run A, blocked in run B) ---")
a_by_day = {r["date"]: [t for t in r["trades"] if t["reason"] != "PARTIAL_TP"] for r in results_a}

removed_wins, removed_losses, unmatched = 0, 0, 0
for r in results_b:
    day = r["date"]
    a_trades = a_by_day.get(day, [])
    for ev in r.get("adx_skip_events", []):
        match = next((t for t in a_trades if t["time"] == ev["time"] and t["side"] == ev["side"]), None)
        if match:
            tag = "WIN" if match["pnl"] > 0 else "LOSS"
            if match["pnl"] > 0: removed_wins += 1
            else: removed_losses += 1
            print(f"  {day} [{ev['side']}] {match['time']}->{match['exit_time']}  "
                  f"pnl={match['pnl']:.2f}  ({tag}, {match['reason']}, ADX={ev['adx']})")
        else:
            unmatched += 1
            print(f"  {day} [{ev['side']}] {ev['time']}  ADX={ev['adx']}  "
                  f"— no matching run-A trade found (signal likely differed by then)")

print(f"\nADX-removed trades matched to run A: {removed_wins + removed_losses} "
      f"({removed_wins} winners, {removed_losses} losers)  |  unmatched: {unmatched}")

print("\nDone.")
