#!/usr/bin/env python3
# backtester.py (v2)
# Versión: v2 (Eliminada la dependencia 'pandas-ta'.
#               EMA y ATR se calculan manualmente con pandas)

import os
import pandas as pd
import numpy as np
import time
import statistics

# --- 1. CONFIGURACIÓN DEL BACKTESTER ---
EMA_PERIOD = 20
ATR_PERIOD = 14
VOLUME_FACTOR = 1.3
CPR_WIDTH_THRESHOLD = 0.2
TIME_STOP_HOURS = 6

# --- Parámetros de Simulación ---
LEVERAGE = 3
INVESTMENT_PCT = 0.01
INITIAL_BALANCE = 10000 
COMMISSION_PCT = 0.0004 
RANGING_SL_MULT = 0.5 
BREAKOUT_SL_MULT = 1.0 
RANGING_TP_MULT = 2.0 
BREAKOUT_TP_MULT = 1.25 

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# --- 2. LÓGICA DE LA ESTRATEGIA (Funciones) ---

def calculate_pivots_for_day(row):
    """Calcula pivotes para un solo día (una fila de datos de 1D)."""
    h, l, c = row['High'], row['Low'], row['Close']
    if l == 0: return {}
    
    piv = (h + l + c) / 3.0
    rng = h - l
    r4 = c + (h - l) * 1.1 / 2
    r3 = c + (h - l) * 1.1 / 4
    r1 = c + (h - l) * 1.1 / 12
    s1 = c - (h - l) * 1.1 / 12
    s3 = c - (h - l) * 1.1 / 4
    s4 = c - (h - l) * 1.1 / 2
    bc = (h + l) / 2.0
    tc = (piv - bc) + piv
    cw = abs(tc - bc) / piv * 100 if piv != 0 else 0

    return {
        "P": piv, "BC": bc, "TC": tc, "width": cw, 
        "is_ranging_day": cw > CPR_WIDTH_THRESHOLD,
        "H1": r1, "H3": r3, "H4": r4,
        "L1": s1, "L3": s3, "L4": s4,
    }

def get_trade_signal(row, p, atr, ema):
    """Refactor de la lógica 'seek_new_trade' de la v65."""
    current_price = row['Close']
    current_volume = row['Quote_Asset_Volume'] # Volumen USDT
    median_vol = row['MedianVol_1m_USDT']
    
    if not all([p, atr > 0, ema > 0, median_vol > 0]):
        return None, None, 0.0, 0.0
        
    required_volume = median_vol * VOLUME_FACTOR
    volume_confirmed = current_volume > required_volume
    
    side, entry_type, sl_price, tp_price = None, None, 0.0, 0.0

    # breakout long (MANTIENE FILTRO EMA)
    if current_price > p["H4"] and volume_confirmed and current_price > ema:
        side = "BUY"
        entry_type = "Breakout Long"
        sl_price = current_price - atr * BREAKOUT_SL_MULT
        tp_price = current_price + atr * BREAKOUT_TP_MULT
    
    # breakout short (MANTIENE FILTRO EMA)
    elif current_price < p["L4"] and volume_confirmed and current_price < ema:
        side = "SELL"
        entry_type = "Breakout Short"
        sl_price = current_price + atr * BREAKOUT_SL_MULT
        tp_price = current_price - atr * BREAKOUT_TP_MULT
    
    # ranging long (FILTRO EMA ELIMINADO)
    elif current_price <= p["L3"] and volume_confirmed:
        side = "BUY"
        entry_type = "Ranging Long"
        sl_price = p["L4"] - atr * RANGING_SL_MULT
        tp_price = p["P"] # TP es el pivote central
    
    # ranging short (FILTRO EMA ELIMINADO)
    elif current_price >= p["H3"] and volume_confirmed:
        side = "SELL"
        entry_type = "Ranging Short"
        sl_price = p["H4"] + atr * RANGING_SL_MULT
        tp_price = p["P"] # TP es el pivote central

    if side:
        return side, entry_type, sl_price, tp_price
    
    return None, None, 0.0, 0.0

# --- 3. FUNCIÓN PRINCIPAL DEL BACKTESTER ---

