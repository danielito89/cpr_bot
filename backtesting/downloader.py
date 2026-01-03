import ccxt
import pandas as pd
import os
import time

# --- CONFIGURACIÓN ---
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

# LISTA DEFINITIVA "HIGH OCTANE" (Squeeze Friendly)
SYMBOLS = [
    'BTC/USDT',   # Solo para filtro Macro
    # --- TIER S (Los Reyes del Breakout) ---
    'SOL/USDT', 'INJ/USDT', 'RNDR/USDT', 'FET/USDT',
    # --- NEW STARS (Ciclo 2024) ---
    'JUP/USDT', 'PYTH/USDT', 'SEI/USDT', 'TIA/USDT', 'SUI/USDT',
    # --- MEME LORDS (High Risk/High Reward) ---
    'DOGE/USDT', 'WIF/USDT', 'BONK/USDT', 'FLOKI/USDT',
    # --- PENDIENTES DE VALIDAR ---
    'NEAR/USDT', 'APT/USDT'
]

TIMEFRAME = '4h'
START_DATE = "2023-01-01 00:00:00"

def download_data():
    exchange = ccxt.binance({'enableRateLimit': True})
    since_ms = exchange.parse8601(START_DATE)
    
    print(f"🚀 DESCARGANDO DATA 'HIGH OCTANE' ({TIMEFRAME})...\n")

    for symbol in SYMBOLS:
        print(f"⏳ {symbol}...", end=" ")
        all_candles = []
        current_since = since_ms
        
        while True:
            try:
                candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=current_since, limit=1000)
                if not candles: break
                all_candles += candles
                current_since = candles[-1][0] + 1
                if len(candles) < 1000: break
                time.sleep(exchange.rateLimit / 1000)
                print(".", end="", flush=True)
            except Exception as e:
                print(f"❌ Error: {e}")
                break
        
        if not all_candles: continue

        df = pd.DataFrame(all_candles, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Date', inplace=True)
        df.drop(columns=['Timestamp'], inplace=True)
        
        safe_symbol = symbol.replace('/', '_')
        if safe_symbol == 'PEPE_USDT': safe_symbol = '1000PEPE_USDT'
            
        path = os.path.join(DATA_DIR, f"{safe_symbol}_{TIMEFRAME}_FULL.csv")
        df.to_csv(path)
        print(f" ✅ ({len(df)})")

if __name__ == "__main__":
    download_data()