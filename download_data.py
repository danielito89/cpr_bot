import os
import pandas as pd
from binance.client import Client
from datetime import datetime

# --- CONFIGURACIÓN ---
# ¡Asegúrate de que estas variables de entorno estén configuradas!
# Usa tus claves de MAINNET para descargar datos reales
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")
SYMBOL = "BTCUSDT"
START_DATE = "2023-01-01" # 1-2 años es un buen comienzo

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
# ---------------------

# Usar el cliente de Mainnet (testnet=False)
client = Client(API_KEY, API_SECRET, testnet=False) 

def download_klines(interval, start, end=None):
    print(f"Descargando datos {interval} para {SYMBOL} desde {start}...")
    
    # El generador maneja la paginación por nosotros
    klines = client.get_historical_klines_generator(SYMBOL, interval, start, end)
    
    # Columnas de klines
    cols = [
        'Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 
        'Close_Time', 'Quote_Asset_Volume', 'Number_of_Trades', 
        'Taker_Buy_Base', 'Taker_Buy_Quote', 'Ignore'
    ]
    df = pd.DataFrame(klines, columns=cols)
    
    if df.empty:
        print(f"No se encontraron datos para {interval}. Fin.")
        return None

    # Convertir a tipos de datos correctos
    df['Open_Time'] = pd.to_datetime(df['Open_Time'], unit='ms')
    for col in ['Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume']:
        df[col] = pd.to_numeric(df[col])
    
    # Seleccionar columnas que nos importan
    df = df[['Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume']]
    df.set_index('Open_Time', inplace=True)
    return df

def run_download():
    end_date = datetime.now().strftime("%Y-%m-%d")

    # --- DATOS DE 1 HORA (Para EMA, ATR) ---
    df_1h = download_klines(Client.KLINE_INTERVAL_1HOUR, START_DATE, end_date)
    if df_1h is not None:
        df_1h.to_csv(os.path.join(DATA_DIR, "mainnet_data_1h.csv"))
        print(f"Datos de 1h guardados! {len(df_1h)} filas.")

    # --- DATOS DE 1 DÍA (Para Pivotes) ---
    df_1d = download_klines(Client.KLINE_INTERVAL_1DAY, START_DATE, end_date)
    if df_1d is not None:
        df_1d.to_csv(os.path.join(DATA_DIR, "mainnet_data_1d.csv"))
        print(f"Datos de 1d guardados! {len(df_1d)} filas.")

    # --- DATOS DE 1 MINUTO (Para simulación) ---
    print("Iniciando descarga de 1m... esto puede tardar varios minutos.")
    df_1m = download_klines(Client.KLINE_INTERVAL_1MINUTE, START_DATE, end_date)
    if df_1m is not None:
        df_1m.to_csv(os.path.join(DATA_DIR, "mainnet_data_1m.csv"))
        print(f"Datos de 1m guardados! {len(df_1m)} filas.")
    
    print("¡Descarga completa!")

if __name__ == "__main__":
    # Asegurarse de que las claves API están presentes
    if not API_KEY or not API_SECRET:
        print("Error: Las variables de entorno BINANCE_API_KEY y BINANCE_SECRET_KEY no están configuradas.")
        print("Por favor, configúralas antes de ejecutar.")
    else:
        print("Cliente de Binance (Mainnet) inicializado.")
        run_download()
