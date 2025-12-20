import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime

# ---------------------------------------------------------
# 1. UTILIDADES Y DESCARGA EXTENDIDA
# ---------------------------------------------------------

def fetch_extended_history(symbol='BTC/USDT', timeframe='5m', total_candles=4000):
    """
    Descarga más datos de los que permite una sola llamada (limit 1000).
    Hace paginación hacia atrás.
    """
    exchange = ccxt.binance()
    all_ohlcv = []
    current_since = None
    
    # Calculamos ms aprox por vela
    timeframe_duration_seconds = 5 * 60 
    limit_per_call = 1000
    
    print(f"📡 Descargando historial extendido ({total_candles} velas)...")
    
    # 1. Primera llamada (más reciente)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit_per_call)
    all_ohlcv = ohlcv
    
    # 2. Llamadas siguientes hacia atrás
    while len(all_ohlcv) < total_candles:
        # Tomamos el timestamp de la vela más antigua que tenemos
        oldest_timestamp = all_ohlcv[0][0]
        # Restamos el tiempo necesario para traer el bloque anterior
        since_timestamp = oldest_timestamp - (limit_per_call * timeframe_duration_seconds * 1000)
        
        try:
            new_batch = exchange.fetch_ohlcv(symbol, timeframe, limit=limit_per_call, since=since_timestamp)
            # Filtramos para no duplicar (tomamos solo los menores al oldest)
            new_batch = [x for x in new_batch if x[0] < oldest_timestamp]
            
            if not new_batch: break # No hay más datos
            
            all_ohlcv = new_batch + all_ohlcv
            print(f"   ... Cargadas {len(all_ohlcv)} velas")
            time.sleep(0.5) # Respetar API rate limit
        except Exception as e:
            print(f"Error fetching: {e}")
            break
            
    # Recortar al límite solicitado
    if len(all_ohlcv) > total_candles:
        all_ohlcv = all_ohlcv[-total_candles:]
        
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

# ---------------------------------------------------------
# 2. INDICADORES (Mismos que V4)
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
# 3. GESTIÓN DE TRADE (Igual que V4)
# ---------------------------------------------------------

def manage_trade_r_logic(df, entry_index, entry_price, direction, atr_value, entry_delta):
    risk_per_share = atr_value * 1.5 
    sl_price = entry_price - risk_per_share if direction == 'LONG' else entry_price + risk_per_share
    tp1_price = entry_price + (risk_per_share * 1.0) if direction == 'LONG' else entry_price - (risk_per_share * 1.0)
    tp2_price = entry_price + (risk_per_share * 2.0) if direction == 'LONG' else entry_price - (risk_per_share * 2.0)
    
    tp1_hit = False
    
    # 1. EARLY EXIT CHECK
    if entry_index + 1 < len(df):
        next_candle = df.iloc[entry_index + 1]
        next_delta = next_candle['delta_norm']
        tolerance = abs(entry_delta) * 0.10
        
        early_exit_triggered = False
        if direction == 'LONG' and next_delta < -tolerance: early_exit_triggered = True
        if direction == 'SHORT' and next_delta > tolerance: early_exit_triggered = True
        
        if early_exit_triggered:
            exit_price = next_candle['close']
            pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
            r_realized = pnl / risk_per_share
            return {"outcome": "EARLY_EXIT", "r_realized": r_realized, "bars": 1, "info": "Delta Reversal"}

    # 2. GESTIÓN NORMAL
    for j in range(1, 9):
        if entry_index + j >= len(df): break
        row = df.iloc[entry_index + j]
        curr_low, curr_high = row['low'], row['high']
        
        if direction == 'LONG':
            if curr_low <= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": "SL Hit"}
            if curr_high >= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": 2.0, "bars": j, "info": "Target 2"}
            if not tp1_hit and curr_high >= tp1_price:
                tp1_hit = True
                sl_price = entry_price 
                
        else: # SHORT
            if curr_high >= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": "SL Hit"}
            if curr_low <= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": 2.0, "bars": j, "info": "Target 2"}
            if not tp1_hit and curr_low <= tp1_price:
                tp1_hit = True
                sl_price = entry_price 

    # 3. TIME STOP
    exit_price = df.iloc[entry_index + 8]['close'] if entry_index + 8 < len(df) else df.iloc[-1]['close']
    pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
    r_realized = pnl / risk_per_share
    if tp1_hit: r_realized = max(r_realized, 0.5) 
    
    return {"outcome": "TIME_STOP", "r_realized": r_realized, "bars": 8, "info": "Time Out"}

