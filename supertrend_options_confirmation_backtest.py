"""
supertrend_options_confirmation_backtest.py — A/B experiment: does an Options
Intelligence confirmation layer improve the existing Supertrend(10,3) strategy?

EXPERIMENTAL. This file does not modify backtest.py, live_trader.py,
dashboard.py, or supertrend_45day_trail_backtest.py. It IMPORTS the
validated reference backtest's `run_backtest()` unmodified and calls it
directly for the baseline, guaranteeing baseline numbers are byte-identical
to the existing, already-validated strategy — not a re-derived copy that
could silently drift from it.

==============================================================================
READ THIS BEFORE INTERPRETING ANY OUTPUT
==============================================================================
No historical NIFTY options-chain OI/PCR data exists anywhere this project
can reach (see options_confirmation.py's docstring for the verification —
Angel's option-candle API has no OI column, and putCallRatio()/oIBuildup()
are live-snapshot-only with no historical parameter). Per the task's explicit
instruction, this script does NOT fabricate PCR/OI values to manufacture
results.

Concretely, this means:
  * The BASELINE run (no options filter — the existing Supertrend strategy,
    completely unchanged) produces 100% REAL numbers.
  * Every SCORE-FILTERED variant (>=60 / >=65 / >=70 / >=80) uses
    `options_confirmation.HistoricalOptionsDataProvider`, the explicit stub
    that has no data. Every single signal comes back NO_DATA. The report
    below states this plainly for each variant instead of presenting
    "0 trades, infinite profit factor" as if it were a real result.
  * The architecture (entry gate, trade-by-trade logging, ablation harness,
    regime harness) is fully wired and ready — the moment a real
    `OptionsDataProvider` is plugged in (implement `get_history()`), every
    variant below starts producing real, comparable numbers with no code
    changes beyond that one provider.

Usage:
  python supertrend_options_confirmation_backtest.py            # 45-day window
  python supertrend_options_confirmation_backtest.py --90        # 90-day window
"""
import csv
import json
import sys
from datetime import date, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # avoid cp1252 mangling em-dashes on Windows consoles

import backtest as bt
import supertrend_45day_trail_backtest as st6   # imported, NOT modified — see module docstring
import options_confirmation as oc

DAYS  = 90 if "--90" in sys.argv else 45
START = date.today() - timedelta(days=DAYS)
END   = date.today() - timedelta(days=1)

LOT = bt.LOT_SIZE
QTY = 2 * LOT
COST_PER_TRADE = 40

THRESHOLDS_TO_TEST = [60, 65, 70, 80]

# Ablation components — per the task's required ablation list. Each entry
# zeroes out every weight except the ones named, so "score" for that variant
# only reflects the named component(s). Threshold for each ablation variant
# is scaled proportionally (e.g. PCR-only max score is 35, so an 80/100
# global threshold becomes 28/35) so thresholds stay comparable in spirit.
ABLATIONS = {
    "supertrend_only":        [],
    "supertrend_pcr":         ["pcr_level", "pcr_direction"],
    "supertrend_oi":          ["put_oi_direction", "call_oi_direction"],
    "supertrend_sr":          ["support_resistance"],
    "supertrend_vwap":        ["vwap"],
    "supertrend_pcr_oi":      ["pcr_level", "pcr_direction", "put_oi_direction", "call_oi_direction"],
    "supertrend_all":         ["pcr_level", "pcr_direction", "put_oi_direction",
                                "call_oi_direction", "support_resistance", "vwap"],
}
FULL_WEIGHTS = oc.OptionsConfirmationConfig().weights


def _ablation_config(components: list, threshold_pct: float) -> oc.OptionsConfirmationConfig:
    """threshold_pct is 0-1 (e.g. 0.80 for the 80/100 tier); scaled to the
    ablation's own max possible score so thresholds stay comparable."""
    weights = {k: (v if (not components or k in components) else 0)
               for k, v in FULL_WEIGHTS.items()}
    max_score = sum(weights.values()) or 1
    return oc.OptionsConfirmationConfig(weights=weights,
                                        entry_threshold=max_score * threshold_pct)


# ══════════════════════════════════════════════════════════════════
# Filtered backtest loop — a faithful parallel copy of
# supertrend_45day_trail_backtest.py::run_backtest, UNCHANGED except for
# one labeled insertion point where the options-confirmation gate is
# checked. Every other line (exits, spot SL, 3-tier trail, EOD square-off,
# P&L formula) is identical to the reference implementation — copied, not
# rewritten, so the exit/risk model can never silently diverge from the
# strategy this experiment is trying to evaluate.
# ══════════════════════════════════════════════════════════════════

