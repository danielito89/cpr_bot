import ccxt
import pandas as pd
import time
from datetime import datetime, timezone

# Config
SYMBOL = "ETH/USDT" 
SINCE_STR = "2022-01-01 00:00:00"

def fetch_funding():
    print(f"📡 Descargando Funding History para {SYMBOL} (Futuros) desde {SINCE_STR}...")
    
    # --- CORRECCIÓN CRÍTICA: 'defaultType': 'future' ---
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future'  # Obligatorio para ver Funding Rates
        }
    })
    
    since_ts = int(datetime.strptime(SINCE_STR, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
    
    all_funding = []
    
    while True:
        try:
            # Descargar historial
            rates = exchange.fetch_funding_rate_history(SYMBOL, since_ts, limit=1000)
            
            if not rates:
                print("⚠️ No se recibieron más datos (lista vacía).")
                break
            
            all_funding.extend(rates)
            
            # Actualizar puntero de tiempo
            last_ts = rates[-1]['timestamp']
            
            # Evitar bucle infinito si Binance devuelve el mismo último dato
            if last_ts == since_ts:
                # Sumamos 1ms para forzar el siguiente bloque
                since_ts += 1
            else:
                since_ts = last_ts + 1
            
            # Progreso visual
            last_date = datetime.fromtimestamp(last_ts/1000, timezone.utc)
            print(f"   📥 Recibidos {len(rates)} datos... Total: {len(all_funding)} (Último: {last_date})")
            
            # Si recibimos menos de 1000, llegamos al presente
            if len(rates) < 1000: 
                break
            
            # Rate limit suave manual
            time.sleep(0.5)
                
        except Exception as e:
            print(f"❌ Error en el bucle: {e}")
            time.sleep(5)

    # --- CHEQUEO DE SEGURIDAD ---
    if not all_funding:
        print("❌ ERROR FATAL: No se descargó ningún dato. Revisa tu conexión o el símbolo.")
        return

    # Convertir a DataFrame
    print("💾 Procesando datos...")
    df = pd.DataFrame(all_funding)
    
    # Asegurarnos de que las columnas existen
    required_cols = ['timestamp', 'fundingRate', 'symbol']
    if not all(col in df.columns for col in required_cols):
        print(f"❌ Error de columnas. Columnas disponibles: {df.columns}")
        return

    df = df[required_cols]
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    # Guardar
    filename = f"data/funding_{SYMBOL.replace('/','')}.csv"
    # Asegurar que el directorio existe
    import os
    os.makedirs('data', exist_ok=True)
    
    df.to_csv(filename, index=False)
    print(f"\n✅ ÉXITO: Guardado en {filename} ({len(df)} registros)")

if __name__ == "__main__":
    fetch_funding()