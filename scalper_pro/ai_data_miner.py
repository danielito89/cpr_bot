import pandas as pd
import numpy as np
import ccxt
import sys
import os

# --- CONFIGURACIÓN ---
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
            if len(all_ohlcv) > 250000: break
        except: break
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_advanced_features(df):
    """FEATURES V9.1 (Corregido: Sin colisión de nombres)"""
    df = df.copy()
    
    # A. DINÁMICA
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    
    atr_fast = true_range.rolling(14).mean()
    atr_slow = true_range.rolling(100).mean()
    
    # CORRECCIÓN 1: Nombre único para Volatilidad
    df['feat_volatility_z'] = atr_fast / atr_slow 
    
    # Squeeze
    df['feat_squeeze'] = (atr_fast / df['close']).rolling(20).std() 

    # B. MICROESTRUCTURA
    candle_range = df['high'] - df['low']
    candle_range = candle_range.replace(0, 0.000001) 
    df['feat_clv'] = ((df['close'] - df['low']) - (df['high'] - df['close'])) / candle_range
    
    upper_wick = df['high'] - df[['close', 'open']].max(axis=1)
    lower_wick = df[['close', 'open']].min(axis=1) - df['low']
    df['feat_wick_up'] = upper_wick / candle_range
    df['feat_wick_down'] = lower_wick / candle_range
    
    body_size = abs(df['close'] - df['open'])
    df['feat_body_r'] = body_size / candle_range

    # C. VOLUMEN E IMPACTO
    vol_ma = df['volume'].rolling(50).mean()
    
    # CORRECCIÓN 2: Nombre único para Volumen
    df['feat_volume_z'] = df['volume'] / vol_ma
    
    df['feat_vol_impact'] = (df['volume'] * df['feat_clv']).rolling(3).mean()

    # D. TENDENCIA
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['feat_rsi'] = 100 - (100 / (1 + rs))
    
    sma50 = df['close'].rolling(50).mean()
    df['feat_dist_sma'] = (df['close'] - sma50) / atr_fast

    # Guardamos ATR para labeling
    df['ATR'] = atr_fast
    
    return df.dropna()

def label_data(df):
    """ETIQUETADO V9.1 (Targets Realistas)"""
    targets = []
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    atrs = df['ATR'].values
    
    LOOK_AHEAD = 12 
    
    for i in range(len(df) - LOOK_AHEAD):
        entry = closes[i]
        sl_dist = atrs[i] * 1.5 
        if sl_dist == 0: targets.append(2); continue
            
        outcome = 2 # WAIT
        
        for j in range(1, LOOK_AHEAD + 1):
            future_high = highs[i+j]
            future_low = lows[i+j]
            
            r_high = (future_high - entry) / sl_dist
            r_low = (entry - future_low) / sl_dist
            
            # CORRECCIÓN 3: Targets Ajustados
            # Sniper > 2.5R (Antes 3.0)
            if r_high > 2.5 or r_low > 2.5:
                outcome = 0 
                break 
            # Flow > 1.2R (Antes 1.5, para capturar más inercia)
            elif (r_high > 1.2 or r_low > 1.2) and outcome == 2:
                outcome = 1 
                
        targets.append(outcome)
            
    targets.extend([2] * LOOK_AHEAD)
    df['TARGET'] = targets
    return df

def run_mining():
    print("⛏️ INICIANDO MINERÍA V9.1 (FIXED)")
    full_dataset = []
    
    for pair in PAIRS:
        df = fetch_data(pair)
        if df.empty: continue
        
        df = calculate_advanced_features(df)
        df = label_data(df)
        
        # Lista corregida de columnas
        cols_to_save = [
            'feat_volatility_z', 'feat_squeeze', 
            'feat_clv', 'feat_wick_up', 'feat_wick_down', 'feat_body_r',
            'feat_volume_z', 'feat_vol_impact', # Ahora ambos existen
            'feat_rsi', 'feat_dist_sma', 
            'TARGET'
        ]
        
        full_dataset.append(df[cols_to_save])
        
    if full_dataset:
        final_df = pd.concat(full_dataset)
        final_df = final_df.replace([np.inf, -np.inf], np.nan).dropna()
        
        filename = "cortex_training_data_v9.csv"
        final_df.to_csv(filename, index=False)
        print(f"✅ DATASET V9.1 GENERADO: {len(final_df)} muestras.")

if __name__ == "__main__":
    run_mining()