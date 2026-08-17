"""
Two-config comparison over the longest window the data pipeline actually
supports (target: 6 calendar months back from 2026-07-24), with a month-by-
month breakdown:
  a) Baseline         — ADX off, EMA_EXIT instant (confirm=1)
  b) Current+backstop — ADX on (V2_ADX_MIN), EMA_EXIT confirm=V2_EMA_EXIT_CONFIRM_CANDLES,
                         EMA-confirm hard backstop on at V2_EMA_CONFIRM_BACKSTOP_PTS

loss_cooldown_candles stays 0 in both.

Run: python3 ab_test_6month_monthly.py
"""
from datetime import date, timedelta
from collections import defaultdict
import backtest as bt
from backtest import fetch_range_data_angel, simulate_day

TARGET = date(2026, 7, 24)


def months_back(d: date, n: int) -> date:
    m, y = d.month - n, d.year
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, d.day)


WARMUP_DAYS  = 40
report_start = months_back(TARGET, 6)
fetch_start  = report_start - timedelta(days=WARMUP_DAYS)

print(f"Requested window: 6 months back from {TARGET} -> report_start = {report_start}")
print(f"Fetching Angel One + NSE VIX data: {fetch_start} -> {TARGET}")

MIN_EXPECTED_ROWS = 8000   # ~6 months of 5m candles
MAX_STALE_DAYS    = 5      # data must reach within this many days of TARGET — Angel
                            # sometimes drops just the LAST chunk of a multi-chunk fetch,
                            # which a pure row-count check won't catch (total rows can
                            # still look "enough" while missing the most recent month+)
for attempt in range(5):
    df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_angel(fetch_start, TARGET)
    coverage_ok = (len(df_5m) >= MIN_EXPECTED_ROWS and not df_5m.empty
                  and (TARGET - df_5m.index.max().date()).days <= MAX_STALE_DAYS
                  and not df_bnf.empty
                  and (TARGET - df_bnf.index.max().date()).days <= MAX_STALE_DAYS)
    if coverage_ok:
        break
    last_5m  = df_5m.index.max().date()  if not df_5m.empty  else None
    last_bnf = df_bnf.index.max().date() if not df_bnf.empty else None
    print(f"  Incomplete fetch (attempt {attempt+1}: {len(df_5m)} rows, "
          f"5m ends {last_5m}, bnf ends {last_bnf}) — retrying...")
else:
    print(f"  WARNING: proceeding with incomplete candle data ({len(df_5m)} rows) after 5 attempts.")

print(f"5m rows: {len(df_5m)} | nbees rows: {len(df_nbees)} | bnf rows: {len(df_bnf)} | vix rows: {len(df_vix)}")

# ── Report actual data coverage (may be shorter than requested) ──────────
actual_candle_start = df_5m.index.min().date() if not df_5m.empty else None
actual_candle_end   = df_5m.index.max().date() if not df_5m.empty else None
actual_vix_start    = df_vix.index.min().date() if not df_vix.empty else None
actual_vix_end      = df_vix.index.max().date() if not df_vix.empty else None

print(f"\nActual NIFTYBEES/BANKBEES coverage: {actual_candle_start} -> {actual_candle_end}")
print(f"Actual VIX coverage: {actual_vix_start} -> {actual_vix_end}")

# The real reporting window is capped by whichever data source is shorter,
# and can never start before the requested report_start regardless.
effective_start = report_start
if actual_candle_start and actual_candle_start > effective_start:
    effective_start = actual_candle_start
if actual_vix_start and actual_vix_start > effective_start:
    effective_start = actual_vix_start

months_available = (TARGET.year - effective_start.year) * 12 + (TARGET.month - effective_start.month) \
                    + (TARGET.day - effective_start.day) / 30.0
if effective_start > report_start:
    print(f"\n*** Requested 6 months back to {report_start}, but usable data only goes back to "
          f"{effective_start} (~{months_available:.1f} months) — reporting on the SHORTER window. ***")
else:
    print(f"\nFull 6-month window is available — reporting {report_start} -> {TARGET}.")

report_start = effective_start

if df_5m.empty:
    print("ERROR: no candle data returned at all — aborting.")
    raise SystemExit(1)


def run_variant(require_adx: bool, ema_exit_confirm: int, ema_confirm_backstop_pts: float = 0) -> list:
    results = []
    current = report_start
    while current <= TARGET:
        if current.weekday() < 5:
            result = simulate_day(current, df_5m, df_1d,
                                  df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix,
                                  loss_cooldown_candles=0,
                                  require_adx=require_adx,
                                  ema_exit_confirm=ema_exit_confirm,
                                  ema_confirm_backstop_pts=ema_confirm_backstop_pts)
            if result:
                results.append(result)
        current += timedelta(days=1)
    return results