# ---------------------------------------------------------
# 4. EJECUCIÓN CON FILTROS DE SESIÓN Y COOLDOWN INTELIGENTE
# ---------------------------------------------------------

def is_ny_session(timestamp):
    """
    Retorna True si la hora es NY Open AM (aprox 13:00 - 17:00 UTC).
    Ajusta según tu necesidad. Binance suele entregar UTC.
    """
    hour = timestamp.hour
    # Ventana de alta volatilidad: 13:00 a 17:00 UTC
    return 13 <= hour <= 17

def run_lab_test_v4_1():
    print("--- ORANGE PI LAB: STRATEGY V4.1 (SESSION + SMART COOLDOWN) ---")
    
    # 1. Descarga Extendida
    df = fetch_extended_history('BTC/USDT', '5m', total_candles=4000)
    
    print("Calculando indicadores...")
    df['RSI'] = calculate_rsi_manual(df['close'], 14)
    df = calculate_normalized_delta_cvd(df)
    df['ATR'] = calculate_atr(df)
    
    last_trade_index = -999
    standard_cooldown = 12 
    smart_cooldown = 2    # Si fue Early Exit, permitimos entrar rápido
    current_cooldown = standard_cooldown
    
    trade_log = []
    
    print(f"\n--- ANALIZANDO ÚLTIMOS {len(df)} PERIODOS (SOLO NY SESSION) ---")
    
    for i in range(300, len(df)):
        # Cooldown dinámico
        if i - last_trade_index < current_cooldown: continue
        
        row = df.iloc[i]
        
        # FILTRO DE SESIÓN (CRÍTICO)
        if not is_ny_session(row['timestamp']): continue
        
        if row['ATR'] < 50: continue 
        
        past_data = df.iloc[i-288:i]
        zones = get_volume_profile_zones(past_data)
        if not zones: continue
        vah, val = zones['VAH'], zones['VAL']
        
        entry_signal = None
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
            
            trade_data = {
                "time": row['timestamp'],
                "type": entry_signal,
                "price": row['close'],
                "outcome": res['outcome'],
                "r": res['r_realized'],
                "bars": res['bars']
            }
            trade_log.append(trade_data)
            
            print(f"[{row['timestamp']}] {entry_signal:<5} | {res['outcome']:<10} | R: {res['r_realized']:.2f}")
            
            # SMART COOLDOWN LOGIC
            last_trade_index = i
            if res['outcome'] == 'EARLY_EXIT':
                current_cooldown = smart_cooldown # Reset rápido
            else:
                current_cooldown = standard_cooldown

    # --- REPORTE PROFESIONAL ---
    if not trade_log:
        print("\nNo se encontraron trades en horario NY.")
        return

    df_res = pd.DataFrame(trade_log)
    
    # Separar Scratches de Real Trades
    df_valid = df_res[df_res['outcome'] != 'EARLY_EXIT']
    df_scratch = df_res[df_res['outcome'] == 'EARLY_EXIT']
    
    print("\n" + "="*50)
    print("ESTADÍSTICAS FINALES (V4.1)")
    print("="*50)
    print(f"Total Oportunidades: {len(df_res)}")
    print(f"Scratches (Early Exit): {len(df_scratch)} (Costo prom: {df_scratch['r'].mean():.2f} R)")
    print("-" * 50)
    
    if len(df_valid) > 0:
        total_r = df_valid['r'].sum() + df_scratch['r'].sum() # PnL total incluye costos de scratches
        win_rate = len(df_valid[df_valid['r'] > 0]) / len(df_valid) * 100
        avg_bars = df_valid['bars'].mean()
        
        print(f"TRADES VÁLIDOS:       {len(df_valid)}")
        print(f"WIN RATE (Válidos):   {win_rate:.1f}%")
        print(f"DURACIÓN PROM:        {avg_bars:.1f} velas")
        print(f"TOTAL R NETO:         {total_r:.2f} R")
        print(f"EXPECTANCY TOTAL:     {total_r / len(df_res):.2f} R / intento")
    else:
        print("No hubo trades válidos (solo scratches).")
        
    print("\nDistribución de Resultados:")
    print(df_res['outcome'].value_counts())

if __name__ == "__main__":
    run_lab_test_v4_1()