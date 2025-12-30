import pandas as pd
import numpy as np
import joblib
import sys
import os
import ccxt

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config

# --- CONFIG ---
START_DATE = "2024-01-01"
END_DATE   = "2024-12-31"
TARGET_PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT', 'LTC/USDT']
MODEL_PATH = "cortex_model_v9.joblib"

PARAMS = {
    'SNIPER': {'vol_thresh': 1.2, 'rsi_long': 40, 'rsi_short': 60, 'tp_mult': 3.0},
    'FLOW':   {'vol_thresh': 0.6, 'rsi_long': 50, 'rsi_short': 50, 'tp_mult': 1.5}
}

def load_data(symbol):
    print(f"📥 {symbol}...", end=" ")
    exchange = ccxt.binance()
    since = exchange.parse8601(f"{START_DATE}T00:00:00Z")
    end_ts = exchange.parse8601(f"{END_DATE}T23:59:59Z")
    all_ohlcv = []
    while since < end_ts:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=1000, since=since)
            if not ohlcv: break
            since = ohlcv[-1][0] + 1
            ohlcv = [x for x in ohlcv if x[0] <= end_ts]
            all_ohlcv.extend(ohlcv)
            # print(".", end="", flush=True) 
        except: break
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_features_v9(df):
    """ V9.1: FEATURES CORREGIDOS (Match con Miner) """
    df = df.copy()
    
    # A. DINÁMICA
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    
    atr_fast = true_range.rolling(14).mean()
    atr_slow = true_range.rolling(100).mean()
    
    # CORRECCIÓN 1
    df['feat_volatility_z'] = atr_fast / atr_slow
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

    # C. IMPACTO
    vol_ma = df['volume'].rolling(50).mean()
    # CORRECCIÓN 2
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

    # Técnicos
    df['rolling_mean'] = df['close'].rolling(300).mean()
    df['rolling_std'] = df['close'].rolling(300).std()
    df['VAH'] = df['rolling_mean'] + df['rolling_std']
    df['VAL'] = df['rolling_mean'] - df['rolling_std']
    df['Vol_MA'] = df['volume'].rolling(20).mean()
    df['ATR'] = atr_fast
    
    return df.dropna()

def run_simulation():
    print(f"\n🧠 BACKTEST V9.1: Fixed & Ready ({START_DATE} - {END_DATE})")
    
    try:
        model = joblib.load(MODEL_PATH)
        print("✅ Modelo V9.1 cargado.")
        class_map = {label: idx for idx, label in enumerate(model.classes_)}
        idx_sniper, idx_flow, idx_wait = class_map.get(0), class_map.get(1), class_map.get(2)
    except Exception as e:
        print(f"❌ Error Modelo: {e}"); return

    global_log = []
    # Lista de features corregida
    feature_cols = [
            'feat_volatility_z', 'feat_squeeze', 
            'feat_clv', 'feat_wick_up', 'feat_wick_down', 'feat_body_r',
            'feat_volume_z', 'feat_vol_impact', 
            'feat_rsi', 'feat_dist_sma'
    ]

    for symbol in TARGET_PAIRS:
        df = load_data(symbol)
        if df.empty: continue
        
        df = calculate_features_v9(df)
        
        # Inferencia
        X_full = df[feature_cols]
        all_probs = model.predict_proba(X_full)
        
        # Arrays
        closes = df['close'].values; opens = df['open'].values
        highs = df['high'].values; lows = df['low'].values
        vols = df['volume'].values; vol_mas = df['Vol_MA'].values
        rsis = df['feat_rsi'].values; atrs = df['ATR'].values
        vahs = df['VAH'].values; vals = df['VAL'].values
        
        trades = []
        cooldown = 0
        
        for i in range(300, len(df)-12):
            if cooldown > 0: cooldown -= 1; continue
            
            probs = all_probs[i]
            p_sniper = probs[idx_sniper]
            p_flow = probs[idx_flow]
            
            # --- LÓGICA DE DECISIÓN ---
            profile = 'WAIT'
            
            # Umbrales ajustados para LightGBM
            if p_sniper > 0.40:    
                profile = 'SNIPER'
            elif p_flow > 0.50:    
                profile = 'FLOW'
            
            if profile == 'WAIT': continue
            
            # --- TÉCNICO ---
            p_params = PARAMS[profile]
            if vols[i] < (vol_mas[i] * p_params['vol_thresh']): continue
            
            signal = None; sl_price = 0
            if lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]:
                 if lows[i] > vals[i] and closes[i] > highs[i-1] and closes[i] > opens[i]:
                     if rsis[i] < p_params['rsi_long']:
                         signal = 'LONG'; sl_price = closes[i] - (atrs[i] * 1.5)
            elif highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]:
                 if highs[i] < vahs[i] and closes[i] < lows[i-1] and closes[i] < opens[i]:
                     if rsis[i] > p_params['rsi_short']:
                         signal = 'SHORT'; sl_price = closes[i] + (atrs[i] * 1.5)
            
            if signal:
                entry = closes[i]
                sl_dist = abs(entry - sl_price)
                if sl_dist == 0: continue
                tp_mult = p_params['tp_mult']
                outcome_r = 0; result_type = "HOLD"
                
                for j in range(1, 13):
                    idx = i + j
                    if signal == 'LONG':
                        r_high = (highs[idx]-entry)/sl_dist; r_low = (lows[idx]-entry)/sl_dist; r_curr = (closes[idx]-entry)/sl_dist
                    else:
                        r_high = (entry-lows[idx])/sl_dist; r_low = (entry-highs[idx])/sl_dist; r_curr = (entry-closes[idx])/sl_dist
                    
                    if r_low <= -1.1: outcome_r = -1.1; result_type = "SL"; break
                    if r_high >= tp_mult: outcome_r = tp_mult; result_type = "TP"; break
                    if j == 12: outcome_r = r_curr; result_type = "TIME"
                
                trades.append({'symbol': symbol, 'profile': profile, 'r_net': outcome_r - 0.05, 'type': result_type})
                cooldown = 12
        
        if trades:
            df_res = pd.DataFrame(trades)
            net_r = df_res['r_net'].sum()
            print(f"   -> {symbol}: {len(trades)} trades | {net_r:.2f} R")
            global_log.extend(trades)
        else:
            print(f"   -> {symbol}: 0 trades")

    if global_log:
        df_glob = pd.DataFrame(global_log)
        print(f"\n💰 R NETO TOTAL V9.1: {df_glob['r_net'].sum():.2f} R")

if __name__ == "__main__":
    run_simulation()