def summarize(results: list) -> dict:
    total_trades = sum(r["trade_count"] for r in results)
    total_wins   = sum(r["win_count"]   for r in results)
    total_pnl    = sum(r["daily_pnl"]   for r in results)
    daily_pnls   = [r["daily_pnl"] for r in results]
    all_trades   = [t for r in results for t in r["trades"]]
    ema_exit_n     = sum(1 for t in all_trades if t["reason"] == "EMA_EXIT")
    ema_backstop_n = sum(1 for t in all_trades if t["reason"] == "EMA_EXIT_BACKSTOP")
    return {
        "days"             : len(results),
        "total_trades"     : total_trades,
        "win_rate_pct"     : round(100 * total_wins / total_trades, 1) if total_trades else 0.0,
        "total_pnl"        : round(total_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / total_trades, 2) if total_trades else 0.0,
        "max_day_loss"     : round(min(daily_pnls), 2) if daily_pnls else 0.0,
        "max_day_win"      : round(max(daily_pnls), 2) if daily_pnls else 0.0,
        "adx_skips"        : sum(r.get("adx_skips", 0) for r in results),
        "ema_unconfirms"   : sum(r.get("ema_unconfirm_count", 0) for r in results),
        "ema_exit_normal"  : ema_exit_n,
        "ema_exit_backstop": ema_backstop_n,
    }


def month_key(date_str: str) -> str:
    return date_str[:7]   # "YYYY-MM-DD" -> "YYYY-MM"


print("\n" + "=" * 70)
print("RUN A — baseline (ADX off, EMA_EXIT instant)")
print("=" * 70)
results_a = run_variant(require_adx=False, ema_exit_confirm=1)
a = summarize(results_a)

print("\n" + "=" * 70)
print(f"RUN B — current + backstop (ADX ON, V2_ADX_MIN={bt.V2_ADX_MIN}; "
      f"EMA-confirm ON N={bt.V2_EMA_EXIT_CONFIRM_CANDLES}; "
      f"backstop ON at {bt.V2_EMA_CONFIRM_BACKSTOP_PTS}pts)")
print("=" * 70)
results_b = run_variant(require_adx=True, ema_exit_confirm=bt.V2_EMA_EXIT_CONFIRM_CANDLES,
                        ema_confirm_backstop_pts=bt.V2_EMA_CONFIRM_BACKSTOP_PTS)
b = summarize(results_b)

print("\n" + "=" * 70)
print(f"OVERALL COMPARISON  ({report_start} -> {TARGET})")
print("=" * 70)
fields = [
    ("Days simulated",                  "days"),
    ("Total trades",                    "total_trades"),
    ("Win rate %",                      "win_rate_pct"),
    ("Total P&L (Rs.)",                 "total_pnl"),
    ("Avg P&L / trade (Rs.)",           "avg_pnl_per_trade"),
    ("Max single-day loss (Rs.)",       "max_day_loss"),
    ("Max single-day win (Rs.)",        "max_day_win"),
    ("Entries skipped by ADX",          "adx_skips"),
    ("EMA_EXIT unconfirm/reset events", "ema_unconfirms"),
    ("EMA_EXIT normal-confirmed exits", "ema_exit_normal"),
    ("EMA_EXIT backstop-forced exits",  "ema_exit_backstop"),
]
print(f"{'Metric':<34}{'A: Baseline':<16}{'B: Current':<16}")
for label, key in fields:
    print(f"{label:<34}{str(a[key]):<16}{str(b[key]):<16}")

# ── Monthly breakdown ──────────────────────────────────────────────
by_month_a = defaultdict(list)
by_month_b = defaultdict(list)
for r in results_a:
    by_month_a[month_key(r["date"])].append(r)
for r in results_b:
    by_month_b[month_key(r["date"])].append(r)

all_months = sorted(set(by_month_a) | set(by_month_b))

print("\n" + "=" * 70)
print("MONTHLY BREAKDOWN")
print("=" * 70)
print(f"{'Month':<10}{'Trades A':<10}{'Trades B':<10}{'P&L A':<14}{'P&L B':<14}"
      f"{'Win% A':<9}{'Win% B':<9}{'ADX skip':<10}{'EMA unconf':<11}{'Backstop':<10}{'Flag':<20}")

flagged_months = []
for mo in all_months:
    sa = summarize(by_month_a.get(mo, []))
    sb = summarize(by_month_b.get(mo, []))
    flag = ""
    if sb["total_pnl"] < sa["total_pnl"]:
        flag = "B WORSE THAN A"
        flagged_months.append((mo, sa, sb))
    print(f"{mo:<10}{sa['total_trades']:<10}{sb['total_trades']:<10}"
          f"{sa['total_pnl']:<14}{sb['total_pnl']:<14}"
          f"{sa['win_rate_pct']:<9}{sb['win_rate_pct']:<9}"
          f"{sb['adx_skips']:<10}{sb['ema_unconfirms']:<11}{sb['ema_exit_backstop']:<10}{flag:<20}")

print("\n--- Months where config B underperformed config A ---")
if flagged_months:
    for mo, sa, sb in flagged_months:
        diff = sb["total_pnl"] - sa["total_pnl"]
        print(f"  {mo}: A={sa['total_pnl']:.2f}  B={sb['total_pnl']:.2f}  "
              f"(B is {diff:.2f} worse)  |  A trades={sa['total_trades']} win%={sa['win_rate_pct']}  "
              f"B trades={sb['total_trades']} win%={sb['win_rate_pct']}")
else:
    print("  None — config B matched or beat config A in every month.")

print("\nDone.")