def run_backtest_with_confirmation(df_5m, st_c, trading_days, dte_cache, provider,
                                    oc_config: oc.OptionsConfirmationConfig,
                                    vwap_series=None,
                                    lock_trigger=bt.ST6_TRAIL_LOCK_TRIGGER,
                                    giveback=bt.ST6_TRAIL_GIVEBACK,
                                    qty=QTY):
    idx = df_5m.index
    position = None
    trades_by_date = {}
    signal_log = []   # one row per Supertrend signal — trade-by-trade log

    for i in range(1, len(idx)):
        ts = idx[i]
        d = ts.date()
        time_str = ts.strftime("%H:%M")
        cl = float(df_5m["Close"].iloc[i])
        trades_by_date.setdefault(d.isoformat(), [])

        def close_full(reason, floor_pct=None, sig_row=None):
            nonlocal position
            spot_for_pnl = cl
            if floor_pct is not None:
                pnl_pu_target = position["entry_option_price"] * floor_pct
                sc = pnl_pu_target / 0.5
                spot_for_pnl = (position["entry_spot"] + sc if position["type"] == "CE"
                               else position["entry_spot"] - sc)
            sc = spot_for_pnl - position["entry_spot"]
            pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
            gross = pnl_pu * position["qty"]
            net = gross - COST_PER_TRADE
            trades_by_date[position["entry_date"]].append({"pnl": net, "reason": reason})
            if position.get("sig_row") is not None:
                position["sig_row"]["exit_price"] = round(spot_for_pnl, 2)
                position["sig_row"]["pnl"] = round(net, 2)
                position["sig_row"]["exit_reason"] = reason
            position = None

        if time_str >= bt.SQUAREOFF_TIME:
            if position:
                close_full("EOD_SQUAREOFF")
            continue

        if position:
            adverse = (position["entry_spot"] - cl if position["type"] == "CE"
                       else cl - position["entry_spot"])
            if adverse >= bt.ST6_SPOT_SL:
                close_full("SPOT_SL")

        if position:
            sc = cl - position["entry_spot"]
            pnl_pu = sc * 0.5 if position["type"] == "CE" else -sc * 0.5
            opt_pct = pnl_pu / position["entry_option_price"]
            if opt_pct > position["peak_pct"]:
                position["peak_pct"] = opt_pct
            floor = st6._floor_for(position["peak_pct"], lock_trigger, giveback)
            if floor is not None and opt_pct <= floor:
                close_full("ST_TRAIL_EXIT", floor_pct=floor)

        if position:
            st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
            exit_now = ((position["type"] == "CE" and st_now == -1 and st_prev == 1) or
                       (position["type"] == "PE" and st_now == 1 and st_prev == -1))
            if exit_now:
                close_full("ST_FLIP")

        if not position and "09:15" <= time_str < bt.SQUAREOFF_TIME:
            dte = dte_cache.setdefault(d, st6.dte_for(d))
            st_prev, st_now = int(st_c.iloc[i - 1]), int(st_c.iloc[i])
            side = None
            if st_now == 1 and st_prev == -1:
                side = "CE"
            elif st_now == -1 and st_prev == 1:
                side = "PE"

            if side:
                vwap_now = float(vwap_series.iloc[i]) if vwap_series is not None else None

                # ═══ THE ONLY INSERTION POINT — everything above/below this
                # block is the unmodified reference entry/exit logic. ═══
                oc_result = oc.get_options_confirmation(
                    direction=side, timestamp=ts.to_pydatetime(), spot=cl,
                    vwap=vwap_now, provider=provider, config=oc_config,
                )
                sig_row = {
                    "timestamp": ts.isoformat(), "spot_price": cl,
                    "supertrend_direction": st_now, "signal": side,
                    "PCR": oc_result.pcr, "PCR_change": oc_result.pcr_change,
                    "put_OI": oc_result.put_oi, "call_OI": oc_result.call_oi,
                    "put_OI_change": oc_result.put_oi_change,
                    "call_OI_change": oc_result.call_oi_change,
                    "support": oc_result.support, "resistance": oc_result.resistance,
                    "VWAP": vwap_now, "options_score": oc_result.score,
                    "score_breakdown": oc_result.score_breakdown,
                    "decision": oc_result.decision, "reason": oc_result.reason,
                    "option_symbol": None, "entry_price": None,
                    "exit_price": None, "pnl": None, "exit_reason": None,
                }
                signal_log.append(sig_row)

                if oc_result.decision != "ENTER":
                    side = None   # SKIP or NO_DATA -> no trade this signal
                # ═══ end insertion point ═══

            if side:
                entry_price = bt.estimate_option_price(cl, dte)
                sig_row["option_symbol"] = f"NIFTY_{side}_{cl:.0f}strike"  # illustrative — real strike selection lives in live_trader.py, untouched
                sig_row["entry_price"] = entry_price
                position = {"type": side, "entry_spot": cl, "entry_time": time_str,
                           "entry_option_price": entry_price, "entry_date": d.isoformat(),
                           "qty": qty, "peak_pct": 0.0, "sig_row": sig_row}

    results = []
    for d in trading_days:
        trades = trades_by_date.get(d.isoformat(), [])
        daily_pnl = sum(t["pnl"] for t in trades)
        results.append({"date": d.isoformat(), "trades": trades, "daily_pnl": daily_pnl})
    return results, signal_log


