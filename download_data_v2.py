import os
import time
import pandas as pd
from binance.client import Client
from datetime import datetime, timedelta

# --- CONFIGURACIÓN ---
# Puedes poner varios pares en la lista para que baje uno tras otro
SYMBOLS = ["ETHUSDT", "BTCUSDT"] 

# FECHAS PARA EL "DEATH MATCH"
# Inicio real de ETH Futures: Nov 2019. Ponemos 2020 para año completo.
START_DATE = "2020-01-01" 
END_DATE = "2021-12-31"   # Fin del ciclo 2021

# Carpeta de datos
DATA_DIR = os.path.join(os.path.dirname(__file__), "cpr_bot_v90", "data")
os.makedirs(DATA_DIR, exist_ok=True)

# API KEYS (Opcional para datos públicos, pero evita rate limits)
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")
# ---------------------

def download_monthly_chunks(client, symbol, interval, start_str, end_str):
    """
    Descarga datos mes a mes y los escribe en disco progresivamente
    para no saturar la RAM de la Orange Pi.
    """
    filename = f"mainnet_data_{interval}_{symbol}_2020-2021.csv"
    filepath = os.path.join(DATA_DIR, filename)
    
    # Convertir fechas
    start_dt = datetime.strptime(start_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_str, "%Y-%m-%d")
    
    current_start = start_dt
    
    # Si el archivo ya existe, lo borramos para empezar limpio
    if os.path.exists(filepath):
        os.remove(filepath)
        
    print(f"🔵 Iniciando descarga SEGURA (RAM Friendly) para {symbol} [{interval}]")
    print(f"   Destino: {filepath}")
    
    first_chunk = True
    total_rows = 0
    
    while current_start < end_dt:
        # Definir chunk de 1 mes aprox
        current_end = current_start + timedelta(days=30)
        if current_end > end_dt:
            current_end = end_dt
            
        str_start = current_start.strftime("%d %b, %Y")
        str_end = current_end.strftime("%d %b, %Y")
        
        print(f"   ⏳ Bajando chunk: {str_start} -> {str_end} ...")
        
        try:
            klines = client.futures_historical_klines(
                symbol, 
                interval, 
                str_start, 
                str_end
            )
            
            if klines:
                cols = [
                    'Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 
                    'Close_Time', 'Quote_Asset_Volume', 'Number_of_Trades', 
                    'Taker_Buy_Base', 'Taker_Buy_Quote', 'Ignore'
                ]
                df = pd.DataFrame(klines, columns=cols)
                
                # Limpieza rápida
                df['Open_Time'] = pd.to_datetime(df['Open_Time'], unit='ms')
                for col in ['Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume']:
                    df[col] = pd.to_numeric(df[col])
                
                df = df[['Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume']]
                # NO seteamos index aquí para que 'Open_Time' se guarde como columna en el CSV
                
                # Guardar (Append mode)
                mode = 'w' if first_chunk else 'a'
                header = True if first_chunk else False
                df.to_csv(filepath, mode=mode, header=header, index=False)
                
                total_rows += len(df)
                first_chunk = False
            
            # Evitar rate limits de Binance
            time.sleep(0.5) 
            
        except Exception as e:
            print(f"   ❌ Error en chunk {str_start}: {e}")
            # Reintentar o seguir? Mejor seguir para no bloquear
        
        # Avanzar al siguiente mes
        current_start = current_end
        
    print(f"✅ Descarga finalizada para {symbol}. Total filas: {total_rows}\n")

def run_download():
    client = Client(API_KEY, API_SECRET)
    
    for symbol in SYMBOLS:
        # Solo bajamos 1 MINUTO (lo único necesario para el backtest preciso)
        download_monthly_chunks(client, symbol, Client.KLINE_INTERVAL_1MINUTE, START_DATE, END_DATE)

if __name__ == "__main__":
    run_download()