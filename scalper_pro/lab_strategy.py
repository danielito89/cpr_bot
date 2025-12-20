import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime

# ---------------------------------------------------------
# 1. UTILIDADES Y DESCARGA MASIVA (50k)
# ---------------------------------------------------------

def fetch_extended_history(symbol='BTC/USDT', timeframe='5m', total_candles=50000):
    exchange = ccxt.binance()
    limit_per_call = 1000
    print(f"📡 {symbol}: Descargando historial masivo ({total_candles} velas)...")
    print("   Esto tomará unos minutos. Paciencia...")
    
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
            
            if len(all_ohlcv) % 10000 == 0:
                print(f"   ... {len(all_ohlcv)} velas cargadas")
            time.sleep(0.15) 
        except Exception as e:
            print(f"Error descarga: {e}")
            break
            
    if len(all_ohlcv) > total_candles:
        all_ohlcv = all_ohlcv[-total_candles:]
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

# ---------------------------------------------------------
# 2. INDICADORES
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
    
    # ATR Threshold (Percentil 0.25 - Mantenemos V5.4)
    df['ATR_Threshold'] = df['ATR'].rolling(window=500).quantile(0.25)
    
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
# 3. GESTIÓN (V5.5 - NO CHANGES HERE)
# ---------------------------------------------------------

def manage_trade_r_logic(df, entry_index, entry_price, direction, atr_value, entry_delta, zone_level):
    risk_per_share = atr_value * 1.5 
    tp1_ratio = 1.0 
    tp2_ratio = 3.0 
    
    sl_price = entry_price - risk_per_share if direction == 'LONG' else entry_price + risk_per_share
    tp1_price = entry_price + (risk_per_share * tp1_ratio) if direction == 'LONG' else entry_price - (risk_per_share * tp1_ratio)
    tp2_price = entry_price + (risk_per_share * tp2_ratio) if direction == 'LONG' else entry_price - (risk_per_share * tp2_ratio)
    
    tp1_hit = False
    
    # EARLY EXIT (25% Tolerance)
    if entry_index + 1 < len(df):
        next_candle = df.iloc[entry_index + 1]
        next_delta = next_candle['delta_norm']
        tolerance = abs(entry_delta) * 0.25
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

    # MANAGEMENT
    for j in range(1, 12): 
        if entry_index + j >= len(df): break
        row = df.iloc[entry_index + j]
        curr_low, curr_high, curr_close = row['low'], row['high'], row['close']
        current_pnl = (curr_close - entry_price) if direction == 'LONG' else (entry_price - curr_close)
        current_r = current_pnl / risk_per_share

        # Stagnant Checks
        if j == 4 and not tp1_hit:
            if current_r < 0.10: return {"outcome": "STAGNANT", "r_realized": -0.05, "bars": j, "info": "Bar 4"}
            if current_r >= 0.60: tp1_hit, sl_price = True, entry_price

        if j == 6 and not tp1_hit:
             if current_r < 0.20: return {"outcome": "STAGNANT", "r_realized": -0.15, "bars": j, "info": "Bar 6"}

        # SL / TP Logic
        if direction == 'LONG':
            if curr_low <= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": "SL Hit"}
            
            # CVD GUARD (Management Filter)
            if not tp1_hit and curr_high >= tp1_price:
                cvd_now = df['cvd'].iloc[entry_index + j]
                cvd_entry = df['cvd'].iloc[entry_index]
                if cvd_now > cvd_entry:
                    tp1_hit = True
                    sl_price = entry_price 
                else:
                    return {"outcome": "TP1_EXIT", "r_realized": 1.0, "bars": j, "info": "CVD Divergence"}

            if curr_high >= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": tp2_ratio, "bars": j, "info": "Target 3R"}

        else: # SHORT
            if curr_high >= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": "SL Hit"}
            
            if not tp1_hit and curr_low <= tp1_price:
                cvd_now = df['cvd'].iloc[entry_index + j]
                cvd_entry = df['cvd'].iloc[entry_index]
                if cvd_now < cvd_entry:
                    tp1_hit = True
                    sl_price = entry_price
                else:
                    return {"outcome": "TP1_EXIT", "r_realized": 1.0, "bars": j, "info": "CVD Divergence"}

            if curr_low <= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": tp2_ratio, "bars": j, "info": "Target 3R"}

    exit_price = df.iloc[entry_index + 11]['close'] if entry_index + 11 < len(df) else df.iloc[-1]['close']
    pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
    r_realized = pnl / risk_per_share
    if tp1_hit: r_realized = max(r_realized, 0.5) 
    return {"outcome": "TIME_STOP", "r_realized": r_realized, "bars": 11, "info": "Time Out"}

