"""
A/B comparison: does the India VIX filter (13-30 band) help or hurt?

  A) Baseline      — VIX filter ON (df_vix passed through, current live/backtest behavior).
  B) Condition 1   — VIX filter OFF (df_vix withheld from simulate_day, so the
                      `13 <= VIX <= 30` gate in backtest.py never triggers a skip).

All other settings match the current validated defaults (require_adx=False,
ema_exit_confirm=1, loss_cooldown_candles=0) so the VIX rule is isolated as
the only variable.

Run: python3 ab_test_no_vix.py
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
          f"min={df_vix['Close'].min():.2f}  max={df_vix['Close'].max():.2f}  "
          f"(current band: {bt.V2_VIX_MIN}-{bt.V2_VIX_MAX})")
else:
    print("WARNING: VIX data is empty — baseline run's filter will not apply either.")
print()

if df_5m.empty:
    print("ERROR: no data returned — aborting.")
    raise SystemExit(1)


def run_variant(use_vix: bool) -> list:
    results = []
    current = report_start
    vix_arg = df_vix if use_vix else None
    while current <= TARGET:
        if current.weekday() < 5:
            result = simulate_day(current, df_5m, df_1d,
                                  df_nbees=df_nbees, df_bnf=df_bnf, df_vix=vix_arg,
                                  loss_cooldown_candles=0,
                                  require_adx=False,
                                  ema_exit_confirm=1)
            if result:
                results.append(result)
        current += timedelta(days=1)
    return results


def summarize(label: str, results: list) -> dict:
    total_trades = sum(r["trade_count"] for r in results)
    total_wins   = sum(r["win_count"]   for r in results)
    total_pnl    = sum(r["daily_pnl"]   for r in results)
    daily_pnls   = [r["daily_pnl"] for r in results]
    vix_skip_days = sum(1 for r in results if any("VIX" in i for i in r.get("insights", [])))
    return {
        "label"            : label,
        "days_simulated"   : len(results),
        "vix_skip_days"    : vix_skip_days,
        "total_trades"     : total_trades,
        "win_rate_pct"     : round(100 * total_wins / total_trades, 1) if total_trades else 0.0,
        "total_pnl"        : round(total_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / total_trades, 2) if total_trades else 0.0,
        "max_day_loss"     : round(min(daily_pnls), 2) if daily_pnls else 0.0,
        "max_day_win"      : round(max(daily_pnls), 2) if daily_pnls else 0.0,
    }


print("=" * 70)
print("RUN A — baseline (VIX filter ON, 13-30 band)")
print("=" * 70)
results_a = run_variant(use_vix=True)
a = summarize("A: VIX ON", results_a)

print("\n" + "=" * 70)
print("RUN B — Condition 1 (VIX filter OFF / removed)")
print("=" * 70)
results_b = run_variant(use_vix=False)
b = summarize("B: VIX OFF", results_b)

print("\n" + "=" * 70)
print("COMPARISON")
print("=" * 70)
fields = [
    ("Days simulated",             "days_simulated"),
    ("Days skipped by VIX filter", "vix_skip_days"),
    ("Total trades",               "total_trades"),
    ("Win rate %",                 "win_rate_pct"),
    ("Total P&L (Rs.)",            "total_pnl"),
    ("Avg P&L / trade (Rs.)",      "avg_pnl_per_trade"),
    ("Max single-day loss (Rs.)",  "max_day_loss"),
    ("Max single-day win (Rs.)",   "max_day_win"),
]
print(f"{'Metric':<32}{'A: VIX ON':<16}{'B: VIX OFF':<16}")
for label, key in fields:
    print(f"{label:<32}{str(a[key]):<16}{str(b[key]):<16}")

# ── Which extra trades did removing VIX add, and were they good or bad? ──
print("\n--- Trades unlocked on VIX-skipped days (blocked in A, allowed in B) ---")
vix_skip_dates = {r["date"] for r in results_a if any("VIX" in i for i in r.get("insights", []))}
b_by_day = {r["date"]: r for r in results_b}

extra_wins, extra_losses, extra_pnl = 0, 0, 0.0
for d in sorted(vix_skip_dates):
    r = b_by_day.get(d)
    if not r:
        continue
    for t in r["trades"]:
        if t["reason"] == "PARTIAL_TP":
            continue
        tag = "WIN" if t["pnl"] > 0 else "LOSS"
        if t["pnl"] > 0: extra_wins += 1
        else: extra_losses += 1
        extra_pnl += t["pnl"]
        print(f"  {d} [{t['side']}] {t['time']}->{t['exit_time']}  "
              f"pnl={t['pnl']:.2f}  ({tag}, {t['reason']})")

print(f"\nTrades unlocked on VIX-skipped days: {extra_wins + extra_losses} "
      f"({extra_wins} winners, {extra_losses} losers)  "
      f"net pnl={round(extra_pnl, 2)}  |  VIX-skip days: {len(vix_skip_dates)}")

print("\nDone.")
