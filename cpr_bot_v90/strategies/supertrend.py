#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V56 – TREND & DIP (HYBRID 4H/1H)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Filtro Macro (El Jefe - 4H) ----
FAST_EMA_4H = 50
SLOW_EMA_4H = 200

# ---- Gatillo Micro (El Obrero - 1H) ----
RSI_PERIOD = 14
RSI_OVERSOLD = 35       # Comprar el dip
RSI_OVERBOUGHT = 75     # Vender la euforia (Take Profit dinámico)

# ---- Salidas de Emergencia ----
SL_ATR_MULT = 2.5       # Stop Loss amplio (por si el dip sigue cayendo)

# ---- Risk & Microestructura (Motor V55) ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.04   # 4% (Un poco menos que V55 porque habrá más frecuencia)
MAX_LEVER = 10          

COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001
MIN_QTY = 0.01

# ======================================================
#  1. CARGA Y PREPARACIÓN DE DATOS DUALES
# ======================================================

def load_and_prepare(symbol):
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

    # --- A. INDICADORES EN 1H (Micro) ---
    print("📐 Calculando indicadores 1H...")
    df['rsi'] = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
    # Pre-cálculos para evitar lookahead en 1H
    df['rsi_prev'] = df['rsi'].shift(1)
    df['atr_prev'] = df['atr'].shift(1)

    # --- B. INDICADORES EN 4H (Macro) ---
    print("🔄 Generando contexto 4H...")
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last'}
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA_4H)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA_4H)
    
    # Definir Tendencia en 4H (1 = Alcista, 0 = Bajista/Neutro)
    df_4h['trend_ok'] = np.where(df_4h['ema_fast'] > df_4h['ema_slow'], 1, 0)
    
    # SHIFT(1) OBLIGATORIO:
    # A las 13:00 (1H) solo conocemos la tendencia de la vela 4H que cerró a las 12:00.
    df_4h_shifted = df_4h.shift(1)

    # --- C. FUSIÓN ---
    print("🔗 Fusionando Timeframes...")
    # Unimos la tendencia 4H a cada vela de 1H
    df_final = df.join(df_4h_shifted[['trend_ok']], rsuffix='_4h')
    
    # Rellenamos los huecos (Forward Fill)
    # Si a las 12:00 la tendencia era alcista, a las 13, 14 y 15 sigue siéndolo hasta nueva orden.
    df_final['trend_ok'] = df_final['trend_ok'].ffill()
    
    df_final.dropna(inplace=True)
    df_final.reset_index(inplace=True)
    
    return df_final

# ======================================================
#  🚀 BACKTEST ENGINE V56
# ======================================================

def run_backtest(symbol):
    df = load_and_prepare(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V56 (Trend 4H + Dip 1H) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance 

    position = None 
    entry_price = 0; quantity = 0; sl = 0
    entry_comm = 0
    
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Datos Híbridos
        trend_4h_bullish = row.trend_ok == 1.0  # Contexto Macro
        rsi_val = row.rsi_prev                  # Trigger Micro (Vela cerrada anterior)
        atr = row.atr_prev
        
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # ----------------------------------------------------
        # 1. ENTRADA (BUY THE DIP IN UPTREND)
        # ----------------------------------------------------
        if position is None:
            # Filtro 1: Tendencia 4H debe ser alcista
            # Filtro 2: RSI 1H debe estar sobrevendido (Dip)
            if trend_4h_bullish and (rsi_val < RSI_OVERSOLD):
                
                entry_price = o * (1 + friction)
                
                # SL de Emergencia (Basado en ATR 1H)
                sl_price = entry_price - (atr * SL_ATR_MULT)
                risk_dist = entry_price - sl_price
                
                if risk_dist > 0:
                    # Sizing: Compound on Peak (La magia de la V55)
                    risk_usd = peak_balance * FIXED_RISK_PCT
                    qty = risk_usd / risk_dist
                    
                    max_qty = (balance * MAX_LEVER) / entry_price
                    qty = min(qty, max_qty)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * entry_price * COMMISSION
                        balance -= entry_comm
                        
                        position = "long"
                        quantity = qty
                        sl = sl_price
                        entry = entry_price
                        entry_comm_paid = entry_comm
                        
                        # Intra-candle Check
                        if l <= sl:
                            exit_p = sl * (1 - SLIPPAGE_PCT)
                            pnl = (exit_p - entry_price) * qty
                            fee = exit_p * qty * COMMISSION
                            balance += (pnl - fee)
                            
                            net = pnl - entry_comm - fee
                            trades.append({'year': ts.year, 'pnl': net, 'type': 'SL Intra'})
                            position = None

        # ----------------------------------------------------
        # 2. GESTIÓN (SALIDA POR RSI ALTO O STOP)
        # ----------------------------------------------------
        elif position == "long":
            exit_p = None
            reason = None
            
            # A) Take Profit Dinámico: RSI Sobrecomprado (Rebote completado)
            # Usamos el RSI actual (calculado al cierre, pero simulamos salida al cierre)
            # Para ser realistas en backtest, si RSI[i-1] > 75, salimos en Open[i].
            # Aquí evaluamos rsi_val (que es rsi_prev). Si al abrir la vela, el RSI previo era alto -> Vender.
            if rsi_val > RSI_OVERBOUGHT:
                exit_p = o * (1 - SLIPPAGE_PCT)
                reason = "TP (RSI High)"
            
            # B) Tendencia 4H rota (Failsafe)
            # Si el jefe dice que la fiesta acabó, nos vamos.
            elif not trend_4h_bullish:
                exit_p = o * (1 - SLIPPAGE_PCT)
                reason = "Trend 4H Broken"

            # C) Stop Loss Hard
            elif l <= sl:
                exit_p = sl * (1 - SLIPPAGE_PCT)
                reason = "Stop Loss"
            
            if exit_p:
                pnl = (exit_p - entry) * quantity
                exit_comm = exit_p * quantity * COMMISSION
                balance += (pnl - exit_comm)
                
                # Compounding: Actualizar Peak solo si ganamos
                if balance > peak_balance: peak_balance = balance
                
                net_pnl = pnl - entry_comm_paid - exit_comm
                trades.append({'year': ts.year, 'pnl': net_pnl, 'type': reason})
                position = None
        
        equity_curve.append(balance)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V56 – TREND & DIP (HYBRID): {symbol}")
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
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)