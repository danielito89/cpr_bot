import os
import time
import pandas as pd
from binance.client import Client
from datetime import datetime

# --- CONFIGURACIÓN M15 ---
SYMBOLS = ["BTCUSDT"] 
START_DATE = "2022-01-01" 
END_DATE = "2025-16-12"   # Vamos a probar un periodo largo
INTERVAL = Client.KLINE_INTERVAL_15MINUTE # <--- CLAVE

# Carpeta de datos
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")

def download_data():
    client = Client(API_KEY, API_SECRET)
    
    for symbol in SYMBOLS:
        print(f"⏳ Bajando {symbol} en 15m desde {START_DATE}...")
        filename = f"mainnet_data_15m_{symbol}_2022-2024.csv"
        filepath = os.path.join(DATA_DIR, filename)
        
        # Descarga en bloques mensuales para no fallar
        klines = client.futures_historical_klines(
            symbol, 
            INTERVAL, 
            START_DATE, 
            END_DATE
        )
        
        if klines:
            cols = [
                'Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 
                'Close_Time', 'Quote_Asset_Volume', 'Number_of_Trades', 
                'Taker_Buy_Base', 'Taker_Buy_Quote', 'Ignore'
            ]
            df = pd.DataFrame(klines, columns=cols)
            
            # Limpieza
            df['Open_Time'] = pd.to_datetime(df['Open_Time'], unit='ms')
            for col in ['Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume']:
                df[col] = pd.to_numeric(df[col])
            
            df = df[['Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume']]
            
            df.to_csv(filepath, index=False)
            print(f"✅ Guardado: {filepath} ({len(df)} velas)")
        else:
            print("❌ No se descargaron datos.")

if __name__ == "__main__":
    download_data()