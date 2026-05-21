from backtest import run_range
from datetime import date, datetime
import json, os

results = run_range(date(2026, 4, 1), date(2026, 5, 20))
cache = {
    "start": "2026-04-01",
    "end": "2026-05-20",
    "generated": datetime.now().strftime("%d %b %Y %H:%M"),
    "results": results
}
os.makedirs("logs", exist_ok=True)
with open("logs/range_cache.json", "w") as f:
    json.dump(cache, f, indent=2, default=str)

total = sum(r["daily_pnl"] for r in results)
wins  = sum(1 for r in results if r["daily_pnl"] > 0)
print(f"\nDone: {len(results)} trading days")
print(f"Total PnL : Rs.{total:,.2f}")
print(f"Win days  : {wins}/{len(results)}")
