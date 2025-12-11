#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V57 – INSIDE BAR BREAKOUT (4H)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia: PRICE ACTION 4H ----
# Tendencia: Solo largos si Close > EMA 50
EMA_TREND = 50 

# ---- Salidas ----
# Stop Loss Técnico: El mínimo de la Inside Bar (o un poco abajo)
SL_BUFFER = 0.002       # 0.2% de aire bajo el patrón
# Take Profit: No fijo. Usamos Trailing Stop para surfear la expansión.
TRAILING_ATR_MULT = 3.0 

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.03   # 3% (Buscamos más frecuencia, bajamos riesgo)
MAX_LEVER = 10          

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
    
    # INDICADORES 4H
    df_4h['ema_trend'] = talib.EMA(df_4h['close'], timeperiod=EMA_TREND)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # DETECCIÓN DE INSIDE BAR (IB)
    # IB: High < Prev_High AND Low > Prev_Low
    # Shift(1) para comparar actual con anterior dentro del marco 4H
    prev_high = df_4h['high'].shift(1)
    prev_low = df_4h['low'].shift(1)
    
    df_4h['is_inside'] = (df_4h['high'] < prev_high) & (df_4h['low'] > prev_low)
    
    # --- LOGICA DE SEÑAL ---
    # Si la vela que ACABA DE CERRAR fue una Inside Bar,
    # y la tendencia es alcista...
    # Ponemos una orden pendiente para las próximas 4 horas en el High de esa IB.
    
    # Trend Filter (Vela cerrada > EMA)
    trend_ok = df_4h['close'] > df_4h['ema_trend']
    
    df_4h['setup_active'] = df_4h['is_inside'] & trend_ok
    df_4h['trigger_price'] = df_4h['high'] # El trigger es el High de la IB
    df_4h['stop_loss_level'] = df_4h['low'] # El Stop es el Low de la IB

    # --- SHIFT CRÍTICO (Lookahead prevention) ---
    # La señal se genera al CIERRE de la vela. Estará disponible en la siguiente.
    df_4h_shifted = df_4h.shift(1)

    print("🔄 Sincronizando con 1H...")
    # Traemos las señales al timeframe de 1H
    cols = ['setup_active', 'trigger_price', 'stop_loss_level', 'atr']
    df_1h = df.join(df_4h_shifted[cols], rsuffix='_4h')
    
    # Rellenamos: La señal de la vela 4H es válida durante las siguientes 4 velas de 1H
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V57
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V57 (4H Inside Bar) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance 

    position = None 
    entry_price = 0; quantity = 0; sl = 0
    entry_comm = 0
    
    trades = []
    
    # Para evitar entrar multiple veces en la misma vela de 4H
    last_trade_4h_idx = -1 

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Datos del Setup (Vienen de la vela 4H anterior)
        setup_active = row.setup_active == 1.0
        trigger = row.trigger_price
        sl_technical = row.stop_loss_level
        atr_4h = row.atr
        
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # ----------------------------------------------------
        # 1. ENTRADA (BREAKOUT)
        # ----------------------------------------------------
        # Si hay setup de Inside Bar activo...
        if position is None and setup_active:
            
            # Verificamos si el precio rompió el Trigger (High de la IB)
            # Usamos High > Trigger para detectar ruptura
            # Pero Open para ver si abrimos con gap
            breakout_occurred = h > trigger
            
            # Filtro adicional: Que el SL no esté pegado al entry (ruido)
            valid_structure = (trigger - sl_technical) > (trigger * 0.002) # 0.2% min dist
            
            if breakout_occurred and valid_structure:
                
                # Precio de ejecución: El nivel del trigger o el Open si hubo gap
                base_entry = max(o, trigger)
                real_entry = base_entry * (1 + friction)
                
                # Stop Loss: Low de la IB
                sl_price = sl_technical * (1 - SL_BUFFER)
                
                # Riesgo
                risk_dist = real_entry - sl_price
                
                if risk_dist > 0:
                    # Risk on Peak
                    risk_usd = peak_balance * FIXED_RISK_PCT
                    qty = risk_usd / risk_dist
                    max_qty = (balance * MAX_LEVER) / real_entry
                    qty = min(qty, max_qty)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * real_entry * COMMISSION
                        balance -= entry_comm
                        
                        position = "long"
                        quantity = qty
                        sl = sl_price
                        entry = real_entry
                        entry_comm_paid = entry_comm
                        
                        # Intra-candle Check
                        if l <= sl:
                            exit_p = sl * (1 - SLIPPAGE_PCT)
                            pnl = (exit_p - real_entry) * qty
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
            
            # A) TRAILING STOP
            # Protegemos ganancias usando volatilidad de 4H
            new_sl = h - (atr_4h * TRAILING_ATR_MULT)
            if new_sl > sl:
                sl = new_sl
            
            # B) Stop Loss Hit
            if l <= sl:
                exit_raw = o if o < sl else sl 
                exit_p = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "Trailing/SL"
            
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
    print(f"📊 RESULTADOS V57 – INSIDE BAR 4H: {symbol}")
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
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)