import pandas as pd
import numpy as np
import joblib
import sys
import os
import ccxt

# Imports locales
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
from core.data_processor import DataProcessor

# --- CONFIGURACIÓN ---
START_DATE = "2024-01-01"
END_DATE   = "2024-12-31"
TARGET_PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT', 'LTC/USDT']
MODEL_PATH = "cortex_model_v1.joblib"

# Definición de Perfiles (Tus Reglas de Oro)
PARAMS = {
    'SNIPER': {'vol_thresh': 1.2, 'rsi_long': 40, 'rsi_short': 60, 'tp_mult': 3.0}, # Agresivo
    'FLOW':   {'vol_thresh': 0.6, 'rsi_long': 50, 'rsi_short': 50, 'tp_mult': 1.5}  # Conservador
}

def load_data(symbol):
    print(f"📥 Descargando {symbol}...", end=" ")
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
            print(".", end="", flush=True)
        except: break
            
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_features_for_inference(df):
    """
    Calcula features IDENTICAS al entrenamiento para alimentar la IA.
    """
    df = df.copy()
    
    # 1. Volatilidad (True Range)
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
    
    # Pre-calcular Bandas VAH/VAL para la estrategia (Simulación rápida)
    df['rolling_mean'] = df['close'].rolling(300).mean()
    df['rolling_std'] = df['close'].rolling(300).std()
    df['VAH'] = df['rolling_mean'] + df['rolling_std']
    df['VAL'] = df['rolling_mean'] - df['rolling_std']
    
    return df.dropna()

def run_simulation():
    print(f"\n🧠 INICIANDO BACKTEST CON CORTEX AI ({START_DATE} - {END_DATE})")
    
    # 1. Cargar Cerebro
    try:
        model = joblib.load(MODEL_PATH)
        print("✅ Modelo cargado correctamente.")
    except:
        print("❌ No se encontró el modelo .joblib")
        return

    global_log = []

    for symbol in TARGET_PAIRS:
        df = load_data(symbol)
        if df.empty: continue
        
        # Pre-calcular features (Vectorizado para velocidad, pero consultado fila a fila)
        df = calculate_features_for_inference(df)
        
        # Arrays numpy para velocidad
        closes = df['close'].values
        opens = df['open'].values
        highs = df['high'].values
        lows = df['low'].values
        vols = df['volume'].values
        vol_mas = df['Vol_MA'].values
        rsis = df['feat_rsi'].values # Usamos el mismo RSI
        vahs = df['VAH'].values
        vals = df['VAL'].values
        atrs = df['ATR'].values
        
        # Features para IA
        X_feats = df[['feat_volatility', 'feat_vol_ratio', 'feat_rsi', 'feat_trend_dev']].values
        
        trades = []
        cooldown = 0
        
        # --- BUCLE DE SIMULACIÓN ---
        # Empezamos después del warmup de indicadores
        for i in range(300, len(df)-12):
            if cooldown > 0: 
                cooldown -= 1
                continue
            
            # 1. CONSULTA A CORTEX (IA) 🧠
            # Extraemos la fila de features actual
            features_row = X_feats[i].reshape(1, -1)
            
            # --- OPCIÓN PRO: PREDICT PROBA (Como sugeriste) ---
            probs = model.predict_proba(features_row)[0] # [Prob_Sniper, Prob_Flow, Prob_Wait]
            # Asumiendo orden de clases: 0=SNIPER, 1=FLOW, 2=WAIT (Verificar model.classes_)
            
            # Lógica de Umbrales (Tus reglas de oro)
            p_sniper = probs[0]
            p_flow = probs[1]
            p_wait = probs[2]
            
            profile = 'WAIT'
            
            # REGLAS DE DECISIÓN
            if p_wait > 0.50: 
                profile = 'WAIT' # Kill Switch
            elif p_sniper > 0.40: # Umbral Sniper (ajustable)
                profile = 'SNIPER'
            else:
                profile = 'FLOW'
                
            # Si Cortex dice WAIT, no operamos
            if profile == 'WAIT':
                continue
                
            # 2. CARGAR PARÁMETROS DEL PERFIL
            p_params = PARAMS[profile]
            
            # 3. VERIFICAR SEÑAL TÉCNICA (V6.5)
            # Filtro de Volumen Dinámico
            if vols[i] < (vol_mas[i] * p_params['vol_thresh']): continue
            
            signal = None
            sl_price = 0
            
            # LONG
            if lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]: # Rechazo
                 if lows[i] > vals[i] and closes[i] > highs[i-1] and closes[i] > opens[i]:
                     if rsis[i] < p_params['rsi_long']:
                         signal = 'LONG'
                         sl_price = closes[i] - (atrs[i] * 1.5)

            # SHORT
            elif highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]:
                 if highs[i] < vahs[i] and closes[i] < lows[i-1] and closes[i] < opens[i]:
                     if rsis[i] > p_params['rsi_short']:
                         signal = 'SHORT'
                         sl_price = closes[i] + (atrs[i] * 1.5)
            
            # 4. EJECUCIÓN SIMULADA
            if signal:
                entry = closes[i]
                sl_dist = abs(entry - sl_price)
                if sl_dist == 0: continue
                
                # Definir TP dinámico según perfil (Tus reglas: Sniper TP largo, Flow TP corto)
                tp_mult = p_params['tp_mult']
                
                outcome_r = 0
                result_type = "HOLD"
                
                # Forward 12 velas
                for j in range(1, 13):
                    idx = i + j
                    if signal == 'LONG':
                        r_high = (highs[idx] - entry) / sl_dist
                        r_low = (lows[idx] - entry) / sl_dist
                        r_curr = (closes[idx] - entry) / sl_dist
                    else:
                        r_high = (entry - lows[idx]) / sl_dist 
                        r_low = (entry - highs[idx]) / sl_dist 
                        r_curr = (entry - closes[idx]) / sl_dist
                    
                    if r_low <= -1.1: # SL Hit (con deslizamiento)
                        outcome_r = -1.1
                        result_type = "SL"
                        break
                    
                    if r_high >= tp_mult: # TP Dinámico Hit
                        outcome_r = tp_mult
                        result_type = "TP"
                        break
                        
                    if j == 12: # Time Stop
                        outcome_r = r_curr
                        result_type = "TIME"
                
                trades.append({
                    'symbol': symbol,
                    'profile': profile,
                    'r_net': outcome_r - 0.05, # Fee
                    'type': result_type
                })
                cooldown = 12
        
        # Reporte por par
        if trades:
            df_res = pd.DataFrame(trades)
            net_r = df_res['r_net'].sum()
            print(f"   -> {symbol}: {len(trades)} trades | {net_r:.2f} R | WinRate: {(df_res['r_net']>0).mean():.1%}")
            global_log.extend(trades)
        else:
            print(f"   -> {symbol}: 0 trades (Cortex protegió todo el tiempo)")

    # --- REPORTE FINAL ---
    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*60)
        print("🤖 RESULTADOS HYDRA V7 (CORTEX DRIVEN)")
        print("="*60)
        print("\n📊 POR PERFIL IA:")
        print(df_glob.groupby('profile')[['r_net']].agg(['count', 'sum', 'mean']))
        
        total_r = df_glob['r_net'].sum()
        print(f"\n💰 R NETO TOTAL: {total_r:.2f} R")
        print("="*60)

if __name__ == "__main__":
    run_simulation()