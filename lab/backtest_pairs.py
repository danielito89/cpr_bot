#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  ⚖️ CONFIG V4 – PAIR TRADING (POLISHED)
# ======================================================

ASSET_A = "ETHUSDT" 
ASSET_B = "BTCUSDT" 
TIMEFRAME_STR = "1h"

# ---- CUANTITATIVO ----
OLS_WINDOW = 480        
Z_ENTRY = 2.0           
Z_EXIT = 0.3            
Z_STOP = 4.0            

# ---- FILTROS (V4: ADX + SLOPE) ----
USE_ADX_FILTER = True
ADX_THRESHOLD = 30
# V4: Solo bloqueamos si ADX es alto Y sigue subiendo (tendencia viva)      

# ---- GESTIÓN ----
MAX_HOLD_HOURS = 120    
TARGET_SPREAD_VOL = 0.01 

# ---- COSTOS ----
INITIAL_BALANCE = 10000
LEVERAGE = 1            
EST_FUNDING_RATE = 0.0001 # 0.01% / 8h
COMMISSION = 0.0004
SLIPPAGE = 0.0006

# ======================================================
#  1. PREPARACIÓN DE DATOS
# ======================================================
def prepare_data():
    print(f"🔄 Sincronizando {ASSET_A} vs {ASSET_B}...")
    
    def load_csv(sym):
        paths = [f"../data/mainnet_data_{TIMEFRAME_STR}_{sym}.csv", f"../data/{sym}_{TIMEFRAME_STR}.csv"]
        for p in paths:
            if os.path.exists(p): return pd.read_csv(p)
        return None

    df_a = load_csv(ASSET_A)
    df_b = load_csv(ASSET_B)
    
    if df_a is None or df_b is None: return None

    for df in [df_a, df_b]:
        df.columns = [c.lower() for c in df.columns]
        col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
        df.rename(columns=col_map, inplace=True)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
        else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
        df.set_index('timestamp', inplace=True)

    df = pd.merge(df_a[['close','high','low']], df_b[['close','high','low']], left_index=True, right_index=True, suffixes=('_A', '_B'))
    
    df['log_A'] = np.log(df['close_A'])
    df['log_B'] = np.log(df['close_B'])
    
    return df

# ======================================================
#  2. MÉTRICAS (V4: ADX SLOPE)
# ======================================================
def calculate_metrics(df):
    print("🧮 Calculando Métricas Pro...")
    
    # OLS Alpha/Beta
    rolling_cov = df['log_A'].rolling(window=OLS_WINDOW).cov(df['log_B'])
    rolling_var = df['log_B'].rolling(window=OLS_WINDOW).var()
    df['beta'] = rolling_cov / rolling_var
    
    mean_A = df['log_A'].rolling(window=OLS_WINDOW).mean()
    mean_B = df['log_B'].rolling(window=OLS_WINDOW).mean()
    df['alpha'] = mean_A - (df['beta'] * mean_B)
    
    df['spread'] = df['log_A'] - (df['alpha'] + df['beta'] * df['log_B'])
    
    # Z-Score
    mean_spread = df['spread'].rolling(window=OLS_WINDOW).mean()
    std_spread = df['spread'].rolling(window=OLS_WINDOW).std()
    df['z_score'] = (df['spread'] - mean_spread) / std_spread
    
    # Vol Scaling
    df['vol_scalar'] = TARGET_SPREAD_VOL / std_spread
    df['vol_scalar'] = df['vol_scalar'].replace([np.inf, -np.inf], 0).fillna(0)
    df['vol_scalar'] = np.where(df['vol_scalar'] > 1.0, 1.0, df['vol_scalar'])
    
    # V4 UPGRADE: ADX + Slope
    df['adx'] = talib.ADX(df['high_B'], df['low_B'], df['close_B'], timeperiod=14)
    df['adx_slope'] = df['adx'].diff() # Pendiente del ADX
    
    return df.dropna()

