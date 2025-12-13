#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🌪️ CONFIG V73 – BLACK SWAN HUNTER (FIXED)
# ======================================================

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "1000PEPEUSDT"]
# Prioridad de ejecución (UPGRADE 3)
PRIORITY_MAP = {
    "BTCUSDT": 1, 
    "ETHUSDT": 2, 
    "SOLUSDT": 3, 
    "BNBUSDT": 4, 
    "ADAUSDT": 5, 
    "1000PEPEUSDT": 6
}
TIMEFRAME_STR = "1h"

# ---- UPGRADE 1: CRASH + ACELERACIÓN ----
CRASH_WINDOW_24H = 24
DROP_THRESHOLD_24H = 0.10   # -10% en 24h
CRASH_WINDOW_6H = 6
ACCEL_THRESHOLD_6H = 0.05   # -5% en las últimas 6h (Velocidad)

# ---- UPGRADE 2: FILTRO DE RÉGIMEN ----
USE_REGIME_FILTER = True    # Solo operar si EMA50 Daily < EMA200 Daily

# ---- UPGRADE 3: CONCENTRACIÓN ----
MAX_ACTIVE_TRADES = 1       # Solo 1 bala a la vez
COOLDOWN_HOURS = 48         # Si operamos, esperar 48h para volver a buscar

# ---- UPGRADE 4: TP ESCALONADO ----
TP1_PCT = 0.05              # Take Profit 1 (+5%)
TP1_SIZE = 0.30             # Cerrar 30%
TP2_PCT = 0.10              # Take Profit 2 (+10%)
TP2_SIZE = 0.30             # Cerrar 30%
# El 40% restante queda con Trailing Stop

TRAILING_START_PCT = 0.02   # Activar trailing si ganamos 2%
TRAILING_DIST_PCT = 0.03    # Distancia del trailing

# ---- GESTIÓN DE RIESGO ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.01       # 1% Riesgo base
SL_FIXED_PCT = 0.05         # Stop Loss de Emergencia (5%)

# Costos
COMMISSION = 0.0004
SLIPPAGE = 0.001            

# ======================================================
#  1. PREPARACIÓN DE DATOS
# ======================================================
def prepare_data(symbol):
    candidates = [
        f"data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"data/{symbol}_{TIMEFRAME_STR}.csv",
        f"cpr_bot_v90/data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv"
    ]
    df = None
    for path in candidates:
        if os.path.exists(path):
            df = pd.read_csv(path)
            break
    if df is None: return None

    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    # --- FIX DE ZONA HORARIA ---
    if df['timestamp'].dt.tz is None: 
        df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: 
        df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
        
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- UPGRADE 2: RÉGIMEN DIARIO ---
    ohlc_1d = {'open':'first', 'high':'max', 'low':'min', 'close':'last'}
    df_1d = df.resample('1D').apply(ohlc_1d).dropna()
    df_1d['ema50_d'] = talib.EMA(df_1d['close'], 50)
    df_1d['ema200_d'] = talib.EMA(df_1d['close'], 200)
    df_1d['bear_regime'] = np.where(df_1d['ema50_d'] < df_1d['ema200_d'], 1, 0)
    
    # --- UPGRADE 1: SEÑAL HORARIA (CRASH + ACCEL) ---
    df['price_24h'] = df['close'].shift(CRASH_WINDOW_24H)
    df['drop_24h'] = (df['close'] - df['price_24h']) / df['price_24h']
    
    df['price_6h'] = df['close'].shift(CRASH_WINDOW_6H)
    df['drop_6h'] = (df['close'] - df['price_6h']) / df['price_6h']
    
    is_crash = (df['drop_24h'] < -DROP_THRESHOLD_24H)
    is_accel = (df['drop_6h'] < -ACCEL_THRESHOLD_6H)
    df['signal_raw'] = np.where(is_crash & is_accel, 1, 0)

    # --- MERGE ---
    df_merged = df.join(df_1d.shift(1)[['bear_regime']], rsuffix='_d')
    df_merged['bear_regime'] = df_merged['bear_regime'].fillna(0)
    
    if USE_REGIME_FILTER:
        df_merged['signal_final'] = np.where((df_merged['signal_raw']==1) & (df_merged['bear_regime']==1), 1, 0)
    else:
        df_merged['signal_final'] = df_merged['signal_raw']
        
    df_merged['symbol'] = symbol
    df_merged['priority'] = PRIORITY_MAP.get(symbol, 99)
    
    return df_merged.dropna()

