import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime

# ---------------------------------------------------------
# 1. UTILIDADES Y DESCARGA
# ---------------------------------------------------------

def fetch_history_for_pair(symbol, timeframe='5m', total_candles=15000):
    exchange = ccxt.binance()
    limit_per_call = 1000
    print(f"📡 {symbol}: Descargando historial ({total_candles} velas)...")
    
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
            time.sleep(0.15) 
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

def calculate_indicators(df):
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))

    # Delta & CVD
    range_candle = (df['high'] - df['low']).replace(0, 0.000001)
    df['delta_norm'] = ((df['close'] - df['open']) / range_candle) * df['volume']
    df['cvd'] = df['delta_norm'].cumsum()

    # ATR & Threshold
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=14).mean()
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
# 3. GESTIÓN V5.2 (CVD GUARD + FEES DUROS)
# ---------------------------------------------------------

def manage_trade_r_logic(df, entry_index, entry_price, direction, atr_value, entry_delta, zone_level):
    risk_per_share = atr_value * 1.5 
    
    tp1_ratio = 1.0 
    tp2_ratio = 3.0 
    
    sl_price = entry_price - risk_per_share if direction == 'LONG' else entry_price + risk_per_share
    tp1_price = entry_price + (risk_per_share * tp1_ratio) if direction == 'LONG' else entry_price - (risk_per_share * tp1_ratio)
    tp2_price = entry_price + (risk_per_share * tp2_ratio) if direction == 'LONG' else entry_price - (risk_per_share * tp2_ratio)
    
    tp1_hit = False
    late_tp1_triggered = False 
    
    # 1. EARLY EXIT
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
    for j in range(1, 12): 
        if entry_index + j >= len(df): break
        row = df.iloc[entry_index + j]
        curr_low, curr_high, curr_close = row['low'], row['high'], row['close']
        
        current_pnl = (curr_close - entry_price) if direction == 'LONG' else (entry_price - curr_close)
        current_r = current_pnl / risk_per_share

        # Stagnant Checks
        if j == 4 and not tp1_hit:
            if current_r < 0.10: return {"outcome": "STAGNANT", "r_realized": -0.05, "bars": j, "info": "Bar 4"}
            if current_r >= 0.60: tp1_hit, sl_price, late_tp1_triggered = True, entry_price, True

        if j == 6 and not tp1_hit:
             if current_r < 0.20: return {"outcome": "STAGNANT", "r_realized": -0.15, "bars": j, "info": "Bar 6"}

        # SL / TP Logic
        if direction == 'LONG':
            if curr_low <= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                info = "Late TP1 BE" if late_tp1_triggered else "SL Hit"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": info}
            
            # --- V5.2 CVD GUARD ---
            # Si tocamos TP1 pero no TP2 aún, chequear CVD para decidir si nos quedamos o nos vamos.
            if not tp1_hit and curr_high >= tp1_price:
                # Chequeo de CVD: ¿Está el CVD por encima del nivel de entrada? (Momentum a favor)
                cvd_now = df['cvd'].iloc[entry_index + j]
                cvd_entry = df['cvd'].iloc[entry_index]
                
                cvd_strong = cvd_now > cvd_entry if direction == 'LONG' else cvd_now < cvd_entry
                
                if cvd_strong:
                    tp1_hit = True
                    sl_price = entry_price # Seguimos buscando TP2
                else:
                    # CVD Débil: Cobramos 1R y nos vamos.
                    return {"outcome": "TP1_EXIT", "r_realized": 1.0, "bars": j, "info": "CVD Divergence Exit"}

            if curr_high >= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": tp2_ratio, "bars": j, "info": "Target 3R"}

        else: # SHORT
            if curr_high >= sl_price:
                r_result = 0.0 if tp1_hit else -1.0
                outcome = "BE_STOP" if tp1_hit else "SL_HIT"
                info = "Late TP1 BE" if late_tp1_triggered else "SL Hit"
                return {"outcome": outcome, "r_realized": r_result, "bars": j, "info": info}
            
            # --- V5.2 CVD GUARD ---
            if not tp1_hit and curr_low <= tp1_price:
                cvd_now = df['cvd'].iloc[entry_index + j]
                cvd_entry = df['cvd'].iloc[entry_index]
                cvd_strong = cvd_now < cvd_entry # Para short queremos CVD bajando
                
                if cvd_strong:
                    tp1_hit = True
                    sl_price = entry_price
                else:
                    return {"outcome": "TP1_EXIT", "r_realized": 1.0, "bars": j, "info": "CVD Divergence Exit"}

            if curr_low <= tp2_price:
                return {"outcome": "TP2_HIT", "r_realized": tp2_ratio, "bars": j, "info": "Target 3R"}

    exit_price = df.iloc[entry_index + 11]['close'] if entry_index + 11 < len(df) else df.iloc[-1]['close']
    pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
    r_realized = pnl / risk_per_share
    if tp1_hit: r_realized = max(r_realized, 0.5) 
    return {"outcome": "TIME_STOP", "r_realized": r_realized, "bars": 11, "info": "Time Out"}

