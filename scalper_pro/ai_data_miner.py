import pandas as pd
import numpy as np
import ccxt
import sys
import os

# --- CONFIGURACIÓN ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT', 'LTC/USDT']
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
            if len(all_ohlcv) > 200000: break 
        except: break
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_features_standard(df):
    """FEATURES (Idéntico a router/backtest)"""
    df = df.copy()
    
    # 1. Volatilidad (True Range Real)
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['ATR'] = true_range.rolling(14).mean()
    df['feat_volatility'] = df['ATR'] / df['close']
    
    # 2. Vol Ratio
    df['Vol_MA'] = df['volume'].rolling(20).mean()
    df['feat_vol_ratio'] = df['volume'] / df['Vol_MA']
    
    # 3. RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['feat_rsi'] = 100 - (100 / (1 + rs))
    
    # 4. Trend Dev
    df['sma50'] = df['close'].rolling(50).mean()
    df['feat_trend_dev'] = (df['close'] - df['sma50']) / df['close']
    
    return df.dropna()

def label_data(df):
    """ETIQUETADO V7.1 (Basado en resultado puro)"""
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
            targets.append(2)
            continue

        outcome = 2 # WAIT
        
        for j in range(1, LOOK_AHEAD + 1):
            future_high = highs[i+j]
            future_low = lows[i+j]
            
            r_high = (future_high - entry) / sl_dist
            r_low = (entry - future_low) / sl_dist
            
            if r_high > 3 or r_low > 3:
                outcome = 0 # SNIPER
                break 
            elif (r_high > 1.5 or r_low > 1.5) and outcome == 2:
                outcome = 1 # FLOW
                
        targets.append(outcome)
            
    targets.extend([2] * LOOK_AHEAD)
    df['TARGET'] = targets
    return df

def run_mining():
    print("⛏️ INICIANDO MINERÍA DE ESPECIALISTAS")
    
    for pair in PAIRS:
        df = fetch_data(pair)
        if df.empty: continue
        
        df = calculate_features_standard(df)
        df = label_data(df)
        
        # Guardamos UN CSV POR PAR
        clean_df = df[['feat_volatility', 'feat_vol_ratio', 'feat_rsi', 'feat_trend_dev', 'TARGET']]
        
        # Limpiamos el nombre del símbolo (BTC/USDT -> BTCUSDT)
        safe_pair = pair.replace('/', '')
        filename = f"cortex_data_{safe_pair}.csv"
        
        clean_df.to_csv(filename, index=False)
        print(f"✅ Guardado: {filename}")

if __name__ == "__main__":
    run_mining()