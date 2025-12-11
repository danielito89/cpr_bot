#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V64 – IRONCLAD HYDRA
# ======================================================

SYMBOL = "ETHUSDT" 
TIMEFRAME_STR = "1h"

# ---- Asignación ----
CAPITAL_ALLOCATION_A = 0.5  # Golden (Long Only)
CAPITAL_ALLOCATION_B = 0.5  # Silver (Long & Short)

# ---- Parametros Estrategia ----
MIN_ADX_4H = 20         

# Salidas (Fix 3: Trailing con memoria)
SL_ATR_MULT = 2.0       
TRAIL_ATR_MULT = 3.0    

# Cooldown
COOLDOWN_BARS = 4       

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.04   
MAX_LEVER = 5           

COMMISSION = 0.0004         
SPREAD_PCT = 0.0002     
SLIPPAGE_PCT = 0.0004   
BASE_LATENCY = 0.0001

MIN_QTY = 0.01          
QTY_PRECISION = 3       

# ======================================================
#  1. CARGA Y PREPARACIÓN (FIX 2 & 1)
# ======================================================

def load_and_resample(symbol):
    print(f"🔍 Cargando datos 1H para {symbol}...")
    candidates = [f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv", f"{symbol}_{TIMEFRAME_STR}.csv"]
    paths = ["data", ".", "cpr_bot_v90/data"]
    
    df = None
    for name in candidates:
        for p in paths:
            path = os.path.join(p, name)
            if os.path.exists(path):
                df = pd.read_csv(path)
                break
        if df is not None: break

    if df is None: return None

    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
    
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)

    if 'volume' not in df.columns: df['volume'] = 1.0

    # --- 1. INDICADORES 1H (FIX 1: ATR SIN SHIFT) ---
    # Calculamos ATR actual. En el loop usaremos row.atr_1h, que es el del cierre de la vela.
    # Para evitar lookahead en el Sizing del Open siguiente, debemos acceder al previo en el loop.
    df['atr_1h'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)

    # --- 2. RESAMPLING A 4H (Macroestructura) ---
    print("🔄 Resampleando a 4H...")
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').agg(ohlc_dict).dropna()
    
    # Indicadores 4H
    df_4h['ema_50'] = talib.EMA(df_4h['close'], timeperiod=50)
    df_4h['ema_200'] = talib.EMA(df_4h['close'], timeperiod=200)
    df_4h['ema_21'] = talib.EMA(df_4h['close'], timeperiod=21)
    df_4h['ema_55'] = talib.EMA(df_4h['close'], timeperiod=55)
    df_4h['adx'] = talib.ADX(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Señales (Calculadas al CIERRE de 4H)
    # A (Golden)
    trend_a = np.where(df_4h['ema_50'] > df_4h['ema_200'], 1, -1)
    df_4h['sig_a'] = np.where((trend_a==1) & (pd.Series(trend_a).shift(1)==-1), 1, 
                     np.where((trend_a==-1) & (pd.Series(trend_a).shift(1)==1), -1, 0))
    
    # B (Silver)
    trend_b = np.where(df_4h['ema_21'] > df_4h['ema_55'], 1, -1)
    df_4h['sig_b'] = np.where((trend_b==1) & (pd.Series(trend_b).shift(1)==-1), 1, 
                     np.where((trend_b==-1) & (pd.Series(trend_b).shift(1)==1), -1, 0))

    # --- FIX 2: ALINEACIÓN PERFECTA ---
    # Queremos que la vela de las 09:00 (1H) tenga los datos de la vela 4H que terminó a las 08:00.
    # 1. Seleccionamos columnas
    cols = ['sig_a', 'sig_b', 'adx', 'high', 'low']
    df_4h_subset = df_4h[cols].copy()
    
    # 2. Reindexamos a 1H con ffill (Propagamos el valor de 08:00 a 09:00, 10:00, etc.)
    # Importante: ffill propaga el valor de 08:00 INCLUYENDO a la fila de 08:00
    df_4h_aligned = df_4h_subset.reindex(df.index, method='ffill')
    
    # 3. Hacemos SHIFT(1) AHORA.
    # Así, a las 08:00 tenemos datos de 07:00 (viejo).
    # A las 09:00 tenemos datos de 08:00 (¡Correcto! La vela cerrada).
    df_4h_final = df_4h_aligned.shift(1).rename(columns={
        'sig_a': 'sig_a_4h', 
        'sig_b': 'sig_b_4h',
        'adx': 'adx_4h',
        'high': 'prev_4h_high', 
        'low': 'prev_4h_low'
    })

    print("🔄 Sincronizando con 1H...")
    df_1h = df.join(df_4h_final)
    df_1h.dropna(inplace=True) # Elimina los NaNs del inicio
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V64 (IRONCLAD)
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V64 (Ironclad Hydra) para {symbol}\n")

    bal_a = INITIAL_BALANCE * CAPITAL_ALLOCATION_A
    bal_b = INITIAL_BALANCE * CAPITAL_ALLOCATION_B
    peak_a = bal_a; peak_b = bal_b
    
    # Estado A (Golden - Long Only)
    pos_a = None; entry_a = 0; qty_a = 0; sl_a = 0; comm_a = 0
    highest_a = 0 # (Fix 3: Memoria para Trailing)
    
    # Estado B (Silver - Long & Short)
    pos_b = None; entry_b = 0; qty_b = 0; sl_b = 0; comm_b = 0
    extreme_b = 0 # (Fix 3: Memoria: High para Long, Low para Short)
    cooldown_b = 0 
    
    equity_curve = []
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Datos Contexto
        # Usamos ATR previo para calculos de entrada (Sizing) para no ver futuro
        atr_now = row.atr_1h
        atr_prev = df.at[i-1, 'atr_1h'] if i > 0 else atr_now
        
        adx_4h = row.adx_4h
        prev_4h_high = row.prev_4h_high
        prev_4h_low = row.prev_4h_low
        
        raw_sig_a = row.sig_a_4h
        raw_sig_b = row.sig_b_4h
        
        friction = SLIPPAGE_PCT + SPREAD_PCT

        # Validaciones
        volatility_ok = adx_4h > MIN_ADX_4H 
        
        # =========================================================
        #  ESTRATEGIA A: GOLDEN (LONG ONLY)
        # =========================================================
        
        # 1. GESTIÓN
        if pos_a == 'long':
            exit_p_a = None
            reason_a = None
            
            # (Fix 3: Trailing Ratchet)
            # Actualizamos el máximo alcanzado desde la entrada
            if h > highest_a: highest_a = h
            
            # SL dinámico basado en el máximo histórico del trade
            # El SL nunca baja, solo sube.
            new_sl = highest_a - (atr_prev * TRAIL_ATR_MULT)
            if new_sl > sl_a: sl_a = new_sl
            
            if raw_sig_a == -1: 
                exit_p_a = o * (1 - SLIPPAGE_PCT); reason_a = "Death Cross (A)"
            elif l <= sl_a: 
                exit_raw = o if o < sl_a else sl_a 
                exit_p_a = exit_raw * (1 - SLIPPAGE_PCT); reason_a = "SL/Trail (A)"
            
            if exit_p_a:
                pnl = (exit_p_a - entry_a) * qty_a
                c_exit = exit_p_a * qty_a * COMMISSION
                bal_a += (pnl - c_exit)
                if bal_a > peak_a: peak_a = bal_a
                
                net = pnl - comm_a - c_exit
                trades.append({'year': ts.year, 'strat': 'A', 'type': reason_a, 'pnl': net})
                pos_a = None; comm_a = 0; highest_a = 0

        # 2. ENTRADA
        elif pos_a is None and raw_sig_a == 1:
            confirmed = o > prev_4h_high
            
            if volatility_ok and confirmed:
                real_entry = o * (1 + friction)
                
                if atr_prev > 0:
                    sl_price = real_entry - (atr_prev * SL_ATR_MULT)
                    dist = real_entry - sl_price
                    
                    if dist > 0:
                        risk_usd = peak_a * FIXED_RISK_PCT
                        raw_qty = min(risk_usd/dist, (bal_a * MAX_LEVER)/real_entry)
                        qty = np.floor(raw_qty * (10**QTY_PRECISION)) / (10**QTY_PRECISION)
                        
                        if qty >= MIN_QTY:
                            cost = qty * real_entry * COMMISSION
                            bal_a -= cost
                            pos_a = 'long'; entry_a = real_entry; qty_a = qty
                            sl_a = sl_price; comm_a = cost
                            highest_a = real_entry # Init memoria
                            
                            # Intra-candle crash
                            if l <= sl_a:
                                pnl = (sl_a*(1-SLIPPAGE_PCT) - real_entry)*qty
                                c_ex = sl_a * qty * COMMISSION
                                bal_a += (pnl - c_ex)
                                trades.append({'year': ts.year, 'strat': 'A', 'type': 'SL Intra', 'pnl': pnl-cost-c_ex})
                                pos_a = None; comm_a = 0

        # =========================================================
        #  ESTRATEGIA B: SILVER (LONG & SHORT)
        # =========================================================
        if cooldown_b > 0: cooldown_b -= 1
        
        # 1. GESTIÓN
        exit_p_b = None
        if pos_b is not None:
            # (Fix 3: Trailing Ratchet para B)
            if pos_b == 'long':
                if h > extreme_b: extreme_b = h
                new_sl = extreme_b - (atr_prev * TRAIL_ATR_MULT)
                if new_sl > sl_b: sl_b = new_sl
            else: # Short
                if l < extreme_b: extreme_b = l
                new_sl = extreme_b + (atr_prev * TRAIL_ATR_MULT)
                if new_sl < sl_b: sl_b = new_sl
            
            # Check Exit
            if pos_b == 'long':
                if raw_sig_b == -1: 
                    exit_p_b = o * (1 - SLIPPAGE_PCT); reason_b = "Flip Short (B)"
                elif l <= sl_b:
                    exit_raw = o if o < sl_b else sl_b
                    exit_p_b = exit_raw * (1 - SLIPPAGE_PCT); reason_b = "SL Long (B)"
            
            elif pos_b == 'short':
                if raw_sig_b == 1: 
                    exit_p_b = o * (1 + SLIPPAGE_PCT); reason_b = "Flip Long (B)"
                elif h >= sl_b:
                    exit_raw = o if o > sl_b else sl_b
                    exit_p_b = exit_raw * (1 + SLIPPAGE_PCT); reason_b = "SL Short (B)"
            
            if exit_p_b:
                if pos_b == 'long': pnl = (exit_p_b - entry_b) * qty_b
                else: pnl = (entry_b - exit_p_b) * qty_b
                
                c_ex = exit_p_b * qty_b * COMMISSION
                bal_b += (pnl - c_ex)
                if bal_b > peak_b: peak_b = bal_b
                
                net = pnl - comm_b - c_ex
                trades.append({'year': ts.year, 'strat': 'B', 'type': reason_b, 'pnl': net})
                
                pos_b = None; comm_b = 0
                cooldown_b = COOLDOWN_BARS
                
                # (Fix 4: Evitar re-entrada inmediata en la misma vela)
                # Usamos 'continue' para saltar a la parte de equity update directamente
                # Pero como Estrategia A ya corrió, solo saltamos el bloque de Entrada B
                goto_equity = True 
            else:
                goto_equity = False

        else:
            goto_equity = False

        # 2. ENTRADA B (Si flat y sin cooldown)
        if pos_b is None and cooldown_b == 0 and not goto_equity:
            new_side = None
            if raw_sig_b == 1 and o > prev_4h_high: new_side = 'long'
            elif raw_sig_b == -1 and o < prev_4h_low: new_side = 'short'
            
            if new_side and volatility_ok and atr_prev > 0:
                if new_side == 'long':
                    real_entry = o * (1 + friction)
                    sl_price = real_entry - (atr_prev * SL_ATR_B)
                else:
                    real_entry = o * (1 - friction)
                    sl_price = real_entry + (atr_prev * SL_ATR_B)
                
                dist = abs(real_entry - sl_price)
                if dist > 0:
                    risk_usd = peak_b * FIXED_RISK_PCT
                    raw_qty = min(risk_usd/dist, (bal_b * MAX_LEVER)/real_entry)
                    qty = np.floor(raw_qty * (10**QTY_PRECISION)) / (10**QTY_PRECISION)
                    
                    if qty >= MIN_QTY:
                        cost = qty * real_entry * COMMISSION
                        bal_b -= cost
                        
                        pos_b = new_side; entry_b = real_entry; qty_b = qty
                        sl_b = sl_price; comm_b = cost
                        extreme_b = real_entry # Init memoria
                        
                        # Intra-candle
                        crash = False
                        if new_side == 'long' and l <= sl_b: crash = True
                        if new_side == 'short' and h >= sl_b: crash = True
                        
                        if crash:
                            exit_p = sl_b * (1 - SLIPPAGE_PCT) if new_side=='long' else sl_b * (1 + SLIPPAGE_PCT)
                            pnl = (exit_p - entry_b)*qty if new_side=='long' else (entry_b - exit_p)*qty
                            c_ex = exit_p * qty * COMMISSION
                            bal_b += (pnl - c_ex)
                            trades.append({'year': ts.year, 'strat': 'B', 'type': 'SL Intra', 'pnl': pnl-cost-c_ex})
                            pos_b = None; comm_b = 0
                            cooldown_b = COOLDOWN_BARS

        # =========================================================
        #  EQUITY UPDATE
        # =========================================================
        eq_a = bal_a
        if pos_a == 'long': eq_a += (c - entry_a) * qty_a
        
        eq_b = bal_b
        if pos_b == 'long': eq_b += (c - entry_b) * qty_b
        elif pos_b == 'short': eq_b += (entry_b - c) * qty_b
        
        equity_curve.append(eq_a + eq_b)

    # REPORTING
    total_bal = bal_a + bal_b
    total_ret = (total_bal - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    trades_df = pd.DataFrame(trades)
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V64 – IRONCLAD HYDRA: {symbol}")
    print("="*55)
    print(f"💰 Balance Final:   ${total_bal:.2f}")
    print(f"📈 Retorno Total:   {total_ret:.2f}%")
    
    eq_series = pd.Series(equity_curve)
    if len(eq_series) > 0:
        dd = (eq_series - eq_series.cummax()) / eq_series.cummax()
        print(f"📉 Max DD:          {dd.min()*100:.2f}%")

    if not trades_df.empty:
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:        {win:.2f}%")
        print(f"🧮 Total Trades:    {len(trades_df)}\n")
        print("\n📅 RENDIMIENTO POR AÑO:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        print("\n🔎 DESGLOSE POR ESTRATEGIA:")
        print(trades_df.groupby("strat")["pnl"].agg(["sum","count", "mean"]))
        
        trades_df.to_csv(f"log_v64_{symbol}.csv", index=False)
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)