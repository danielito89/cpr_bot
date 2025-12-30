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
MODEL_PATH = "cortex_model_v9_2.joblib"

# PARAMS FIJOS (La estrategia técnica manda, la IA solo aprueba)
# Usamos una configuración "Flow" robusta por defecto
TECH_PARAMS = {'vol_thresh': 0.8, 'rsi_long': 50, 'rsi_short': 50, 'tp_mult': 1.5}

def load_data(symbol):
    # ... (Igual que antes) ...
    # Copia la función load_data de scripts anteriores
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
        except: break
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_features_v9(df):
    # ... (Copia EXACTA de calculate_features_v9 del script V9.1) ...
    # Asegúrate de usar la versión corregida con feat_volatility_z y feat_volume_z
    return df # (Simplificado aquí por brevedad, usa el código completo anterior)

# [INSERTA AQUI calculate_features_v9 COMPLETA DEL SCRIPT ANTERIOR]
# Para que el script funcione, necesito que uses la función real.

def run_simulation():
    print(f"\n🧠 BACKTEST V9.2: AI GATEKEEPER ({START_DATE} - {END_DATE})")
    
    try:
        model = joblib.load(MODEL_PATH)
        print("✅ Modelo V9.2 (Profit Buckets) cargado.")
        # Clases: 0=TOXIC, 1=NOISE, 2=PROFIT
        class_map = {label: idx for idx, label in enumerate(model.classes_)}
        idx_toxic = class_map.get(0)
        idx_noise = class_map.get(1)
        idx_profit = class_map.get(2)
    except Exception as e:
        print(f"❌ Error Modelo: {e}"); return

    global_log = []
    feature_cols = [
            'feat_volatility_z', 'feat_squeeze', 
            'feat_clv', 'feat_wick_up', 'feat_wick_down', 'feat_body_r',
            'feat_volume_z', 'feat_vol_impact', 
            'feat_rsi', 'feat_dist_sma'
    ]

    for symbol in TARGET_PAIRS:
        df = load_data(symbol)
        if df.empty: continue
        
        # Necesitas pegar la función calculate_features_v9 arriba o importarla
        # Asumo que ya la tienes definida correctamente
        # df = calculate_features_v9(df) 
        # (Si copiaste el script anterior, reutiliza esa función aquí)
        
        # --- SIMULACIÓN DE CÁLCULO DE FEATURES ---
        # (Para este ejemplo asumo que ya lo hiciste en el paso anterior o lo tienes en memoria)
        # Si no, copia la función calculate_advanced_features y renómbrala.
        # Por seguridad, usaremos un placeholder si no la pegaste:
        try:
             df = calculate_features_v9(df)
        except:
             print("⚠️ Falta función calculate_features_v9. Copiala del script V9.1")
             return

        X_full = df[feature_cols]
        all_probs = model.predict_proba(X_full)
        
        # Arrays
        closes = df['close'].values; highs = df['high'].values; lows = df['low'].values
        vols = df['volume'].values; vol_mas = df['Vol_MA'].values
        rsis = df['feat_rsi'].values; atrs = df['ATR'].values
        vahs = df['VAH'].values; vals = df['VAL'].values
        
        trades = []
        cooldown = 0
        
        for i in range(300, len(df)-12):
            if cooldown > 0: cooldown -= 1; continue
            
            # --- 1. LÓGICA DE GATEKEEPER (EL FILTRO IA) ---
            probs = all_probs[i]
            p_profit = probs[idx_profit]
            p_toxic  = probs[idx_toxic]
            
            is_safe = False
            
            # FILTRO DINÁMICO POR ACTIVO
            if "BTC" in symbol:
                # BTC es difícil. Exigimos altísima certeza de Profit.
                # Y castigamos cualquier sospecha de Toxicidad.
                if p_profit > 0.60 and p_toxic < 0.20:
                    is_safe = True
            else:
                # Alts son más perdonables si hay tendencia
                if p_profit > 0.45:
                    is_safe = True
            
            # SI LA IA DICE "NO ES SEGURO", NO OPERAMOS (WAIT)
            if not is_safe:
                continue

            # --- 2. LÓGICA TÉCNICA CLÁSICA (V6.5) ---
            # Si pasamos el Gatekeeper, aplicamos la estrategia robusta
            p_params = TECH_PARAMS
            
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
                
                trades.append({'symbol': symbol, 'profile': 'FILTERED', 'r_net': outcome_r - 0.05, 'type': result_type})
                cooldown = 12
        
        if trades:
            df_res = pd.DataFrame(trades)
            net_r = df_res['r_net'].sum()
            print(f"   -> {symbol}: {len(trades)} trades | {net_r:.2f} R")
            global_log.extend(trades)
        else:
            print(f"   -> {symbol}: 0 trades (Gatekeeper bloqueó todo)")

    if global_log:
        df_glob = pd.DataFrame(global_log)
        print(f"\n💰 R NETO TOTAL V9.2: {df_glob['r_net'].sum():.2f} R")

if __name__ == "__main__":
    run_simulation()