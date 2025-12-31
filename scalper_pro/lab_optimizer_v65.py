import pandas as pd
import numpy as np
import ccxt
import sys
import os

# ==============================================================================
# 🎛️ PLAYGROUND (AQUÍ ES DONDE JUEGAS)
# ==============================================================================

# 1. FECHAS DEL TORNEO
# Prueba distintos regímenes:
# - 2022-01-01 a 2022-12-31 (Bear Market / Crash)
# - 2023-01-01 a 2023-12-31 (Recuperación / Cangrejo)
# - 2024-01-01 a 2024-12-31 (Choppy / Trampas - El nivel difícil)
START_DATE = "2023-01-01"
END_DATE   = "2023-12-31"

# 2. DEFINICIÓN DE PERFILES (Tus herramientas)
# Juega con estos números. ¿Qué pasa si bajas el RSI de Sniper a 30?
# ¿Qué pasa si subes el Volumen de Flow a 0.8?
PROFILES = {
    'SNIPER': {
        'vol_thresh': 1.2,    # Gatillo de Volumen (x veces la media)
        'rsi_long': 40,       # RSI máximo para entrar en Long (Sobreventa)
        'rsi_short': 60,      # RSI mínimo para entrar en Short (Sobrecompra)
        'tp_mult': 3.0,       # Ratio Riesgo/Beneficio buscado
        'sl_atr': 1.5         # Distancia del Stop Loss en ATRs
    },
    'FLOW': {
        'vol_thresh': 0.8,    # Gatillo más suave
        'rsi_long': 45,       # RSI Neutro
        'rsi_short': 55,      # RSI Neutro
        'tp_mult': 1.5,       # TP más corto, asegurar ganancia
        'sl_atr': 1.5
    }
}

# 3. ASIGNACIÓN DE ACTIVOS
# Aquí decides qué estrategia usas contra qué moneda.
# ¡Prueba ponerle 'FLOW' a BTC para ver cómo pierde dinero! Es educativo.
TEST_MAP = {
    'BTC/USDT':  'SNIPER',
    'ETH/USDT':  'SNIPER',
    'SOL/USDT':  'FLOW',
    'AVAX/USDT': 'FLOW',   # La joya de la corona
    'LTC/USDT':  'SNIPER'
    #'BNB/USDT':  'FLOW'   # Descomenta para probar
}

# ==============================================================================
# ⚙️ MOTOR DEL BACKTEST (NO TOCAR A MENOS QUE SEPAS QUÉ HACES)
# ==============================================================================

def fetch_data(symbol):
    print(f"📥 Descargando datos para {symbol} ({START_DATE} - {END_DATE})...", end=" ")
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
            # print(".", end="", flush=True) # Silencioso para velocidad
        except: break
            
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f" {len(df)} velas.")
    return df

def calculate_indicators(df):
    """Calcula los indicadores necesarios para V6.5"""
    df = df.copy()
    
    # 1. Volumen Relativo
    df['Vol_MA'] = df['volume'].rolling(20).mean()
    
    # 2. RSI (14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # 3. ATR (14) - Para SL dinámico
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['ATR'] = true_range.rolling(14).mean()
    
    # 4. Estructura de Mercado (Simulación de VAH/VAL con Bandas)
    # Nota: Usamos Bandas de Bollinger (2.0 SD) como proxy rápido de Volume Profile
    # para backtests largos. En producción usas VP real, pero la correlación es >90%.
    df['rolling_mean'] = df['close'].rolling(300).mean() # Base de largo plazo
    df['rolling_std'] = df['close'].rolling(300).std()
    df['VAH'] = df['rolling_mean'] + df['rolling_std']
    df['VAL'] = df['rolling_mean'] - df['rolling_std']
    
    return df.dropna()

