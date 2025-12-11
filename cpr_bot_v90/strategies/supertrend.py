#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V55 – GOLDEN COMPOUND (FINAL)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia: TENDENCIA PURA 4H ----
FAST_EMA = 50
SLOW_EMA = 200

# ---- Salidas ----
# Volvemos a la salida por "Death Cross" (Cruce bajista)
# Es lenta, pero es la única que captura el 100% del bull run.
SL_ATR_MULT = 3.0       # Solo stop de catástrofe

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.05   # 5% por trade (Agresivo porque confiamos en la tendencia 4H)
MAX_LEVER = 5           

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

    # --- RESAMPLING A 4H ---
    print("🔄 Resampleando a 4H...")
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    
    # INDICADORES EN 4H
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # SEÑALES (Sobre vela CERRADA)
    # Trend = 1 si Fast > Slow
    df_4h['trend_up'] = np.where(df_4h['ema_fast'] > df_4h['ema_slow'], 1, 0)
    
    # Cruces (Shift 1 para comparar con vela anterior)
    df_4h['prev_trend'] = df_4h['trend_up'].shift(1)
    
    # Golden Cross: Hoy es 1, Ayer era 0
    df_4h['signal_buy'] = np.where((df_4h['trend_up'] == 1) & (df_4h['prev_trend'] == 0), 1, 0)
    
    # Death Cross: Hoy es 0, Ayer era 1
    df_4h['signal_sell'] = np.where((df_4h['trend_up'] == 0) & (df_4h['prev_trend'] == 1), 1, 0)

    # SHIFT(1) para evitar Lookahead Bias (Ver futuro)
    df_4h_shifted = df_4h.shift(1)

    print("🔄 Sincronizando con 1H...")
    df_1h = df.join(df_4h_shifted[['ema_fast', 'ema_slow', 'atr', 'signal_buy', 'signal_sell']], rsuffix='_4h')
    
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V55
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V55 (Golden Compound) para {symbol}\n")

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
            
            # SL Inicial (Catastrófico)
            sl_price = entry_price - (atr_4h * SL_ATR_MULT)
            risk_dist = entry_price - sl_price
            
            if risk_dist > 0:
                # SIZING AGRESIVO (Compound on Peak)
                # Usamos peak_balance para calcular el riesgo en USD.
                # Esto acelera el crecimiento en rachas ganadoras.
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
                    
                    # Intra-candle Crash Check
                    if l <= sl:
                        exit_p = sl * (1 - SLIPPAGE_PCT)
                        pnl = (exit_p - entry_price) * qty
                        fee = exit_p * qty * COMMISSION
                        balance += (pnl - fee)
                        net = pnl - entry_comm - fee
                        trades.append({'year': ts.year, 'pnl': net, 'type': 'SL Crash'})
                        position = None

        # ----------------------------------------------------
        # 2. GESTIÓN
        # ----------------------------------------------------
        elif position == "long":
            exit_p = None
            reason = None
            
            # A) Salida Técnica: DEATH CROSS
            # Esperamos pacientemente a que la tendencia cambie en 4H.
            if signal_sell:
                exit_p = o * (1 - SLIPPAGE_PCT)
                reason = "Death Cross"
            
            # B) Stop Loss Hit (Emergencia)
            elif l <= sl:
                exit_p = sl * (1 - SLIPPAGE_PCT)
                reason = "Stop Loss"
            
            if exit_p:
                pnl = (exit_p - entry) * quantity
                exit_comm = exit_p * quantity * COMMISSION
                balance += (pnl - exit_comm)
                
                # Actualizamos Peak Balance solo si ganamos
                if balance > peak_balance: peak_balance = balance
                
                net_pnl = pnl - entry_comm_paid - exit_comm
                trades.append({'year': ts.year, 'pnl': net_pnl, 'type': reason})
                position = None
        
        equity_curve.append(balance)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V55 – GOLDEN COMPOUND: {symbol}")
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
        print(f"🧮 Total Trades:    {len(trades_df)}\n")
        print("📅 RENDIMIENTO POR AÑO:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        print("="*55)
        
        # Guardar historial para análisis
        trades_df.to_csv("golden_cross_log.csv", index=False)
        print("💾 Log guardado en golden_cross_log.csv")
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)