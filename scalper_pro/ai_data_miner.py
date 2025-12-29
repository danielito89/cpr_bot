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
    ETIQUETADO (Looking Ahead 12 velas / 1 Hora)
    0 = SNIPER (Alta Volatilidad / Explosión)
    1 = FLOW (Movimiento Sostenido)
    2 = WAIT (Ruido / Lateral / Pérdida)
    """
    targets = []
    # Convertimos a numpy para velocidad
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    atrs = df['ATR'].values
    vol_ratios = df['feat_vol_ratio'].values
    
    LOOK_AHEAD = 12
    
    for i in range(len(df) - LOOK_AHEAD):
        entry = closes[i]
        current_atr = atrs[i]
        
        # Futuro próximo
        future_high = np.max(highs[i+1 : i+LOOK_AHEAD+1])
        future_low = np.min(lows[i+1 : i+LOOK_AHEAD+1])
        
        sl_dist = current_atr * 1.5
        if sl_dist == 0: 
            targets.append(2)
            continue
            
        max_long_r = (future_high - entry) / sl_dist
        max_short_r = (entry - future_low) / sl_dist
        
        # LOGICA V7.0: Detección de Magnitud
        # Si explota > 3R y hay volumen, era un setup SNIPER
        if (max_long_r > 3 or max_short_r > 3) and vol_ratios[i] > 1.0:
            targets.append(0) 
            
        # Si se mueve > 1.5R decentemente, era un setup FLOW
        elif (max_long_r > 1.5 or max_short_r > 1.5):
            targets.append(1) 
            
        # Si no, mejor esperar
        else:
            targets.append(2)
            
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