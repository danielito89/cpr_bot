import os
import pandas as pd
from binance.client import Client
from datetime import datetime

# --- CONFIGURACIÓN ---
# Cambia esto por el par que quieras bajar (ahora SÍ acepta 1000PEPEUSDT)
SYMBOL = "ETHUSDT" 

# Fecha de inicio (2 años atrás para tu prueba completa)
START_DATE = "2023-01-01"

DATA_DIR = os.path.join(os.path.dirname(__file__), "cpr_bot_v90", "data")
os.makedirs(DATA_DIR, exist_ok=True)

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")
# ---------------------

def download_klines(client, interval, start, end=None):
    print(f"Descargando datos FUTUROS {interval} para {SYMBOL} desde {start}...")
    
    # --- CAMBIO CRÍTICO: Usar el método de FUTUROS ---
    # Antes: client.get_historical_klines_generator (Spot)
    # Ahora: client.futures_historical_klines_generator (Futuros)
    klines = client.futures_historical_klines_generator(SYMBOL, interval, start, end)
    
    cols = [
        'Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 
        'Close_Time', 'Quote_Asset_Volume', 'Number_of_Trades', 
        'Taker_Buy_Base', 'Taker_Buy_Quote', 'Ignore'
    ]
    df = pd.DataFrame(klines, columns=cols)
    
    if df.empty:
        print(f"No se encontraron datos para {interval}. Fin.")
        return None

    # Limpieza y formato
    df['Open_Time'] = pd.to_datetime(df['Open_Time'], unit='ms')
    for col in ['Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume']:
        df[col] = pd.to_numeric(df[col])
    
    df = df[['Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume']]
    df.set_index('Open_Time', inplace=True)
    return df

def run_download():
    # No necesitas API Keys para datos históricos públicos, pero es mejor ponerlas si las tienes
    client = Client(API_KEY, API_SECRET) 
    end_date = datetime.now().strftime("%Y-%m-%d")

    # 1. Datos de 1 HORA
    df_1h = download_klines(client, Client.KLINE_INTERVAL_1HOUR, START_DATE, end_date)
    if df_1h is not None:
        filename = f"mainnet_data_1h_{SYMBOL}.csv"
        df_1h.to_csv(os.path.join(DATA_DIR, filename))
        print(f"Guardado: {filename} ({len(df_1h)} filas)")

    # 2. Datos de 1 DÍA
    df_1d = download_klines(client, Client.KLINE_INTERVAL_1DAY, START_DATE, end_date)
    if df_1d is not None:
        filename = f"mainnet_data_1d_{SYMBOL}.csv"
        df_1d.to_csv(os.path.join(DATA_DIR, filename))
        print(f"Guardado: {filename} ({len(df_1d)} filas)")

    # 3. Datos de 1 MINUTO
    print("Iniciando descarga de 1m... esto tomará tiempo.")
    df_1m = download_klines(client, Client.KLINE_INTERVAL_1MINUTE, START_DATE, end_date)
    if df_1m is not None:
        filename = f"mainnet_data_1m_{SYMBOL}.csv"
        df_1m.to_csv(os.path.join(DATA_DIR, filename))
        print(f"Guardado: {filename} ({len(df_1m)} filas)")
    
    print("\n¡Descarga completa!")

if __name__ == "__main__":
    run_download()