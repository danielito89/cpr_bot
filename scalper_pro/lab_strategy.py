import ccxt
import pandas as pd
import numpy as np

# ---------------------------------------------------------
# 1. INDICADORES (Núcleo matemático)
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
    return {'VAH': vah, 'VAL': val}

# ---------------------------------------------------------
# 2. GESTIÓN PROFESIONAL POR R-MULTIPLES
# ---------------------------------------------------------

def manage_trade_r_logic(df, entry_index, entry_price, direction, atr_value, entry_delta):
    """
    Retorna un diccionario con el resultado matemático del trade.
    R = Beneficio / Riesgo Inicial
    """
    risk_per_share = atr_value * 1.5 # Distancia al Stop Loss (1R)
    
    sl_price = entry_price - risk_per_share if direction == 'LONG' else entry_price + risk_per_share
    tp1_price = entry_price + (risk_per_share * 1.0) if direction == 'LONG' else entry_price - (risk_per_share * 1.0) # TP1 = 1R (aprox 1.5 ATR)
    tp2_price = entry_price + (risk_per_share * 2.0) if direction == 'LONG' else entry_price - (risk_per_share * 2.0) # TP2 = 2R
    
    tp1_hit = False
    max_r_reached = 0.0
    
    # 1. EARLY EXIT CHECK (Suavizado)
    if entry_index + 1 < len(df):
        next_candle = df.iloc[entry_index + 1]
        next_delta = next_candle['delta_norm']
        
        # Tolerancia: 10% del delta de entrada
        tolerance = abs(entry_delta) * 0.10
        
        early_exit_triggered = False
        if direction == 'LONG' and next_delta < -tolerance: early_exit_triggered = True
        if direction == 'SHORT' and next_delta > tolerance: early_exit_triggered = True
        
        if early_exit_triggered:
            # Calcular R realizada (será negativa pequeña, ej: -0.1R)
            exit_price = next_candle['close']
            pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
            r_realized = pnl / risk_per_share
            return {
                "outcome": "EARLY_EXIT",
                "r_realized": r_realized,
                "bars": 1,
                "info": f"Delta Reversal > {tolerance:.2f}"
            }

    # 2. GESTIÓN DEL TRADE (8 Barras Time Stop)
    for j in range(1, 9):
        if entry_index + j >= len(df): break
        row = df.iloc[entry_index + j]
        
        # Precios actuales
        curr_low = row['low']
        curr_high = row['high']
        curr_close = row['close']
        
        # --- Lógica LONG ---
        if direction == 'LONG':
            # Stop Loss (Full -1R o BE)
            if curr_low <= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": "Stop Loss tocado"}

            # TP2 (Full Win +2R)
            if curr_high >= tp2_price:
                # Asumimos que salimos con todo o promedio. 
                # Simplificación: Si toca TP2, es un Home Run de 2R.
                return {"outcome": "TP2_HIT", "r_realized": 2.0, "bars": j, "info": "Target 2 alcanzado"}

            # TP1 (Parcial +1R y mover a BE)
            if not tp1_hit and curr_high >= tp1_price:
                tp1_hit = True
                sl_price = entry_price # Stop a Breakeven
                
        # --- Lógica SHORT ---
        else:
            if curr_high >= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": "Stop Loss tocado"}

            if curr_low <= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": 2.0, "bars": j, "info": "Target 2 alcanzado"}

            if not tp1_hit and curr_low <= tp1_price:
                tp1_hit = True
                sl_price = entry_price 

    # 3. TIME STOP
    exit_price = df.iloc[entry_index + 8]['close'] if entry_index + 8 < len(df) else df.iloc[-1]['close']
    pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
    r_realized = pnl / risk_per_share
    
    # Si ya habíamos cobrado TP1, el trade es ganador pase lo que pase. 
    # Simplificación: Promediamos la R (1R asegurada + flotante).
    if tp1_hit: r_realized = max(r_realized, 0.5) 
    
    return {
        "outcome": "TIME_STOP",
        "r_realized": r_realized,
        "bars": 8,
        "info": "Cierre por tiempo"
    }

# ---------------------------------------------------------
# 3. EJECUCIÓN PRINCIPAL
# ---------------------------------------------------------

def run_lab_test_v4():
    print("--- ORANGE PI LAB: STRATEGY V4 (R-MULTIPLES) ---")
    exchange = ccxt.binance()
    symbol = 'BTC/USDT'
    # IMPORTANTE: Aumentamos el límite para tener estadística real
    limit = 1500 # Aprox 5 días
    
    print(f"Descargando {limit} velas de {symbol}...")
    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    df['RSI'] = calculate_rsi_manual(df['close'], 14)
    df = calculate_normalized_delta_cvd(df)
    df['ATR'] = calculate_atr(df)
    
    last_trade_index = -999
    cooldown_bars = 12
    trade_log = []
    
    print("--- INICIANDO ANÁLISIS ---")
    
    for i in range(300, len(df)):
        if i - last_trade_index < cooldown_bars: continue
        row = df.iloc[i]
        
        if row['ATR'] < 50: continue 
        
        # Zonas
        zones = get_volume_profile_zones(df.iloc[i-288:i])
        if not zones: continue
        vah, val = zones['VAH'], zones['VAL']
        
        entry_signal = None
        
        # Lógica V3
        is_long = row['low'] <= val and row['close'] > val
        is_short = row['high'] >= vah and row['close'] < vah
        
        if is_long:
            if row['RSI'] < 45 and row['delta_norm'] > 0 and df['cvd'].iloc[i] > df['cvd'].iloc[i-3]:
                entry_signal = 'LONG'
        elif is_short:
            if row['RSI'] > 55 and row['delta_norm'] < 0 and df['cvd'].iloc[i] < df['cvd'].iloc[i-3]:
                entry_signal = 'SHORT'
                
        if entry_signal:
            res = manage_trade_r_logic(df, i, row['close'], entry_signal, row['ATR'], row['delta_norm'])
            
            # Guardamos Log
            trade_data = {
                "time": row['timestamp'],
                "type": entry_signal,
                "price": row['close'],
                "outcome": res['outcome'],
                "r": res['r_realized'],
                "info": res['info']
            }
            trade_log.append(trade_data)
            
            print(f"[{row['timestamp']}] {entry_signal:<5} | {res['outcome']:<10} | R: {res['r_realized']:.2f} | {res['info']}")
            last_trade_index = i

    # --- ESTADÍSTICAS FINALES ---
    print("\n" + "="*40)
    print("RESULTADOS V4 (EXPECTANCY)")
    print("="*40)
    
    if not trade_log:
        print("No se encontraron trades suficientes.")
    else:
        df_res = pd.DataFrame(trade_log)
        total_r = df_res['r'].sum()
        count = len(df_res)
        win_rate = len(df_res[df_res['r'] > 0]) / count * 100
        expectancy = total_r / count
        
        print(f"Total Trades:    {count}")
        print(f"Total R:         {total_r:.2f} R")
        print(f"Expectancy:      {expectancy:.2f} R por trade")
        print(f"Win Rate (>0R):  {win_rate:.1f}%")
        print("-" * 40)
        print("Detalle de Outcomes:")
        print(df_res['outcome'].value_counts())

if __name__ == "__main__":
    run_lab_test_v4()