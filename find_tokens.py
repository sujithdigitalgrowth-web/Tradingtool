"""Find NSE tokens for NIFTYBEES and BANKBEES from Angel One instrument master."""
import requests

url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
print("Downloading instrument master...")
data = requests.get(url, timeout=30).json()
print(f"Loaded {len(data)} instruments\n")

keywords = ["NIFTYBEES", "BANKBEES", "JUNIORBEES", "INDIAVIX"]
found = {}

for inst in data:
    sym = inst.get("symbol", "").upper()
    name = inst.get("name", "").upper()
    exch = inst.get("exch_seg", "")
    for kw in keywords:
        if kw in sym and exch == "NSE":
            if kw not in found:
                found[kw] = []
            found[kw].append({
                "symbol": inst.get("symbol"),
                "token" : inst.get("token"),
                "name"  : inst.get("name"),
                "exch"  : exch,
                "itype" : inst.get("instrumenttype"),
            })

for kw, items in found.items():
    print(f"=== {kw} ===")
    for i in items[:5]:
        print(f"  token={i['token']:>8}  symbol={i['symbol']:<25}  type={i['itype']}")
    print()