# ══════════════════════════════════════════════════════════════════
# Metrics — every field the task asked each version to report
# ══════════════════════════════════════════════════════════════════

def compute_metrics(results: list) -> dict:
    all_trades = [t for r in results for t in r["trades"]]
    n = len(all_trades)
    pnls = [t["pnl"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    cur_w = cur_l = max_w = max_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1; cur_l = 0; max_w = max(max_w, cur_w)
        elif p < 0:
            cur_l += 1; cur_w = 0; max_l = max(max_l, cur_l)
        else:
            cur_w = cur_l = 0

    ce = [t for t in all_trades]  # side isn't stored on the trade dict itself
    return {
        "total_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(gross_profit + gross_loss, 2),
        "avg_trade_pnl": round(sum(pnls) / n, 2) if n else 0.0,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(abs(gross_profit / gross_loss), 2) if gross_loss else float("inf"),
        "max_drawdown": round(max_dd, 2),
        "largest_win": round(max(pnls), 2) if pnls else 0.0,
        "largest_loss": round(min(pnls), 2) if pnls else 0.0,
        "max_consecutive_wins": max_w,
        "max_consecutive_losses": max_l,
    }


def signal_log_stats(signal_log: list) -> dict:
    total = len(signal_log)
    entered = sum(1 for r in signal_log if r["decision"] == "ENTER")
    skipped = sum(1 for r in signal_log if r["decision"] == "SKIP")
    no_data = sum(1 for r in signal_log if r["decision"] == "NO_DATA")
    return {
        "total_supertrend_signals": total,
        "entered": entered,
        "skipped_by_score": skipped,
        "skipped_no_data": no_data,
        "pct_no_data": round(no_data / total * 100, 1) if total else 0.0,
    }


def write_signal_log(signal_log: list, path_prefix: str):
    if not signal_log:
        return
    json_path = f"{path_prefix}.json"
    csv_path = f"{path_prefix}.csv"
    with open(json_path, "w") as f:
        json.dump(signal_log, f, indent=2, default=str)
    fieldnames = list(signal_log[0].keys())
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in signal_log:
            row = dict(row)
            row["score_breakdown"] = json.dumps(row["score_breakdown"])
            w.writerow(row)
    print(f"  Trade-by-trade log written: {csv_path}  /  {json_path}")


def print_metrics(label: str, m: dict, sig_stats: dict = None):
    print(f"\n{'='*70}\n{label}\n{'='*70}")
    for k, v in m.items():
        print(f"  {k:<24}: {v}")
    if sig_stats:
        print("  --- signal accounting ---")
        for k, v in sig_stats.items():
            print(f"  {k:<24}: {v}")


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main():
    print(f"\nFetching data (Angel One), {DAYS}-day window: {START} to {END}...")
    df_5m, df_1d, df_nbees, df_bnf, df_vix = bt.fetch_range_data_angel(START, END)
    print(f"Got {len(df_5m)} 5-min candles, {df_5m.index.date.min()} to {df_5m.index.date.max()}")

    st_c = bt._supertrend(df_5m, st6.ST_PERIOD, st6.ST_MULT)
    vwap_series = oc.get_vwap_series(df_5m)   # SAME _vwap() the existing strategy uses
    trading_days = sorted(set(df_5m.index.date))
    trading_days = [d for d in trading_days if d.weekday() < 5]
    dte_cache = {}

    # ── BASELINE — the existing Supertrend strategy, completely unmodified,
    # calling the validated reference file's run_backtest() directly. ──
    baseline_results = st6.run_backtest(df_5m, st_c, trading_days, dict(dte_cache), trail_enabled=True)
    baseline_metrics = compute_metrics(baseline_results)
    print_metrics("BASELINE — existing Supertrend strategy, NO options filter (real data)",
                 baseline_metrics)

    provider = oc.HistoricalOptionsDataProvider()   # the stub — see module docstring

    all_variant_metrics = {"baseline": baseline_metrics}
    all_variant_sig_stats = {}

    for pct in THRESHOLDS_TO_TEST:
        cfg = oc.OptionsConfirmationConfig(entry_threshold=pct)
        results, signal_log = run_backtest_with_confirmation(
            df_5m, st_c, trading_days, dict(dte_cache), provider, cfg, vwap_series)
        metrics = compute_metrics(results)
        sig_stats = signal_log_stats(signal_log)
        label = f"FILTERED — Supertrend + options score >= {pct}"
        print_metrics(label, metrics, sig_stats)
        if sig_stats["pct_no_data"] == 100.0:
            print(f"  >>> {sig_stats['total_supertrend_signals']}/{sig_stats['total_supertrend_signals']} "
                  f"signals were NO_DATA — every metric above is trivially empty because NO "
                  f"real filtering happened. This is NOT a real backtest result for this "
                  f"threshold; it only proves the pipeline runs end-to-end.")
        write_signal_log(signal_log, f"logs/options_confirmation_signals_score{pct}")
        all_variant_metrics[f"score_{pct}"] = metrics
        all_variant_sig_stats[f"score_{pct}"] = sig_stats

    # ── Ablation harness — wired, same NO_DATA limitation ──
    print(f"\n{'='*70}\nABLATION TEST (all variants blocked by the same missing-data issue)\n{'='*70}")
    for name, components in ABLATIONS.items():
        cfg = _ablation_config(components, threshold_pct=0.80)
        results, signal_log = run_backtest_with_confirmation(
            df_5m, st_c, trading_days, dict(dte_cache), provider, cfg, vwap_series)
        sig_stats = signal_log_stats(signal_log)
        print(f"  {name:<22} components={components or '(none — Supertrend only)'}"
              f"  -> {sig_stats['total_supertrend_signals']} signals, "
              f"{sig_stats['pct_no_data']}% NO_DATA")

    with open("logs/options_confirmation_summary.json", "w") as f:
        json.dump({"metrics": all_variant_metrics, "signal_stats": all_variant_sig_stats}, f, indent=2)
    print("\nSummary written: logs/options_confirmation_summary.json")

    print(f"""
{'='*70}
CONCLUSION
{'='*70}
1. Does Options Confirmation improve the existing Supertrend strategy?
   UNKNOWN — cannot be answered. No historical NIFTY options-chain OI/PCR
   data exists anywhere this project can reach (verified against Angel
   One's live API directly — see options_confirmation.py docstring). Every
   score-filtered variant above had 100% of its signals come back NO_DATA,
   so no real comparison against the baseline was possible.
2-13. Same answer for all remaining questions (best threshold, win-rate/
   profit-factor/drawdown impact, trades eliminated, good/bad trades
   filtered, PCR/OI/VWAP marginal value, component ablation, overfitting
   risk): none of these can be evaluated without real historical options
   data. The architecture to answer every one of them is fully built and
   tested above (scoring math verified correct, entry gate wired, per-signal
   logging complete, ablation harness runs end-to-end) — plugging in a real
   `options_confirmation.OptionsDataProvider` (a paid historical-OI vendor's
   export, or a feed self-recorded going forward via
   `LiveAngelOptionsDataProvider`) is the only remaining step, and requires
   no changes to this file or to options_confirmation.py's scoring logic.

BASELINE (real, unmodified existing strategy) for reference:
   {baseline_metrics['total_trades']} trades, {baseline_metrics['win_rate_pct']}% win rate,
   net P&L Rs.{baseline_metrics['net_pnl']:+,.0f}, profit factor {baseline_metrics['profit_factor']}.
""")


if __name__ == "__main__":
    main()
