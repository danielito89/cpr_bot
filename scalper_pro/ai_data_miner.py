import pandas as pd
import numpy as np
import ccxt
import sys
import os

# --- CONFIGURACIÓN ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT', 'LTC/USDT']
START_DATE = "2023-01-01" # 2 Años de historial
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
            if len(all_ohlcv) > 200000: break # Limite de seguridad
        except: break
            
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_features_standard(df):
    """
    ESTA FUNCIÓN ES LA VERDAD ABSOLUTA.
    Debe ser copiada EXACTAMENTE IGUAL al script de inferencia (ai_router.py).
    """
    df = df.copy()
    
    # 1. Volatilidad Relativa (ATR 14 Real / Close)
    # Calculamos True Range Vectorizado
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    
    df['ATR'] = true_range.rolling(14).mean()
    df['feat_volatility'] = df['ATR'] / df['close']
    
    # 2. Volumen Relativo (Vol / MA20)
    df['Vol_MA'] = df['volume'].rolling(20).mean()
    df['feat_vol_ratio'] = df['volume'] / df['Vol_MA']
    
    # 3. Eficiencia de Tendencia (RSI Clasico)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['feat_rsi'] = 100 - (100 / (1 + rs))
    
    # 4. Distancia a la Media (Tendencia)
    df['sma50'] = df['close'].rolling(50).mean()
    df['feat_trend_dev'] = (df['close'] - df['sma50']) / df['close']
    
    return df.dropna()

def label_data(df):
    """
    ETIQUETADO V7.1 (CORREGIDO)
    El Target se basa PURAMENTE en el resultado del precio.
    No miramos el volumen aquí para evitar 'Data Leakage'.
    """
    targets = []
    # Convertimos a numpy para velocidad
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    atrs = df['ATR'].values
    
    # Importante: No usamos vol_ratios aquí. 
    # Dejamos que la IA descubra su importancia.
    
    LOOK_AHEAD = 12 # 1 Hora
    
    for i in range(len(df) - LOOK_AHEAD):
        entry = closes[i]
        current_atr = atrs[i]
        
        # SL Estimado (1.5 ATR)
        sl_dist = current_atr * 1.5
        if sl_dist == 0: 
            targets.append(2)
            continue

        # Definimos niveles de precio objetivo
        # Sniper Target: 3R
        # Flow Target: 1.5R
        
        # --- SIMULACIÓN DE FUTURO ---
        # Buscamos si tocó TP antes que SL en la ventana de 12 velas
        outcome = 2 # Default WAIT
        
        # Revisamos vela por vela hacia el futuro para ver qué toca primero
        for j in range(1, LOOK_AHEAD + 1):
            future_high = highs[i+j]
            future_low = lows[i+j]
            
            # Calculamos R al High y al Low
            r_high = (future_high - entry) / sl_dist
            r_low = (entry - future_low) / sl_dist # Nota: Para short sería al revés, aquí simplificamos magnitud
            
            # LOGICA MAGNITUD ABSOLUTA (Captura movimiento fuerte en cualquier dirección)
            # Asumimos que el bot acierta la dirección (Long/Short) por su estrategia base.
            # Aquí evaluamos VOLATILIDAD y POTENCIAL.
            
            # Si el movimiento en CUALQUIER dirección supera 3R...
            if r_high > 3 or r_low > 3:
                outcome = 0 # SNIPER (Detectamos explosión)
                break # Ya encontramos el target mayor, salimos
            
            # Si supera 1.5R...
            elif (r_high > 1.5 or r_low > 1.5) and outcome == 2:
                outcome = 1 # FLOW (Es candidato, pero seguimos mirando por si se vuelve Sniper)
                # No hacemos break aquí, porque podría convertirse en Sniper en la siguiente vela
                
            # NOTA: En un labeling perfecto tick-by-tick chequearíamos SL.
            # Aquí asumimos que si hay rango para 3R, es un entorno Sniper.
            
        targets.append(outcome)
            
    # Rellenar final
    targets.extend([2] * LOOK_AHEAD)
    df['TARGET'] = targets
    return df

def run_mining():
    print("⛏️ INICIANDO MINERÍA DE DATOS CORTEX V7")
    full_dataset = []
    
    for pair in PAIRS:
        df = fetch_data(pair)
        if df.empty: continue
        
        df = calculate_features_standard(df)
        df = label_data(df)
        
        # Guardamos Features + Target
        clean_df = df[['feat_volatility', 'feat_vol_ratio', 'feat_rsi', 'feat_trend_dev', 'TARGET']]
        full_dataset.append(clean_df)
        
    if full_dataset:
        final_df = pd.concat(full_dataset)
        final_df = final_df.replace([np.inf, -np.inf], np.nan).dropna()
        
        filename = "cortex_training_data.csv"
        final_df.to_csv(filename, index=False)
        
        print("\n" + "="*50)
        print(f"✅ DATASET GENERADO: {filename}")
        print(f"📊 Muestras Totales: {len(final_df)}")
        print("🔍 Distribución de Clases (Chequeo de Balance):")
        print(final_df['TARGET'].value_counts(normalize=True))
        print("="*50)

if __name__ == "__main__":
    run_mining()