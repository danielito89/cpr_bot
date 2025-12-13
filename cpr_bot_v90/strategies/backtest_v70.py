#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🐻 CONFIG V71 – BEAR HUNTER RELOADED
# ======================================================

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"]
TIMEFRAME_STR = "1h"

# ---- Parámetros Capa 1 (Régimen) ----
EMA_DAILY_PERIOD = 200
SLOPE_LOOKBACK = 5      # Comparar EMA de hoy con hace 5 días para ver pendiente

# ---- Parámetros Capa 2 (Volatilidad) ----
MIN_ATR_PCT = 1.5       # El ATR debe ser al menos el 1.5% del precio (Volatilidad Alta)

# ---- Parámetros Capa 3 (Trigger Breakdown) ----
BREAKDOWN_PERIOD = 20   # Romper el mínimo de las últimas 20 velas de 4H

# ---- Gestión ----
SL_ATR_MULT = 1.5       # Stop Loss ajustado
RISK_REWARD_RATIO = 1.5 # TP Fijo

# ---- Risk ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.015  # 1.5% Riesgo Conservador
MAX_LEVER = 3           

COMMISSION = 0.0004; SPREAD_PCT = 0.0004; SLIPPAGE_PCT = 0.0006; BASE_LATENCY = 0.0001; MIN_QTY = 0.01

# ======================================================
#  CARGA Y PROCESAMIENTO
# ======================================================
def load_data(symbol):
    candidates = [
        f"data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"data/{symbol}_{TIMEFRAME_STR}.csv",
        f"cpr_bot_v90/data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv"
    ]
    df = None
    for path in candidates:
        if os.path.exists(path):
            print(f"   📄 {symbol}: Cargando...")
            df = pd.read_csv(path)
            break
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
    return df

def process_symbol(symbol):
    df = load_data(symbol)
    if df is None: return None

    # --- A. CAPA 1: RÉGIMEN DIARIO (SLOPE & PRICE) ---
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_1d = df.resample('1D').apply(ohlc_dict).dropna()
    
    df_1d['ema200'] = talib.EMA(df_1d['close'], timeperiod=EMA_DAILY_PERIOD)
    
    # Pendiente: EMA Hoy < EMA hace 5 días
    df_1d['ema_falling'] = np.where(df_1d['ema200'] < df_1d['ema200'].shift(SLOPE_LOOKBACK), 1, 0)
    
    # Precio bajo EMA
    df_1d['price_below_ema'] = np.where(df_1d['close'] < df_1d['ema200'], 1, 0)
    
    # Régimen BEAR = Precio Abajo + EMA Cayendo
    df_1d['bear_regime'] = np.where((df_1d['ema_falling']==1) & (df_1d['price_below_ema']==1), 1, 0)
    
    df_1d_shifted = df_1d.shift(1)[['bear_regime']]

    # --- B. CAPA 2 & 3: TACTICAL 4H (VOLATILIDAD & BREAKDOWN) ---
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Filtro Volatilidad Relativa (%)
    df_4h['atr_pct'] = (df_4h['atr'] / df_4h['close']) * 100
    df_4h['vol_ok'] = np.where(df_4h['atr_pct'] > MIN_ATR_PCT, 1, 0)
    
    # Trigger Breakdown: Close < Lowest Low de N periodos previos
    # Shift(1) en el rolling para no incluir la vela actual en el cálculo del mínimo previo
    df_4h['lowest_low'] = df_4h['low'].rolling(window=BREAKDOWN_PERIOD).min().shift(1)
    
    # Señal: Close actual rompe el Lowest Low previo
    df_4h['breakdown'] = np.where(df_4h['close'] < df_4h['lowest_low'], 1, 0)
    
    # Shift 4H (Anti-Lookahead) - Traemos la señal cerrada
    cols_4h = ['breakdown', 'vol_ok', 'atr', 'high'] 
    df_4h_shifted = df_4h.shift(1)[cols_4h].rename(columns={'high': 'prev_high'})

    # --- C. MERGE ---
    df_merged = df.join(df_1d_shifted, rsuffix='_1d')
    df_merged = df_merged.join(df_4h_shifted, rsuffix='_4h')
    df_merged.ffill(inplace=True)
    df_merged.dropna(inplace=True)
    df_merged.reset_index(inplace=True)
    
    return df_merged

