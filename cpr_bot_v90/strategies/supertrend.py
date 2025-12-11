#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V65 – THE PRAGMATIC BREAKOUT
# ======================================================

SYMBOL = "ETHUSDT" 
TIMEFRAME_STR = "1h"

# ---- Estrategia: TENDENCIA FRACTAL (4H + 1H) ----
# Macro: EMA 50 > 200 en 4H (Dirección)
# Micro: EMA 50 > 200 en 1H (Momento)
EMA_FAST = 50
EMA_SLOW = 200

# ---- Entrada (Fix 1: Validación) ----
# Trigger = High_4H_Previo + (ATR_1H * BUFFER)
BREAKOUT_BUFFER_ATR = 0.25  

# ---- Salidas & Gestión (Fix 5 & 6) ----
SL_ATR_1H_MULT = 2.0        # Parte A del SL Híbrido
SL_ATR_4H_MULT = 0.5        # Parte B del SL Híbrido
TRAIL_ATR_MULT = 3.0        # Trailing sobre Cierre

BE_TRIGGER_ATR = 0.8        # Mover a BE si ganamos 0.8 ATR
STALL_EXIT_BARS = 3         # Si en 3 horas no arranca...
STALL_MIN_PROFIT = 0.4      # ...y no ganamos al menos 0.4 ATR, cerrar.

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.03       # 3% (Conservador para probar la nueva mecánica)
MAX_LEVER = 5           

