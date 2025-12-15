import ccxt
import pandas as pd
import time
import os
from datetime import datetime, timezone

# 📋 LISTA DE ACTIVOS
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "1000PEPE/USDT"] 
SINCE_STR = "2023-01-01 00:00:00"

def fetch_symbol(symbol):
    print(f"\n📡 DESCARGANDO: {symbol} (Futuros)...")
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'} # CLAVE
    })
    
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
            
            print(f"   📥 {len(all_funding)} datos acumulados... (Último: {datetime.fromtimestamp(last_ts/1000)})")
            
            if len(rates) < 1000: break
            time.sleep(0.2)
            
        except Exception as e:
            print(f"❌ Error: {e}")
            break
            
    if all_funding:
        df = pd.DataFrame(all_funding)
        # Limpieza nombre archivo (1000PEPE/USDT -> 1000PEPEUSDT)
        safe_sym = symbol.replace("/", "")
        path = f"data/funding_{safe_sym}.csv"
        os.makedirs('data', exist_ok=True)
        df.to_csv(path, index=False)
        print(f"✅ GUARDADO: {path}")

if __name__ == "__main__":
    for sym in SYMBOLS:
        fetch_symbol(sym)