#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V61 – HYBRID HYDRA (COMBINED STRATEGY)
# ======================================================

SYMBOL = "ETHUSDT" # <--- CAMBIA ESTO PARA PROBAR OTROS PARES
TIMEFRAME_STR = "1h"

# ---- Asignación de Capital ----
CAPITAL_ALLOCATION_A = 0.5  # 50% para Golden (Long Only)
CAPITAL_ALLOCATION_B = 0.5  # 50% para Silver (Long & Short)

# ---- Estrategia A: GOLDEN CROSS (Long Only) ----
EMA_A_FAST = 50
EMA_A_SLOW = 200
SL_ATR_A = 3.0

# ---- Estrategia B: SILVER BULLET (Long & Short) ----
EMA_B_FAST = 21
EMA_B_SLOW = 55
SL_ATR_B = 2.5

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.05   # Riesgo por trade (aplicado a la sub-cuenta asignada)
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
    
    # --- INDICADORES ---
    # Para Estrategia A
    df_4h['ema_50'] = talib.EMA(df_4h['close'], timeperiod=EMA_A_FAST)
    df_4h['ema_200'] = talib.EMA(df_4h['close'], timeperiod=EMA_A_SLOW)
    
    # Para Estrategia B
    df_4h['ema_21'] = talib.EMA(df_4h['close'], timeperiod=EMA_B_FAST)
    df_4h['ema_55'] = talib.EMA(df_4h['close'], timeperiod=EMA_B_SLOW)
    
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # --- SEÑALES (A) ---
    # 1 = Bull, -1 = Bear
    df_4h['trend_a'] = np.where(df_4h['ema_50'] > df_4h['ema_200'], 1, -1)
    df_4h['prev_trend_a'] = df_4h['trend_a'].shift(1)
    # Solo compramos cruce alcista, vendemos cruce bajista (pero no shorteamos)
    df_4h['sig_a'] = np.where((df_4h['trend_a']==1) & (df_4h['prev_trend_a']==-1), 1, 
                              np.where((df_4h['trend_a']==-1) & (df_4h['prev_trend_a']==1), -1, 0))

    # --- SEÑALES (B) ---
    df_4h['trend_b'] = np.where(df_4h['ema_21'] > df_4h['ema_55'], 1, -1)
    df_4h['prev_trend_b'] = df_4h['trend_b'].shift(1)
    # Aquí operamos ambos lados: 1 = Go Long, -1 = Go Short
    df_4h['sig_b'] = np.where((df_4h['trend_b']==1) & (df_4h['prev_trend_b']==-1), 1, 
                              np.where((df_4h['trend_b']==-1) & (df_4h['prev_trend_b']==1), -1, 0))

    # SHIFT(1) Anti-Lookahead
    df_4h_shifted = df_4h.shift(1)

    print("🔄 Sincronizando con 1H...")
    cols = ['atr', 'sig_a', 'sig_b']
    df_1h = df.join(df_4h_shifted[cols], rsuffix='_4h')
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V61 (HYBRID)
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V61 (Hybrid Hydra) para {symbol}\n")

    # Inicializamos dos "sub-cuentas" virtuales
    bal_a = INITIAL_BALANCE * CAPITAL_ALLOCATION_A
    bal_b = INITIAL_BALANCE * CAPITAL_ALLOCATION_B
    
    peak_a = bal_a
    peak_b = bal_b
    
    # Estado Estrategia A (Golden - Long Only)
    pos_a = None; entry_a = 0; qty_a = 0; sl_a = 0
    
    # Estado Estrategia B (Silver - Long/Short)
    pos_b = None; entry_b = 0; qty_b = 0; sl_b = 0
    
    equity_curve = []
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        atr = row.atr
        
        # Señales (-1, 0, 1)
        sig_a = row.sig_a
        sig_b = row.sig_b
        
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # =========================================================
        #  ESTRATEGIA A: GOLDEN CROSS (LONG ONLY)
        # =========================================================
        # Salida
        exit_p_a = None
        if pos_a == 'long':
            # Death Cross o SL
            if sig_a == -1: exit_p_a = o * (1 - SLIPPAGE_PCT); reason = "Death Cross (A)"
            elif l <= sl_a: exit_p_a = sl_a * (1 - SLIPPAGE_PCT); reason = "SL (A)"
            
            if exit_p_a:
                pnl = (exit_p_a - entry_a) * qty_a
                comm = exit_p_a * qty_a * COMMISSION
                bal_a += (pnl - comm)
                if bal_a > peak_a: peak_a = bal_a
                trades.append({'year': ts.year, 'pnl': pnl-comm, 'strat': 'A', 'type': reason})
                pos_a = None

        # Entrada
        if pos_a is None and sig_a == 1:
            real_entry = o * (1 + friction)
            sl_price = real_entry - (atr * SL_ATR_A)
            dist = real_entry - sl_price
            if dist > 0:
                risk_usd = peak_a * FIXED_RISK_PCT
                qty = min(risk_usd/dist, (bal_a * MAX_LEVER)/real_entry)
                if qty >= MIN_QTY:
                    cost = qty * real_entry * COMMISSION
                    bal_a -= cost
                    pos_a = 'long'; entry_a = real_entry; qty_a = qty; sl_a = sl_price
                    # Check intra-candle
                    if l <= sl_a:
                        pnl = (sl_a*(1-SLIPPAGE_PCT) - real_entry)*qty
                        bal_a += pnl
                        trades.append({'year': ts.year, 'pnl': pnl-cost, 'strat': 'A', 'type': 'SL Intra'})
                        pos_a = None

        # =========================================================
        #  ESTRATEGIA B: SILVER BULLET (LONG & SHORT)
        # =========================================================
        # Salida / Reversal
        exit_p_b = None
        
        if pos_b == 'long':
            # Reverse signal (-1) o SL
            if sig_b == -1: exit_p_b = o * (1 - SLIPPAGE_PCT); reason = "Flip Short (B)"
            elif l <= sl_b: exit_p_b = sl_b * (1 - SLIPPAGE_PCT); reason = "SL Long (B)"
            
            if exit_p_b:
                pnl = (exit_p_b - entry_b) * qty_b
                comm = exit_p_b * qty_b * COMMISSION
                bal_b += (pnl - comm)
                if bal_b > peak_b: peak_b = bal_b
                trades.append({'year': ts.year, 'pnl': pnl-comm, 'strat': 'B', 'type': reason})
                pos_b = None # (Se abrirá short abajo si era flip)

        elif pos_b == 'short':
            # Reverse signal (1) o SL
            if sig_b == 1: exit_p_b = o * (1 + SLIPPAGE_PCT); reason = "Flip Long (B)"
            elif h >= sl_b: exit_p_b = sl_b * (1 + SLIPPAGE_PCT); reason = "SL Short (B)"
            
            if exit_p_b:
                pnl = (entry_b - exit_p_b) * qty_b
                comm = exit_p_b * qty_b * COMMISSION
                bal_b += (pnl - comm)
                if bal_b > peak_b: peak_b = bal_b
                trades.append({'year': ts.year, 'pnl': pnl-comm, 'strat': 'B', 'type': reason})
                pos_b = None

        # Entrada (Si no hay pos o acabamos de cerrar)
        if pos_b is None:
            new_side = None
            if sig_b == 1: new_side = 'long'
            elif sig_b == -1: new_side = 'short'
            
            if new_side:
                if new_side == 'long':
                    real_entry = o * (1 + friction)
                    sl_price = real_entry - (atr * SL_ATR_B)
                else:
                    real_entry = o * (1 - friction)
                    sl_price = real_entry + (atr * SL_ATR_B)
                
                dist = abs(real_entry - sl_price)
                if dist > 0:
                    risk_usd = peak_b * FIXED_RISK_PCT
                    qty = min(risk_usd/dist, (bal_b * MAX_LEVER)/real_entry)
                    
                    if qty >= MIN_QTY:
                        cost = qty * real_entry * COMMISSION
                        bal_b -= cost
                        pos_b = new_side; entry_b = real_entry; qty_b = qty; sl_b = sl_price
                        
                        # Intra-candle
                        crash = False
                        if new_side == 'long' and l <= sl_b: crash = True
                        if new_side == 'short' and h >= sl_b: crash = True
                        
                        if crash:
                            loss_p = sl_b * (1 - SLIPPAGE_PCT) if new_side=='long' else sl_b * (1 + SLIPPAGE_PCT)
                            pnl = (loss_p - entry_b)*qty if new_side=='long' else (entry_b - loss_p)*qty
                            bal_b += pnl
                            trades.append({'year': ts.year, 'pnl': pnl-cost, 'strat': 'B', 'type': 'SL Intra'})
                            pos_b = None

        # =========================================================
        #  EQUITY TOTAL
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
    print(f"📊 RESULTADOS V61 – HYBRID HYDRA (A+B): {symbol}")
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
        print("="*55)
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)