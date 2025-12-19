import os
import pandas as pd
from binance.client import Client
from datetime import datetime, timedelta
import time

# --- CONFIGURACIÓN ---
SYMBOLS = ["ETHUSDT", "BTCUSDT", "BNBUSDT", "1000PEPEUSDT"]
INTERVAL = Client.KLINE_INTERVAL_1HOUR  # <--- CLAVE: 1 HORA
START_DATE = "1 Jan, 2021"
END_DATE = "now"
OUTPUT_FOLDER = "data"
# ---------------------

def download_data(symbol, interval, start_str, end_str, folder):
    client = Client() # No hace falta API Key para datos públicos
    print(f"⬇️ Descargando {symbol} ({interval}) desde {start_str}...")
    
    klines = client.get_historical_klines(symbol, interval, start_str, end_str)
    
    data = []
    for k in klines:
        data.append({
            "timestamp": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "quote_asset_volume": float(k[7]), # Volumen en USDT
            "number_of_trades": int(k[8])
        })
    
    df = pd.DataFrame(data)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    if not os.path.exists(folder):
        os.makedirs(folder)
    
    # Nombre compatible con tu Backtester V19
    # Formato: mainnet_data_1h_ETHUSDT.csv
    filename = f"{folder}/mainnet_data_1h_{symbol}.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Guardado: {filename} ({len(df)} velas)")

if __name__ == "__main__":
    for sym in SYMBOLS:
        try:
            download_data(sym, INTERVAL, START_DATE, END_DATE, OUTPUT_FOLDER)
            time.sleep(1) # Respetar limites de API
        except Exception as e:
            print(f"❌ Error descargando {sym}: {e}")