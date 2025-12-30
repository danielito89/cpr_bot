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
    """FEATURES V9 (Microestructura + Dinámica)"""
    df = df.copy()
    
    # A. DINÁMICA
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    
    atr_fast = true_range.rolling(14).mean()
    atr_slow = true_range.rolling(100).mean()
    
    # Volatilidad Z
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

    # C. VOLUMEN
    vol_ma = df['volume'].rolling(50).mean()
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
    """
    ETIQUETADO V9.2: PROFIT BUCKETS
    0 = TOXIC (Pérdida o Chop violento)
    1 = NOISE (Mercado muerto o ruido)
    2 = PROFIT (Ganancia limpia > 1.5R)
    """
    targets = []
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    atrs = df['ATR'].values
    
    LOOK_AHEAD = 12 
    
    for i in range(len(df) - LOOK_AHEAD):
        entry = closes[i]
        sl_dist = atrs[i] * 1.5 
        if sl_dist == 0: 
            targets.append(1) # Noise
            continue
            
        outcome = 1 # Default NOISE
        
        future_highs = highs[i+1 : i+LOOK_AHEAD+1]
        future_lows = lows[i+1 : i+LOOK_AHEAD+1]
        
        max_up = np.max(future_highs)
        max_down = np.min(future_lows)
        
        dist_up = (max_up - entry) / sl_dist
        dist_down = (entry - max_down) / sl_dist
        
        # --- LÓGICA DE CLASIFICACIÓN PnL ---
        
        # 1. PROFIT: Expansión fuerte (>1.5R) y limpia (no nos stopea antes)
        if (dist_up > 1.5 and dist_down < 1.0) or (dist_down > 1.5 and dist_up < 1.0):
            outcome = 2 
            
        # 2. TOXIC: Chop violento (rompe ambos lados) o Stop directo
        elif (dist_up > 1.0 and dist_down > 1.0): 
            outcome = 0 
        elif (dist_up < 0.5 and dist_down > 1.0) or (dist_down < 0.5 and dist_up > 1.0):
            outcome = 0 
            
        # 3. NOISE: Mercado muerto (ya es el default 1)
        
        targets.append(outcome)
            
    targets.extend([1] * LOOK_AHEAD)
    df['TARGET'] = targets
    return df

def run_mining():
    print("⛏️ INICIANDO MINERÍA V9.2 (PROFIT BUCKETS)")
    full_dataset = []
    
    for pair in PAIRS:
        df = fetch_data(pair)
        if df.empty: continue
        
        df = calculate_advanced_features(df)
        df = label_data(df)
        
        cols_to_save = [
            'feat_volatility_z', 'feat_squeeze', 
            'feat_clv', 'feat_wick_up', 'feat_wick_down', 'feat_body_r',
            'feat_volume_z', 'feat_vol_impact', 
            'feat_rsi', 'feat_dist_sma', 
            'TARGET'
        ]
        
        full_dataset.append(df[cols_to_save])
        
    if full_dataset:
        final_df = pd.concat(full_dataset)
        final_df = final_df.replace([np.inf, -np.inf], np.nan).dropna()
        
        filename = "cortex_training_data_v9_2.csv"
        final_df.to_csv(filename, index=False)
        
        print("\n" + "="*50)
        print(f"✅ DATASET V9.2 GENERADO: {filename}")
        print(f"📊 Muestras Totales: {len(final_df)}")
        print("🔍 Balance de Clases (0=Toxic, 1=Noise, 2=Profit):")
        print(final_df['TARGET'].value_counts(normalize=True))
        print("="*50)

if __name__ == "__main__":
    run_mining()