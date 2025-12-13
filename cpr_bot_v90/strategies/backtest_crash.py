#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🌪️ CONFIG V75 – THE SURGEON (FINAL SHORT)
# ======================================================

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "1000PEPEUSDT"]
PRIORITY_MAP = {"BTCUSDT": 1, "ETHUSDT": 2, "SOLUSDT": 3, "BNBUSDT": 4, "ADAUSDT": 5, "1000PEPEUSDT": 6}
TIMEFRAME_STR = "1h"

# ---- TRIGGER (CRASH + ACCEL) ----
CRASH_WINDOW_24H = 24
DROP_THRESHOLD_24H = 0.08   # 8% (Sensible)
CRASH_WINDOW_6H = 6
ACCEL_THRESHOLD_6H = 0.05   # 5% (Rápido)

# ---- FILTROS ----
USE_REGIME_FILTER = True    # EMA50 < EMA200 Daily
MAX_ACTIVE_TRADES = 1       # Sniper (1 bala)
COOLDOWN_HOURS = 48         

# ---- GESTIÓN DE SALIDA (SCALING OUT) ----
TP1_PCT = 0.06              # TP1: +6%
TP1_SIZE = 0.40             # Cerrar 40%
TP2_PCT = 0.12              # TP2: +12%
TP2_SIZE = 0.30             # Cerrar 30%
# 30% restante: Trailing Stop

# ---- PULIDO 4: TRAILING INTELIGENTE ----
TRAILING_START_PCT = 0.03   # Activar si ganamos 3%
TRAILING_DIST_PCT = 0.03    # Distancia 3%

# ---- PULIDO 2: SL DINÁMICO ----
SL_ATR_MULT = 2.0           # SL = Entry + 2*ATR

