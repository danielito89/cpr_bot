import ccxt
import pandas as pd
import time
from datetime import datetime, timezone
import matplotlib.pyplot as plt

# 🪙 MONEDAS A COMPARAR
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "1000PEPE/USDT"] 
SINCE_STR = "2023-01-01 00:00:00" # Miremos desde 2023 (Post-FTX)

def fetch_and_calculate(symbol):
    print(f"📡 Analizando {symbol}...")
    exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
    since_ts = int(datetime.strptime(SINCE_STR, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
    
    all_funding = []
    
    while True:
        try:
            rates = exchange.fetch_funding_rate_history(symbol, since_ts, limit=1000)
            if not rates: break
            all_funding.extend(rates)
            last_ts = rates[-1]['timestamp']
            if last_ts == since_ts: since_ts += 1
            else: since_ts = last_ts + 1
            if len(rates) < 1000: break
            time.sleep(0.1)
        except Exception as e:
            print(f"Error {symbol}: {e}")
            break
            
    if not all_funding: return 0, []
    
    df = pd.DataFrame(all_funding)
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('datetime', inplace=True)
    
    # Simular Retorno Compuesto
    balance = 10000
    equity = []
    
    for rate in df['fundingRate']:
        # Si rate es positivo, cobramos. Si es negativo, pagamos.
        payout = balance * rate
        balance += payout
        equity.append(balance)
        
    total_ret = (balance - 10000) / 10000 * 100
    return total_ret, equity

def main():
    print(f"📊 COMPARATIVA DE CASH & CARRY ({SINCE_STR} - Hoy)\n")
    results = {}
    
    for sym in SYMBOLS:
        ret, curve = fetch_and_calculate(sym)
        results[sym] = ret
        print(f"   👉 {sym}: {ret:.2f}% Acumulado")

    print("\n🏆 RANKING DE RENTABILIDAD:")
    sorted_res = dict(sorted(results.items(), key=lambda item: item[1], reverse=True))
    for sym, ret in sorted_res.items():
        print(f"   {sym:<15} {ret:.2f}%")

if __name__ == "__main__":
    main()