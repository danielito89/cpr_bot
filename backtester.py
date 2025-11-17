#!/usr/bin/env python3
import os
import pandas as pd
import numpy as np
import time
import logging

# Configurar logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- 1. CONFIGURACIÓN DEL BACKTESTER ---

# Símbolo a testear (debe coincidir con lo que descargaste)
SYMBOL_TO_TEST = "BTCUSDT" 

# Parámetros GANADORES (Optimizados)
EMA_PERIOD = 20
ATR_PERIOD = 14
VOLUME_FACTOR = 1.3
CPR_WIDTH_THRESHOLD = 0.2
TIME_STOP_HOURS = 12  # <-- El gran ganador

# Parámetros de Riesgo
LEVERAGE = 30
INVESTMENT_PCT = 0.05
INITIAL_BALANCE = 10000 
COMMISSION_PCT = 0.0004 
DAILY_LOSS_LIMIT_PCT = 0.15 # 15% de margen diario

# Multiplicadores de TP/SL
RANGING_SL_MULT = 0.5 
BREAKOUT_SL_MULT = 1.0 
RANGING_TP_MULT = 2.0 
BREAKOUT_TP_MULT = 1.25 

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# --- 2. FUNCIONES ---

def calculate_pivots_for_day(row):
    """Calcula pivotes diarios (Camarilla + CPR)."""
    h, l, c = row['High'], row['Low'], row['Close']
    if l == 0: return {}
    
    piv = (h + l + c) / 3.0
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
        "P": piv, "H1": r1, "H3": r3, "H4": r4,
        "L1": s1, "L3": s3, "L4": s4, "width": cw
    }

def get_trade_signal(row, atr, ema):
    """Lógica de entrada (Igual a v71/v81)."""
    current_price = row.Close
    current_volume = row.Quote_Asset_Volume # USDT
    median_vol = row.MedianVol_1m_USDT
    
    # Filtro de dirección de vela
    is_green_candle = current_price > row.Open
    is_red_candle = current_price < row.Open
    
    if not all([atr > 0, ema > 0, median_vol > 0]):
        return None, None, 0.0, 0.0
        
    required_volume = median_vol * VOLUME_FACTOR
    volume_confirmed = current_volume > required_volume
    
    side, entry_type, sl_price, tp_price = None, None, 0.0, 0.0

    # 1. Breakout Long (H4)
    if current_price > row.H4 and volume_confirmed and current_price > ema and is_green_candle:
        side = "BUY"
        entry_type = "Breakout Long"
        sl_price = current_price - atr * BREAKOUT_SL_MULT
        tp_price = current_price + atr * BREAKOUT_TP_MULT
    
    # 2. Breakout Short (L4)
    elif current_price < row.L4 and volume_confirmed and current_price < ema and is_red_candle:
        side = "SELL"
        entry_type = "Breakout Short"
        sl_price = current_price + atr * BREAKOUT_SL_MULT
        tp_price = current_price - atr * BREAKOUT_TP_MULT
    
    # 3. Ranging Long (L3)
    elif current_price <= row.L3 and volume_confirmed and is_green_candle:
        side = "BUY"
        entry_type = "Ranging Long"
        sl_price = row.L4 - atr * RANGING_SL_MULT
        tp_price = row.P 
    
    # 4. Ranging Short (H3)
    elif current_price >= row.H3 and volume_confirmed and is_red_candle:
        side = "SELL"
        entry_type = "Ranging Short"
        sl_price = row.H4 + atr * RANGING_SL_MULT
        tp_price = row.P

    if side:
        return side, entry_type, sl_price, tp_price
    return None, None, 0.0, 0.0

# --- 3. BACKTESTER ---

