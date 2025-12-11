#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V63 – HYDRA POLISHED
# ======================================================

SYMBOL = "ETHUSDT" 
TIMEFRAME_STR = "1h"

# ---- Asignación ----
CAPITAL_ALLOCATION_A = 0.5  # Golden
CAPITAL_ALLOCATION_B = 0.5  # Silver

# ---- Parametros Estrategia ----
# Filtro Rango (Fix 2)
MIN_ADX_4H = 20         

# Salidas (Fix 1 & 5)
SL_ATR_MULT = 2.0       # SL Inicial (Usando ATR 1H)
TRAIL_ATR_MULT = 3.0    # Trailing Stop (Usando ATR 1H)

# Cooldown (Fix 4)
COOLDOWN_BARS = 4       # Horas de espera tras cierre en Strat B

# ---- Risk & Microestructura (Fix 6) ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.04   # 4% Riesgo compuesto
MAX_LEVER = 5           

COMMISSION = 0.0004         
SPREAD_PCT = 0.0002     # (Fix 6: Spread ajustado)    
SLIPPAGE_PCT = 0.0004   # (Fix 6: Slippage ajustado)
# BASE_LATENCY eliminada del precio

MIN_QTY = 0.01          
QTY_PRECISION = 3       

# ======================================================
#  1. CARGA Y PREPARACIÓN
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

    # --- 1. INDICADORES 1H (Microestructura) ---
    # (Fix 1: ATR de 1H para Sizing/SL)
    df['atr_1h'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
    # Shift 1H para no ver futuro inmediato
    df['atr_1h'] = df['atr_1h'].shift(1) 

    # --- 2. RESAMPLING A 4H (Macroestructura) ---
    print("🔄 Resampleando a 4H...")
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').agg(ohlc_dict).dropna()
    
    # EMAs A (Golden)
    df_4h['ema_50'] = talib.EMA(df_4h['close'], timeperiod=50)
    df_4h['ema_200'] = talib.EMA(df_4h['close'], timeperiod=200)
    
    # EMAs B (Silver)
    df_4h['ema_21'] = talib.EMA(df_4h['close'], timeperiod=21)
    df_4h['ema_55'] = talib.EMA(df_4h['close'], timeperiod=55)
    
    # ADX 4H (Fix 2: Filtro Rango)
    df_4h['adx'] = talib.ADX(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Señales (Vela Cerrada)
    # A (Golden)
    trend_a = np.where(df_4h['ema_50'] > df_4h['ema_200'], 1, -1)
    df_4h['sig_a'] = np.where((trend_a==1) & (pd.Series(trend_a).shift(1)==-1), 1, 
                     np.where((trend_a==-1) & (pd.Series(trend_a).shift(1)==1), -1, 0))
    
    # B (Silver)
    trend_b = np.where(df_4h['ema_21'] > df_4h['ema_55'], 1, -1)
    df_4h['sig_b'] = np.where((trend_b==1) & (pd.Series(trend_b).shift(1)==-1), 1, 
                     np.where((trend_b==-1) & (pd.Series(trend_b).shift(1)==1), -1, 0))

    # --- SHIFT 4H & MERGE ---
    # Shift(1) para evitar lookahead. Al momento de operar (1H), vemos la vela 4H CERRADA anterior.
    # También traemos High/Low previos para confirmación (Fix 3)
    cols_to_bring = ['sig_a', 'sig_b', 'adx', 'high', 'low']
    df_4h_shifted = df_4h.shift(1)[cols_to_bring].rename(columns={
        'sig_a': 'sig_a_4h', 
        'sig_b': 'sig_b_4h',
        'adx': 'adx_4h',
        'high': 'prev_4h_high', # Para confirmación Long
        'low': 'prev_4h_low'    # Para confirmación Short
    })

    print("🔄 Sincronizando con 1H...")
    df_1h = df.join(df_4h_shifted)
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V63
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V63 (Hydra Polished) para {symbol}\n")

    bal_a = INITIAL_BALANCE * CAPITAL_ALLOCATION_A
    bal_b = INITIAL_BALANCE * CAPITAL_ALLOCATION_B
    peak_a = bal_a; peak_b = bal_b
    
    # Estado A
    pos_a = None; entry_a = 0; qty_a = 0; sl_a = 0; comm_a = 0
    
    # Estado B
    pos_b = None; entry_b = 0; qty_b = 0; sl_b = 0; comm_b = 0
    cooldown_b = 0 # (Fix 4)
    
    equity_curve = []
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Datos del contexto (vienen de 4H)
        atr_1h = row.atr_1h # (Fix 1)
        adx_4h = row.adx_4h
        prev_4h_high = row.prev_4h_high
        prev_4h_low = row.prev_4h_low
        
        # Señales Base
        raw_sig_a = row.sig_a_4h
        raw_sig_b = row.sig_b_4h
        
        # Costos (Fix 6)
        friction = SLIPPAGE_PCT + SPREAD_PCT

        # Validaciones Comunes
        volatility_ok = adx_4h > MIN_ADX_4H # (Fix 2: Filtro Rango)
        
        # =========================================================
        #  ESTRATEGIA A: GOLDEN (LONG ONLY)
        # =========================================================
        
        # 1. GESTIÓN DE POSICIÓN
        if pos_a == 'long':
            exit_p_a = None
            reason_a = None
            
            # (Fix 5: Trailing Stop)
            new_sl = c - (atr_1h * TRAIL_ATR_MULT)
            if new_sl > sl_a: sl_a = new_sl
            
            # Checks
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
                pos_a = None; comm_a = 0

        # 2. ENTRADA
        elif pos_a is None and raw_sig_a == 1:
            # (Fix 3: Confirmación de Ruptura)
            # Solo entramos si el precio actual (o close previo) rompió el High de la señal
            confirmed = o > prev_4h_high
            
            if volatility_ok and confirmed:
                real_entry = o * (1 + friction)
                
                # (Fix 1: SL usando ATR 1H)
                sl_price = real_entry - (atr_1h * SL_ATR_MULT)
                dist = real_entry - sl_price
                
                if dist > 0:
                    risk_usd = peak_a * FIXED_RISK_PCT
                    raw_qty = min(risk_usd/dist, (bal_a * MAX_LEVER)/real_entry)
                    qty = np.floor(raw_qty * (10**QTY_PRECISION)) / (10**QTY_PRECISION)
                    
                    if qty >= MIN_QTY:
                        cost = qty * real_entry * COMMISSION
                        bal_a -= cost
                        pos_a = 'long'; entry_a = real_entry; qty_a = qty; sl_a = sl_price
                        comm_a = cost
                        
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
            # Trailing Stop (Fix 5)
            if pos_b == 'long':
                new_sl = c - (atr_1h * TRAIL_ATR_MULT)
                if new_sl > sl_b: sl_b = new_sl
            else: # Short
                new_sl = c + (atr_1h * TRAIL_ATR_MULT)
                if new_sl < sl_b: sl_b = new_sl
            
            # Check Exit Conditions
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
                cooldown_b = COOLDOWN_BARS # (Fix 4: Activar cooldown)

        # 2. ENTRADA (Si flat y sin cooldown)
        if pos_b is None and cooldown_b == 0:
            new_side = None
            # (Fix 3: Confirmación)
            if raw_sig_b == 1 and o > prev_4h_high: new_side = 'long'
            elif raw_sig_b == -1 and o < prev_4h_low: new_side = 'short'
            
            if new_side and volatility_ok:
                if new_side == 'long':
                    real_entry = o * (1 + friction)
                    sl_price = real_entry - (atr_1h * SL_ATR_MULT)
                else:
                    real_entry = o * (1 - friction)
                    sl_price = real_entry + (atr_1h * SL_ATR_MULT)
                
                dist = abs(real_entry - sl_price)
                if dist > 0:
                    risk_usd = peak_b * FIXED_RISK_PCT
                    raw_qty = min(risk_usd/dist, (bal_b * MAX_LEVER)/real_entry)
                    qty = np.floor(raw_qty * (10**QTY_PRECISION)) / (10**QTY_PRECISION)
                    
                    if qty >= MIN_QTY:
                        cost = qty * real_entry * COMMISSION
                        bal_b -= cost
                        pos_b = new_side; entry_b = real_entry; qty_b = qty; sl_b = sl_price
                        comm_b = cost
                        
                        # Intra-candle check
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
    print(f"📊 RESULTADOS V63 – HYDRA POLISHED: {symbol}")
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
        print(f"🧮 Total Trades:    {len(trades_df)}")
        print("\n📅 RENDIMIENTO POR AÑO:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        print("\n🔎 DESGLOSE POR ESTRATEGIA:")
        print(trades_df.groupby("strat")["pnl"].agg(["sum","count", "mean"]))
        
        trades_df.to_csv(f"log_v63_{symbol}.csv", index=False)
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)