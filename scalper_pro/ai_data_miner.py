import pandas as pd
import numpy as np
import ccxt
import sys
import os

# Agregamos path para importar DataProcessor si es necesario, 
# pero aquí haremos cálculos puros para evitar dependencias cruzadas complejas.

# --- CONFIGURACIÓN ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT', 'LTC/USDT']
START_DATE = "2023-01-01" # Usamos 2 años de data (2023-2024) para entrenar
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
            if len(all_ohlcv) > 150000: break # Limite de seguridad
        except: break
            
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_features(df):
    """Genera los 'ojos' de la IA (Inputs)"""
    df = df.copy()
    
    # 1. Volatilidad Relativa (ATR 14 / Close)
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
    
    # 3. Eficiencia de Tendencia (RSI)
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
    EL JUEZ: Decide qué perfil ganó en el futuro (Target)
    0 = SNIPER (Movimiento explosivo)
    1 = FLOW (Movimiento sostenido)
    2 = WAIT (Ruido/Pérdida)
    """
    targets = []
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    atrs = df['ATR'].values
    
    # Miramos 12 velas al futuro (1 hora)
    LOOK_AHEAD = 12
    
    for i in range(len(df) - LOOK_AHEAD):
        entry = closes[i]
        current_atr = atrs[i]
        
        # Futuro
        future_high = np.max(highs[i+1 : i+LOOK_AHEAD+1])
        future_low = np.min(lows[i+1 : i+LOOK_AHEAD+1])
        
        # Calculamos R potencial (Asumiendo SL = 1.5 ATR)
        sl_dist = current_atr * 1.5
        if sl_dist == 0: 
            targets.append(2)
            continue
            
        max_long_r = (future_high - entry) / sl_dist
        max_short_r = (entry - future_low) / sl_dist
        
        # LÓGICA DE CLASIFICACIÓN
        # Si hubo una explosión (>3R) -> SNIPER
        if max_long_r > 3 or max_short_r > 3:
            targets.append(0) # Sniper
        # Si hubo movimiento decente (>1.5R) -> FLOW
        elif max_long_r > 1.5 or max_short_r > 1.5:
            targets.append(1) # Flow
        # Si no pasó nada -> WAIT
        else:
            targets.append(2) # Wait
            
    # Rellenar el final
    targets.extend([2] * LOOK_AHEAD)
    df['TARGET'] = targets
    return df

def run_mining():
    print("⛏️ INICIANDO MINERÍA DE DATOS CORTEX V7")
    full_dataset = []
    
    for pair in PAIRS:
        df = fetch_data(pair)
        df = calculate_features(df)
        df = label_data(df)
        
        # Guardamos solo lo necesario para entrenar
        clean_df = df[['feat_volatility', 'feat_vol_ratio', 'feat_rsi', 'feat_trend_dev', 'TARGET']]
        full_dataset.append(clean_df)
        
    final_df = pd.concat(full_dataset)
    # Limpiar infinitos y NaNs
    final_df = final_df.replace([np.inf, -np.inf], np.nan).dropna()
    
    final_df.to_csv("cortex_training_data.csv", index=False)
    print(f"✅ Dataset Generado: cortex_training_data.csv ({len(final_df)} muestras)")

if __name__ == "__main__":
    run_mining()