COMMISSION = 0.0004         
SPREAD_PCT = 0.0002         
SLIPPAGE_PCT = 0.0004       
BASE_LATENCY = 0.0000       # (Fix: Latency fuera del precio)

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

    # --- 1. INDICADORES 1H (Micro) ---
    print("📐 Calculando Microestructura 1H...")
    df['atr_1h'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
    df['ema50_1h'] = talib.EMA(df['close'], timeperiod=50)
    df['ema200_1h'] = talib.EMA(df['close'], timeperiod=200)
    
    # Tendencia Micro (Shift 1 para no ver futuro)
    df['trend_1h'] = np.where(df['ema50_1h'] > df['ema200_1h'], 1, -1)
    df['trend_1h'] = df['trend_1h'].shift(1)
    
    # Shift ATR 1H para cálculos de entrada (usamos volatilidad reciente conocida)
    df['atr_1h_prev'] = df['atr_1h'].shift(1)

    # --- 2. RESAMPLING A 4H (Macro) ---
    print("🔄 Resampleando a 4H...")
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').agg(ohlc_dict).dropna()
    
    # Indicadores 4H
    df_4h['ema50_4h'] = talib.EMA(df_4h['close'], timeperiod=50)
    df_4h['ema200_4h'] = talib.EMA(df_4h['close'], timeperiod=200)
    df_4h['atr_4h'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Tendencia Macro (1 = Bull, -1 = Bear)
    df_4h['trend_4h'] = np.where(df_4h['ema50_4h'] > df_4h['ema200_4h'], 1, -1)
    
    # --- ALINEACIÓN EXACTA (Fix Lookahead) ---
    # Shift(1) en 4H: A las 09:00 vemos la vela cerrada de las 08:00
    # Traemos el High/Low para definir el rango de ruptura
    cols = ['trend_4h', 'high', 'low', 'atr_4h']
    df_4h_shifted = df_4h.shift(1)[cols].rename(columns={
        'high': 'prev_4h_high',
        'low': 'prev_4h_low'
    })

    print("🔄 Sincronizando...")
    df_1h = df.join(df_4h_shifted)
    
    # Propagamos la señal 4H a las velas de 1H correspondientes
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V65
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V65 (The Pragmatic Breakout) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance
    
    position = None 
    entry_price = 0; qty = 0; sl = 0
    entry_comm = 0
    
    # Variables de Gestión V65
    highest_close = 0       # (Fix 5: Trailing en Close)
    bars_held = 0           # (Fix 6: Time Decay)
    is_be = False           # Flag Breakeven
    
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Datos Contexto
        atr_1h = row.atr_1h_prev
        atr_4h = row.atr_4h
        
        # Estructura
        trend_macro = row.trend_4h == 1
        trend_micro = row.trend_1h == 1
        
        # Niveles Clave
        breakout_level = row.prev_4h_high
        
        friction = SLIPPAGE_PCT + SPREAD_PCT

        # =========================================================
        # 1. GESTIÓN DE POSICIÓN (LONG ONLY)
        # =========================================================
        if position == 'long':
            exit_p = None
            reason = None
            bars_held += 1
            
            # --- A. ACTULIZAR TRAILING (Fix 5: Close Based) ---
            if c > highest_close: 
                highest_close = c
            
            # Calculamos nuevo trailing basado en el highest close
            trail_sl = highest_close - (atr_4h * TRAIL_ATR_MULT) # Usamos ATR 4H para trailing largo
            # El SL nunca baja
            if trail_sl > sl: sl = trail_sl
            
            # --- B. BREAKEVEN CHECK (Fix 6) ---
            if not is_be:
                current_profit = h - entry_price
                if current_profit > (atr_1h * BE_TRIGGER_ATR):
                    # Mover a Entrada + un poquito para cubrir fees
                    be_price = entry_price * (1 + 0.001) 
                    if be_price > sl: 
                        sl = be_price
                        is_be = True
            
            # --- C. TIME DECAY (Fix 6) ---
            # Si pasaron 3 velas y no ganamos 0.4 ATR, cerrar.
            if bars_held >= STALL_EXIT_BARS:
                current_profit_close = c - entry_price
                if current_profit_close < (atr_1h * STALL_MIN_PROFIT):
                    exit_p = c * (1 - SLIPPAGE_PCT)
                    reason = "Stall Exit (Time)"

            # --- D. STOP LOSS / TRAILING HIT ---
            if exit_p is None and l <= sl:
                exit_raw = o if o < sl else sl 
                exit_p = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "Stop Loss/Trail" if not is_be else "Breakeven"
            
            # EJECUTAR SALIDA
            if exit_p:
                pnl = (exit_p - entry_price) * qty
                comm_exit = exit_p * qty * COMMISSION
                balance += (pnl - comm_exit)
                
                if balance > peak_balance: peak_balance = balance
                
                net = pnl - entry_comm - comm_exit
                trades.append({'year': ts.year, 'type': reason, 'pnl': net, 'bars': bars_held})
                position = None
                qty = 0

        # =========================================================
        # 2. ENTRADA (BREAKOUT VALIDADO)
        # =========================================================
        # Solo entramos si NO tenemos posición
        if position is None:
            
            # FILTRO: Tendencia alineada (4H y 1H alcistas) (Fix 4 de tu lista)
            if trend_macro and trend_micro:
                
                # TRIGGER: Precio actual supera (High 4H Previo + Buffer)
                buffer = atr_1h * BREAKOUT_BUFFER_ATR
                trigger_price = breakout_level + buffer
                
                # Chequeamos si en esta vela el precio rompió el nivel
                # (Asumimos entrada stop en el trigger)
                if h > trigger_price:
                    
                    # Precio real de ejecución (Trigger o Open si gap)
                    base_entry = max(o, trigger_price)
                    real_entry = base_entry * (1 + friction)
                    
                    # SL HÍBRIDO (Fix 2: Tu fórmula exacta)
                    sl_1 = 2 * atr_1h
                    sl_2 = 0.5 * atr_4h
                    sl_dist = max(sl_1, sl_2)
                    
                    sl_price = real_entry - sl_dist
                    risk_dist = real_entry - sl_price
                    
                    if risk_dist > 0:
                        # Sizing
                        risk_usd = peak_balance * FIXED_RISK_PCT
                        raw_qty = min(risk_usd/risk_dist, (balance * MAX_LEVER)/real_entry)
                        qty_calc = np.floor(raw_qty * (10**QTY_PRECISION)) / (10**QTY_PRECISION)
                        
                        if qty_calc >= MIN_QTY:
                            cost = qty_calc * real_entry * COMMISSION
                            balance -= cost
                            
                            position = 'long'
                            entry_price = real_entry
                            qty = qty_calc
                            sl = sl_price
                            entry_comm = cost
                            
                            # Estado inicial gestión
                            highest_close = c # Empezamos a trackear desde el cierre actual
                            bars_held = 0
                            is_be = False
                            
                            # Intra-candle check (Muerte súbita)
                            if l <= sl:
                                exit_p = sl * (1 - SLIPPAGE_PCT)
                                pnl = (exit_p - real_entry) * qty_calc
                                c_ex = exit_p * qty_calc * COMMISSION
                                balance += (pnl - c_ex)
                                trades.append({'year': ts.year, 'type': 'Instant SL', 'pnl': pnl-cost-c_ex})
                                position = None

        # Equity Update
        curr_eq = balance
        if position == 'long':
            curr_eq += (c - entry_price) * qty
        equity_curve.append(curr_eq)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V65 – THE PRAGMATIC BREAKOUT: {symbol}")
    print("="*55)
    print(f"💰 Balance Final:   ${balance:.2f}")
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
        
        trades_df.to_csv(f"log_v65_{symbol}.csv", index=False)
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)