# ======================================================
#  🚀 BACKTEST ENGINE (SHORT ONLY)
# ======================================================
def backtest_symbol(symbol, df):
    balance = INITIAL_BALANCE
    position = None 
    entry_price = 0; quantity = 0; sl = 0; tp = 0; entry_comm = 0
    trades = []
    
    friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Condiciones
        regime_ok = row.bear_regime == 1
        vol_ok = row.vol_ok == 1
        signal = row.breakdown == 1
        
        # --- ENTRADA ---
        if position is None:
            if regime_ok and vol_ok and signal:
                
                real_entry = o * (1 - friction)
                atr = row.atr
                
                # SL: Por encima del High de la vela de ruptura (Swing High local) + Buffer
                sl_price = row.prev_high + (atr * 0.5) 
                if sl_price <= real_entry: sl_price = real_entry + atr # Safety
                
                risk_dist = sl_price - real_entry
                tp_dist = risk_dist * RISK_REWARD_RATIO
                tp_price = real_entry - tp_dist
                
                if risk_dist > 0:
                    risk_usd = balance * FIXED_RISK_PCT
                    qty = min(risk_usd / risk_dist, (balance * MAX_LEVER) / real_entry)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * real_entry * COMMISSION
                        balance -= entry_comm
                        position = 'short'
                        quantity = qty
                        sl = sl_price
                        tp = tp_price
                        entry_price = real_entry
                        
                        # Instant Crash Check
                        if h >= sl: # Squeeze en la misma vela de entrada
                            exit_p = sl * (1 + SLIPPAGE_PCT)
                            pnl = (entry_price - exit_p) * qty
                            fee = exit_p * qty * COMMISSION
                            balance += (pnl - fee)
                            trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm-fee, 'type': 'SL Instant'})
                            position = None

        # --- GESTIÓN ---
        elif position == "short":
            exit_p = None
            reason = None
            
            if h >= sl:
                exit_p = sl * (1 + SLIPPAGE_PCT)
                reason = "SL"
            elif l <= tp:
                exit_p = tp * (1 - SLIPPAGE_PCT)
                reason = "TP Target"
            
            if exit_p:
                pnl = (entry_price - exit_p) * quantity
                fee = exit_p * quantity * COMMISSION
                balance += (pnl - fee)
                trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm-fee, 'type': reason})
                position = None
        
    return trades, balance

# ======================================================
#  RUNNER
# ======================================================
def run_portfolio():
    print(f"\n🐻 V71 BEAR HUNTER RELOADED (Slope + Breakdown + VolFilter)")
    print(f"   Regime: EMA200 Falling + Price Below")
    print(f"   Filter: ATR% > {MIN_ATR_PCT}%")
    print(f"   Trigger: Breakdown 20-Period Low")
    print("="*60)
    
    all_trades = []
    final_balances = {}
    
    for symbol in SYMBOLS:
        df = process_symbol(symbol)
        if df is not None:
            trades, final_bal = backtest_symbol(symbol, df)
            all_trades.extend(trades)
            final_balances[symbol] = final_bal
            
    # Reporte
    initial_total = INITIAL_BALANCE * len(final_balances)
    final_total = sum(final_balances.values())
    total_ret = (final_total - initial_total) / initial_total * 100
    
    print("\n" + "="*60)
    print(f"💰 Inicial:   ${initial_total:.2f}")
    print(f"💰 Final:     ${final_total:.2f}")
    print(f"🚀 Retorno:   {total_ret:.2f}%")
    print("="*60)
    
    if all_trades:
        trades_df = pd.DataFrame(all_trades)
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:  {win:.2f}%")
        print(f"🧮 Trades:    {len(trades_df)}")
        print("\n📅 RENDIMIENTO ANUAL (Crucial: Ver 2022):")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        
        print("\n🏅 POR ACTIVO:")
        for sym, bal in final_balances.items():
            ret = (bal - INITIAL_BALANCE)/INITIAL_BALANCE*100
            print(f"   {sym}: {ret:6.2f}%")

if __name__ == "__main__":
    run_portfolio()