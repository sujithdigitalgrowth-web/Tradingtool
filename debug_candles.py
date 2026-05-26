import yfinance as yf
import warnings
warnings.filterwarnings('ignore')
from datetime import date

ticker = yf.Ticker('^NSEBANK')
df = ticker.history(period='5d', interval='5m')
df.index = df.index.tz_convert('Asia/Kolkata')
today = df[df.index.date == date(2026, 5, 20)]
today = today.between_time('09:15', '15:30')

print(f"Total 5-min candles: {len(today)}")
print(f"\nFirst 15 candles:")
print(today[['Open','High','Low','Close']].head(15).to_string())
print(f"\nLast 5 candles:")
print(today[['Open','High','Low','Close']].tail(5).to_string())
print(f"\nDay range: Low={today['Low'].min():.2f}  High={today['High'].max():.2f}")

R1, S1 = 53466.94, 53351.39
above_r1 = today[today['Close'] >= R1 * 1.001]
below_s1 = today[today['Close'] <= S1 * 0.999]
print(f"\nGann: BASE=53409  R1={R1:.0f}  S1={S1:.0f}")
print(f"Candles closing ABOVE R1 breakout: {len(above_r1)}")
if not above_r1.empty:
    print(above_r1[['Close']].head(3).to_string())
print(f"Candles closing BELOW S1 breakdown: {len(below_s1)}")
if not below_s1.empty:
    print(below_s1[['Close']].head(3).to_string())

# Check for crossover pairs
closes = list(today['Close'])
times = list(today.index.strftime('%H:%M'))
for i in range(1, len(closes)):
    prev = closes[i-1]
    curr = closes[i]
    if prev <= R1 and curr >= R1 * 1.001:
        print(f"\nR1 BREAKOUT at {times[i]}: prev={prev:.0f} -> curr={curr:.0f}")
    if prev >= S1 and curr <= S1 * 0.999:
        print(f"\nS1 BREAKDOWN at {times[i]}: prev={prev:.0f} -> curr={curr:.0f}")
