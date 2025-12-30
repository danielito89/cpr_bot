import pandas as pd
import numpy as np
import joblib
import sys
import os
import ccxt

# Imports locales (Asegúrate que la ruta sea correcta en tu Orange Pi)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
from core.data_processor import DataProcessor

# --- CONFIGURACIÓN DEL BACKTEST ---
START_DATE = "2024-01-01"
END_DATE   = "2024-12-31"
TARGET_PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT', 'LTC/USDT']

# Definición de Perfiles Tácticos (Reglas de Oro V7)
PARAMS = {
    'SNIPER': {
        'vol_thresh': 1.2,    # Requiere volumen explosivo
        'rsi_long': 40,       # Sobrevendido fuerte
        'rsi_short': 60,      # Sobrecomprado fuerte
        'tp_mult': 3.0        # Busca Home Runs
    },
    'FLOW': {
        'vol_thresh': 0.6,    # Volumen medio aceptable
        'rsi_long': 50,       # Neutro
        'rsi_short': 50,      # Neutro
        'tp_mult': 1.5        # Busca ganancias constantes
    }
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
    Calcula features IDENTICAS al entrenamiento.
    """
    df = df.copy()
    
    # 1. Volatilidad (True Range Real Vectorizado)
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
    
    # --- DATOS TÉCNICOS PARA LA ESTRATEGIA (V6.5) ---
    # Simulación rápida de Bandas/Zones
    df['rolling_mean'] = df['close'].rolling(300).mean()
    df['rolling_std'] = df['close'].rolling(300).std()
    df['VAH'] = df['rolling_mean'] + df['rolling_std']
    df['VAL'] = df['rolling_mean'] - df['rolling_std']
    
    return df.dropna()

def run_simulation():
    print(f"\n🧠 INICIANDO BACKTEST ESPECIALISTA V7 ({START_DATE} - {END_DATE})")
    print("="*65)
    
    global_log = []
    # Nombres de columnas EXACTOS para evitar warnings de sklearn
    feature_cols = ['feat_volatility', 'feat_vol_ratio', 'feat_rsi', 'feat_trend_dev']

    for symbol in TARGET_PAIRS:
        # 1. CARGAR MODELO ESPECIALISTA
        # Construimos el nombre del archivo: cortex_model_BTCUSDT.joblib
        safe_pair = symbol.replace('/', '')
        model_path = f"cortex_model_{safe_pair}.joblib"
        
        try:
            model = joblib.load(model_path)
            
            # MAPEO DE CLASES (CRÍTICO)
            # Aseguramos saber qué índice es SNIPER(0), FLOW(1), WAIT(2)
            class_map = {label: idx for idx, label in enumerate(model.classes_)}
            idx_sniper = class_map.get(0)
            idx_flow = class_map.get(1)
            idx_wait = class_map.get(2)
            
            if None in [idx_sniper, idx_flow, idx_wait]:
                print(f"❌ Error en clases del modelo {symbol}. Saltando.")
                continue
                
        except Exception as e:
            print(f"⚠️ No se encontró modelo para {symbol} ({e}). Saltando.")
            continue

        # 2. CARGAR DATOS
        df = load_data(symbol)
        if df.empty: continue
        
        # 3. VECTORIZACIÓN DE PREDICCIONES (VELOCIDAD MÁXIMA) 🚀
        print(f"   ⚙️  Consultando IA Especialista para {symbol}...")
        df = calculate_features_for_inference(df)
        
        # Extraemos features como DataFrame con nombres (para evitar Warnings)
        X_full = df[feature_cols]
        
        # Predicción masiva (Milisegundos)
        all_probs = model.predict_proba(X_full)
        
        # 4. PREPARACIÓN DE ARRAYS PARA BUCLE
        closes = df['close'].values
        opens = df['open'].values
        highs = df['high'].values
        lows = df['low'].values
        vols = df['volume'].values
        vol_mas = df['Vol_MA'].values
        rsis = df['feat_rsi'].values
        vahs = df['VAH'].values
        vals = df['VAL'].values
        atrs = df['ATR'].values
        
        trades = []
        cooldown = 0
        
        # 5. BUCLE DE TRADING (CANDLE BY CANDLE)
        # Ajustamos offset porque df.dropna() eliminó las primeras velas
        # Iteramos sobre el tamaño actual del DF
        for i in range(300, len(df)-12):
            if cooldown > 0: 
                cooldown -= 1
                continue
            
            # --- CEREBRO IA ---
            probs = all_probs[i]
            p_sniper = probs[idx_sniper]
            p_flow = probs[idx_flow]
            p_wait = probs[idx_wait]
            
            profile = 'WAIT'
            
            # LÓGICA DE META-CONTROLADOR
            if p_wait > 0.50: 
                profile = 'WAIT' # Kill Switch
            elif p_sniper > 0.40: 
                profile = 'SNIPER' # Modo Agresivo
            else:
                profile = 'FLOW' # Modo Defensivo
                
            if profile == 'WAIT': continue
            
            # --- LÓGICA TÉCNICA (V6.5) ---
            p_params = PARAMS[profile]
            
            # Filtro de Volumen Dinámico
            if vols[i] < (vol_mas[i] * p_params['vol_thresh']): continue
            
            signal = None
            sl_price = 0
            
            # LONG SETUP
            if lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]: # Rechazo VAL
                 if lows[i] > vals[i] and closes[i] > highs[i-1] and closes[i] > opens[i]:
                     if rsis[i] < p_params['rsi_long']:
                         signal = 'LONG'
                         sl_price = closes[i] - (atrs[i] * 1.5)

            # SHORT SETUP
            elif highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]: # Rechazo VAH
                 if highs[i] < vahs[i] and closes[i] < lows[i-1] and closes[i] < opens[i]:
                     if rsis[i] > p_params['rsi_short']:
                         signal = 'SHORT'
                         sl_price = closes[i] + (atrs[i] * 1.5)
            
            # --- EJECUCIÓN SIMULADA ---
            if signal:
                entry = closes[i]
                sl_dist = abs(entry - sl_price)
                if sl_dist == 0: continue
                
                tp_mult = p_params['tp_mult']
                outcome_r = 0
                result_type = "HOLD"
                
                # Forward simulation (12 velas / 1 Hora)
                for j in range(1, 13):
                    idx = i + j
                    if idx >= len(closes): break
                    
                    if signal == 'LONG':
                        r_high = (highs[idx] - entry) / sl_dist
                        r_low = (lows[idx] - entry) / sl_dist
                        r_curr = (closes[idx] - entry) / sl_dist
                    else:
                        r_high = (entry - lows[idx]) / sl_dist 
                        r_low = (entry - highs[idx]) / sl_dist 
                        r_curr = (entry - closes[idx]) / sl_dist
                    
                    # Chequeo SL (Pesimista)
                    if r_low <= -1.1:
                        outcome_r = -1.1
                        result_type = "SL"
                        break
                    # Chequeo TP (Dinámico)
                    if r_high >= tp_mult:
                        outcome_r = tp_mult
                        result_type = "TP"
                        break
                    # Time Stop
                    if j == 12:
                        outcome_r = r_curr
                        result_type = "TIME"
                
                # Fee Stress Test (-0.05R por trade)
                trades.append({
                    'symbol': symbol,
                    'profile': profile,
                    'r_net': outcome_r - 0.05,
                    'type': result_type
                })
                cooldown = 12

        # Reporte por Par
        if trades:
            df_res = pd.DataFrame(trades)
            net_r = df_res['r_net'].sum()
            win_rate = (df_res['r_net']>0).mean()
            print(f"   -> {symbol}: {len(trades)} trades | {net_r:.2f} R | WinRate: {win_rate:.1%}")
            global_log.extend(trades)
        else:
            print(f"   -> {symbol}: 0 trades (Modo Protección Activado 🛡️)")

    # --- REPORTE GLOBAL ---
    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*65)
        print("🤖 RESULTADOS FINALES HYDRA V7 (CORTEX ESPECIALISTAS)")
        print("="*65)
        
        print("\n📊 RENDIMIENTO POR PERFIL IA:")
        print(df_glob.groupby('profile')[['r_net']].agg(['count', 'sum', 'mean']))
        
        print("\n🏆 RANKING POR MONEDA:")
        print(df_glob.groupby('symbol')['r_net'].sum().sort_values(ascending=False))
        
        total_r = df_glob['r_net'].sum()
        print(f"\n💰 R NETO TOTAL: {total_r:.2f} R")
        print("="*65)

if __name__ == "__main__":
    run_simulation()