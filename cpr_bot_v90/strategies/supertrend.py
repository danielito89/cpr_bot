#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V67 – REFINED RISK & COOLDOWN
# ======================================================

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "1000PEPEUSDT"]
TIMEFRAME_STR = "1h"

# ---- Estrategia V66 (Golden Cross) ----
FAST_EMA = 50
SLOW_EMA = 200
SL_ATR_MULT = 3.0       

# ---- MEJORA 1: RISK MODEL ----
# True = Usar Balance Actual (Conservador, menor Drawdown)
# False = Usar Peak Balance (Agresivo, mayor Retorno, mayor riesgo de ruina)
USE_CURRENT_BALANCE_RISK = True 

# ---- MEJORA 2: COOLDOWN ----
# Cuántas velas de 4H esperar después de cerrar un trade antes de volver a entrar
COOLDOWN_CANDLES = 1    

# ---- Microestructura ----
INITIAL_BALANCE_PER_COIN = 2000
FIXED_RISK_PCT = 0.05   
MAX_LEVER = 5           
COMMISSION = 0.0004; SPREAD_PCT = 0.0004; SLIPPAGE_PCT = 0.0006; BASE_LATENCY = 0.0001; MIN_QTY = 0.01

# ======================================================
#  CARGA DE DATOS
# ======================================================
def load_and_process(symbol):
    # Buscar archivo
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

    # Limpieza
    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- RESAMPLING 4H ---
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    
    # Indicadores
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Señales
    df_4h['trend_up'] = np.where(df_4h['ema_fast'] > df_4h['ema_slow'], 1, 0)
    df_4h['prev_trend'] = df_4h['trend_up'].shift(1)
    
    df_4h['signal_buy'] = np.where((df_4h['trend_up'] == 1) & (df_4h['prev_trend'] == 0), 1, 0)
    df_4h['signal_sell'] = np.where((df_4h['trend_up'] == 0) & (df_4h['prev_trend'] == 1), 1, 0)

    # Shift (Anti-Lookahead) & Merge
    df_4h_shifted = df_4h.shift(1)
    df_1h = df.join(df_4h_shifted[['ema_fast', 'ema_slow', 'atr', 'signal_buy', 'signal_sell']], rsuffix='_4h')
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  BACKTEST ENGINE
# ======================================================
def backtest_symbol(symbol, df):
    balance = INITIAL_BALANCE_PER_COIN
    peak_balance = balance 
    position = None 
    entry_price = 0; quantity = 0; sl = 0; entry_comm = 0
    
    # Cooldown Logic
    cooldown_counter = 0 # Contador en velas de 1H
    cooldown_hours = COOLDOWN_CANDLES * 4 # Convertir velas 4H a horas
    
    trades = []
    equity_curve = []

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        atr_4h = row.atr
        signal_buy = row.signal_buy == 1
        signal_sell = row.signal_sell == 1
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # Decrementar cooldown
        if cooldown_counter > 0:
            cooldown_counter -= 1

        # --- ENTRADA ---
        if position is None and signal_buy:
            # Solo entramos si no hay cooldown
            if cooldown_counter == 0:
                entry_price = o * (1 + friction)
                sl_price = entry_price - (atr_4h * SL_ATR_MULT)
                risk_dist = entry_price - sl_price
                
                if risk_dist > 0:
                    # MEJORA 1: SELECCIÓN DE BASE DE RIESGO
                    risk_base = balance if USE_CURRENT_BALANCE_RISK else peak_balance
                    
                    risk_usd = risk_base * FIXED_RISK_PCT
                    qty = min(risk_usd / risk_dist, (balance * MAX_LEVER) / entry_price)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * entry_price * COMMISSION
                        balance -= entry_comm
                        position = 'long'; quantity = qty; sl = sl_price
                        entry = entry_price; entry_comm_paid = entry_comm
                        
                        # Crash check
                        if l <= sl:
                            exit_p = sl * (1 - SLIPPAGE_PCT)
                            pnl = (exit_p - entry) * qty
                            fee = exit_p * qty * COMMISSION
                            balance += (pnl - fee)
                            trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm-fee})
                            position = None
                            cooldown_counter = cooldown_hours # Activar cooldown tras loss

        # --- SALIDA ---
        elif position == "long":
            exit_p = None
            reason = None
            
            if signal_sell: 
                exit_p = o * (1 - SLIPPAGE_PCT)
                reason = "Death Cross"
            elif l <= sl: 
                exit_p = sl * (1 - SLIPPAGE_PCT)
                reason = "SL"
            
            if exit_p:
                pnl = (exit_p - entry) * quantity
                fee = exit_p * quantity * COMMISSION
                balance += (pnl - fee)
                if balance > peak_balance: peak_balance = balance
                
                trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm_paid-fee})
                position = None
                
                # MEJORA 2: ACTIVAR COOLDOWN AL SALIR
                cooldown_counter = cooldown_hours
        
        curr_eq = balance
        if position == 'long': curr_eq += (c - entry) * quantity
        equity_curve.append({'timestamp': ts, 'equity': curr_eq})

    return trades, equity_curve, balance

# ======================================================
#  RUNNER
# ======================================================
def run_portfolio():
    print(f"\n🧪 BACKTEST V67: REFINED RISK & COOLDOWN")
    print(f"   Risk Base: {'CURRENT BALANCE (Safe)' if USE_CURRENT_BALANCE_RISK else 'PEAK BALANCE (Aggressive)'}")
    print(f"   Cooldown:  {COOLDOWN_CANDLES} vela(s) de 4H ({COOLDOWN_CANDLES*4} horas)")
    print("="*60)
    
    all_trades = []
    portfolio_equity = pd.DataFrame()
    final_balances = {}
    
    for symbol in SYMBOLS:
        df = load_and_process(symbol)
        if df is not None:
            trades, equity_data, final_bal = backtest_symbol(symbol, df)
            all_trades.extend(trades)
            final_balances[symbol] = final_bal
            
            eq_df = pd.DataFrame(equity_data).set_index('timestamp')
            eq_df.rename(columns={'equity': symbol}, inplace=True)
            
            if portfolio_equity.empty: portfolio_equity = eq_df
            else: portfolio_equity = portfolio_equity.join(eq_df, how='outer')
    
    portfolio_equity.ffill(inplace=True)
    portfolio_equity.fillna(INITIAL_BALANCE_PER_COIN, inplace=True)
    portfolio_equity['Total'] = portfolio_equity.sum(axis=1)
    
    # Reporte
    initial_total = INITIAL_BALANCE_PER_COIN * len(final_balances)
    final_total = sum(final_balances.values())
    total_ret = (final_total - initial_total) / initial_total * 100
    
    peak = portfolio_equity['Total'].cummax()
    dd = (portfolio_equity['Total'] - peak) / peak
    max_dd = dd.min() * 100
    
    print("\n" + "="*60)
    print(f"💰 Capital Inicial:   ${initial_total:.2f}")
    print(f"💰 Capital Final:     ${final_total:.2f}")
    print(f"🚀 Retorno Total:     {total_ret:.2f}%")
    print(f"📉 Max Drawdown:      {max_dd:.2f}%")
    print("="*60)
    
    if all_trades:
        trades_df = pd.DataFrame(all_trades)
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:  {win:.2f}%")
        print(f"🧮 Trades:    {len(trades_df)}")
        print("\n📅 RENDIMIENTO ANUAL:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))

if __name__ == "__main__":
    run_portfolio()