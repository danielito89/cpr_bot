import ccxt
import pandas as pd
import os
import time
from datetime import datetime

# --- CONFIGURACIÓN ---
# Carpeta donde se guardarán los CSV
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# La lista "ALPHA SQUAD" + BTC
# Nota: En Binance Spot es 'PEPE/USDT', no '1000PEPE'. El script lo renombrará si quieres.
SYMBOLS = [
    'BTC/USDT',
    'SOL/USDT',
    'INJ/USDT',
    'NEAR/USDT',
    'SUI/USDT',
    'APT/USDT',
    'FET/USDT',
    'RNDR/USDT',
    'ARKM/USDT',
    'WLD/USDT',
    'DOGE/USDT',
    'WIF/USDT',
    'PEPE/USDT',  # En el sim lo llamamos 1000PEPE, aquí bajamos PEPE normal
    'BONK/USDT'
]

TIMEFRAME = '4h'
START_DATE = "2023-01-01 00:00:00"

def download_data():
    # Usamos Binance Spot por defecto
    exchange = ccxt.binance({'enableRateLimit': True})
    
    # Convertir fecha inicio a timestamp milisegundos
    since_ms = exchange.parse8601(START_DATE)
    
    print(f"🚀 INICIANDO DESCARGA DE DATOS ({TIMEFRAME}) DESDE {START_DATE}...")
    print(f"📂 Destino: {DATA_DIR}\n")

    for symbol in SYMBOLS:
        print(f"⏳ Descargando {symbol}...", end=" ")
        
        all_candles = []
        current_since = since_ms
        
        while True:
            try:
                candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=current_since, limit=1000)
                
                if not candles:
                    break
                
                all_candles += candles
                
                # Actualizar tiempo para la siguiente página
                current_since = candles[-1][0] + 1
                
                # Si la última vela es reciente, terminamos
                if len(candles) < 1000:
                    break
                    
                # Respetar rate limit
                time.sleep(exchange.rateLimit / 1000)
                print(".", end="", flush=True)
                
            except Exception as e:
                print(f"\n❌ Error descargando {symbol}: {e}")
                break
        
        if not all_candles:
            print("❌ Vacío.")
            continue

        # Convertir a DataFrame
        df = pd.DataFrame(all_candles, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Date', inplace=True)
        df.drop(columns=['Timestamp'], inplace=True)
        
        # Guardar CSV
        # Ajuste de nombre para PEPE (para que coincida con tu config si usabas 1000PEPE)
        safe_symbol = symbol.replace('/', '_')
        if safe_symbol == 'PEPE_USDT': safe_symbol = '1000PEPE_USDT'
            
        filename = f"{safe_symbol}_{TIMEFRAME}_FULL.csv"
        path = os.path.join(DATA_DIR, filename)
        
        df.to_csv(path)
        print(f" ✅ OK! ({len(df)} velas)")

    print("\n✨ DESCARGA COMPLETADA.")

if __name__ == "__main__":
    download_data()