#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
from datetime import timedelta

try:
    import talib
    HAS_TALIB = True
except:
    HAS_TALIB = False
    print("❌ TA-Lib no está instalado. Instálalo para usar V45.")

# ======================================================
#  🔥 CONFIG V45 – LIQUIDITY SWEEP (PRICE ACTION)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia Core (SMC / Price Action) ----
SWING_LOOKBACK = 20     # Miramos el mínimo de las últimas 20 velas
EMA_TREND = 200         # Filtro Macro (Solo sweeps a favor de tendencia)

# ---- Salidas ----
RR_TARGET = 2.0         # Risk:Reward fijo de 1:2 (Simple y efectivo)
SL_BUFFER = 0.001       # 0.1% de aire debajo de la mecha del sweep
EXIT_HOURS = 24         # Scalp/DayTrade rápido (1 día máx)

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
TARGET_VOL = 0.015
BASE_VAR = 0.02
COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001

MIN_QTY = 0.01
QTY_PRECISION = 3 

DD_LIMIT = 0.15
DD_FACTOR = 0.5
MAX_LEVER = 20              

MAX_TRADES_MONTH = 20     
BAD_HOURS = [3,4,5]

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
    df['time_diff'] = df['timestamp'].diff().dt.total_seconds()
    if 'volume' not in df.columns: df['volume'] = 1.0

    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  📐 INDICADORES (V45)
# ======================================================

def calc_indicators(df):
    print("📐 Calculando indicadores V45 (Liquidity Sweeps)...")

    if not HAS_TALIB: raise Exception("TA-Lib requerido.")

    # 1. ATR (Para dimensionamiento de posición, no para SL)
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], 14)
    df['atr_prev'] = df['atr'].shift(1)

    # 2. EMA Tendencia
    df['ema_trend'] = talib.EMA(df['close'], timeperiod=EMA_TREND)

    # 3. SWING POINTS (Liquidez)
    # El Swing Low es el mínimo de las últimas N velas (sin contar la actual)
    # Shift(1) es vital para no mirar el Low de la vela que estamos analizando
    df['swing_low_support'] = df['low'].rolling(window=SWING_LOOKBACK).min().shift(1)

    # Gap Detection
    jump = abs(df['open'] - df['close'].shift(1))
    atr_thr = df['atr'].shift(1) * 3
    gap = (df['time_diff'] > 9000) | (jump > atr_thr)
    df['gap'] = gap
    
    df['prev_close'] = df['close'].shift(1)

    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  🚀 BACKTEST ENGINE – V45
# ======================================================