def run_backtest():
    logging.info(f"Iniciando backtest para {SYMBOL_TO_TEST}...")
    start_time = time.time()

    # --- Cargar Datos ---
    try:
        f_1h = os.path.join(DATA_DIR, f"mainnet_data_1h_{SYMBOL_TO_TEST}.csv")
        f_1d = os.path.join(DATA_DIR, f"mainnet_data_1d_{SYMBOL_TO_TEST}.csv")
        f_1m = os.path.join(DATA_DIR, f"mainnet_data_1m_{SYMBOL_TO_TEST}.csv")
        
        df_1h = pd.read_csv(f_1h, index_col="Open_Time", parse_dates=True)
        df_1d = pd.read_csv(f_1d, index_col="Open_Time", parse_dates=True)
        df_1m = pd.read_csv(f_1m, index_col="Open_Time", parse_dates=True)
    except FileNotFoundError:
        logging.error("Archivos no encontrados. Ejecuta download_data.py primero.")
        return

    # --- Calcular Indicadores ---
    logging.info("Calculando EMA y ATR (1h)...")
    df_1h['EMA_1h'] = df_1h['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    
    tr1 = df_1h['High'] - df_1h['Low']
    tr2 = abs(df_1h['High'] - df_1h['Close'].shift(1))
    tr3 = abs(df_1h['Low'] - df_1h['Close'].shift(1))
    df_1h['TR'] = pd.DataFrame({'a': tr1, 'b': tr2, 'c': tr3}).max(axis=1)
    df_1h['ATR_1h'] = df_1h['TR'].ewm(alpha=1/ATR_PERIOD, adjust=False).mean()

    logging.info("Calculando Mediana de Volumen (1m)...")
    # Mediana de los últimos 60 periodos (shift 1 para no ver el futuro)
    df_1m['MedianVol_1m_USDT'] = df_1m['Quote_Asset_Volume'].rolling(window=60).median().shift(1)

    logging.info("Calculando Pivotes (1d)...")
    shifted_1d = df_1d.shift(1).dropna()
    pivots_list = []
    for date, row in shifted_1d.iterrows():
        p = calculate_pivots_for_day(row)
        p['date'] = date
        pivots_list.append(p)
    df_pivots = pd.DataFrame(pivots_list).set_index('date')
    
    # --- Unir todo ---
    logging.info("Combinando datos...")
    df_merged = pd.merge_asof(df_1m, df_1h[['EMA_1h', 'ATR_1h']], left_index=True, right_index=True, direction='backward')
    df_merged['date'] = df_merged.index.date
    df_pivots.index = df_pivots.index.date
    df_merged = pd.merge(df_merged, df_pivots, left_on='date', right_index=True, how='left')
    df_merged.dropna(inplace=True)
    
    # --- Simulación ---
    logging.info(f"Simulando {len(df_merged)} velas...")
    
    balance = INITIAL_BALANCE
    in_position = False
    pos = {}
    trades = []
    daily_pnl = 0.0
    current_day = None

    for row in df_merged.itertuples():
        if current_day != row.Index.date():
            current_day = row.Index.date()
            daily_pnl = 0.0
        
        if daily_pnl <= -(INITIAL_BALANCE * DAILY_LOSS_LIMIT_PCT):
            continue 

        if in_position:
            pnl = 0.0
            reason = None
            
            # SL
            if (pos['side'] == 'BUY' and row.Low <= pos['sl']) or \
               (pos['side'] == 'SELL' and row.High >= pos['sl']):
                pnl = (pos['sl'] - pos['entry']) * pos['size']
                if pos['side'] == 'SELL': pnl = -pnl
                reason = "Stop-Loss"
            
            # TP
            elif (pos['side'] == 'BUY' and row.High >= pos['tp']) or \
                 (pos['side'] == 'SELL' and row.Low <= pos['tp']):
                pnl = (pos['tp'] - pos['entry']) * pos['size']
                if pos['side'] == 'SELL': pnl = -pnl
                reason = "Take-Profit"

            # Time Stop
            elif (pos['type'].startswith("Ranging") and 
                  (row.Index - pos['time']).total_seconds()/3600 > TIME_STOP_HOURS):
                pnl = (row.Close - pos['entry']) * pos['size']
                if pos['side'] == 'SELL': pnl = -pnl
                reason = f"Time-Stop ({TIME_STOP_HOURS}h)"

            if reason:
                balance += pnl - pos['comm']
                daily_pnl += pnl
                trades.append({
                    'entry_time': pos['time'], 'exit_time': row.Index,
                    'side': pos['side'], 'pnl': pnl - pos['comm'], 'reason': reason
                })
                in_position = False
                continue

        if not in_position:
            atr, ema = row.ATR_1h, row.EMA_1h
            side, type_, sl, tp = get_trade_signal(row, atr, ema)
            
            if side:
                size = (balance * INVESTMENT_PCT * LEVERAGE) / row.Close
                if size == 0: continue
                comm = (size * row.Close) * COMMISSION_PCT
                
                balance -= comm
                in_position = True
                pos = {
                    'entry': row.Close, 'side': side, 'size': size,
                    'tp': tp, 'sl': sl, 'type': type_, 'time': row.Index, 'comm': comm
                }

    # --- Resultados ---
    logging.info(f"Backtest finalizado en {time.time()-start_time:.2f}s")
    
    if not trades:
        logging.info("Sin operaciones.")
        return

    df_res = pd.DataFrame(trades)
    total_pnl = df_res['pnl'].sum()
    wins = len(df_res[df_res['pnl']>0])
    win_rate = (wins/len(df_res))*100
    
    gross_profit = df_res[df_res['pnl']>0]['pnl'].sum()
    gross_loss = abs(df_res[df_res['pnl']<0]['pnl'].sum())
    pf = gross_profit / gross_loss if gross_loss != 0 else 0

    print("\n" + "="*40)
    print(f" RESULTADOS: {SYMBOL_TO_TEST} ({len(df_merged)} velas)")
    print("="*40)
    print(f" PnL Neto:      ${total_pnl:.2f}")
    print(f" Balance Final: ${balance:.2f}")
    print(f" Profit Factor: {pf:.2f}")
    print(f" Win Rate:      {win_rate:.2f}% ({wins}/{len(df_res)})")
    print(f" Trades/Día:    {len(df_res)/((df_merged.index[-1]-df_merged.index[0]).days):.1f}")
    print("="*40)

if __name__ == "__main__":
    run_backtest()
