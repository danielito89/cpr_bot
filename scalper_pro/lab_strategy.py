import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime

# ---------------------------------------------------------
# 1. UTILIDADES Y DESCARGA
# ---------------------------------------------------------

def fetch_extended_history(symbol='BTC/USDT', timeframe='5m', total_candles=15000):
    exchange = ccxt.binance()
    limit_per_call = 1000
    print(f"📡 Descargando historial masivo ({total_candles} velas)...")
    
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit_per_call)
    all_ohlcv = ohlcv
    timeframe_duration_seconds = 5 * 60 
    
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
            time.sleep(0.15) 
        except Exception as e:
            break
    if len(all_ohlcv) > total_candles:
        all_ohlcv = all_ohlcv[-total_candles:]
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

# ---------------------------------------------------------
# 2. INDICADORES + ATR DINÁMICO
# ---------------------------------------------------------

def calculate_indicators(df):
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))

    range_candle = (df['high'] - df['low']).replace(0, 0.000001)
    df['delta_norm'] = ((df['close'] - df['open']) / range_candle) * df['volume']
    df['cvd'] = df['delta_norm'].cumsum()

    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=14).mean()
    
    # --- V5.0: ATR DINÁMICO (PERCENTIL) ---
    # Calculamos el umbral dinámico (Percentil 40 de las últimas 500 velas)
    df['ATR_Threshold'] = df['ATR'].rolling(window=500).quantile(0.4)
    
    return df

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
# 3. GESTIÓN (V5.0 - THE ROBUST ONE)
# ---------------------------------------------------------

def manage_trade_r_logic(df, entry_index, entry_price, direction, atr_value, entry_delta, zone_level):
    risk_per_share = atr_value * 1.5 
    tp1_ratio = 0.8
    sl_price = entry_price - risk_per_share if direction == 'LONG' else entry_price + risk_per_share
    tp1_price = entry_price + (risk_per_share * tp1_ratio) if direction == 'LONG' else entry_price - (risk_per_share * tp1_ratio)
    tp2_price = entry_price + (risk_per_share * 2.0) if direction == 'LONG' else entry_price - (risk_per_share * 2.0)
    
    tp1_hit = False
    late_tp1_triggered = False 
    
    # 1. EARLY EXIT (V4.4 Logic)
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

    # 2. MANAGEMENT
    for j in range(1, 9):
        if entry_index + j >= len(df): break
        row = df.iloc[entry_index + j]
        curr_low, curr_high, curr_close = row['low'], row['high'], row['close']
        
        current_pnl = (curr_close - entry_price) if direction == 'LONG' else (entry_price - curr_close)
        current_r = current_pnl / risk_per_share

        # --- AJUSTE C: STAGNANT ESCALONADO ---
        
        # BAR 4: Salida barata (-0.05R)
        if j == 4 and not tp1_hit:
            if current_r < 0.10:
                return {"outcome": "STAGNANT", "r_realized": -0.05, "bars": j, "info": "Stagnant Bar 4"}
            # Accelerator
            if current_r >= 0.50:
                tp1_hit = True
                sl_price = entry_price 
                late_tp1_triggered = True 
        
        # BAR 6: Salida media (-0.15R) - Si sobrevivió la 4 pero sigue sin arrancar
        if j == 6 and not tp1_hit:
             if current_r < 0.20: # Exigimos un poco más de avance en barra 6
                return {"outcome": "STAGNANT", "r_realized": -0.15, "bars": j, "info": "Stagnant Bar 6"}

        # SL / TP Logic
        if direction == 'LONG':
            if curr_low <= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                info = "Late TP1 BE" if late_tp1_triggered else "SL Hit"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": info}
            if curr_high >= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": 2.0, "bars": j, "info": "Target 2"}
            if not tp1_hit and curr_high >= tp1_price:
                tp1_hit = True
                sl_price = entry_price 
        else:
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

    exit_price = df.iloc[entry_index + 8]['close'] if entry_index + 8 < len(df) else df.iloc[-1]['close']
    pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
    r_realized = pnl / risk_per_share
    if tp1_hit: r_realized = max(r_realized, 0.4) 
    info_msg = "Time Out (Late TP1)" if late_tp1_triggered else "Time Out"
    return {"outcome": "TIME_STOP", "r_realized": r_realized, "bars": 8, "info": info_msg}

# ---------------------------------------------------------
# 4. EJECUCIÓN (V5.0 - FINAL)
# ---------------------------------------------------------

# AJUSTE A: SESIÓN ESTRICTA (CALIDAD)
def is_core_session(timestamp):
    hour = timestamp.hour
    return 14 <= hour <= 16 # 14:00 - 16:59 (3 horas de oro)