# ======================================================
#  2. MOTOR DE BACKTEST (EVENT DRIVEN + PORTFOLIO)
# ======================================================
def run_backtest():
    print(f"\n🌪️ INICIANDO BACKTEST V73 (FINAL - TIMEZONE FIXED)")
    print(f"   Trigger: Drop > {DROP_THRESHOLD_24H*100}% (24h) AND > {ACCEL_THRESHOLD_6H*100}% (6h)")
    print(f"   Regime Filter: {'ACTIVADO' if USE_REGIME_FILTER else 'DESACTIVADO'}")
    print("="*60)

    all_data = []
    for s in SYMBOLS:
        d = prepare_data(s)
        if d is not None: all_data.append(d)
    
    if not all_data: return
    master_df = pd.concat(all_data).sort_index()
    timeline = master_df.groupby(level=0)

    balance = INITIAL_BALANCE
    positions = [] 
    trades_log = []
    
    # --- FIX CRÍTICO: INICIALIZAR EN UTC ---
    last_trade_time = pd.Timestamp("2000-01-01", tz="UTC") 

    for ts, group in timeline:
        
        # 1. GESTIÓN DE POSICIONES
        active_pos = []
        for pos in positions:
            sym = pos['symbol']
            if sym not in group['symbol'].values:
                active_pos.append(pos)
                continue
            
            row = group[group['symbol'] == sym].iloc[0]
            h, l, c = row['high'], row['low'], row['close']
            entry = pos['entry']
            
            # --- SL ---
            if h >= pos['sl']:
                exit_p = pos['sl'] * (1 + SLIPPAGE)
                remaining_qty = pos['current_qty']
                pnl = (entry - exit_p) * remaining_qty
                cost = exit_p * remaining_qty * COMMISSION
                balance += (pnl - cost + (entry * remaining_qty * COMMISSION))
                trades_log.append({'ts': ts, 'symbol': sym, 'pnl': pnl-cost, 'type': 'SL/Trail', 'year': ts.year})
                continue 
                
            # --- TPs ESCALONADOS ---
            profit_pct = (entry - l) / entry
            
            # TP1
            if not pos['tp1_done'] and profit_pct >= TP1_PCT:
                qty_close = pos['total_qty'] * TP1_SIZE
                exit_p = entry * (1 - TP1_PCT)
                pnl = (entry - exit_p) * qty_close
                cost = exit_p * qty_close * COMMISSION
                balance += (pnl - cost + (entry * qty_close * COMMISSION))
                pos['current_qty'] -= qty_close
                pos['tp1_done'] = True
                trades_log.append({'ts': ts, 'symbol': sym, 'pnl': pnl-cost, 'type': 'TP1', 'year': ts.year})
            
            # TP2
            if not pos['tp2_done'] and profit_pct >= TP2_PCT:
                qty_close = pos['total_qty'] * TP2_SIZE
                exit_p = entry * (1 - TP2_PCT)
                pnl = (entry - exit_p) * qty_close
                cost = exit_p * qty_close * COMMISSION
                balance += (pnl - cost + (entry * qty_close * COMMISSION))
                pos['current_qty'] -= qty_close
                pos['tp2_done'] = True
                trades_log.append({'ts': ts, 'symbol': sym, 'pnl': pnl-cost, 'type': 'TP2', 'year': ts.year})

            # --- TRAILING ---
            if profit_pct >= TRAILING_START_PCT:
                new_sl = l * (1 + TRAILING_DIST_PCT)
                if new_sl < pos['sl']:
                    pos['sl'] = new_sl
            
            if pos['current_qty'] > 0:
                active_pos.append(pos)
        
        positions = active_pos

        # 2. ENTRADAS
        if len(positions) >= MAX_ACTIVE_TRADES: continue
        
        # Cooldown check
        if ts < last_trade_time + pd.Timedelta(hours=COOLDOWN_HOURS): continue

        candidates = group[group['signal_final'] == 1]
        
        if not candidates.empty:
            candidates = candidates.sort_values('priority')
            best_pick = candidates.iloc[0]
            
            sym = best_pick['symbol']
            price = best_pick['open'] * (1 - SLIPPAGE)
            
            sl_price = price * (1 + SL_FIXED_PCT)
            dist = sl_price - price
            risk_amt = balance * FIXED_RISK_PCT
            
            qty = risk_amt / dist
            if (qty * price) > balance: qty = balance / price

            cost = qty * price * COMMISSION
            balance -= cost
            
            positions.append({
                'symbol': sym,
                'entry': price,
                'total_qty': qty,
                'current_qty': qty,
                'sl': sl_price,
                'tp1_done': False,
                'tp2_done': False
            })
            
            last_trade_time = ts

    # --- REPORTE ---
    print("\n" + "="*60)
    print(f"💰 Balance Final:   ${balance:.2f}")
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    print(f"🚀 Retorno Total:   {total_ret:.2f}%")
    
    if trades_log:
        df_t = pd.DataFrame(trades_log)
        print("\n📅 RENDIMIENTO ANUAL (CRASH BOT V73):")
        annual = df_t.groupby('year')['pnl'].sum()
        count = df_t.groupby('year')['pnl'].count()
        print(pd.concat([annual, count], axis=1, keys=['PnL', 'Events']))

if __name__ == "__main__":
    run_backtest()