import pandas as pd
import numpy as np
import ccxt
import sys
import os

# --- CONFIGURACIÓN ---
# Usamos una mezcla de activos volátiles y estables para que la IA aprenda generalidades
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT', 'LTC/USDT', 'BNB/USDT']
START_DATE = "2023-01-01" 
TIMEFRAME = '5m'

def fetch_data(symbol):
    print(f"📥 Descargando {symbol}...", end=" ")
    exchange = ccxt.binance()
    since = exchange.parse8601(f"{START_DATE}T00:00:00Z")
    all_ohlcv = []
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=1000, since=since)
            if not ohlcv: break
            since = ohlcv[-1][0] + 1
            all_ohlcv.extend(ohlcv)
            print(".", end="", flush=True)
            if len(all_ohlcv) > 250000: break # Tope de seguridad
        except: break
            
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_features_normalized(df):
    """
    MATEMÁTICA V8: NORMALIZACIÓN UNIVERSAL
    Convierte datos absolutos en relativos para que el modelo generalice.
    """
    df = df.copy()
    
    # 1. VOLATILIDAD Z-SCORE (Relativa a su propia historia)
    # True Range Vectorizado
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    
    # ATR actual (14)
    atr = true_range.rolling(14).mean()
    
    # Baseline de ATR (100 periodos - Largo Plazo)
    atr_baseline = atr.rolling(100).mean()
    
    # Feature 1: ¿Cuántas veces es la volatilidad actual respecto a la normal?
    # 1.0 = Normal, 2.0 = Doble de lo usual (Pánico/Euforia)
    df['feat_vol_norm'] = atr / atr_baseline
    
    # 2. VOLUMEN RELATIVO (Suavizado)
    # Usamos media de 50 para que sea más estable que la de 20
    vol_ma_long = df['volume'].rolling(50).mean()
    df['feat_volume_norm'] = df['volume'] / vol_ma_long
    
    # 3. RSI (Ya es normalizado 0-100)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['feat_rsi'] = 100 - (100 / (1 + rs))
    
    # 4. TENDENCIA NORMALIZADA (Unidades de Riesgo)
    # Distancia a la media de 50, medida en "unidades de ATR"
    sma50 = df['close'].rolling(50).mean()
    dist_sma = df['close'] - sma50
    df['feat_trend_z'] = dist_sma / atr
    
    # Guardamos ATR puro solo para el etiquetado (Labeling), no es feature
    df['ATR'] = atr 
    
    return df.dropna()

def label_data(df):
    """ETIQUETADO RESULT-BASED (Sin Leakage de Volumen)"""
    targets = []
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    atrs = df['ATR'].values
    
    LOOK_AHEAD = 12 # 1 Hora
    
    for i in range(len(df) - LOOK_AHEAD):
        entry = closes[i]
        
        # Usamos ATR local para definir objetivos dinámicos
        sl_dist = atrs[i] * 1.5 
        if sl_dist == 0: 
            targets.append(2)
            continue
            
        outcome = 2 # WAIT (Default)
        
        # Simulamos futuro
        for j in range(1, LOOK_AHEAD + 1):
            future_high = highs[i+j]
            future_low = lows[i+j]
            
            # Calculamos R potencial en ambas direcciones
            r_high = (future_high - entry) / sl_dist
            r_low = (entry - future_low) / sl_dist
            
            # SNIPER: Movimiento explosivo > 3R
            if r_high > 3.0 or r_low > 3.0:
                outcome = 0 
                break # Encontramos el mejor target, salimos
            
            # FLOW: Movimiento decente > 1.5R (pero seguimos buscando Sniper)
            elif (r_high > 1.5 or r_low > 1.5) and outcome == 2:
                outcome = 1 
        
        targets.append(outcome)
            
    # Rellenar el final que no se pudo calcular
    targets.extend([2] * LOOK_AHEAD)
    df['TARGET'] = targets
    return df

def run_mining():
    print("⛏️ INICIANDO MINERÍA V8 (NORMALIZADA)")
    full_dataset = []
    
    for pair in PAIRS:
        df = fetch_data(pair)
        if df.empty: continue
        
        # 1. Calcular Features Normalizadas
        df = calculate_features_normalized(df)
        
        # 2. Etiquetar
        df = label_data(df)
        
        # 3. Guardar solo lo que la IA necesita
        clean_df = df[['feat_vol_norm', 'feat_volume_norm', 'feat_rsi', 'feat_trend_z', 'TARGET']]
        full_dataset.append(clean_df)
        
    if full_dataset:
        final_df = pd.concat(full_dataset)
        # Limpieza final de infinitos
        final_df = final_df.replace([np.inf, -np.inf], np.nan).dropna()
        
        filename = "cortex_training_data_v8.csv"
        final_df.to_csv(filename, index=False)
        
        print("\n" + "="*50)
        print(f"✅ DATASET MAESTRO GENERADO: {filename}")
        print(f"📊 Muestras Totales: {len(final_df)}")
        print("🔍 Balance de Clases:")
        print(final_df['TARGET'].value_counts(normalize=True))
        print("="*50)

if __name__ == "__main__":
    run_mining()