# ---------------------------------------------------------
# 4. EJECUCIÓN MULTIPAIR
# ---------------------------------------------------------

def is_core_session(timestamp):
    hour = timestamp.hour
    return 14 <= hour <= 16

def run_multipair_test():
    print("--- ORANGE PI LAB: MULTIPAIR VALIDATOR (V5.2) ---")
    pairs = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
    
    global_trade_log = []
    
    for symbol in pairs:
        print(f"\nProcesando {symbol}...")
        df = fetch_history_for_pair(symbol, '5m', total_candles=15000)
        df = calculate_indicators(df)
        
        last_trade_index = -999
        trade_log = []
        
        # Filtros iguales para todos
        for i in range(500, len(df)):
            if i - last_trade_index < 12: continue
            row = df.iloc[i]
            
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
            
            if is_long:
                if prev_row['close'] < val: continue 
                if (val - row['low']) > (row['ATR'] * 0.3): continue
                if row['RSI'] < 45 and row['delta_norm'] > 0 and df['cvd'].iloc[i] > df['cvd'].iloc[i-3]:
                    entry_signal = 'LONG'
            elif is_short:
                if prev_row['close'] > vah: continue
                if (row['high'] - vah) > (row['ATR'] * 0.3): continue
                if row['RSI'] > 55 and row['delta_norm'] < 0 and df['cvd'].iloc[i] < df['cvd'].iloc[i-3]:
                    entry_signal = 'SHORT'
                    
            if entry_signal:
                zone_level = val if entry_signal == 'LONG' else vah
                res = manage_trade_r_logic(df, i, row['close'], entry_signal, row['ATR'], row['delta_norm'], zone_level)
                
                # FEE 0.045
                final_r = res['r_realized'] - 0.045
                
                trade_data = {
                    "symbol": symbol,
                    "time": row['timestamp'],
                    "type": entry_signal,
                    "outcome": res['outcome'],
                    "r_net": final_r,
                    "info": res['info']
                }
                trade_log.append(trade_data)
                last_trade_index = i

        print(f"-> {symbol}: {len(trade_log)} trades encontrados.")
        global_trade_log.extend(trade_log)

    # --- REPORTE GLOBAL ---
    if not global_trade_log:
        print("\nNo se encontraron trades en ningún par.")
        return

    df_all = pd.DataFrame(global_trade_log)
    
    print("\n" + "="*50)
    print("RESULTADOS GLOBALES MULTIPAIR (V5.2)")
    print("="*50)
    print(f"Total Trades: {len(df_all)}")
    
    df_exec = df_all[~df_all['outcome'].isin(['EARLY_EXIT', 'STAGNANT'])]
    
    total_r = df_all['r_net'].sum()
    expectancy_total = total_r / len(df_all)
    expectancy_exec = df_exec['r_net'].sum() / len(df_exec) if not df_exec.empty else 0
    
    print(f"TOTAL R NETO (w/High Fees): {total_r:.2f} R")
    print(f"EXPECTANCY TOTAL:         {expectancy_total:.3f} R / trade")
    print(f"EXPECTANCY EJECUTADA:     {expectancy_exec:.3f} R / trade")
    
    print("\nDesglose por Par:")
    print(df_all.groupby('symbol')['r_net'].sum())
    
    print("\nDistribución de Outcomes:")
    print(df_all['outcome'].value_counts())

if __name__ == "__main__":
    run_multipair_test()