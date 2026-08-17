"""
One-off A/B comparison: loss-cooldown OFF (baseline) vs ON, over the last 45
calendar days ending today. Fetches Angel One data once, runs simulate_day
twice per day (loss_cooldown_candles=0 vs bt.V2_LOSS_COOLDOWN_CANDLES),
and prints a comparison summary plus any "stacked loss" instances (same
direction traded twice with the first ending in a loss).

Run: python3 ab_test_loss_cooldown.py
"""
from datetime import date, timedelta
import backtest as bt
from backtest import fetch_range_data_angel, simulate_day

TARGET = date.today()
RANGE_DAYS  = 45     # comparison window
WARMUP_DAYS = 40     # extra history fetched so the first compared day still warms up EMA20 etc.

report_start = TARGET - timedelta(days=RANGE_DAYS)
fetch_start  = report_start - timedelta(days=WARMUP_DAYS)

print(f"Fetching Angel One data: {fetch_start} -> {TARGET} "
      f"(reporting window: {report_start} -> {TARGET})")
df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_angel(fetch_start, TARGET)
print(f"5m rows: {len(df_5m)} | nbees rows: {len(df_nbees)} | bnf rows: {len(df_bnf)} | vix rows: {len(df_vix)}\n")

if df_5m.empty:
    print("ERROR: no data returned — aborting.")
    raise SystemExit(1)


def run_variant(loss_cooldown_candles: int) -> list:
    results = []
    current = report_start
    while current <= TARGET:
        if current.weekday() < 5:
            result = simulate_day(current, df_5m, df_1d,
                                  df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix,
                                  loss_cooldown_candles=loss_cooldown_candles)
            if result:
                results.append(result)
        current += timedelta(days=1)
    return results


def find_stacked_losses(results: list) -> list:
    """Same-direction trades on the same day where an earlier one lost."""
    instances = []
    for r in results:
        real_trades = [t for t in r["trades"] if t["reason"] != "PARTIAL_TP"]
        for a, b in zip(real_trades, real_trades[1:]):
            if a["side"] == b["side"] and a["pnl"] < 0:
                instances.append({
                    "date": r["date"], "side": a["side"],
                    "trade1": f"{a['time']}->{a['exit_time']} pnl={a['pnl']:.2f} ({a['reason']})",
                    "trade2": f"{b['time']}->{b['exit_time']} pnl={b['pnl']:.2f} ({b['reason']})",
                })
    return instances


def summarize(label: str, results: list) -> dict:
    total_trades = sum(r["trade_count"] for r in results)
    total_wins   = sum(r["win_count"]   for r in results)
    total_pnl    = sum(r["daily_pnl"]   for r in results)
    daily_pnls   = [r["daily_pnl"] for r in results]
    cooldown_skips = sum(r.get("cooldown_skips", 0) for r in results)
    days_hit_loss_cap = sum(1 for r in results if r["daily_pnl"] <= bt.MAX_DAILY_LOSS)
    stacked = find_stacked_losses(results)

    summary = {
        "label"            : label,
        "days_simulated"   : len(results),
        "total_trades"     : total_trades,
        "win_rate_pct"     : round(100 * total_wins / total_trades, 1) if total_trades else 0.0,
        "total_pnl"        : round(total_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / total_trades, 2) if total_trades else 0.0,
        "max_day_loss"     : round(min(daily_pnls), 2) if daily_pnls else 0.0,
        "max_day_win"      : round(max(daily_pnls), 2) if daily_pnls else 0.0,
        "days_hit_max_daily_loss": days_hit_loss_cap,
        "cooldown_skips"   : cooldown_skips,
        "stacked_loss_instances": stacked,
    }
    return summary


print("=" * 70)
print("RUN 1/2 — baseline (loss cooldown OFF, current behavior)")
print("=" * 70)
baseline_results = run_variant(loss_cooldown_candles=0)
baseline = summarize("Baseline (OFF)", baseline_results)

print("\n" + "=" * 70)
print(f"RUN 2/2 — updated (loss cooldown ON, N={bt.V2_LOSS_COOLDOWN_CANDLES} candles)")
print("=" * 70)
updated_results = run_variant(loss_cooldown_candles=bt.V2_LOSS_COOLDOWN_CANDLES)
updated = summarize(f"Updated (ON, N={bt.V2_LOSS_COOLDOWN_CANDLES})", updated_results)

print("\n" + "=" * 70)
print("COMPARISON")
print("=" * 70)
fields = [
    ("Days simulated",              "days_simulated"),
    ("Total trades",                "total_trades"),
    ("Win rate %",                  "win_rate_pct"),
    ("Total P&L (Rs.)",             "total_pnl"),
    ("Avg P&L / trade (Rs.)",       "avg_pnl_per_trade"),
    ("Max single-day loss (Rs.)",   "max_day_loss"),
    ("Max single-day win (Rs.)",    "max_day_win"),
    ("Days hit MAX_DAILY_LOSS",     "days_hit_max_daily_loss"),
    ("Trades skipped by cooldown",  "cooldown_skips"),
]
print(f"{'Metric':<32}{'Baseline (OFF)':<20}{'Updated (ON)':<20}")
for label, key in fields:
    print(f"{label:<32}{str(baseline[key]):<20}{str(updated[key]):<20}")

print("\n--- Stacked-loss instances: BASELINE ---")
if baseline["stacked_loss_instances"]:
    for inst in baseline["stacked_loss_instances"]:
        print(f"  {inst['date']} [{inst['side']}]  {inst['trade1']}  ->  {inst['trade2']}")
else:
    print("  (none found)")

print("\n--- Stacked-loss instances: UPDATED (cooldown ON) ---")
if updated["stacked_loss_instances"]:
    for inst in updated["stacked_loss_instances"]:
        print(f"  {inst['date']} [{inst['side']}]  {inst['trade1']}  ->  {inst['trade2']}")
else:
    print("  (none found)")

print("\nDone.")
