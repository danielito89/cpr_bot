#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os

# Gestión segura de TA-Lib
try:
    import talib
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False
    print("❌ TA-Lib no está instalado. El script fallará.")

# ======================================================
#  🔥 CONFIG V54 – GOLDEN CROSS PLUS (4H)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia: TENDENCIA 4H ----
FAST_EMA = 50
SLOW_EMA = 200

# ---- Salidas (La Mejora) ----
# Usamos un Chandelier Exit / Supertrend como Trailing Stop
# Si el precio cae X ATRs desde el máximo, salimos.
TRAILING_ATR_PERIOD = 14
TRAILING_ATR_MULT = 3.5   # Le damos espacio (3.5 ATR) para no salir en ruidos

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.05     # 5% por trade (Agresivo porque hay pocos trades)
MAX_LEVER = 5             # Apalancamiento Swing

COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001
MIN_QTY = 0.01

# ======================================================
#  1. CARGA Y RESAMPLING
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

    # --- RESAMPLING A 4H (El filtro de oro) ---
    print("🔄 Resampleando a 4H para eliminar ruido...")
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    
    if not HAS_TALIB: return None

    # INDICADORES EN 4H
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=TRAILING_ATR_PERIOD)
    
    # SEÑALES (Calculadas sobre vela CERRADA)
    # 1. Tendencia Alcista
    df_4h['trend_up'] = np.where(df_4h['ema_fast'] > df_4h['ema_slow'], 1, 0)
    
    # 2. Cruce (Golden Cross) - Shift(1) para comparar hoy vs ayer
    df_4h['prev_trend_up'] = df_4h['trend_up'].shift(1)
    df_4h['signal_buy'] = np.where((df_4h['trend_up'] == 1) & (df_4h['prev_trend_up'] == 0), 1, 0)
    
    # 3. Salida Técnica (Death Cross)
    df_4h['signal_sell'] = np.where((df_4h['trend_up'] == 0) & (df_4h['prev_trend_up'] == 1), 1, 0)

    # --- FIX LOOKAHEAD: SHIFT(1) ---
    # Movemos todo 1 vela 4H adelante para que a las 12:00 veamos la data de las 08:00
    df_4h_shifted = df_4h.shift(1)

    print("🔄 Sincronizando con 1H...")
    df_1h = df.join(df_4h_shifted[['ema_fast', 'ema_slow', 'atr', 'signal_buy', 'signal_sell']], rsuffix='_4h')
    
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V54
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V54 (Golden Cross + Trailing) para {symbol}\n")

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
        
        # Datos 4H
        atr_4h = row.atr
        signal_buy = row.signal_buy == 1
        signal_sell = row.signal_sell == 1
        
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # ----------------------------------------------------
        # 1. ENTRADA (GOLDEN CROSS)
        # ----------------------------------------------------
        if position is None and signal_buy:
            
            entry_price = o * (1 + friction)
            
            # SL Inicial (Stop Loss de Volatilidad Amplio)
            # Usamos 3.5 ATR para darle mucho aire al inicio
            sl_price = entry_price - (atr_4h * TRAILING_ATR_MULT)
            
            risk_dist = entry_price - sl_price
            
            if risk_dist > 0:
                # Sizing Compuesto (Risk on Peak)
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
        # 2. GESTIÓN
        # ----------------------------------------------------
        elif position == "long":
            exit_p = None
            reason = None
            
            # A) TRAILING STOP (La Mejora) 
            # Si el precio sube, subimos el SL. Nunca lo bajamos.
            # El SL está a X ATRs del Máximo alcanzado (High - 3.5 ATR)
            # Pero usamos ATR de 4H que es más estable.
            new_sl = h - (atr_4h * TRAILING_ATR_MULT)
            if new_sl > sl:
                sl = new_sl
            
            # B) Stop Loss Hit (Trailing o Inicial)
            if l <= sl:
                # Gap protection
                exit_raw = o if o < sl else sl
                exit_p = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "Trailing Stop"
            
            # C) Salida Técnica (Death Cross)
            # Si ocurre el cruce bajista ANTES de tocar el trailing
            elif signal_sell:
                exit_p = o * (1 - SLIPPAGE_PCT)
                reason = "Death Cross"
            
            if exit_p:
                pnl = (exit_p - entry) * quantity
                exit_comm = exit_p * quantity * COMMISSION
                balance += (pnl - exit_comm)
                
                if balance > peak_balance: peak_balance = balance
                
                net_pnl = pnl - entry_comm_paid - exit_comm
                trades.append({'year': ts.year, 'pnl': net_pnl, 'type': reason})
                position = None
        
        equity_curve.append(balance)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V54 – GOLDEN CROSS PLUS (4H): {symbol}")
    print("="*55)
    print(f"💰 Balance Final:   ${balance:.2f}")
    print(f"📈 Retorno Total:   {total_return:.2f}%")
    
    eq_series = pd.Series(equity_curve)
    if len(eq_series) > 0:
        dd = (eq_series - eq_series.cummax()) / eq_series.cummax()
        print(f"📉 Max DD:          {dd.min()*100:.2f}%")

    if not trades_df.empty:
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:        {win:.2f}%")
        print(f"🧮 Total Trades:    {len(trades_df)}\n")
        print("📅 RENDIMIENTO POR AÑO:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)