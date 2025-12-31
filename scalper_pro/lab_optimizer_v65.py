import pandas as pd
import numpy as np
import ccxt
import sys
import os

# ==============================================================================
# 🎛️ PLAYGROUND V7.3 (TREND HUNTER)
# ==============================================================================

START_DATE = "2024-01-01"
END_DATE   = "2024-12-31"

PROFILES = {
    'SNIPER': {
        'description': 'Reversión Clásica',
        'vol_threshold': 1.0,   
        'rsi_long': 40,
        'rsi_short': 60,
        'tp_mult': 3.0,
        'sl_atr': 1.5,
        'mode': 'REVERSION',
        'cooldown': 12
    },
    'BREAKOUT': {
        'description': 'Trend Following (Long Only + BE)',
        'vol_threshold': 1.1,   
        'rsi_long': 50,         
        'rsi_short': 50,        
        'tp_mult': 4.0,         # Buscamos 5R
        'sl_atr': 1.0,          # SL ajustado
        'lookback': 144,        # Estructura 24h
        'max_range_atr': 8.0,   # Filtro de expansión previa
        'mode': 'BREAKOUT',
        'direction': 'LONG_ONLY', # <--- NUEVO FILTRO
        'breakeven_trigger': 1.5, # <--- NUEVO: Si toca +1.5R, mover a BE
        'cooldown': 48          # 8 Horas
    }
}

TEST_MAP = {
    'BTC/USDT':  'SNIPER',    # Base
    'BTC/USDT':  'BREAKOUT',  # Descomenta para probar si BTC rompe bien
    'SOL/USDT':  'BREAKOUT',  # El candidato principal
    # 'AVAX/USDT': REMOVIDO
    'ETH/USDT':  'SNIPER',
    'ETH/USDT':  'BREAKOUT'
}

# ==============================================================================
# ⚙️ MOTOR V7.3
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
    
    # Básicos
    df['Vol_MA'] = df['volume'].rolling(20).mean()
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # ATR
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['ATR'] = true_range.rolling(14).mean()
    
    # Sniper Bandas
    df['rolling_mean'] = df['close'].rolling(300).mean()
    df['rolling_std'] = df['close'].rolling(300).std()
    df['VAH'] = df['rolling_mean'] + df['rolling_std']
    df['VAL'] = df['rolling_mean'] - df['rolling_std']
    
    # Breakout Donchian
    lb = params.get('lookback', 288) if params else 288
    df['Donchian_High'] = df['high'].rolling(lb).max().shift(1)
    df['Donchian_Low']  = df['low'].rolling(lb).min().shift(1)
    df['Channel_Width'] = df['Donchian_High'] - df['Donchian_Low']
    
    return df.dropna()

