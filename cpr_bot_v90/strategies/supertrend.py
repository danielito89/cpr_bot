#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V58 – FULL CYCLE (LONG & SHORT)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia: TENDENCIA PURA 4H ----
FAST_EMA = 50
SLOW_EMA = 200

# ---- Salidas de Emergencia (Solo Stops catastróficos) ----
# El Short necesita un stop más corto porque el mercado puede subir infinito
LONG_SL_ATR = 3.0       
SHORT_SL_ATR = 2.0      

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.05   # 5% por trade (Agresivo)
MAX_LEVER = 5           # Leverage conservador (Swing)

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
    
    # TENDENCIA (1 = Bullish, -1 = Bearish)
    df_4h['trend'] = np.where(df_4h['ema_fast'] > df_4h['ema_slow'], 1, -1)
    
    # SEÑAL DE CAMBIO (Flip)
    # Detectamos cuando la tendencia cambia respecto a la vela anterior
    df_4h['prev_trend'] = df_4h['trend'].shift(1)
    
    # Signal 1 (Go Long), Signal -1 (Go Short), 0 (Hold)
    df_4h['signal'] = np.where(
        (df_4h['trend'] == 1) & (df_4h['prev_trend'] == -1), 1, 
        np.where((df_4h['trend'] == -1) & (df_4h['prev_trend'] == 1), -1, 0)
    )

    # SHIFT(1) OBLIGATORIO (Anti-Lookahead)
    df_4h_shifted = df_4h.shift(1)

    print("🔄 Sincronizando con 1H...")
    df_1h = df.join(df_4h_shifted[['ema_fast', 'ema_slow', 'atr', 'signal', 'trend']], rsuffix='_4h')
    
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V58 (LONG & SHORT)
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V58 (Full Cycle Long/Short) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance 

    position = None # None, 'long', 'short'
    entry_price = 0; quantity = 0; sl = 0
    entry_comm = 0
    
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        atr_4h = row.atr
        signal = row.signal # 1 = Buy, -1 = Sell, 0 = Hold
        
        # Friction
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # ----------------------------------------------------
        # LÓGICA DE CIERRE / REVERSAL
        # ----------------------------------------------------
        # Si tenemos posición y llega señal contraria -> CERRAR Y REVERTIR
        
        close_long = (position == 'long' and signal == -1)
        close_short = (position == 'short' and signal == 1)
        
        # Stops de Emergencia
        sl_hit_long = (position == 'long' and l <= sl)
        sl_hit_short = (position == 'short' and h >= sl)
        
        # EJECUTAR SALIDAS
        exit_p = None
        reason = None
        
        if close_long or sl_hit_long:
            # Vender Long
            base_exit = o if close_long else sl # Si es señal, salimos al Open. Si es SL, al precio SL.
            if sl_hit_long and o < sl: base_exit = o # Gap protection
            
            exit_p = base_exit * (1 - friction) # Recibimos menos al vender
            reason = "Signal Flip" if close_long else "Stop Loss"
            
            pnl = (exit_p - entry_price) * quantity
            
        elif close_short or sl_hit_short:
            # Comprar Short (Buy to Cover)
            base_exit = o if close_short else sl
            if sl_hit_short and o > sl: base_exit = o # Gap protection
            
            exit_p = base_exit * (1 + friction) # Pagamos más al recomprar
            reason = "Signal Flip" if close_short else "Stop Loss"
            
            pnl = (entry_price - exit_p) * quantity # PnL Short: Entrada - Salida
            
        if exit_p:
            exit_comm = exit_p * quantity * COMMISSION
            balance += (pnl - exit_comm)
            
            if balance > peak_balance: peak_balance = balance
            
            net_pnl = pnl - entry_comm - exit_comm
            trades.append({'year': ts.year, 'pnl': net_pnl, 'type': reason, 'side': position})
            
            position = None
            quantity = 0

        # ----------------------------------------------------
        # LÓGICA DE APERTURA
        # ----------------------------------------------------
        # Si no tenemos posición (o acabamos de cerrar una), miramos señal
        
        if position is None:
            new_pos_type = None
            
            if signal == 1: new_pos_type = 'long'
            elif signal == -1: new_pos_type = 'short'
            
            if new_pos_type:
                # Calcular Entry y SL
                if new_pos_type == 'long':
                    real_entry = o * (1 + friction)
                    sl_price = real_entry - (atr_4h * LONG_SL_ATR)
                    risk_dist = real_entry - sl_price
                else: # Short
                    real_entry = o * (1 - friction) # Vendemos al Bid (más barato)
                    sl_price = real_entry + (atr_4h * SHORT_SL_ATR)
                    risk_dist = sl_price - real_entry
                
                if risk_dist > 0:
                    # Sizing (Risk on Peak)
                    risk_usd = peak_balance * FIXED_RISK_PCT
                    qty = risk_usd / risk_dist
                    
                    max_qty = (balance * MAX_LEVER) / real_entry
                    qty = min(qty, max_qty)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * real_entry * COMMISSION
                        balance -= entry_comm
                        
                        position = new_pos_type
                        entry_price = real_entry
                        quantity = qty
                        sl = sl_price
                        entry_comm = entry_comm
                        
                        # INTRA-CANDLE CRASH CHECK (Misma vela)
                        # Si entramos y en la misma vela nos stopea
                        sl_trigger = False
                        if position == 'long' and l <= sl: sl_trigger = True
                        if position == 'short' and h >= sl: sl_trigger = True
                        
                        if sl_trigger:
                            # Revertimos inmediatamente
                            if position == 'long':
                                exit_p = sl * (1 - friction)
                                pnl = (exit_p - entry_price) * qty
                            else:
                                exit_p = sl * (1 + friction)
                                pnl = (entry_price - exit_p) * qty
                                
                            exit_comm = exit_p * qty * COMMISSION
                            balance += (pnl - exit_comm)
                            net = pnl - entry_comm - exit_comm
                            trades.append({'year': ts.year, 'pnl': net, 'type': 'SL Intra', 'side': position})
                            position = None

        # Equity Curve
        curr_eq = balance
        if position == 'long':
            curr_eq += (c - entry_price) * quantity
        elif position == 'short':
            curr_eq += (entry_price - c) * quantity
            
        equity_curve.append(curr_eq)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V58 – FULL CYCLE (LONG & SHORT): {symbol}")
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
        print(f"🧮 Total Trades:    {len(trades_df)}")
        print("\n📅 RENDIMIENTO POR AÑO:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)