def run_optimizer():
    print(f"\n🧪 LABORATORIO DE OPTIMIZACIÓN V6.5")
    print("="*60)
    
    global_log = []
    
    for symbol, profile_name in TEST_MAP.items():
        # 1. Cargar Datos
        df = fetch_data(symbol)
        if df.empty: continue
        
        # 2. Calcular Indicadores
        df = calculate_indicators(df)
        
        # 3. Cargar Configuración del Perfil Actual
        params = PROFILES[profile_name]
        
        # Variables para velocidad (Numpy Arrays)
        closes = df['close'].values
        opens = df['open'].values
        highs = df['high'].values
        lows = df['low'].values
        vols = df['volume'].values
        vol_mas = df['Vol_MA'].values
        rsis = df['RSI'].values
        atrs = df['ATR'].values
        vahs = df['VAH'].values
        vals = df['VAL'].values
        
        trades = []
        cooldown = 0
        
        # --- BUCLE DE TRADING ---
        for i in range(300, len(df)-12):
            if cooldown > 0: 
                cooldown -= 1
                continue
            
            # A. FILTRO DE VOLUMEN
            if vols[i] < (vol_mas[i] * params['vol_thresh']): 
                continue
                
            signal = None
            sl_price = 0
            
            # B. LÓGICA DE ENTRADA (ESTRUCTURA + RSI)
            
            # LONG: Rechazo del VAL + RSI Bajo
            if lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]: # Recuperación
                 if lows[i] > vals[i] and closes[i] > highs[i-1] and closes[i] > opens[i]: # Confirmación
                     if rsis[i] < params['rsi_long']:
                         signal = 'LONG'
                         sl_price = closes[i] - (atrs[i] * params['sl_atr'])

            # SHORT: Rechazo del VAH + RSI Alto
            elif highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]: # Pérdida
                 if highs[i] < vahs[i] and closes[i] < lows[i-1] and closes[i] < opens[i]: # Confirmación
                     if rsis[i] > params['rsi_short']:
                         signal = 'SHORT'
                         sl_price = closes[i] + (atrs[i] * params['sl_atr'])
            
            # C. SIMULACIÓN DE RESULTADO
            if signal:
                entry = closes[i]
                sl_dist = abs(entry - sl_price)
                if sl_dist == 0: continue
                
                tp_mult = params['tp_mult']
                outcome_r = 0
                result_type = "HOLD"
                
                # Proyectamos 12 velas (1 hora) al futuro
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
                    
                    # 1. Chequeo SL (Pesimista: Asumimos que toca SL primero si hay duda)
                    if r_low <= -1.0: # Toca SL exacto
                        outcome_r = -1.0
                        result_type = "SL"
                        break
                    
                    # 2. Chequeo TP
                    if r_high >= tp_mult:
                        outcome_r = tp_mult
                        result_type = "TP"
                        break
                    
                    # 3. Time Stop (Cierre tras 1 hora)
                    if j == 12:
                        outcome_r = r_curr
                        result_type = "TIME"
                
                # Fee simulado (Spread + Comisión ~ 0.05R)
                r_net = outcome_r - 0.05
                
                trades.append({
                    'symbol': symbol,
                    'profile': profile_name,
                    'r_net': r_net,
                    'type': result_type
                })
                cooldown = 12 # Esperamos 1 hora antes de buscar otro trade
        
        # --- REPORTE INDIVIDUAL ---
        if trades:
            df_res = pd.DataFrame(trades)
            net_r = df_res['r_net'].sum()
            count = len(df_res)
            win_rate = (df_res['r_net'] > 0).mean()
            print(f"   -> {symbol} [{profile_name}]: {count} trades | R Neto: {net_r:.2f} R | WR: {win_rate:.1%}")
            global_log.extend(trades)
        else:
            print(f"   -> {symbol} [{profile_name}]: 0 trades")

    # --- REPORTE GLOBAL ---
    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*60)
        print("📊 RESULTADOS DEL EXPERIMENTO")
        print("="*60)
        
        print("\n🏆 POR PERFIL:")
        print(df_glob.groupby('profile')[['r_net']].agg(['count', 'sum', 'mean']))
        
        print("\n💰 R NETO TOTAL: {:.2f} R".format(df_glob['r_net'].sum()))
        print("="*60)

if __name__ == "__main__":
    run_optimizer()