import ccxt
import pandas as pd
import numpy as np
import time

# ---------------------------------------------------------
# 1. CÁLCULOS MATEMÁTICOS OPTIMIZADOS
# ---------------------------------------------------------

def calculate_rsi_manual(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_normalized_delta_cvd(df):
    """
    Mejora V2.2: Delta Ponderado por rango de vela.
    Penaliza Dojis y premia velas con cuerpo grande y poca mecha.
    """
    # Evitar división por cero
    range_candle = (df['high'] - df['low']).replace(0, 0.000001)
    
    # Delta = (Cuerpo / Rango Total) * Volumen
    # Si la vela es todo cuerpo, Delta = Volumen. Si es Doji, Delta ~ 0.
    df['delta_norm'] = ((df['close'] - df['open']) / range_candle) * df['volume']
    
    # CVD acumulado
    df['cvd'] = df['delta_norm'].cumsum()
    return df

def calculate_atr(df, period=14):
    """True Range para Volatilidad y Stop Loss dinámico"""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def is_rejection_candle(row, factor=1.5):
    """Factor ajustado a 1.5 para ser un poco más permisivo con la confirmación de cierre"""
    body = abs(row['close'] - row['open'])
    wick_top = row['high'] - max(row['close'], row['open'])
    wick_bottom = min(row['close'], row['open']) - row['low']
    if body == 0: body = 0.000001
    
    if wick_top > (body * factor): return 1 # Bearish Wick
    elif wick_bottom > (body * factor): return -1 # Bullish Wick
    return 0

def get_volume_profile_zones(df, lookback_bars=288):
    subset = df.iloc[-lookback_bars:].copy()
    price_min = subset['low'].min()
    price_max = subset['high'].max()
    
    if price_min == price_max: return None
    
    bins = np.linspace(price_min, price_max, 100)
    subset['bin'] = pd.cut(subset['close'], bins=bins)
    vp = subset.groupby('bin', observed=False)['volume'].sum().reset_index()
    
    total_volume = vp['volume'].sum()
    value_area_vol = total_volume * 0.70
    
    vp_sorted = vp.sort_values(by='volume', ascending=False)
    vp_sorted['cum_vol'] = vp_sorted['volume'].cumsum()
    va_df = vp_sorted[vp_sorted['cum_vol'] <= value_area_vol]
    
    if va_df.empty: return None
    
    vah = va_df['bin'].apply(lambda x: x.right).max()
    val = va_df['bin'].apply(lambda x: x.left).min()
    poc = vp.loc[vp['volume'].idxmax(), 'bin'].mid
    
    return {'VAH': vah, 'VAL': val, 'POC': poc}

# ---------------------------------------------------------
# 2. SIMULACIÓN DE ESTRATEGIA + RESULTADO
# ---------------------------------------------------------

def check_trade_result(df, entry_index, entry_price, direction, atr_value):
    """
    Mira el futuro para ver si el trade funcionó.
    SL = 1.5 * ATR
    TP = 2.0 * ATR
    """
    sl_dist = atr_value * 1.5
    tp_dist = atr_value * 2.0
    
    stop_loss = entry_price - sl_dist if direction == 'LONG' else entry_price + sl_dist
    take_profit = entry_price + tp_dist if direction == 'LONG' else entry_price - tp_dist
    
    # Revisar las siguientes 24 velas (2 horas)
    for j in range(1, 25):
        if entry_index + j >= len(df): break
        
        future_row = df.iloc[entry_index + j]
        
        if direction == 'LONG':
            if future_row['low'] <= stop_loss: return "❌ LOSS"
            if future_row['high'] >= take_profit: return "✅ WIN"
        else: # SHORT
            if future_row['high'] >= stop_loss: return "❌ LOSS"
            if future_row['low'] <= take_profit: return "✅ WIN"
            
    return "➖ TIME OUT"

def run_lab_test_v2():
    print("--- ORANGE PI LAB: STRATEGY V2.2 (REFINED) ---")
    exchange = ccxt.binance()
    symbol = 'BTC/USDT'
    limit = 1000 

    print(f"Descargando datos recientes de {symbol}...")
    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    # Indicadores
    df['RSI'] = calculate_rsi_manual(df['close'], 14)
    df = calculate_normalized_delta_cvd(df)
    df['ATR'] = calculate_atr(df)
    
    print("--- INICIANDO BACKTEST QUIRÚRGICO ---")
    
    last_trade_index = -999
    cooldown_bars = 12 # 1 hora de espera entre trades
    
    trades_found = 0
    
    for i in range(300, len(df)):
        # Cooldown check
        if i - last_trade_index < cooldown_bars: continue
        
        current_row = df.iloc[i]
        
        # Filtro de Volatilidad: Si ATR es muy bajo, ignorar (mercado muerto)
        # if current_row['ATR'] < df['ATR'].mean() * 0.5: continue 
        
        past_data = df.iloc[i-288:i]
        zones = get_volume_profile_zones(past_data)
        if not zones: continue
        
        vah, val = zones['VAH'], zones['VAL']
        rejection = is_rejection_candle(current_row)
        
        # --- LÓGICA REFINADA ---
        
        # LONG: Toca VAL pero CIERRA encima de VAL (No knife catch)
        is_long_zone = current_row['low'] <= val and current_row['close'] > val
        
        if is_long_zone:
            cond_rsi = current_row['RSI'] < 45 # Un poco más relajado al usar confirmación de cierre
            cond_delta = current_row['delta_norm'] > 0
            cond_cvd = df['cvd'].iloc[i] > df['cvd'].iloc[i-3] # CVD subiendo
            
            if cond_rsi and cond_delta and cond_cvd: # Rejection es opcional si exigimos cierre > val
                res = check_trade_result(df, i, current_row['close'], 'LONG', current_row['ATR'])
                print(f"[{current_row['timestamp']}] 🚀 LONG  @ {current_row['close']:.1f} | Result: {res}")
                print(f"    Vals: RSI={current_row['RSI']:.1f} | CVD_Slope=UP | Zone=VAL ({val:.1f})")
                last_trade_index = i
                trades_found += 1

        # SHORT: Toca VAH pero CIERRA debajo de VAH
        is_short_zone = current_row['high'] >= vah and current_row['close'] < vah
        
        if is_short_zone:
            cond_rsi = current_row['RSI'] > 55
            cond_delta = current_row['delta_norm'] < 0
            cond_cvd = df['cvd'].iloc[i] < df['cvd'].iloc[i-3] # CVD bajando
            
            if cond_rsi and cond_delta and cond_cvd:
                res = check_trade_result(df, i, current_row['close'], 'SHORT', current_row['ATR'])
                print(f"[{current_row['timestamp']}] 🔴 SHORT @ {current_row['close']:.1f} | Result: {res}")
                print(f"    Vals: RSI={current_row['RSI']:.1f} | CVD_Slope=DOWN | Zone=VAH ({vah:.1f})")
                last_trade_index = i
                trades_found += 1

    if trades_found == 0:
        print("No se encontraron trades con estos filtros estrictos.")
    else:
        print(f"\nTotal trades simulados: {trades_found}")

if __name__ == "__main__":
    run_lab_test_v2()