# ---- RIESGO ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.025      # 2.5% Riesgo por evento
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
    
    # Timezone fix
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
        
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)

    # Indicadores
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], 14)
    
    # PULIDO 1: Necesitamos el Low previo para el Breakout
    df['prev_low'] = df['low'].shift(1)

    # Regime
    ohlc_1d = {'open':'first', 'high':'max', 'low':'min', 'close':'last'}
    df_1d = df.resample('1D').apply(ohlc_1d).dropna()
    df_1d['ema50_d'] = talib.EMA(df_1d['close'], 50)
    df_1d['ema200_d'] = talib.EMA(df_1d['close'], 200)
    df_1d['bear_regime'] = np.where(df_1d['ema50_d'] < df_1d['ema200_d'], 1, 0)
    
    # Trigger Crash
    df['drop_24h'] = (df['close'] - df['close'].shift(CRASH_WINDOW_24H)) / df['close'].shift(CRASH_WINDOW_24H)
    df['drop_6h'] = (df['close'] - df['close'].shift(CRASH_WINDOW_6H)) / df['close'].shift(CRASH_WINDOW_6H)
    
    is_crash = (df['drop_24h'] < -DROP_THRESHOLD_24H)
    is_accel = (df['drop_6h'] < -ACCEL_THRESHOLD_6H)
    df['signal_raw'] = np.where(is_crash & is_accel, 1, 0)

    # Merge
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
#  2. MOTOR DE BACKTEST V75 (SURGEON)
# ======================================================
def run_backtest():
    print(f"\n🌪️ INICIANDO BACKTEST V75 (THE SURGEON)")
    print(f"   Entry: Breakout Prev Low | SL: {SL_ATR_MULT}x ATR")
    print(f"   Trailing: Smart (New Lows Only)")
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
    last_trade_time = pd.Timestamp("2000-01-01", tz="UTC") 

    for ts, group in timeline:
        
        # --- 1. GESTIÓN DE POSICIONES ---
        active_pos = []
        for pos in positions:
            sym = pos['symbol']
            if sym not in group['symbol'].values:
                active_pos.append(pos)
                continue
            
            row = group[group['symbol'] == sym].iloc[0]
            h, l, c = row['high'], row['low'], row['close']
            entry = pos['entry']
            
            # Chequear SL
            if h >= pos['sl']:
                exit_p = pos['sl'] * (1 + SLIPPAGE)
                # Ajuste por gap (si abre por encima del SL)
                if row['open'] > pos['sl']: exit_p = row['open'] * (1 + SLIPPAGE)
                
                remaining_qty = pos['current_qty']
                pnl = (entry - exit_p) * remaining_qty
                cost = exit_p * remaining_qty * COMMISSION
                balance += (pnl - cost + (entry * remaining_qty * COMMISSION))
                trades_log.append({'ts': ts, 'symbol': sym, 'pnl': pnl-cost, 'type': 'SL', 'year': ts.year})
                continue 

            # Chequear TPs
            profit_pct = (entry - l) / entry
            
            if not pos['tp1_done'] and profit_pct >= TP1_PCT:
                qty_close = pos['total_qty'] * TP1_SIZE
                exit_p = entry * (1 - TP1_PCT)
                # Si hubo gap a favor, tomamos precio mejor
                if row['open'] < exit_p: exit_p = row['open']
                
                pnl = (entry - exit_p) * qty_close
                cost = exit_p * qty_close * COMMISSION
                balance += (pnl - cost + (entry * qty_close * COMMISSION))
                pos['current_qty'] -= qty_close
                pos['tp1_done'] = True
                trades_log.append({'ts': ts, 'symbol': sym, 'pnl': pnl-cost, 'type': 'TP1', 'year': ts.year})

            if not pos['tp2_done'] and profit_pct >= TP2_PCT:
                qty_close = pos['total_qty'] * TP2_SIZE
                exit_p = entry * (1 - TP2_PCT)
                if row['open'] < exit_p: exit_p = row['open']
                
                pnl = (entry - exit_p) * qty_close
                cost = exit_p * qty_close * COMMISSION
                balance += (pnl - cost + (entry * qty_close * COMMISSION))
                pos['current_qty'] -= qty_close
                pos['tp2_done'] = True
                trades_log.append({'ts': ts, 'symbol': sym, 'pnl': pnl-cost, 'type': 'TP2', 'year': ts.year})

            # PULIDO 4: TRAILING SMART
            # Solo actualizamos el trailing si el precio hace un NUEVO LOW
            if profit_pct >= TRAILING_START_PCT:
                if l < pos['lowest_price']:
                    pos['lowest_price'] = l
                    new_sl = l * (1 + TRAILING_DIST_PCT)
                    if new_sl < pos['sl']: # Short SL solo baja
                        pos['sl'] = new_sl
            
            if pos['current_qty'] > 0:
                active_pos.append(pos)
        
        positions = active_pos

        # --- 2. ENTRADAS (BREAKOUT) ---
        if len(positions) >= MAX_ACTIVE_TRADES: continue
        if ts < last_trade_time + pd.Timedelta(hours=COOLDOWN_HOURS): continue

        candidates = group[group['signal_final'] == 1]
        
        if not candidates.empty:
            candidates = candidates.sort_values('priority')
            best_pick = candidates.iloc[0]
            
            # PULIDO 1: ENTRY LOGIC (BREAKOUT PREV LOW)
            # La señal se generó al cierre de la vela anterior.
            # Estamos en la vela actual.
            # Queremos entrar solo si Low_Actual < Low_Previo
            
            prev_low = best_pick['prev_low']
            curr_low = best_pick['low']
            
            if curr_low < prev_low:
                # Ejecutamos al precio de ruptura (Stop Sell)
                entry_price = prev_low * (1 - SLIPPAGE)
                # Si el open ya abrió abajo (Gap Down), entramos al Open
                if best_pick['open'] < prev_low:
                    entry_price = best_pick['open'] * (1 - SLIPPAGE)

                sym = best_pick['symbol']
                atr = best_pick['atr']
                
                # PULIDO 2: SL por ATR
                sl_price = entry_price + (atr * SL_ATR_MULT)
                dist = sl_price - entry_price
                
                # Sizing
                risk_amt = balance * FIXED_RISK_PCT
                qty = risk_amt / dist
                if (qty * entry_price) > balance: qty = balance / entry_price # No leverage > 1x

                cost = qty * entry_price * COMMISSION
                balance -= cost
                
                positions.append({
                    'symbol': sym,
                    'entry': entry_price,
                    'total_qty': qty,
                    'current_qty': qty,
                    'sl': sl_price,
                    'lowest_price': entry_price, # Para trailing
                    'tp1_done': False,
                    'tp2_done': False
                })
                
                last_trade_time = ts
                # print(f"💉 [{ts}] SURGEON ENTRY: {sym} @ {entry_price:.2f}")

    # --- REPORTE ---
    print("\n" + "="*60)
    print(f"💰 Balance Final:   ${balance:.2f}")
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    print(f"🚀 Retorno Total:   {total_ret:.2f}%")
    
    if trades_log:
        df_t = pd.DataFrame(trades_log)
        print("\n📅 RENDIMIENTO ANUAL (V75):")
        annual = df_t.groupby('year')['pnl'].sum()
        count = df_t.groupby('year')['pnl'].count()
        print(pd.concat([annual, count], axis=1, keys=['PnL', 'Events']))

if __name__ == "__main__":
    run_backtest()