def run_backtest():
    print("Iniciando backtest...")
    start_time = time.time()

    # --- 3.1 Cargar Datos ---
    print("Cargando datos (puede tardar)...")
    try:
        df_1h = pd.read_csv(os.path.join(DATA_DIR, "mainnet_data_1h.csv"), index_col="Open_Time", parse_dates=True)
        df_1d = pd.read_csv(os.path.join(DATA_DIR, "mainnet_data_1d.csv"), index_col="Open_Time", parse_dates=True)
        df_1m = pd.read_csv(os.path.join(DATA_DIR, "mainnet_data_1m.csv"), index_col="Open_Time", parse_dates=True)
    except FileNotFoundError:
        print("Error: Archivos de datos no encontrados. Ejecuta 'download_data.py' primero.")
        return

    # --- 3.2 Calcular Indicadores (¡MANUALMENTE!) ---
    print("Calculando indicadores de 1h (EMA, ATR)...")
    
    # Calcular EMA
    df_1h['EMA_1h'] = df_1h['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    
    # Calcular ATR
    tr1 = df_1h['High'] - df_1h['Low']
    tr2 = abs(df_1h['High'] - df_1h['Close'].shift(1))
    tr3 = abs(df_1h['Low'] - df_1h['Close'].shift(1))
    df_1h['TR'] = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
    # Usamos ewm (Exponential Weighted Moving) con alpha para simular el ATR (RMA)
    df_1h['ATR_1h'] = df_1h['TR'].ewm(alpha=1/ATR_PERIOD, adjust=False).mean()

    # --- 3.3 Calcular Mediana de Volumen (1m) ---
    print("Calculando mediana de volumen de 1m (esto tarda)...")
    df_1m['MedianVol_1m_USDT'] = df_1m['Quote_Asset_Volume'].rolling(window=60).median().shift(1)

    # --- 3.4 Calcular Pivotes (1d) ---
    print("Calculando pivotes diarios...")
    shifted_1d = df_1d.shift(1).dropna()
    pivots_list = []
    for date, row in shifted_1d.iterrows():
        pivots = calculate_pivots_for_day(row)
        pivots['date'] = date
        pivots_list.append(pivots)
    
    df_pivots = pd.DataFrame(pivots_list).set_index('date')
    
    # --- 3.5 Unir Datos (Merge) ---
    print("Combinando datos...")
    df_merged = pd.merge_asof(
        df_1m, 
        df_1h[['EMA_1h', 'ATR_1h']], 
        left_index=True, 
        right_index=True, 
        direction='backward'
    )
    
    df_merged['date'] = df_merged.index.date
    df_pivots.index = df_pivots.index.date
    df_merged = pd.merge(
        df_merged,
        df_pivots,
        left_on='date',
        right_index=True,
        how='left'
    )
    
    df_merged = df_merged.dropna()
    
    # --- 3.6 Bucle de Simulación ---
    print(f"Iniciando simulación sobre {len(df_merged)} velas de 1m...")
    
    balance = INITIAL_BALANCE
    in_position = False
    position_info = {}
    trades = []
    daily_pnl = 0.0
    current_day = None

    for row in df_merged.itertuples():
        current_price = row.Close
        current_time = row.Index
        
        if current_day != current_time.date():
            current_day = current_time.date()
            daily_pnl = 0.0
        
        if daily_pnl <= -(INITIAL_BALANCE * DAILY_LOSS_LIMIT_PCT):
            continue 

        # --- Gestión de Posición ---
        if in_position:
            pnl = 0.0
            close_reason = None
            
            # 1. Comprobar Stop-Loss
            if (position_info['side'] == 'BUY' and row.Low <= position_info['sl']) or \
               (position_info['side'] == 'SELL' and row.High >= position_info['sl']):
                pnl = (position_info['sl'] - position_info['entry']) * position_info['pos_size']
                if position_info['side'] == 'SELL': pnl = -pnl
                close_reason = "Stop-Loss"
            
            # 2. Comprobar Take-Profit
            elif (position_info['side'] == 'BUY' and row.High >= position_info['tp']) or \
                 (position_info['side'] == 'SELL' and row.Low <= position_info['tp']):
                pnl = (position_info['tp'] - position_info['entry']) * position_info['pos_size']
                if position_info['side'] == 'SELL': pnl = -pnl
                close_reason = "Take-Profit"

            # 3. Comprobar Time Stop
            hours_in_trade = (current_time - position_info['entry_time']).total_seconds() / 3600
            if (position_info['type'].startswith("Ranging") and 
                hours_in_trade > TIME_STOP_HOURS):
                
                pnl = (current_price - position_info['entry']) * position_info['pos_size']
                if position_info['side'] == 'SELL': pnl = -pnl
                close_reason = "Time-Stop"

            # Si se cerró la posición...
            if close_reason:
                balance += pnl
                balance -= position_info['commission'] # Pagar comisión de cierre
                daily_pnl += pnl
                trades.append({
                    "entry_time": position_info['entry_time'],
                    "close_time": current_time,
                    "side": position_info['side'],
                    "entry_price": position_info['entry'],
                    "close_price": current_price,
                    "sl": position_info['sl'],
                    "tp": position_info['tp'],
                    "pnl": pnl - position_info['commission'], # PnL Neto
                    "reason": close_reason
                })
                in_position = False
                position_info = {}
                continue 

        # --- Búsqueda de Nuevas Entradas ---
        if not in_position:
            p = row._asdict() # Pivotes están en la fila
            atr = row.ATR_1h
            ema = row.EMA_1h
            
            side, entry_type, sl_price, tp_price = get_trade_signal(row, p, atr, ema)
            
            if side:
                investment = balance * INVESTMENT_PCT
                pos_size = (investment * LEVERAGE) / current_price
                commission = (pos_size * current_price) * COMMISSION_PCT
                
                if pos_size == 0: continue

                in_position = True
                balance -= commission # Pagar comisión de apertura
                
                position_info = {
                    "entry": current_price,
                    "side": side,
                    "pos_size": pos_size,
                    "tp": tp_price,
                    "sl": sl_price,
                    "type": entry_type,
                    "entry_time": current_time,
                    "commission": commission,
                    "sl_moved_to_be": False,
                }
    
    # --- 3.7 Análisis de Resultados ---
    print("\n--- ¡Backtest Completo! ---")
    print(f"Tiempo de ejecución: {time.time() - start_time:.2f} segundos")
    
    if not trades:
        print("No se realizó ninguna operación.")
        return

    df_trades = pd.DataFrame(trades)
    
    total_trades = len(df_trades)
    wins = len(df_trades[df_trades['pnl'] > 0])
    win_rate = (wins / total_trades) * 100
    
    total_pnl = df_trades['pnl'].sum()
    avg_win = df_trades[df_trades['pnl'] > 0]['pnl'].mean()
    avg_loss = df_trades[df_trades['pnl'] < 0]['pnl'].mean()
    profit_factor = 0
    if df_trades[df_trades['pnl'] < 0]['pnl'].sum() != 0:
        profit_factor = abs(df_trades[df_trades['pnl'] > 0]['pnl'].sum() / df_trades[df_trades['pnl'] < 0]['pnl'].sum())

    print("\n--- 📊 Resultados (Estrategia v65) ---")
    print(f" Parámetros: EMA={EMA_PERIOD}, VolFactor={VOLUME_FACTOR}, TimeStop={TIME_STOP_HOURS}h")
    print(f" Período: {df_merged.index.min()} a {df_merged.index.max()}")
    print("-----------------------------------")
    print(f" Balance Inicial: ${INITIAL_BALANCE:.2f}")
    print(f" Balance Final:   ${balance:.2f}")
    print(f" PnL Neto:        ${total_pnl:.2f}")
    print("-----------------------------------")
    print(f" Total Trades:    {total_trades}")
    print(f" Win Rate:        {win_rate:.2f}%")
    print(f" Profit Factor:   {profit_factor:.2f}")
    print(f" Avg. Ganancia:   ${avg_win:.2f}")
    print(f" Avg. Pérdida:    ${avg_loss:.2f}")

    df_trades.to_csv(os.path.join(DATA_DIR, "backtest_results_v65.csv"))
    print("\nResultados detallados guardados en 'data/backtest_results_v65.csv'")

if __name__ == "__main__":
    run_backtest()