def run_backtest(symbol):
    df = load_data(symbol)
    if df is None: return
    df = calc_indicators(df)

    print(f"🚀 Iniciando Backtest V45 (Liquidity Sweep) para {symbol}\n")

    balance = INITIAL_BALANCE
    peak = balance
    equity_curve = [balance]

    # Estado
    position = None
    entry = 0; quantity = 0; sl = 0; tp = 0; entry_time = None
    
    position_comm_paid = 0.0 
    
    month = -1; trades_month = 0; cooldown = 0
    
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        trade_active_this_candle = False

        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        atr_prev = row.atr_prev
        swing_support = row.swing_low_support
        
        # Costos
        total_friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # Gestión Mes
        if ts.month != month:
            month = ts.month
            trades_month = 0

        if row.gap: cooldown = 24
        if cooldown > 0: cooldown -= 1

        # ============================================================
        # 1) BÚSQUEDA DE ENTRADA (SWEEP & RECLAIM)
        # ============================================================
        if position is None and cooldown == 0:
            if trades_month < MAX_TRADES_MONTH and ts.hour not in BAD_HOURS:
                
                # A) Filtro Tendencia: Solo Sweeps alcistas sobre la EMA
                trend_ok = c > row.ema_trend
                
                # B) Patrón Sweep:
                # 1. El precio perforó el soporte (Low < Swing Low)
                # 2. Pero cerró POR ENCIMA del soporte (Close > Swing Low)
                # 3. Y la vela es verde (Close > Open) - Opcional pero recomendado
                
                swept_liquidity = l < swing_support
                reclaimed_level = c > swing_support
                green_candle = c > o
                
                if trend_ok and swept_liquidity and reclaimed_level and green_candle:
                    
                    # --- EJECUCIÓN ---
                    # Entramos en la apertura de la SIGUIENTE vela (simulado aquí en el mismo loop
                    # asumiendo ejecución inmediata al cierre/open siguiente)
                    # Para backtest vectorizado loop: ejecutamos ahora con precio de cierre + friccion?
                    # NO, lo correcto es: Detectamos señal en 'i', entramos en 'i+1'.
                    # Pero para simplificar lógica en este framework:
                    # Asumimos entrada al CIERRE de esta vela (Close) o simulamos Open siguiente (Close ~ Open next)
                    
                    # Usaremos CLOSE de la vela de sweep como base de entrada (Mark Price)
                    base_price = c 
                    entry_price = base_price * (1 + total_friction)
                    
                    # SL: Debajo de la mecha del sweep (Low de la vela actual)
                    sl_price = l * (1 - SL_BUFFER)
                    
                    # TP: Risk Reward 1:2
                    risk_dist = entry_price - sl_price
                    tp_price = entry_price + (risk_dist * RR_TARGET)

                    if risk_dist > 0:
                        # Sizing
                        vol_smooth = atr_prev / c
                        var_factor = min(1.0, TARGET_VOL / max(vol_smooth, 1e-6))
                        dd = (peak - balance) / peak
                        dd_adj = DD_FACTOR if dd > DD_LIMIT else 1.0
                        
                        final_risk_pct = BASE_VAR * var_factor * dd_adj
                        risk_usd = peak * final_risk_pct

                        max_contracts = (balance * MAX_LEVER) / entry_price
                        qty = min(risk_usd / risk_dist, max_contracts)
                        
                        if qty < MIN_QTY: qty = 0
                        else: qty = round(qty, QTY_PRECISION)

                        if qty > 0:
                            entry_comm = qty * entry_price * COMMISSION
                            balance -= entry_comm

                            position = "long"
                            entry = entry_price
                            sl = sl_price
                            tp = tp_price
                            quantity = qty
                            entry_time = ts
                            
                            position_comm_paid = entry_comm
                            trades_month += 1
                            trade_active_this_candle = True
                            
                            # (No chequeamos Intra-Candle exit porque entramos al cierre)

        # ============================================================
        # 2) GESTIÓN DE POSICIÓN ABIERTA
        # ============================================================
        if position == "long" and not trade_active_this_candle:
            
            exit_price = None
            reason = None

            # SL Check
            if l <= sl:
                exit_raw = o if o < sl else sl 
                exit_price = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "SL"

            # TP Check
            elif h >= tp:
                exit_raw = max(o, tp) # Gap up favor
                exit_price = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "TP Target"

            # Time Exit
            elif (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600:
                exit_price = c * (1 - SLIPPAGE_PCT)
                reason = "Time"

            if exit_price:
                pnl = (exit_price - entry) * quantity
                exit_comm = exit_price * quantity * COMMISSION
                
                balance += (pnl - exit_comm)
                if balance > peak: peak = balance
                
                net_pnl = pnl - position_comm_paid - exit_comm
                
                trades.append({
                    "year": entry_time.year, "month": entry_time.month,
                    "pnl": net_pnl, "type": reason
                })
                
                position = None
                position_comm_paid = 0.0
                trade_active_this_candle = True

        # ============================================================
        # 3) EQUITY UPDATE
        # ============================================================
        if not trade_active_this_candle:
            curr_eq = balance
            if position == "long":
                unrealized = (c - entry) * quantity
                curr_eq += unrealized
            equity_curve.append(curr_eq)
        else:
            equity_curve.append(balance)

    # REPORT
    eq_series = pd.Series(equity_curve)
    if len(eq_series) > 0:
        dd = (eq_series - eq_series.cummax()) / eq_series.cummax()
        max_dd = dd.min() * 100
    else: max_dd = 0

    total_return = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    trades_df = pd.DataFrame(trades)

    print("\n" + "="*55)
    print(f"📊 RESULTADOS FINALES V45 – LIQUIDITY SWEEP: {symbol}")
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
        print("="*55)
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)