# ======================================================
#  3. MOTOR DE BACKTEST V4
# ======================================================
def run_backtest():
    df = prepare_data()
    if df is None: return
    df = calculate_metrics(df)
    
    print(f"\n⚖️ INICIANDO BACKTEST V4 ({ASSET_A} / {ASSET_B})")
    print(f"   Fixes: Funding Acumulado | ADX Slope | Logic Clean")
    print("="*60)
    
    balance = INITIAL_BALANCE
    equity_curve = [] 
    trades = []
    
    # Estado
    pos_dir = 0       
    qty_a = 0; qty_b = 0
    entry_val_a = 0; entry_val_b = 0
    entry_ts = None   
    
    # V4: Acumulador de funding por trade
    accumulated_funding = 0.0
    
    friction = COMMISSION + SLIPPAGE

    for ts, row in df.iterrows():
        z = row['z_score']
        beta = row['beta']
        vol_scalar = row['vol_scalar']
        price_a = row['close_A']
        price_b = row['close_B']
        adx = row['adx']
        adx_slope = row['adx_slope']
        
        # --- A. MARK TO MARKET ---
        unrealized_pnl = 0
        if pos_dir != 0:
            val_a_now = qty_a * price_a
            val_b_now = qty_b * price_b
            
            if pos_dir == 1: # Long Ratio (L A / S B)
                pnl_a = val_a_now - entry_val_a
                pnl_b = entry_val_b - val_b_now 
            else: # Short Ratio (S A / L B)
                pnl_a = entry_val_a - val_a_now
                pnl_b = val_b_now - entry_val_b
            
            unrealized_pnl = pnl_a + pnl_b
            
            # V4 FIX: Acumular funding, NO restar del balance todavía
            step_funding = (val_a_now + val_b_now) * (EST_FUNDING_RATE / 8)
            accumulated_funding += step_funding
            
        # Equity = Balance Realizado + PnL Latente - Costos Latentes (Funding)
        current_equity = balance + unrealized_pnl - accumulated_funding
        equity_curve.append({'ts': ts, 'equity': current_equity})

        # --- B. TRADING LOGIC ---
        
        # 1. CIERRE
        should_close = False
        reason = ""
        
        if pos_dir != 0:
            hours_held = (ts - entry_ts).total_seconds() / 3600
            
            if abs(z) > Z_STOP:
                should_close = True; reason = "Hard Stop (Z)"
            elif hours_held > MAX_HOLD_HOURS:
                should_close = True; reason = "Time Stop"
            
            # V4 BUGFIX: Lógica limpia
            elif pos_dir == 1 and z >= -Z_EXIT:
                should_close = True; reason = "TP (Band)"
            elif pos_dir == -1 and z <= Z_EXIT:
                should_close = True; reason = "TP (Band)"

            if should_close:
                val_a_exit = qty_a * price_a
                val_b_exit = qty_b * price_b
                exit_cost = (val_a_exit + val_b_exit) * friction
                
                # V4 FIX: Descontar funding acumulado al cerrar
                net_pnl = unrealized_pnl - exit_cost - accumulated_funding
                balance += net_pnl
                
                trades.append({
                    'ts': ts, 'type': 'Close', 'reason': reason, 'pnl': net_pnl, 
                    'duration': hours_held, 'beta': beta, 'funding': accumulated_funding
                })
                
                pos_dir = 0; qty_a = 0; qty_b = 0
                entry_val_a = 0; entry_val_b = 0; entry_ts = None
                accumulated_funding = 0.0 # Reset
                continue

        # 2. APERTURA
        if pos_dir == 0:
            # V4 UPGRADE: Bloquear solo si ADX es alto Y la pendiente es positiva
            if USE_ADX_FILTER and adx > ADX_THRESHOLD and adx_slope > 0: 
                continue 
            
            # Sizing & Hedge Ratio (Igual que V3)
            base_size = balance * 0.40 * LEVERAGE
            risk_adjusted_size_a = base_size * vol_scalar
            
            target_val_a = risk_adjusted_size_a
            target_val_b = target_val_a * beta 
            
            if beta < 0.2 or beta > 3.0: continue
            if (target_val_a + target_val_b) > balance * 0.95: 
                ratio = (balance * 0.95) / (target_val_a + target_val_b)
                target_val_a *= ratio
                target_val_b *= ratio

            # Entry
            if z > Z_ENTRY: 
                pos_dir = -1
                qty_a = target_val_a / price_a; entry_val_a = target_val_a
                qty_b = target_val_b / price_b; entry_val_b = target_val_b
                balance -= (target_val_a + target_val_b) * friction
                entry_ts = ts

            elif z < -Z_ENTRY: 
                pos_dir = 1
                qty_a = target_val_a / price_a; entry_val_a = target_val_a
                qty_b = target_val_b / price_b; entry_val_b = target_val_b
                balance -= (target_val_a + target_val_b) * friction
                entry_ts = ts

    # --- REPORTE ---
    print("\n" + "="*60)
    print(f"💰 Balance Final:   ${balance:.2f}")
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    print(f"🚀 Retorno Total:   {total_ret:.2f}%")
    
    if trades:
        df_t = pd.DataFrame(trades)
        df_eq = pd.DataFrame(equity_curve).set_index('ts')
        
        peak = df_eq['equity'].cummax()
        dd = (df_eq['equity'] - peak) / peak
        max_dd = dd.min() * 100
        
        df_eq['ret'] = df_eq['equity'].pct_change()
        sharpe = (df_eq['ret'].mean() / df_eq['ret'].std()) * np.sqrt(365*24)
        
        avg_dur = df_t['duration'].mean()
        win_rate = (df_t['pnl'] > 0).mean() * 100
        total_funding = df_t['funding'].sum()
        
        print("-" * 30)
        print(f"📉 Max Drawdown:    {max_dd:.2f}%")
        print(f"📊 Sharpe Ratio:    {sharpe:.2f}")
        print(f"⏱️ Avg Duration:    {avg_dur:.1f} horas")
        print(f"🏆 Win Rate:        {win_rate:.2f}%")
        print(f"💸 Total Funding:   ${total_funding:.2f}")
        print("-" * 30)
        
        print("\n📅 PnL ANUAL:")
        df_t['year'] = df_t['ts'].dt.year
        print(df_t.groupby('year')['pnl'].sum())

if __name__ == "__main__":
    run_backtest()