#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib # Asumimos que ya está instalado

# ======================================================
#  🔥 CONFIG V49 – SUPERTREND (FIXED)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia Core ----
ST_PERIOD = 24          
ST_MULTIPLIER = 3.0     

# ---- Filtros ----
ADX_FILTER = 20         
BAD_HOURS = []          

# ---- Salidas ----
EXIT_HOURS = 336        

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.02   
MAX_LEVER = 20          # Variable definida
COMMISSION = 0.0004     
SPREAD_PCT = 0.0004     
SLIPPAGE_PCT = 0.0006   
BASE_LATENCY = 0.0001

MIN_QTY = 0.01
QTY_PRECISION = 3 

# ======================================================
#  🧩 DATA LOADING
# ======================================================

def load_data(symbol):
    print(f"🔍 Cargando {symbol} ...")
    candidates = [f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv", f"{symbol}_{TIMEFRAME_STR}.csv"]
    paths = ["data", ".", "cpr_bot_v90/data"]
    
    df = None
    for name in candidates:
        for p in paths:
            path = os.path.join(p, name)
            if os.path.exists(path):
                print(f"📁 Archivo encontrado: {path}")
                df = pd.read_csv(path)
                break
        if df is not None: break

    if df is None: return None

    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert("UTC") 

    df.sort_values("timestamp", inplace=True)
    if 'volume' not in df.columns: df['volume'] = 1.0
    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  📐 INDICADORES (CORREGIDO)
# ======================================================

def calc_indicators(df):
    print("📐 Calculando Supertrend V49 (Fixed)...")

    # 1. ATR y ADX
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=ST_PERIOD)
    df['adx'] = talib.ADX(df['high'], df['low'], df['close'], timeperiod=14)
    
    # --- FIX CRÍTICO: LIMPIAR NaNs ANTES DEL BUCLE ---
    # Eliminamos las primeras filas que tienen NaN por el cálculo de ATR/ADX
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    
    # 2. CÁLCULO MANUAL SUPERTREND 
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    atr = df['atr'].values
    
    # Arrays
    n = len(df)
    upper_band = np.zeros(n)
    lower_band = np.zeros(n)
    supertrend = np.zeros(n)
    trend = np.zeros(n) # 1 up, -1 down
    
    # Init valores iniciales
    trend[0] = 1
    upper_band[0] = (high[0] + low[0]) / 2 + (ST_MULTIPLIER * atr[0])
    lower_band[0] = (high[0] + low[0]) / 2 - (ST_MULTIPLIER * atr[0])
    supertrend[0] = lower_band[0]
    
    for i in range(1, n):
        mid_price = (high[i] + low[i]) / 2
        basic_upper = mid_price + (ST_MULTIPLIER * atr[i])
        basic_lower = mid_price - (ST_MULTIPLIER * atr[i])
        
        # Upper Band Logic (No baja en tendencia bajista)
        if (basic_upper < upper_band[i-1]) or (close[i-1] > upper_band[i-1]):
            upper_band[i] = basic_upper
        else:
            upper_band[i] = upper_band[i-1]
            
        # Lower Band Logic (No sube en tendencia alcista)
        if (basic_lower > lower_band[i-1]) or (close[i-1] < lower_band[i-1]):
            lower_band[i] = basic_lower
        else:
            lower_band[i] = lower_band[i-1]
            
        # Trend Logic
        prev_trend = trend[i-1]
        
        if prev_trend == -1 and close[i] > upper_band[i-1]:
            trend[i] = 1
        elif prev_trend == 1 and close[i] < lower_band[i-1]:
            trend[i] = -1
        else:
            trend[i] = prev_trend
            
        # Supertrend Line
        if trend[i] == 1:
            supertrend[i] = lower_band[i]
        else:
            supertrend[i] = upper_band[i]
            
    df['supertrend'] = supertrend
    df['trend'] = trend 
    
    # Diagnóstico
    print(f"   👉 Estado Tendencia: {df['trend'].value_counts().to_dict()}")
    
    return df

# ======================================================
#  🚀 BACKTEST ENGINE – V49
# ======================================================