def run_lab_test_v5_0():
    print("--- ORANGE PI LAB: STRATEGY V5.0 (THE ROBUST ONE) ---")
    df = fetch_extended_history('BTC/USDT', '5m', total_candles=15000)
    print("Calculando indicadores (incl. ATR Percentil)...")
    df = calculate_indicators(df)
    
    last_trade_index = -999
    standard_cooldown = 12 
    smart_cooldown = 2
    current_cooldown = standard_cooldown
    trade_log = []
    
    print(f"\n--- BACKTEST FINAL: FEES SIMULADOS INCLUIDOS ---")
    
    for i in range(500, len(df)): # Empezamos en 500 para tener data del ATR Percentil
        if i - last_trade_index < current_cooldown: continue
        row = df.iloc[i]
        
        # Filtro de Sesión Estricto
        if not is_core_session(row['timestamp']): continue
        
        # AJUSTE B: ATR DINÁMICO
        # Si el ATR actual es menor que el percentil 40 histórico -> Mercado Muerto
        if row['ATR'] < row['ATR_Threshold']: 
            continue 
        
        # Filtro Anti-Expansión (V4.8)
        prev_row = df.iloc[i-1]
        prev_range = prev_row['high'] - prev_row['low']
        if prev_range > (row['ATR'] * 1.2): continue 
            
        zones = get_volume_profile_zones(df.iloc[i-288:i])
        if not zones: continue
        vah, val = zones['VAH'], zones['VAL']
        
        entry_signal = None
        is_long = row['low'] <= val and row['close'] > val
        is_short = row['high'] >= vah and row['close'] < vah
        
        # GATEKEEPER + FALLING KNIFE
        penetration_threshold = 0.30 
        
        if is_long:
            if prev_row['close'] < val: continue 
            if (val - row['low']) > (row['ATR'] * penetration_threshold): continue
            if row['RSI'] < 45 and row['delta_norm'] > 0 and df['cvd'].iloc[i] > df['cvd'].iloc[i-3]:
                entry_signal = 'LONG'
                
        elif is_short:
            if prev_row['close'] > vah: continue
            if (row['high'] - vah) > (row['ATR'] * penetration_threshold): continue
            if row['RSI'] > 55 and row['delta_norm'] < 0 and df['cvd'].iloc[i] < df['cvd'].iloc[i-3]:
                entry_signal = 'SHORT'
                
        if entry_signal:
            zone_level = val if entry_signal == 'LONG' else vah
            res = manage_trade_r_logic(df, i, row['close'], entry_signal, row['ATR'], row['delta_norm'], zone_level)
            
            # --- SIMULACIÓN DE FEES + SLIPPAGE ---
            # Restamos 0.03 R a cada trade (aprox 0.04% fee + slippage sobre riesgo)
            fee_cost_r = 0.03
            final_r = res['r_realized'] - fee_cost_r
            
            trade_data = {
                "time": row['timestamp'],
                "type": entry_signal,
                "price": row['close'],
                "outcome": res['outcome'],
                "r_gross": res['r_realized'],
                "r_net": final_r, # Usamos el Neto para las stats
                "info": res['info']
            }
            trade_log.append(trade_data)
            
            last_trade_index = i
            if res['outcome'] in ['EARLY_EXIT', 'STAGNANT']:
                current_cooldown = smart_cooldown
            else:
                current_cooldown = standard_cooldown

    # --- REPORTE ---
    if not trade_log:
        print("\nNo se encontraron trades.")
        return

    df_res = pd.DataFrame(trade_log)
    df_valid = df_res[~df_res['outcome'].isin(['EARLY_EXIT', 'STAGNANT'])] 
    
    print("\n" + "="*50)
    print("ESTADÍSTICAS FINALES (V5.0 - ROBUST)")
    print("="*50)
    print(f"Total Trades: {len(df_res)}")
    
    scratches = len(df_res[df_res['outcome'].isin(['EARLY_EXIT', 'STAGNANT'])])
    print(f"Scratches/Stagnant: {scratches}")
    
    if not df_res.empty:
        # USAMOS R NETO (DESPUÉS DE FEES)
        total_r_net = df_res['r_net'].sum()
        expectancy_net = total_r_net / len(df_res)
        
        if not df_valid.empty:
            win_rate = len(df_valid[df_valid['r_gross'] > 0]) / len(df_valid) * 100
        else:
            win_rate = 0.0
        
        print(f"TRADES COMPLETOS:     {len(df_valid)}")
        print(f"WIN RATE (Completos): {win_rate:.1f}%")
        print(f"TOTAL R NETO (w/Fees):{total_r_net:.2f} R")
        print(f"EXPECTANCY NETO:      {expectancy_net:.3f} R / trade")
        
        print("\nEvolución de R Acumulado (Neto):")
        print(df_res['r_net'].cumsum().tail(10))
    
    print("-" * 50)
    print("Distribución de Outcomes:")
    print(df_res['outcome'].value_counts())

if __name__ == "__main__":
    run_lab_test_v5_0()