import ccxt
import pandas as pd
import os
import time

# --- CONFIGURACIÓN ---
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data_futures') # Guardamos en carpeta separada
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

# LISTA COMPLETA (FUTUROS PERPETUOS)
# Binance Futures usa simbolos sin '/' en la API interna, pero CCXT los normaliza.
SYMBOLS = [
    # --- EQUIPO BREAKOUT (Hydra) ---
    'FLOKI/USDT', 'WIF/USDT', 'NEAR/USDT', 'INJ/USDT', 'BONK/USDT', 
    '1000PEPE/USDT', # Ojo: En Futuros suele ser 1000PEPE
    
    # --- EQUIPO REVERSIÓN (Shield) ---
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'ADA/USDT' 
]

TIMEFRAME = '4h'
START_DATE = "2023-01-01 00:00:00"

def download_futures_data():
    # INSTANCIAMOS BINANCE FUTURES
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'} # <--- LA CLAVE
    })
    
    since_ms = exchange.parse8601(START_DATE)
    print(f"🚀 DESCARGANDO FUTUROS PERPETUOS ({TIMEFRAME})...\n")

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
                # A veces el ticker cambia nombre (ej: 1000PEPE vs PEPE)
                print(f"❌ Error: {e}")
                break
        
        if not all_candles: continue

        df = pd.DataFrame(all_candles, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Date', inplace=True)
        df.drop(columns=['Timestamp'], inplace=True)
        
        # Nombre de archivo distintivo
        safe_symbol = symbol.replace('/', '')
        filename = f"{safe_symbol}_{TIMEFRAME}_FUTURES.csv"
        path = os.path.join(DATA_DIR, filename)
        
        df.to_csv(path)
        print(f" ✅ ({len(df)} velas)")

if __name__ == "__main__":
    download_futures_data()