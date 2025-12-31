import pandas as pd
import numpy as np
import ccxt
import sys
import os

# ==============================================================================
# 🎛️ PLAYGROUND (TU ZONA DE JUEGO)
# ==============================================================================

# 1. FECHAS
# Prueba 2024 (Volátil) vs 2023 (Lateral)
START_DATE = "2024-01-01"
END_DATE   = "2024-12-31"

# 2. PERFILES (Tus "Armas")
# Aquí es donde puedes ajustar la sensibilidad
PROFILES = {
    'SNIPER': {
        'description': 'Configuración Estricta (BTC)',
        'vol_threshold': 1.0,   # Gatillo de Volumen (x veces la media)
        'rsi_long': 40,         # Compra si RSI < 40
        'rsi_short': 60,        # Vende si RSI > 60
        'tp_mult': 3.0,         # Ratio Riesgo/Beneficio
        'sl_atr': 1.5,          # Distancia Stop Loss en ATR
        'cooldown': 12          # Velas de espera tras un trade (1h)
    },
    'FLOW': {
        'description': 'Configuración Momentum (SOL)',
        'vol_threshold': 0.6,   # Gatillo más suave
        'rsi_long': 50,         # Neutro
        'rsi_short': 50,        # Neutro
        'tp_mult': 2.0,         # TP más corto = Más Win Rate
        'sl_atr': 1.5,
        'cooldown': 12
    }
}

# 3. MAPA DE PRUEBAS
# ¿Qué perfil le aplicamos a qué moneda?
TEST_MAP = {
    'BTC/USDT': 'SNIPER', # El Rey
    'SOL/USDT': 'FLOW',   # El Príncipe
    'ETH/USDT': 'SNIPER'  # El Hermano (A ver si logras hacerlo rentable)
}

# ==============================================================================
# ⚙️ MOTOR V6.5 (REVERSIÓN PURA)
# ==============================================================================

def fetch_data(symbol):
    print(f"📥 Descargando {symbol} ({START_DATE} - {END_DATE})...", end=" ")
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

def calculate_indicators(df):
    df = df.copy()
    
    # 1. Volumen Relativo
    df['Vol_MA'] = df['volume'].rolling(20).mean()
    
    # 2. RSI (14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # 3. ATR (14)
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['ATR'] = true_range.rolling(14).mean()
    
    # 4. Bandas (VAH/VAL Proxy con Bollinger 2SD)
    # En producción usas Volume Profile real, pero la correlación es >90%
    df['rolling_mean'] = df['close'].rolling(300).mean()
    df['rolling_std'] = df['close'].rolling(300).std()
    df['VAH'] = df['rolling_mean'] + df['rolling_std']
    df['VAL'] = df['rolling_mean'] - df['rolling_std']
    
    return df.dropna()

def run_optimizer():
    print(f"\n🧪 LABORATORIO V6.5 (PLAYGROUND)")
    print("="*60)
    
    global_log = []
    
    for symbol, profile_name in TEST_MAP.items():
        # 1. Preparar Datos
        raw_df = fetch_data(symbol)
        if raw_df.empty: continue
        
        df = calculate_indicators(raw_df)
        params = PROFILES[profile_name]
        
        # Numpy Arrays (Velocidad pura)
        closes = df['close'].values; opens = df['open'].values
        highs = df['high'].values; lows = df['low'].values
        vols = df['volume'].values; vol_mas = df['Vol_MA'].values
        rsis = df['RSI'].values; atrs = df['ATR'].values
        vahs = df['VAH'].values; vals = df['VAL'].values
        
        trades = []
        cooldown = 0
        cooldown_limit = params.get('cooldown', 12)
        
        # 2. Bucle de Simulación
        for i in range(300, len(df)-12):
            if cooldown > 0: cooldown -= 1; continue
            
            # FILTRO VOLUMEN
            if vols[i] < (vol_mas[i] * params['vol_threshold']): 
                continue
                
            signal = None; sl_price = 0
            
            # --- LÓGICA DE REVERSIÓN ---
            
            # LONG: Precio toca VAL (Suelo) y rebota
            if lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]: # Toque previo
                 # Confirmación actual: Cierra arriba del open y del high anterior (Vela de fuerza)
                 if lows[i] > vals[i] and closes[i] > highs[i-1] and closes[i] > opens[i]:
                     # Filtro RSI (No compramos si ya está caro)
                     if rsis[i] < params['rsi_long']:
                         signal = 'LONG'; sl_price = closes[i] - (atrs[i] * params['sl_atr'])

            # SHORT: Precio toca VAH (Techo) y cae
            elif highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]:
                 if highs[i] < vahs[i] and closes[i] < lows[i-1] and closes[i] < opens[i]:
                     if rsis[i] > params['rsi_short']:
                         signal = 'SHORT'; sl_price = closes[i] + (atrs[i] * params['sl_atr'])
            
            # --- EJECUCIÓN DEL TRADE ---
            if signal:
                entry = closes[i]
                sl_dist = abs(entry - sl_price)
                if sl_dist == 0: continue
                
                tp_mult = params['tp_mult']
                outcome_r = 0; result_type = "HOLD"
                
                # Proyección 1 Hora (12 velas)
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
                    
                    # Chequeo SL primero (Pesimista)
                    if r_low <= -1.0: outcome_r = -1.0; result_type = "SL"; break
                    # Chequeo TP
                    if r_high >= tp_mult: outcome_r = tp_mult; result_type = "TP"; break
                    # Time Stop
                    if j == 12: outcome_r = r_curr; result_type = "TIME"
                
                # Fees (-0.05R aprox por trade ida y vuelta)
                r_net = outcome_r - 0.05
                trades.append({'symbol': symbol, 'profile': profile_name, 'r_net': r_net, 'type': result_type})
                cooldown = cooldown_limit
        
        # 3. Reporte Individual
        if trades:
            df_res = pd.DataFrame(trades)
            net_r = df_res['r_net'].sum()
            win_rate = (df_res['r_net'] > 0).mean()
            print(f"   -> {symbol} [{profile_name}]: {len(trades)} trades | R Neto: {net_r:.2f} R | WR: {win_rate:.1%}")
            global_log.extend(trades)
        else:
            print(f"   -> {symbol} [{profile_name}]: 0 trades")

    # 4. Reporte Global
    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*60)
        print("📊 RESULTADOS FINALES")
        print("="*60)
        print(df_glob.groupby('profile')[['r_net']].agg(['count', 'sum', 'mean']))
        print("-" * 30)
        print(f"💰 R NETO TOTAL: {df_glob['r_net'].sum():.2f} R")
        print("="*60)

if __name__ == "__main__":
    run_optimizer()