# ---------------------------------------------------------
# 4. EJECUCIÓN (V5.5 - ASYMMETRIC ENTRY)
# ---------------------------------------------------------

def is_core_session(timestamp):
    hour = timestamp.hour
    return 14 <= hour <= 16

def run_v5_5_massive_test():
    print("--- ORANGE PI LAB: V5.5 (50K CANDLES + RELAXED ENTRY) ---")
    # Descarga MASIVA 
    df = fetch_extended_history('BTC/USDT', '5m', total_candles=50000)
    print("Calculando indicadores...")
    df = calculate_indicators(df)
    
    last_trade_index = -999
    current_cooldown = 12 
    trade_log = []
    
    print(f"\n--- INICIANDO BACKTEST (6 MESES APROX) ---")
    
    for i in range(500, len(df)):
        if i - last_trade_index < current_cooldown: continue
        row = df.iloc[i]
        
        # Filtros Estructurales (SE MANTIENEN)
        if not is_core_session(row['timestamp']): continue
        if row['ATR'] < row['ATR_Threshold']: continue 
        
        prev_row = df.iloc[i-1]
        if (prev_row['high'] - prev_row['low']) > (row['ATR'] * 1.2): continue 
            
        zones = get_volume_profile_zones(df.iloc[i-288:i])
        if not zones: continue
        vah, val = zones['VAH'], zones['VAL']
        
        entry_signal = None
        is_long = row['low'] <= val and row['close'] > val
        is_short = row['high'] >= vah and row['close'] < vah
        
        # GATEKEEPER (0.5 ATR)
        penetration_threshold = 0.50 
        
        # --- CAMBIO CLAVE: ENTRADA RELAJADA (SIN CVD) ---
        if is_long:
            if prev_row['close'] < val: continue # Falling Knife
            if (val - row['low']) > (row['ATR'] * penetration_threshold): continue
            
            # RSI < 48 (antes 45) | Delta > 0 | CVD ELIMINADO
            if row['RSI'] < 48 and row['delta_norm'] > 0:
                entry_signal = 'LONG'
                
        elif is_short:
            if prev_row['close'] > vah: continue # Rising Knife
            if (row['high'] - vah) > (row['ATR'] * penetration_threshold): continue
            
            # RSI > 52 (antes 55) | Delta < 0 | CVD ELIMINADO
            if row['RSI'] > 52 and row['delta_norm'] < 0:
                entry_signal = 'SHORT'
                
        if entry_signal:
            zone_level = val if entry_signal == 'LONG' else vah
            res = manage_trade_r_logic(df, i, row['close'], entry_signal, row['ATR'], row['delta_norm'], zone_level)
            
            # Smart Fees
            if res['outcome'] in ['EARLY_EXIT', 'STAGNANT']:
                fee = 0.015
            else:
                fee = 0.045
            final_r = res['r_realized'] - fee
            
            trade_data = {
                "time": row['timestamp'],
                "type": entry_signal,
                "outcome": res['outcome'],
                "r_net": final_r,
            }
            trade_log.append(trade_data)
            
            last_trade_index = i
            if res['outcome'] in ['EARLY_EXIT', 'STAGNANT']:
                current_cooldown = 2
            else:
                current_cooldown = 12

    # --- REPORTE ---
    if not trade_log:
        print("\nNo se encontraron trades.")
        return

    df_res = pd.DataFrame(trade_log)
    df_scratches = df_res[df_res['outcome'].isin(['EARLY_EXIT', 'STAGNANT'])]
    df_exec = df_res[~df_res['outcome'].isin(['EARLY_EXIT', 'STAGNANT'])]
    
    total_r_net = df_res['r_net'].sum()
    
    print("\n" + "="*50)
    print("V5.5 - ASYMMETRIC ENTRY (50K CANDLES)")
    print("="*50)
    print(f"Periodo:        {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}")
    print(f"Total Trades:   {len(df_res)}")
    print(f"TOTAL R NETO:   {total_r_net:.2f} R")
    print("-" * 50)
    print(f"SCRATCHES:      {len(df_scratches)}")
    print(f"EJECUTADOS:     {len(df_exec)}")
    
    if not df_exec.empty:
        exec_profit = df_exec['r_net'].sum()
        print(f"PROFIT EJEC:    {exec_profit:.2f} R")
        exp_total = total_r_net / len(df_res)
        print(f"EXPECTANCY TOT: {exp_total:.3f} R / trade")

    print("\nDistribución:")
    print(df_res['outcome'].value_counts())

if __name__ == "__main__":
    run_v5_5_massive_test()