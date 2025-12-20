import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime

# ---------------------------------------------------------
# 1. UTILIDADES Y DESCARGA MASIVA
# ---------------------------------------------------------

def fetch_extended_history(symbol='BTC/USDT', timeframe='5m', total_candles=15000):
    exchange = ccxt.binance()
    limit_per_call = 1000
    print(f"📡 Descargando historial masivo ({total_candles} velas)...")
    
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit_per_call)
    all_ohlcv = ohlcv
    timeframe_duration_seconds = 5 * 60 
    
    # Barra de progreso simple
    while len(all_ohlcv) < total_candles:
        oldest_timestamp = all_ohlcv[0][0]
        since_timestamp = oldest_timestamp - (limit_per_call * timeframe_duration_seconds * 1000)
        try:
            new_batch = exchange.fetch_ohlcv(symbol, timeframe, limit=limit_per_call, since=since_timestamp)
            new_batch = [x for x in new_batch if x[0] < oldest_timestamp]
            if not new_batch: break
            all_ohlcv = new_batch + all_ohlcv
            
            if len(all_ohlcv) % 2000 == 0:
                print(f"   ... {len(all_ohlcv)} velas cargadas")
            time.sleep(0.15) # Rate limit friendly
        except Exception as e:
            print(f"Error: {e}")
            break
            
    if len(all_ohlcv) > total_candles:
        all_ohlcv = all_ohlcv[-total_candles:]
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

# ---------------------------------------------------------
# 2. INDICADORES
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
# 3. GESTIÓN (V4.6 - THE ACCELERATOR)
# ---------------------------------------------------------

def manage_trade_r_logic(df, entry_index, entry_price, direction, atr_value, entry_delta, zone_level):
    risk_per_share = atr_value * 1.5 
    tp1_ratio = 0.8
    sl_price = entry_price - risk_per_share if direction == 'LONG' else entry_price + risk_per_share
    tp1_price = entry_price + (risk_per_share * tp1_ratio) if direction == 'LONG' else entry_price - (risk_per_share * tp1_ratio)
    tp2_price = entry_price + (risk_per_share * 2.0) if direction == 'LONG' else entry_price - (risk_per_share * 2.0)
    
    tp1_hit = False
    late_tp1_triggered = False # Flag para diferenciar en el log
    
    # 1. HYBRID EARLY EXIT (V4.4)
    if entry_index + 1 < len(df):
        next_candle = df.iloc[entry_index + 1]
        next_delta = next_candle['delta_norm']
        tolerance = abs(entry_delta) * 0.10
        potential_early_exit = False
        
        if direction == 'LONG':
            if next_delta < -tolerance and next_candle['close'] < entry_price: potential_early_exit = True
        elif direction == 'SHORT':
            if next_delta > tolerance and next_candle['close'] > entry_price: potential_early_exit = True
        
        if potential_early_exit:
            exit_price = next_candle['close']
            pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
            r_realized = pnl / risk_per_share
            structural_break = False
            if direction == 'LONG':
                if next_candle['low'] < (zone_level - atr_value * 0.1): structural_break = True
            elif direction == 'SHORT':
                if next_candle['high'] > (zone_level + atr_value * 0.1): structural_break = True
            
            if r_realized >= -0.35 or structural_break:
                reason = "Safe Scratch" if r_realized >= -0.35 else "Structural Break"
                return {"outcome": "EARLY_EXIT", "r_realized": r_realized, "bars": 1, "info": reason}

    # 2. GESTIÓN NORMAL + V4.6 ACCELERATOR
    for j in range(1, 9):
        if entry_index + j >= len(df): break
        row = df.iloc[entry_index + j]
        curr_low, curr_high, curr_close = row['low'], row['high'], row['close']
        
        # --- V4.6: MID-GAME ACCELERATOR (Bar 4) ---
        if j == 4 and not tp1_hit:
            # Calcular R flotante al cierre de la vela 4
            current_pnl = (curr_close - entry_price) if direction == 'LONG' else (entry_price - curr_close)
            current_r = current_pnl / risk_per_share
            
            if current_r >= 0.5:
                tp1_hit = True
                sl_price = entry_price # Move to BE
                late_tp1_triggered = True # Marcamos que fue por aceleración
                # No retornamos, seguimos buscando TP2 pero protegidos
        
        # --- Lógica Estándar ---
        if direction == 'LONG':
            if curr_low <= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                # Si fue por Late TP1, es un BE positivo técnicamente (o small win)
                info = "Late TP1 BE" if late_tp1_triggered else "SL Hit"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": info}
            
            if curr_high >= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": 2.0, "bars": j, "info": "Target 2"}
            
            if not tp1_hit and curr_high >= tp1_price:
                tp1_hit = True
                sl_price = entry_price 
                
        else: # SHORT
            if curr_high >= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                info = "Late TP1 BE" if late_tp1_triggered else "SL Hit"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": info}
            
            if curr_low <= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": 2.0, "bars": j, "info": "Target 2"}
            
            if not tp1_hit and curr_low <= tp1_price:
                tp1_hit = True
                sl_price = entry_price 

    # 3. TIME STOP
    exit_price = df.iloc[entry_index + 8]['close'] if entry_index + 8 < len(df) else df.iloc[-1]['close']
    pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
    r_realized = pnl / risk_per_share
    
    # Si cobramos TP1 (Normal o Late), aseguramos ganancia
    if tp1_hit: 
        min_win = 0.5 if late_tp1_triggered else 0.4 
        r_realized = max(r_realized, min_win)
    
    info_msg = "Time Out (Late TP1)" if late_tp1_triggered else "Time Out"
    return {"outcome": "TIME_STOP", "r_realized": r_realized, "bars": 8, "info": info_msg}

