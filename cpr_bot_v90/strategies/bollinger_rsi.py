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
    print("❌ TA-Lib no está instalado. Instálalo para usar V46.")

# ======================================================
#  🔥 CONFIG V46 – CONNORS RSI-2 SCALPER
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia Core (Mean Reversion Ultra-Rápida) ----
RSI_PERIOD = 2          # El secreto de Connors: RSI muy corto
RSI_BUY_LEVEL = 10      # Nivel de pánico extremo (Standard es 10 o 5 para crypto)
TREND_MA_PERIOD = 200   # Filtro Macro (SMA 200)
EXIT_MA_PERIOD = 5      # Salida rápida al tocar la media de 5

# ---- Salidas de Emergencia ----
STOP_LOSS_ATR = 4.0     # Stop de catástrofe (amplio, para dejar trabajar la prob)
EXIT_HOURS = 24         # Si no rebotó en 24h, salir

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
# Usamos un % fijo del equity porque la estrategia tiene alto Win Rate
FIXED_RISK_PCT = 0.05   # 5% del capital por trade (Size agresivo por alta prob)
COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001

MIN_QTY = 0.01
QTY_PRECISION = 3 

# ---- Filtros ----
BAD_HOURS = [3,4,5]     # Evitar hora muerta

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
#  📐 INDICADORES (V46)
# ======================================================

def calc_indicators(df):
    print("📐 Calculando indicadores V46 (RSI-2 + SMA)...")

    if not HAS_TALIB: raise Exception("TA-Lib requerido.")

    # 1. RSI Ultra Corto
    df['rsi_2'] = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
    df['rsi_prev'] = df['rsi_2'].shift(1) # Para evitar lookahead

    # 2. Medias Móviles
    df['sma_trend'] = talib.SMA(df['close'], timeperiod=TREND_MA_PERIOD) # Filtro Trend
    df['sma_exit'] = talib.SMA(df['close'], timeperiod=EXIT_MA_PERIOD)   # Trigger Salida

    # 3. ATR para Stop de Emergencia
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], 14)
    df['atr_prev'] = df['atr'].shift(1)

    # Gap Detection
    jump = abs(df['open'] - df['close'].shift(1))
    atr_thr = df['atr'].shift(1) * 3
    gap = (df['time_diff'] > 9000) | (jump > atr_thr)
    df['gap'] = gap
    
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  🚀 BACKTEST ENGINE – V46
# ======================================================

def run_backtest(symbol):
    df = load_data(symbol)
    if df is None: return
    df = calc_indicators(df)

    print(f"🚀 Iniciando Backtest V46 (Connors RSI-2) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]

    # Estado
    position = None
    entry = 0; quantity = 0; sl = 0
    entry_time = None
    position_comm_paid = 0.0
    
    cooldown = 0
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        trade_active_this_candle = False

        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        atr_prev = row.atr_prev
        
        # Costos
        rel_vol = atr_prev / c
        slippage_pct = SLIPPAGE_PCT # Simplificado para V46
        total_entry_cost = slippage_pct + SPREAD_PCT + BASE_LATENCY

        if row.gap: cooldown = 24
        if cooldown > 0: cooldown -= 1

        # ============================================================
        # 1) BÚSQUEDA DE ENTRADA
        # ============================================================
        if position is None and cooldown == 0:
            if ts.hour not in BAD_HOURS:
                
                # A) Filtro Tendencia: Precio > SMA 200
                trend_ok = c > row.sma_trend
                
                # B) Trigger: RSI(2) < 10 (Sobreventa profunda)
                # Usamos rsi_prev para decidir al cierre de la vela anterior
                # Entramos en la APERTURA de esta vela
                oversold = row.rsi_prev < RSI_BUY_LEVEL
                
                if trend_ok and oversold:
                    
                    # --- EJECUCIÓN ---
                    base_entry = o
                    entry_price = base_entry * (1 + total_entry_cost)
                    
                    # Stop de Emergencia (Lejos)
                    sl_price = entry_price - (atr_prev * STOP_LOSS_ATR)
                    
                    # Sizing: % Fijo del Balance (Fixed Fractional)
                    risk_capital = balance * FIXED_RISK_PCT
                    # Usamos apalancamiento implícito (max 2x o 3x según config, pero controlado por %)
                    qty = risk_capital / entry_price 
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * entry_price * COMMISSION
                        balance -= entry_comm

                        position = "long"
                        entry = entry_price
                        sl = sl_price
                        quantity = qty
                        entry_time = ts
                        position_comm_paid = entry_comm
                        
                        trade_active_this_candle = True

                        # INTRA-CANDLE SL CHECK
                        if l <= sl:
                            exit_price = sl * (1 - slippage_pct)
                            pnl = (exit_price - entry) * qty
                            fee = exit_price * qty * COMMISSION
                            
                            balance += (pnl - fee)
                            net_pnl = pnl - entry_comm - fee
                            
                            trades.append({
                                "year": ts.year, "pnl": net_pnl, "type": "SL Intra"
                            })
                            position = None
                            position_comm_paid = 0

        # ============================================================
        # 2) GESTIÓN DE POSICIÓN
        # ============================================================
        if position == "long" and not trade_active_this_candle:
            
            exit_price = None
            reason = None

            # A) Salida Táctica: Cierre > SMA 5
            # Connors sale cuando el precio cruza la media corta. 
            # Esto suele pasar en 1 o 2 velas.
            # Verificamos si el CIERRE actual está por encima de la SMA5 actual
            if c > row.sma_exit:
                exit_price = c * (1 - slippage_pct)
                reason = "Target (SMA5)"

            # B) Stop Loss Emergencia
            elif l <= sl:
                exit_raw = o if o < sl else sl
                exit_price = exit_raw * (1 - slippage_pct)
                reason = "SL Emergency"

            # C) Time Exit
            elif (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600:
                exit_price = c * (1 - slippage_pct)
                reason = "Time"

            if exit_price:
                pnl = (exit_price - entry) * quantity
                exit_comm = exit_price * quantity * COMMISSION
                
                balance += (pnl - exit_comm)
                net = pnl - position_comm_paid - exit_comm

                trades.append({
                    "year": entry_time.year, "pnl": net, "type": reason
                })
                
                position = None
                position_comm_paid = 0
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
    print(f"📊 RESULTADOS FINALES V46 – CONNORS RSI-2: {symbol}")
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