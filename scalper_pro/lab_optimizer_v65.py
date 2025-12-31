import pandas as pd
import numpy as np
import ccxt
import sys
import os

# ==============================================================================
# 🎛️ PLAYGROUND (ZONA DE EXPERIMENTOS)
# ==============================================================================

# 1. FECHAS DEL TORNEO
START_DATE = "2024-01-01"
END_DATE   = "2024-12-31"

# 2. DEFINICIÓN DE PERFILES (V7.1 ESTRUCTURAL)
PROFILES = {
    'SNIPER': {
        'description': 'Reversión Clásica (Bandas)',
        'vol_threshold': 1.0,   
        'rsi_long': 40,
        'rsi_short': 60,
        'tp_mult': 3.0,
        'sl_atr': 1.5,
        'mode': 'REVERSION'
    },
    'BREAKOUT': {
        'description': 'Ruptura de Estructura (Donchian)',
        'vol_threshold': 1.2,   # Volumen confirmatorio
        'rsi_long': 50,         # RSI debe tener espacio para subir
        'rsi_short': 50,        
        'tp_mult': 3.0,         # Buscamos recorrido medio-largo
        'sl_atr': 1.0,          # SL ajustado bajo la ruptura
        'lookback': 48,         # 48 velas de 5m = 4 Horas de Estructura
        'mode': 'BREAKOUT'
    }
}

# 3. ASIGNACIÓN DE ACTIVOS
TEST_MAP = {
    'BTC/USDT':  'SNIPER',    # BTC ama la reversión
    'SOL/USDT':  'BREAKOUT',  # SOL ama la tendencia
    'AVAX/USDT': 'BREAKOUT'   # Probemos si Donchian arregla a AVAX
}

# ==============================================================================
# ⚙️ MOTOR DEL BACKTEST
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

def calculate_indicators(df, params=None):
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
    
    # 4. Bandas (Para SNIPER)
    df['rolling_mean'] = df['close'].rolling(300).mean()
    df['rolling_std'] = df['close'].rolling(300).std()
    df['VAH'] = df['rolling_mean'] + df['rolling_std']
    df['VAL'] = df['rolling_mean'] - df['rolling_std']
    
    # 5. Estructura Donchian (Para BREAKOUT)
    # Lookback dinámico o default 48
    lb = params.get('lookback', 48) if params else 48
    
    # IMPORTANTE: Shift(1) para no mirar el futuro. 
    # Queremos romper el High de las 48 velas ANTERIORES.
    df['Donchian_High'] = df['high'].rolling(lb).max().shift(1)
    df['Donchian_Low']  = df['low'].rolling(lb).min().shift(1)
    
    return df.dropna()

def run_optimizer():
    print(f"\n🧪 LABORATORIO V7.1 (DONCHIAN STRUCTURE)")
    print("="*60)
    
    global_log = []
    
    for symbol, profile_name in TEST_MAP.items():
        # Pasamos params al calculador para el lookback dinámico
        raw_df = fetch_data(symbol)
        if raw_df.empty: continue
        
        params = PROFILES[profile_name]
        df = calculate_indicators(raw_df, params)
        mode = params.get('mode', 'REVERSION')
        
        # Numpy Arrays
        closes = df['close'].values; opens = df['open'].values
        highs = df['high'].values; lows = df['low'].values
        vols = df['volume'].values; vol_mas = df['Vol_MA'].values
        rsis = df['RSI'].values; atrs = df['ATR'].values
        
        # Sniper Arrays
        vahs = df['VAH'].values; vals = df['VAL'].values
        # Breakout Arrays
        d_highs = df['Donchian_High'].values; d_lows = df['Donchian_Low'].values
        
        trades = []
        cooldown = 0
        
        for i in range(300, len(df)-12):
            if cooldown > 0: cooldown -= 1; continue
            
            # FILTRO VOLUMEN
            if vols[i] < (vol_mas[i] * params['vol_threshold']): 
                continue
                
            signal = None; sl_price = 0
            
            # ==================================================================
            # 🛡️ MODO REVERSIÓN (SNIPER)
            # ==================================================================
            if mode == 'REVERSION':
                if lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]:
                     if lows[i] > vals[i] and closes[i] > highs[i-1] and closes[i] > opens[i]:
                         if rsis[i] < params['rsi_long']:
                             signal = 'LONG'; sl_price = closes[i] - (atrs[i] * params['sl_atr'])

                elif highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]:
                     if highs[i] < vahs[i] and closes[i] < lows[i-1] and closes[i] < opens[i]:
                         if rsis[i] > params['rsi_short']:
                             signal = 'SHORT'; sl_price = closes[i] + (atrs[i] * params['sl_atr'])

            # ==================================================================
            # 🚀 MODO BREAKOUT (ESTRUCTURAL)
            # ==================================================================
            elif mode == 'BREAKOUT':
                # LONG: Precio rompe el Techo de 4 horas
                # + Vela Verde + RSI saludable (no extremo 90, pero sí fuerte >50)
                if closes[i] > d_highs[i] and closes[i] > opens[i]:
                    if rsis[i] > 50 and rsis[i] < 80:
                        signal = 'LONG'
                        sl_price = closes[i] - (atrs[i] * params['sl_atr'])
                
                # SHORT: Precio rompe el Piso de 4 horas
                elif closes[i] < d_lows[i] and closes[i] < opens[i]:
                    if rsis[i] < 50 and rsis[i] > 20:
                        signal = 'SHORT'
                        sl_price = closes[i] + (atrs[i] * params['sl_atr'])

            # ==================================================================
            # SIMULACIÓN
            # ==================================================================
            if signal:
                entry = closes[i]
                sl_dist = abs(entry - sl_price)
                if sl_dist == 0: continue
                
                tp_mult = params['tp_mult']
                outcome_r = 0; result_type = "HOLD"
                
                for j in range(1, 13): # 1 Hora
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
                    
                    if r_low <= -1.0: outcome_r = -1.0; result_type = "SL"; break
                    if r_high >= tp_mult: outcome_r = tp_mult; result_type = "TP"; break
                    if j == 12: outcome_r = r_curr; result_type = "TIME"
                
                r_net = outcome_r - 0.05
                trades.append({'symbol': symbol, 'profile': profile_name, 'r_net': r_net, 'type': result_type})
                cooldown = 12 
        
        if trades:
            df_res = pd.DataFrame(trades)
            net_r = df_res['r_net'].sum()
            win_rate = (df_res['r_net'] > 0).mean()
            print(f"   -> {symbol} [{profile_name}]: {len(trades)} trades | R Neto: {net_r:.2f} R | WR: {win_rate:.1%}")
            global_log.extend(trades)
        else:
            print(f"   -> {symbol} [{profile_name}]: 0 trades")

    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*60)
        print("📊 RESULTADOS V7.1 (DONCHIAN)")
        print("="*60)
        print(df_glob.groupby('profile')[['r_net']].agg(['count', 'sum', 'mean']))
        print("\n💰 R NETO TOTAL: {:.2f} R".format(df_glob['r_net'].sum()))
        print("="*60)

if __name__ == "__main__":
    run_optimizer()