#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V53 – DAILY ORB (OPENING RANGE BREAKOUT)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia: ORB (Tiempo y Rango) ----
# Definimos el rango inicial del día (UTC)
RANGE_START_HOUR = 0
RANGE_END_HOUR = 3      # Las primeras 4 horas (0, 1, 2, 3) definen el rango

# ---- Filtros ----
# Solo tomamos el breakout si la tendencia macro acompaña
EMA_TREND = 200         
MIN_RANGE_ATR = 0.5     # El rango debe tener cierto tamaño (evitar días muertos)

# ---- Salidas ----
SL_PCT_OF_RANGE = 0.5   # Stop Loss en la mitad del rango (agresivo)
TP_MULT = 3.0           # Buscamos un día expansivo (3 veces el riesgo)
TIME_EXIT_HOUR = 23     # Cierre forzoso al final del día

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.02   # 2% por día
MAX_LEVER = 10

COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001
MIN_QTY = 0.01

# ======================================================
#  1. CARGA DE DATOS
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
    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  2. INDICADORES (Solo para filtros)
# ======================================================

def calc_indicators(df):
    if not HAS_TALIB: raise Exception("TA-Lib requerido.")
    
    # EMA Macro para filtro de tendencia (Solo largos si estamos arriba)
    df['ema_trend'] = talib.EMA(df['close'], timeperiod=EMA_TREND)
    
    # ATR para medir si el rango inicial es digno
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
    
    # Shift para no ver el futuro
    df['ema_trend'] = df['ema_trend'].shift(1)
    df['atr'] = df['atr'].shift(1)
    
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  🚀 BACKTEST ENGINE V53 (ORB)
# ======================================================

def run_backtest(symbol):
    df = load_data(symbol)
    if df is None: return
    df = calc_indicators(df)

    print(f"🚀 Iniciando Backtest V53 (Daily ORB) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]
    
    position = None 
    entry_price = 0; quantity = 0; sl = 0; tp = 0
    entry_comm = 0
    
    # Variables del Día
    day_high = -1
    day_low = 999999
    range_established = False
    trades_today = 0
    
    current_day = -1
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # --- GESTIÓN DE NUEVO DÍA ---
        if ts.day != current_day:
            current_day = ts.day
            day_high = -1
            day_low = 999999
            range_established = False
            trades_today = 0 # Max 1 trade por día
            
            # Si teníamos posición overnight, la cerramos (Hard Close al open)
            if position == "long":
                exit_p = o * (1 - SLIPPAGE_PCT)
                pnl = (exit_p - entry_price) * quantity
                fee = exit_p * quantity * COMMISSION
                balance += (pnl - fee)
                trades.append({'year': ts.year, 'pnl': pnl - entry_comm - fee, 'type': 'Overnight Close'})
                position = None

        # --- 1. CONSTRUCCIÓN DEL RANGO (00:00 - 03:00) ---
        if ts.hour <= RANGE_END_HOUR:
            day_high = max(day_high, h)
            day_low = min(day_low, l)
            
            # Al final de la hora límite, el rango está listo
            if ts.hour == RANGE_END_HOUR:
                range_established = True
                range_size = day_high - day_low
                
                # Filtro: ¿Es el rango demasiado pequeño (día muerto)?
                min_size = row.atr * MIN_RANGE_ATR
                if range_size < min_size:
                    range_established = False # Rango inválido, no operamos hoy

        # --- 2. BÚSQUEDA DE RUPTURA (04:00 - 23:00) ---
        elif range_established and position is None and trades_today == 0:
            
            # Filtro Tendencia Macro
            trend_ok = c > row.ema_trend
            
            # TRIGGER: El precio rompe el High del Rango establecido
            # Usamos High > DayHigh para detectar, pero entramos con orden Stop
            breakout = h > day_high
            
            if breakout and trend_ok:
                
                friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY
                
                # Entramos al nivel de ruptura (o al open si hubo gap)
                base_entry = max(o, day_high)
                real_entry = base_entry * (1 + friction)
                
                # SL: En la mitad del rango (Si vuelve a entrar mucho, es fakeout)
                range_height = day_high - day_low
                stop_price = day_high - (range_height * SL_PCT_OF_RANGE)
                
                # TP: Expansión de 3 veces el riesgo
                risk_per_share = real_entry - stop_price
                target_price = real_entry + (risk_per_share * TP_MULT)
                
                if risk_per_share > 0:
                    risk_usd = balance * FIXED_RISK_PCT
                    qty = risk_usd / risk_per_share
                    max_qty = (balance * MAX_LEVER) / real_entry
                    qty = min(qty, max_qty)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * real_entry * COMMISSION
                        balance -= entry_comm
                        
                        position = "long"
                        entry_price = real_entry
                        sl = stop_price
                        tp = target_price
                        quantity = qty
                        entry_comm = entry_comm
                        trades_today += 1
                        
                        # Intra-candle Check
                        if l <= sl:
                            exit_p = sl * (1 - SLIPPAGE_PCT)
                            pnl = (exit_p - real_entry) * qty
                            fee = exit_p * qty * COMMISSION
                            balance += (pnl - fee)
                            trades.append({'year': ts.year, 'pnl': pnl - entry_comm - fee, 'type': 'SL Intra'})
                            position = None
                        elif h >= tp:
                            exit_p = tp * (1 - SLIPPAGE_PCT)
                            pnl = (exit_p - real_entry) * qty
                            fee = exit_p * qty * COMMISSION
                            balance += (pnl - fee)
                            trades.append({'year': ts.year, 'pnl': pnl - entry_comm - fee, 'type': 'TP Intra'})
                            position = None

        # --- 3. GESTIÓN ---
        elif position == "long":
            exit_p = None
            reason = None
            
            # SL
            if l <= sl:
                exit_p = sl * (1 - SLIPPAGE_PCT)
                reason = "SL"
            # TP
            elif h >= tp:
                exit_p = tp * (1 - SLIPPAGE_PCT)
                reason = "TP"
            # Time Exit (Fin del día)
            elif ts.hour == TIME_EXIT_HOUR:
                exit_p = c * (1 - SLIPPAGE_PCT)
                reason = "EOD Exit"
            
            if exit_p:
                pnl = (exit_p - entry_price) * quantity
                fee = exit_p * quantity * COMMISSION
                balance += (pnl - fee)
                net = pnl - entry_comm - fee
                trades.append({'year': ts.year, 'pnl': net, 'type': reason})
                position = None

        equity_curve.append(balance)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V53 – DAILY ORB (NO INDICATORS): {symbol}")
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
        try:
            print("\n📅 Rendimiento Anual:")
            print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        except: pass
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)