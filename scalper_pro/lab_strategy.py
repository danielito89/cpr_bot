import ccxt
import pandas as pd
import numpy as np

# ---------------------------------------------------------
# 1. CÁLCULOS (Igual que antes)
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
    range_candle = (df['high'] - df['low']).replace(0, 0.000001)
    df['delta_norm'] = ((df['close'] - df['open']) / range_candle) * df['volume']
    df['cvd'] = df['delta_norm'].cumsum()
    return df

def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def is_rejection_candle(row, factor=1.5):
    body = abs(row['close'] - row['open'])
    if body == 0: body = 0.000001
    wick_top = row['high'] - max(row['close'], row['open'])
    wick_bottom = min(row['close'], row['open']) - row['low']
    
    if wick_top > (body * factor): return 1
    elif wick_bottom > (body * factor): return -1
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
# 2. GESTIÓN DE POSICIÓN ACTIVA (NUEVO MOTOR)
# ---------------------------------------------------------

def manage_trade_simulation(df, entry_index, entry_price, direction, atr_value):
    """
    Simula la gestión profesional: Early Exit, TP Parcial y Break Even.
    """
    # Configuración de Gestión
    sl_pips = atr_value * 1.5
    tp1_pips = atr_value * 1.0  # Primer TP (Caja rápida)
    tp2_pips = atr_value * 2.0  # Segundo TP (Run)
    
    # Precios iniciales
    stop_loss = entry_price - sl_pips if direction == 'LONG' else entry_price + sl_pips
    tp1 = entry_price + tp1_pips if direction == 'LONG' else entry_price - tp1_pips
    tp2 = entry_price + tp2_pips if direction == 'LONG' else entry_price - tp2_pips
    
    tp1_hit = False
    
    # 1. EARLY EXIT CHECK (Vela siguiente inmediata)
    # Si entramos y la siguiente vela tiene delta contrario fuerte, salimos.
    if entry_index + 1 < len(df):
        next_candle = df.iloc[entry_index + 1]
        
        # Si es LONG y el delta siguiente es negativo -> SALIR
        if direction == 'LONG' and next_candle['delta_norm'] < 0:
            return f"⚠️ EARLY EXIT (Next Delta Negative). Close: {next_candle['close']:.1f}"
            
        # Si es SHORT y el delta siguiente es positivo -> SALIR
        if direction == 'SHORT' and next_candle['delta_norm'] > 0:
            return f"⚠️ EARLY EXIT (Next Delta Positive). Close: {next_candle['close']:.1f}"

    # 2. LOOP DE GESTIÓN (8 Velas = 40 min Time Stop)
    for j in range(1, 9):
        if entry_index + j >= len(df): break
        current_candle = df.iloc[entry_index + j]
        
        # --- Lógica LONG ---
        if direction == 'LONG':
            # Chequear Stop Loss
            if current_candle['low'] <= stop_loss:
                if tp1_hit: return "✅ WIN PARCIAL (TP1 Hit, luego BE)"
                return "❌ LOSS (SL Hit)"
            
            # Chequear TP2 (Full Win)
            if current_candle['high'] >= tp2:
                return "🏆 FULL WIN (TP2 Hit)"
                
            # Chequear TP1
            if not tp1_hit and current_candle['high'] >= tp1:
                tp1_hit = True
                stop_loss = entry_price # MOVER A BREAK EVEN
                # No retornamos, seguimos buscando TP2 pero protegidos
                
        # --- Lógica SHORT ---
        else:
            # Chequear Stop Loss
            if current_candle['high'] >= stop_loss:
                if tp1_hit: return "✅ WIN PARCIAL (TP1 Hit, luego BE)"
                return "❌ LOSS (SL Hit)"
            
            # Chequear TP2 (Full Win)
            if current_candle['low'] <= tp2:
                return "🏆 FULL WIN (TP2 Hit)"
                
            # Chequear TP1
            if not tp1_hit and current_candle['low'] <= tp1:
                tp1_hit = True
                stop_loss = entry_price # MOVER A BREAK EVEN

    # 3. TIME STOP
    if tp1_hit:
        return "✅ WIN PARCIAL (Time Stop post TP1)"
    else:
        return "➖ TIME OUT (Cierre por tiempo)"

def run_lab_test_v3():
    print("--- ORANGE PI LAB: STRATEGY V3 (ACTIVE MANAGEMENT) ---")
    exchange = ccxt.binance()
    symbol = 'BTC/USDT'
    limit = 1000 

    print(f"Descargando datos recientes de {symbol}...")
    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    df['RSI'] = calculate_rsi_manual(df['close'], 14)
    df = calculate_normalized_delta_cvd(df)
    df['ATR'] = calculate_atr(df)
    
    print("--- INICIANDO BACKTEST CON GESTIÓN PROFESIONAL ---")
    
    last_trade_index = -999
    cooldown_bars = 12 
    trades_found = 0
    
    for i in range(300, len(df)):
        if i - last_trade_index < cooldown_bars: continue
        current_row = df.iloc[i]
        
        # Filtro de volatilidad mínima
        if current_row['ATR'] < 50: continue # Si ATR < 50 USDT en BTC, el mercado está muerto.

        past_data = df.iloc[i-288:i]
        zones = get_volume_profile_zones(past_data)
        if not zones: continue
        
        vah, val = zones['VAH'], zones['VAL']
        
        # Condiciones de Entrada (Mismas que V2.2)
        is_long_zone = current_row['low'] <= val and current_row['close'] > val
        is_short_zone = current_row['high'] >= vah and current_row['close'] < vah
        
        entry_signal = None
        
        if is_long_zone:
            cond_rsi = current_row['RSI'] < 45
            cond_delta = current_row['delta_norm'] > 0
            cond_cvd = df['cvd'].iloc[i] > df['cvd'].iloc[i-3]
            if cond_rsi and cond_delta and cond_cvd: entry_signal = 'LONG'

        elif is_short_zone:
            cond_rsi = current_row['RSI'] > 55
            cond_delta = current_row['delta_norm'] < 0
            cond_cvd = df['cvd'].iloc[i] < df['cvd'].iloc[i-3]
            if cond_rsi and cond_delta and cond_cvd: entry_signal = 'SHORT'
            
        if entry_signal:
            result_str = manage_trade_simulation(df, i, current_row['close'], entry_signal, current_row['ATR'])
            
            print(f"[{current_row['timestamp']}] {entry_signal} @ {current_row['close']:.1f}")
            print(f"   Ref: Zone={'VAL' if entry_signal=='LONG' else 'VAH'} | ATR={current_row['ATR']:.1f}")
            print(f"   Resultado: {result_str}")
            print("-" * 50)
            
            last_trade_index = i
            trades_found += 1

    print(f"\nTotal trades simulados: {trades_found}")

if __name__ == "__main__":
    run_lab_test_v3()