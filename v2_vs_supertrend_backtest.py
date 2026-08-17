"""
V2 (as currently deployed) vs. Supertrend(10,3)+50pt SL -- same-window
comparison, so the numbers from the Supertrend backtests are actually
comparable to the live strategy instead of just sitting next to each
other in conversation.

V2 config matches what's live today: ADX regime filter ON (V2_ADX_MIN),
EMA_EXIT requires V2_EMA_EXIT_CONFIRM_CANDLES consecutive confirmed
closes with the V2_EMA_CONFIRM_BACKSTOP_PTS hard backstop, loss cooldown
ON (V2_LOSS_COOLDOWN_CANDLES), MAX_DAILY_LOSS/DAILY_PROFIT_TARGET caps
active (hardcoded in simulate_day, same as live). Same Rs.40/trade flat
cost applied on top for parity with the Supertrend scripts, since
backtest.py's simulate_day doesn't deduct one itself.

Fetches one 90-day (+ 40-day warmup) Angel One window and reports both
a 45-day and a 90-day slice from it, so it lines up with the two
Supertrend runs already done this session.
"""
from datetime import date, timedelta
import backtest as bt
from backtest import fetch_range_data_angel, simulate_day

TARGET = date.today() - timedelta(days=1)
WARMUP_DAYS = 40
COST_PER_TRADE = 40

report_start_90 = date.today() - timedelta(days=90)
report_start_45 = date.today() - timedelta(days=45)
fetch_start = report_start_90 - timedelta(days=WARMUP_DAYS)

print(f"Fetching Angel One + NSE VIX data: {fetch_start} -> {TARGET}")
MIN_EXPECTED_ROWS = 8000
MAX_STALE_DAYS = 5
for attempt in range(5):
    df_5m, df_1d, df_nbees, df_bnf, df_vix = fetch_range_data_angel(fetch_start, TARGET)
    coverage_ok = (len(df_5m) >= MIN_EXPECTED_ROWS and not df_5m.empty
                  and (TARGET - df_5m.index.max().date()).days <= MAX_STALE_DAYS
                  and not df_bnf.empty
                  and (TARGET - df_bnf.index.max().date()).days <= MAX_STALE_DAYS)
    if coverage_ok:
        break
    print(f"  Incomplete fetch (attempt {attempt+1}: {len(df_5m)} rows) -- retrying...")
else:
    print(f"  WARNING: proceeding with incomplete candle data ({len(df_5m)} rows) after 5 attempts.")

actual_start = df_5m.index.min().date()
effective_start_90 = max(report_start_90, actual_start)
effective_start_45 = max(report_start_45, actual_start)
print(f"Actual candle coverage: {actual_start} -> {df_5m.index.max().date()}\n")

results = []
current = effective_start_90
while current <= TARGET:
    if current.weekday() < 5:
        r = simulate_day(current, df_5m, df_1d,
                         df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix,
                         require_adx=True,
                         ema_exit_confirm=bt.V2_EMA_EXIT_CONFIRM_CANDLES,
                         ema_confirm_backstop_pts=bt.V2_EMA_CONFIRM_BACKSTOP_PTS,
                         loss_cooldown_candles=bt.V2_LOSS_COOLDOWN_CANDLES)
        if r:
            results.append(r)
    current += timedelta(days=1)


def summarize(results, label):
    all_trades = [t for r in results for t in r["trades"]]
    n_days = len(results)
    n_trades = len(all_trades)
    for t in all_trades:
        t["net_pnl"] = t["pnl"] - COST_PER_TRADE
    gross_pnl = sum(t["pnl"] for t in all_trades)
    net_pnl = sum(t["net_pnl"] for t in all_trades)
    wins = [t["net_pnl"] for t in all_trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in all_trades if t["net_pnl"] < 0]
    win_rate = len(wins) / n_trades * 100 if n_trades else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses else float("inf")

    # recompute daily net pnl (trade costs applied) for day-level stats
    daily_net = {}
    for r in results:
        daily_net[r["date"]] = sum(t["net_pnl"] for t in r["trades"])
    day_vals = list(daily_net.values())
    win_days = sum(1 for v in day_vals if v > 0)
    loss_days = sum(1 for v in day_vals if v < 0)
    flat_days = sum(1 for v in day_vals if v == 0)
    best_day = max(day_vals, default=0)
    worst_day = min(day_vals, default=0)
    best_trade = max((t["net_pnl"] for t in all_trades), default=0)
    worst_trade = min((t["net_pnl"] for t in all_trades), default=0)

    equity = peak = max_dd = 0.0
    max_dd_date = None
    for d in sorted(daily_net):
        equity += daily_net[d]
        peak = max(peak, equity)
        dd = equity - peak
        if dd < max_dd:
            max_dd, max_dd_date = dd, d

    print("\n" + "=" * 60)
    print(f"SUMMARY -- V2 (live config), {label} ({n_days} trading days)")
    print("=" * 60)
    print(f"Total trades           : {n_trades}  ({n_trades/n_days:.2f}/day)" if n_days else "n/a")
    print(f"Win rate                : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Avg win / Avg loss      : Rs.{avg_win:+,.0f} / Rs.{avg_loss:+,.0f}")
    print(f"Profit factor           : {profit_factor:.2f}")
    print(f"Gross P&L               : Rs.{gross_pnl:+,.0f}")
    print(f"Net P&L (@Rs.{COST_PER_TRADE}/trade)  : Rs.{net_pnl:+,.0f}")
    print(f"Avg P&L/day              : Rs.{net_pnl/n_days:+,.0f}" if n_days else "n/a")
    print(f"Win days / Loss / Flat   : {win_days} / {loss_days} / {flat_days}")
    print(f"Best day / Worst day     : Rs.{best_day:+,.0f} / Rs.{worst_day:+,.0f}")
    print(f"Best trade / Worst trade : Rs.{best_trade:+,.0f} / Rs.{worst_trade:+,.0f}")
    print(f"Max drawdown             : Rs.{max_dd:+,.0f}  (as of {max_dd_date})")

    exit_reasons = {}
    for t in all_trades:
        exit_reasons[t["reason"]] = exit_reasons.get(t["reason"], 0) + 1
    print("\nExit reason breakdown:")
    for reason, cnt in sorted(exit_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason:<20} {cnt}")

    month_pnl = {}
    for d, v in daily_net.items():
        m = d[:7]
        month_pnl[m] = month_pnl.get(m, 0.0) + v
    print("\nMonthly breakdown:")
    for m, pnl in sorted(month_pnl.items()):
        print(f"  {m}: Rs.{pnl:+,.0f}")

    return {"net_pnl": net_pnl, "n_trades": n_trades, "n_days": n_days,
           "win_rate": win_rate, "profit_factor": profit_factor,
           "max_dd": max_dd, "worst_trade": worst_trade, "avg_pnl_day": net_pnl/n_days if n_days else 0}


results_45 = [r for r in results if r["date"] >= effective_start_45.isoformat()]
summarize(results_45, "last 45 days")
summarize(results, "last 90 days")