def run_optimizer():
    print(f"\n🧪 LABORATORIO V7.3 (TREND HUNTER + BE)")
    print("="*60)
    
    global_log = []
    
    for symbol, profile_name in TEST_MAP.items():
        raw_df = fetch_data(symbol)
        if raw_df.empty: continue
        
        params = PROFILES[profile_name]
        df = calculate_indicators(raw_df, params)
        
        mode = params.get('mode', 'REVERSION')
        cooldown_limit = params.get('cooldown', 12)
        direction_filter = params.get('direction', 'BOTH')
        be_trigger = params.get('breakeven_trigger', None)
        max_range_atr = params.get('max_range_atr', 100.0)
        
        # Arrays
        closes = df['close'].values; opens = df['open'].values
        highs = df['high'].values; lows = df['low'].values
        vols = df['volume'].values; vol_mas = df['Vol_MA'].values
        rsis = df['RSI'].values; atrs = df['ATR'].values
        vahs = df['VAH'].values; vals = df['VAL'].values
        d_highs = df['Donchian_High'].values; d_lows = df['Donchian_Low'].values
        chan_width = df['Channel_Width'].values
        
        trades = []
        cooldown = 0
        
        for i in range(300, len(df)-288): # Margen para simulación larga
            if cooldown > 0: cooldown -= 1; continue
            
            if vols[i] < (vol_mas[i] * params['vol_threshold']): 
                continue
                
            signal = None; sl_price = 0
            
            # --- REVERSION ---
            if mode == 'REVERSION':
                if lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]:
                     if lows[i] > vals[i] and closes[i] > highs[i-1] and closes[i] > opens[i]:
                         if rsis[i] < params['rsi_long']:
                             signal = 'LONG'; sl_price = closes[i] - (atrs[i] * params['sl_atr'])

                elif highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]:
                     if highs[i] < vahs[i] and closes[i] < lows[i-1] and closes[i] < opens[i]:
                         if rsis[i] > params['rsi_short']:
                             signal = 'SHORT'; sl_price = closes[i] + (atrs[i] * params['sl_atr'])

            # --- BREAKOUT ---
            elif mode == 'BREAKOUT':
                if chan_width[i] > (atrs[i] * max_range_atr): continue 

                # LONG
                if closes[i] > d_highs[i] and closes[i] > opens[i]:
                    if rsis[i] > 50 and rsis[i] < 80:
                        signal = 'LONG'
                        sl_price = closes[i] - (atrs[i] * params['sl_atr'])
                
                # SHORT (Solo si no está filtrado)
                elif closes[i] < d_lows[i] and closes[i] < opens[i] and direction_filter != 'LONG_ONLY':
                    if rsis[i] < 50 and rsis[i] > 20:
                        signal = 'SHORT'
                        sl_price = closes[i] + (atrs[i] * params['sl_atr'])

            # --- SIMULACIÓN AVANZADA (BE + LONG RUN) ---
            if signal:
                entry = closes[i]
                current_sl = sl_price
                sl_dist = abs(entry - current_sl)
                if sl_dist == 0: continue
                
                tp_mult = params['tp_mult']
                outcome_r = 0; result_type = "HOLD"
                
                # Tiempo extendido: 24h para Breakout, 1h para Sniper
                look_forward = 288 if mode == 'BREAKOUT' else 12 
                is_breakeven = False
                
                for j in range(1, look_forward + 1): 
                    idx = i + j
                    
                    # Precios de la vela futura
                    candle_high = highs[idx]
                    candle_low = lows[idx]
                    candle_close = closes[idx]
                    
                    # 1. Chequeo de TP/SL
                    if signal == 'LONG':
                        # Logica BE
                        if be_trigger and not is_breakeven:
                            max_profit_r = (candle_high - entry) / sl_dist
                            if max_profit_r >= be_trigger:
                                current_sl = entry + (sl_dist * 0.1) # Asegurar fees
                                is_breakeven = True
                        
                        # Hit SL?
                        if candle_low <= current_sl:
                            # Si era BE, salimos en 0.1, si no, en -1.0
                            outcome_r = 0.1 if is_breakeven else -1.0
                            result_type = "BE" if is_breakeven else "SL"
                            break
                        
                        # Hit TP?
                        if candle_high >= (entry + (sl_dist * tp_mult)):
                            outcome_r = tp_mult
                            result_type = "TP"
                            break
                            
                    else: # SHORT
                        if be_trigger and not is_breakeven:
                            max_profit_r = (entry - candle_low) / sl_dist
                            if max_profit_r >= be_trigger:
                                current_sl = entry - (sl_dist * 0.1)
                                is_breakeven = True
                                
                        if candle_high >= current_sl:
                            outcome_r = 0.1 if is_breakeven else -1.0
                            result_type = "BE" if is_breakeven else "SL"
                            break
                            
                        if candle_low <= (entry - (sl_dist * tp_mult)):
                            outcome_r = tp_mult
                            result_type = "TP"
                            break
                    
                    # Time Exit
                    if j == look_forward:
                        # Calculamos R al cierre
                        if signal == 'LONG':
                            outcome_r = (candle_close - entry) / sl_dist
                        else:
                            outcome_r = (entry - candle_close) / sl_dist
                        result_type = "TIME"
                
                # Fees reales
                r_net = outcome_r - 0.05
                trades.append({'symbol': symbol, 'profile': profile_name, 'r_net': r_net, 'type': result_type})
                
                cooldown = cooldown_limit 
        
        if trades:
            df_res = pd.DataFrame(trades)
            net_r = df_res['r_net'].sum()
            win_rate = (df_res['r_net'] > 0).mean()
            print(f"   -> {symbol} [{profile_name}]: {len(trades)} trades | R Neto: {net_r:.2f} R | WR: {win_rate:.1%} | Avg: {df_res['r_net'].mean():.2f}")
            global_log.extend(trades)
        else:
            print(f"   -> {symbol} [{profile_name}]: 0 trades")

    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*60)
        print("📊 RESULTADOS V7.3 (TREND HUNTER)")
        print("="*60)
        print(df_glob.groupby(['profile', 'type'])['r_net'].count().unstack().fillna(0))
        print("-" * 30)
        print(f"💰 R NETO TOTAL: {df_glob['r_net'].sum():.2f} R")
        print("="*60)

if __name__ == "__main__":
    run_optimizer()