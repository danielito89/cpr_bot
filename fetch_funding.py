import ccxt
import pandas as pd
import time
from datetime import datetime, timezone

# Config
SYMBOL = "ETH/USDT" # El rey del funding
SINCE_STR = "2022-01-01 00:00:00"

def fetch_funding():
    print(f"📡 Descargando Funding History para {SYMBOL} desde {SINCE_STR}...")
    exchange = ccxt.binance({'enableRateLimit': True})
    
    since_ts = int(datetime.strptime(SINCE_STR, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
    
    all_funding = []
    
    while True:
        try:
            # Fetch funding rate history
            rates = exchange.fetch_funding_rate_history(SYMBOL, since_ts, limit=1000)
            if not rates:
                break
            
            all_funding.extend(rates)
            
            # Actualizar puntero de tiempo
            last_ts = rates[-1]['timestamp']
            if last_ts == since_ts: # Evitar bucle infinito si no hay nuevos datos
                break
            since_ts = last_ts + 1
            
            # Progreso
            last_date = datetime.fromtimestamp(last_ts/1000, timezone.utc)
            print(f"   📥 Recibidos {len(rates)} datos... (Último: {last_date})")
            
            if len(rates) < 1000: # Final del historial
                break
                
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(5)

    # Convertir a DataFrame
    df = pd.DataFrame(all_funding)
    df = df[['timestamp', 'fundingRate', 'symbol']]
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    # Guardar
    filename = f"data/funding_{SYMBOL.replace('/','')}.csv"
    df.to_csv(filename, index=False)
    print(f"\n✅ Guardado en {filename} ({len(df)} registros)")

if __name__ == "__main__":
    fetch_funding()