def run_backtest(symbol):
    df = load_data(symbol)
    if df is None: return
    df = calc_indicators(df)

    print(f"🚀 Iniciando Backtest V49 (Supertrend) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]

    position = None 
    entry = 0; quantity = 0; sl = 0
    entry_time = None
    entry_comm_paid = 0.0
    
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        current_trend = row.trend
        st_value = row.supertrend
        
        # Miramos el trend de la vela ANTERIOR para decidir entrada en esta
        prev_trend = df.at[i-1, 'trend'] if i > 0 else 0
        
        total_entry_cost = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # ============================================================
        # 1) BÚSQUEDA DE ENTRADA (LONG ONLY)
        # ============================================================
        # Si la tendencia ya es alcista (1)
        signal_buy = (prev_trend == 1) 
        adx_ok = row.adx > ADX_FILTER
        
        if position is None:
            if signal_buy and adx_ok:
                
                entry_price = o * (1 + total_entry_cost)
                sl_price = st_value 
                
                risk_dist = entry_price - sl_price
                
                if risk_dist > 0:
                    risk_capital = balance * FIXED_RISK_PCT
                    qty = risk_capital / risk_dist 
                    
                    max_qty = (balance * MAX_LEVER) / entry_price
                    qty = min(qty, max_qty)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * entry_price * COMMISSION
                        balance -= entry_comm

                        position = "long"
                        entry = entry_price
                        sl = sl_price
                        quantity = qty
                        entry_time = ts
                        entry_comm_paid = entry_comm
                        
                        # Intra-candle Check
                        if l <= sl:
                            exit_price = sl * (1 - SLIPPAGE_PCT)
                            pnl = (exit_price - entry) * qty
                            fee = exit_price * qty * COMMISSION
                            
                            balance += (pnl - fee)
                            net = pnl - entry_comm - fee
                            trades.append({"year": ts.year, "pnl": net, "type": "SL Intra"})
                            position = None

        # ============================================================
        # 2) GESTIÓN DE POSICIÓN
        # ============================================================
        elif position == "long":
            
            exit_price = None
            reason = None
            
            # Trailing Stop del Supertrend
            if st_value > sl:
                sl = st_value

            # A) Cambio de Tendencia (Flip a Bajista)
            # Si la vela actual cerró y cambió el trend a -1, salimos (simulado al Open de esta vela o Close)
            # En backtest loop 'i', usamos el estado actual. Si current_trend es -1,
            # significa que en ESTA vela el precio cruzó.
            # Salida de emergencia: Si current_trend == -1, salimos al Close (o Open next).
            # Para ser conservadores, salimos al Close de esta vela que confirmó el cambio.
            if current_trend == -1:
                exit_price = c * (1 - SLIPPAGE_PCT) 
                reason = "Trend Flip"
            
            # B) Stop Loss Intradía
            elif l <= sl:
                exit_raw = o if o < sl else sl 
                exit_price = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "SL (Supertrend)"
                
            # C) Time Exit
            elif (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600:
                exit_price = c * (1 - SLIPPAGE_PCT)
                reason = "Time"

            if exit_price:
                pnl = (exit_price - entry) * quantity
                exit_comm = exit_price * quantity * COMMISSION
                
                balance += (pnl - exit_comm)
                net = pnl - entry_comm_paid - exit_comm

                trades.append({
                    "year": entry_time.year, "pnl": net, "type": reason
                })
                
                position = None

        # Equity Update
        curr_eq = balance
        if position == "long":
            curr_eq += (c - entry) * quantity
        equity_curve.append(curr_eq)

    # REPORT
    eq_series = pd.Series(equity_curve)
    if len(eq_series) > 0:
        dd = (eq_series - eq_series.cummax()) / eq_series.cummax()
        max_dd = dd.min() * 100
    else: max_dd = 0

    total_return = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    trades_df = pd.DataFrame(trades)

    print("\n" + "="*55)
    print(f"📊 RESULTADOS FINALES V49 – SUPERTREND: {symbol}")
    print("="*55)
    print(f"💰 Balance Final:   ${balance:.2f}")
    print(f"📈 Retorno Total:   {total_return:.2f}%")
    print(f"📉 Max DD:          {max_dd:.2f}%\n")

    if not trades_df.empty:
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:        {win:.2f}%")
        print(f"🧮 Total Trades:    {len(trades_df)}\n")
        try:
            print("📅 RENDIMIENTO POR AÑO:")
            print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        except: pass
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)