# ---------------------------------------------------------
# 4. EJECUCIÓN (V4.6 - FINAL BACKTEST)
# ---------------------------------------------------------

def is_ny_session(timestamp):
    hour = timestamp.hour
    return 13 <= hour <= 17

def run_lab_test_v4_6():
    print("--- ORANGE PI LAB: STRATEGY V4.6 (THE ACCELERATOR) ---")
    # Descarga MASIVA para validación final
    df = fetch_extended_history('BTC/USDT', '5m', total_candles=15000)
    
    print("Calculando indicadores...")
    df['RSI'] = calculate_rsi_manual(df['close'], 14)
    df = calculate_normalized_delta_cvd(df)
    df['ATR'] = calculate_atr(df)
    
    last_trade_index = -999
    standard_cooldown = 12 
    smart_cooldown = 2
    current_cooldown = standard_cooldown
    
    trade_log = []
    
    print(f"\n--- BACKTEST EXTENDIDO ({len(df)} Velas) ---")
    
    for i in range(300, len(df)):
        if i - last_trade_index < current_cooldown: continue
        row = df.iloc[i]
        if not is_ny_session(row['timestamp']): continue
        if row['ATR'] < 50: continue 
        
        zones = get_volume_profile_zones(df.iloc[i-288:i])
        if not zones: continue
        vah, val = zones['VAH'], zones['VAL']
        
        entry_signal = None
        is_long = row['low'] <= val and row['close'] > val
        is_short = row['high'] >= vah and row['close'] < vah
        
        # GATEKEEPER (V4.5)
        penetration_threshold = 0.30 
        if is_long:
            if (val - row['low']) > (row['ATR'] * penetration_threshold): continue
            if row['RSI'] < 45 and row['delta_norm'] > 0 and df['cvd'].iloc[i] > df['cvd'].iloc[i-3]:
                entry_signal = 'LONG'
        elif is_short:
            if (row['high'] - vah) > (row['ATR'] * penetration_threshold): continue
            if row['RSI'] > 55 and row['delta_norm'] < 0 and df['cvd'].iloc[i] < df['cvd'].iloc[i-3]:
                entry_signal = 'SHORT'
                
        if entry_signal:
            zone_level = val if entry_signal == 'LONG' else vah
            res = manage_trade_r_logic(df, i, row['close'], entry_signal, row['ATR'], row['delta_norm'], zone_level)
            
            trade_data = {
                "time": row['timestamp'],
                "type": entry_signal,
                "price": row['close'],
                "outcome": res['outcome'],
                "r": res['r_realized'],
                "info": res['info']
            }
            trade_log.append(trade_data)
            
            # Print simplificado para no saturar consola con 15k velas
            # print(f"[{row['timestamp']}] {entry_signal:<5} | {res['outcome']:<10} | R: {res['r_realized']:.2f}")
            
            last_trade_index = i
            if res['outcome'] == 'EARLY_EXIT':
                current_cooldown = smart_cooldown
            else:
                current_cooldown = standard_cooldown

    # --- REPORTE MASIVO ---
    if not trade_log:
        print("\nNo se encontraron trades.")
        return

    df_res = pd.DataFrame(trade_log)
    df_valid = df_res[df_res['outcome'] != 'EARLY_EXIT']
    
    print("\n" + "="*50)
    print("ESTADÍSTICAS FINALES (EXTENDED BACKTEST)")
    print("="*50)
    print(f"Data Range: {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}")
    print(f"Total Trades: {len(df_res)}")
    
    scratches = len(df_res[df_res['outcome'] == 'EARLY_EXIT'])
    print(f"Scratches: {scratches}")
    
    if not df_valid.empty:
        total_r = df_res['r'].sum()
        expectancy = total_r / len(df_res)
        win_rate = len(df_valid[df_valid['r'] > 0]) / len(df_valid) * 100
        
        print(f"TRADES VÁLIDOS:       {len(df_valid)}")
        print(f"WIN RATE (Válidos):   {win_rate:.1f}%")
        print(f"TOTAL R NETO:         {total_r:.2f} R")
        print(f"EXPECTANCY:           {expectancy:.3f} R / trade")
        
        # Equity Curve simple
        print("\nEvolución de R Acumulado (Últimos 10 trades):")
        print(df_res['r'].tail(10).cumsum())
    
    print("-" * 50)
    print("Distribución de Outcomes:")
    print(df_res['outcome'].value_counts())

if __name__ == "__main__":
    run_lab_test_v4_6()