"""
Compare V2 strategy with VIX_MIN=13 (current live setting) vs VIX_MIN=12 (proposed).
How many extra trades would VIX_MIN=12 have unlocked, and what's the P&L impact?

Period: last 30 calendar days.

NOTE: VIX comes directly from Angel One's India VIX index (token 99926017),
NOT Yahoo Finance. bt.fetch_range_data_angel()'s built-in Yahoo VIX fetch was
found to silently fail (rate-limited) and swallow the exception, leaving
df_vix empty -- which disables the VIX filter entirely without any warning.
That bug produced a false "VIX never blocked anything" result in an earlier
run of this script. Fetching VIX from Angel One directly avoids it.
"""
from datetime import date, timedelta
import angel_data as ad
import backtest as bt

VIX_TOKEN = "99926017"

END   = date.today() - timedelta(days=1)
START = END - timedelta(days=30)

VIX_MINS = [13, 12]

print(f"\nBacktest range: {START} to {END}")
print("Fetching data...\n")

df_5m, df_1d, df_nbees, df_bnf = ad.fetch_all(START, END)

print("Fetching real India VIX history from Angel One...")
_, auth_token, api_key = ad._angel_login()
df_vix = ad._fetch_daily(auth_token, api_key, VIX_TOKEN, START, END)
print(f"  {len(df_vix)} VIX rows fetched\n")
print(df_vix[["Close"]])

bt.QTY = 1 * bt.LOT_SIZE   # 1 lot — matches live config

all_results = {}
for vmin in VIX_MINS:
    bt.V2_VIX_MIN = vmin
    rows = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            r = bt.simulate_day(current, df_5m, df_1d,
                                df_nbees=df_nbees, df_bnf=df_bnf, df_vix=df_vix)
            if r:
                rows.append(r)
        current += timedelta(days=1)
    all_results[vmin] = rows

bt.V2_VIX_MIN = 13  # reset to live default


def trades_of(results):
    return [t for r in results for t in r.get("trades", []) if t["reason"] != "PARTIAL_TP"]


base_results = all_results[13]
prop_results = all_results[12]
base_trades  = trades_of(base_results)
prop_trades  = trades_of(prop_results)

base_days_by_date = {r["date"]: r for r in base_results}
prop_days_by_date = {r["date"]: r for r in prop_results}

print(f"\n{'DATE':<12} {'DOW':<4} {'@13':<28} {'@12':<28}")
print("-" * 90)

new_trade_days = []
for day in sorted(set(base_days_by_date) | set(prop_days_by_date)):
    rb = base_days_by_date.get(day)
    rp = prop_days_by_date.get(day)
    dow = date.fromisoformat(day).strftime("%a")

    note_b = ((rb.get("insights") or [""])[0] if rb else "") or ""
    note_p = ((rp.get("insights") or [""])[0] if rp else "") or ""

    b_desc = f"{len(rb.get('trades', [])) if rb else 0} trades  {note_b[:20]}"
    p_desc = f"{len(rp.get('trades', [])) if rp else 0} trades  {note_p[:20]}"

    blocked_at_13 = "VIX" in note_b and (not rb or not rb.get("trades"))
    has_trades_12 = rp and rp.get("trades")

    flag = "  <-- unlocked by VIX_MIN=12" if (blocked_at_13 and has_trades_12) else ""
    print(f"{day:<12} {dow:<4} {b_desc:<28} {p_desc:<28}{flag}")

    if blocked_at_13 and has_trades_12:
        new_trade_days.append((day, rp))

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"VIX_MIN=13 (current) : {len(base_trades)} trades, total P&L Rs.{sum(r['daily_pnl'] for r in base_results):,.0f}")
print(f"VIX_MIN=12 (proposed): {len(prop_trades)} trades, total P&L Rs.{sum(r['daily_pnl'] for r in prop_results):,.0f}")
print(f"Extra trades from lowering to 12: {len(prop_trades) - len(base_trades)}")
print(f"Extra days unlocked (VIX 12-13 range): {len(new_trade_days)}")

if new_trade_days:
    print("\nDetail of newly-unlocked trades:")
    for day, r in new_trade_days:
        for t in r.get("trades", []):
            print(f"  {day} {t.get('time','?')}->{t.get('exit_time','?')}  "
                  f"{t['side']} {t.get('strike','?')}  pnl={t['pnl']:+,.0f}  reason={t['reason']}")

extra_pnl = sum(r["daily_pnl"] for _, r in new_trade_days)
print(f"\nP&L contribution from newly-unlocked days: Rs.{extra_pnl:+,.0f}")
