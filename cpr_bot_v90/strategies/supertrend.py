#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib
import glob

# ======================================================
#  🔥 CONFIG V66 – THE ETF BUILDER (PORTFOLIO)
# ======================================================

# LISTA DE ACTIVOS A PROBAR
# Asegúrate de tener los CSVs: "mainnet_data_1h_SYMBOL.csv" o similar
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "1000PEPE"]
TIMEFRAME_STR = "1h"

# ---- Estrategia: GOLDEN CROSS V59 (La Ganadora) ----
FAST_EMA = 50
SLOW_EMA = 200
SL_ATR_MULT = 3.0       

# ---- Risk & Microestructura ----
INITIAL_BALANCE_PER_COIN = 2000 # Dividimos capital entre activos (Ej: 10k total / 5 coins)
FIXED_RISK_PCT = 0.05           # Riesgo por trade en cada moneda
MAX_LEVER = 5           

COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001
MIN_QTY = 0.01

# ======================================================
#  1. FUNCIÓN DE CARGA (Adaptada para múltiples)
# ======================================================

def load_data(symbol):
    candidates = [
        f"data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"data/{symbol}_{TIMEFRAME_STR}.csv",
        f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv", 
        f"{symbol}_{TIMEFRAME_STR}.csv"
    ]
    
    df = None
    for path in candidates:
        if os.path.exists(path):
            print(f"   📄 Cargando {symbol} desde {path}...")
            df = pd.read_csv(path)
            break
            
    if df is None: 
        print(f"   ⚠️ No se encontró datos para {symbol}. Saltando.")
        return None

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

# ======================================================
#  2. LÓGICA CORE (V59 RESAMPLED)
# ======================================================

def process_symbol(symbol):
    df = load_data(symbol)
    if df is None: return None

    # Resampling 4H
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    
    # Indicadores 4H
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Señales (Vela Cerrada)
    df_4h['trend_up'] = np.where(df_4h['ema_fast'] > df_4h['ema_slow'], 1, 0)
    df_4h['prev_trend'] = df_4h['trend_up'].shift(1)
    
    df_4h['signal_buy'] = np.where((df_4h['trend_up'] == 1) & (df_4h['prev_trend'] == 0), 1, 0)
    df_4h['signal_sell'] = np.where((df_4h['trend_up'] == 0) & (df_4h['prev_trend'] == 1), 1, 0)

    # Shift & Merge
    df_4h_shifted = df_4h.shift(1)
    df_1h = df.join(df_4h_shifted[['ema_fast', 'ema_slow', 'atr', 'signal_buy', 'signal_sell']], rsuffix='_4h')
    df_1h.ffill(inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST INDIVIDUAL (RETORNA RESULTADOS)
# ======================================================

def backtest_symbol(symbol, df):
    balance = INITIAL_BALANCE_PER_COIN
    peak_balance = balance 
    position = None 
    entry_price = 0; quantity = 0; sl = 0; entry_comm = 0
    
    trades = []
    equity_curve = [] # Lista de tuplas (timestamp, equity)

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        atr_4h = row.atr
        signal_buy = row.signal_buy == 1
        signal_sell = row.signal_sell == 1
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # Entrada
        if position is None and signal_buy:
            entry_price = o * (1 + friction)
            sl_price = entry_price - (atr_4h * SL_ATR_MULT)
            risk_dist = entry_price - sl_price
            
            if risk_dist > 0:
                risk_usd = peak_balance * FIXED_RISK_PCT
                qty = min(risk_usd / risk_dist, (balance * MAX_LEVER) / entry_price)
                
                if qty >= MIN_QTY:
                    entry_comm = qty * entry_price * COMMISSION
                    balance -= entry_comm
                    position = 'long'; quantity = qty; sl = sl_price
                    entry = entry_price; entry_comm_paid = entry_comm
                    
                    if l <= sl: # Instant crash
                        exit_p = sl * (1 - SLIPPAGE_PCT)
                        pnl = (exit_p - entry) * qty
                        fee = exit_p * qty * COMMISSION
                        balance += (pnl - fee)
                        trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm-fee})
                        position = None

        # Gestión
        elif position == "long":
            exit_p = None
            if signal_sell: exit_p = o * (1 - SLIPPAGE_PCT)
            elif l <= sl: exit_p = sl * (1 - SLIPPAGE_PCT)
            
            if exit_p:
                pnl = (exit_p - entry) * quantity
                fee = exit_p * quantity * COMMISSION
                balance += (pnl - fee)
                if balance > peak_balance: peak_balance = balance
                trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm_paid-fee})
                position = None
        
        # Equity Tracking
        curr_eq = balance
        if position == 'long': curr_eq += (c - entry) * quantity
        equity_curve.append({'timestamp': ts, 'equity': curr_eq})

    return trades, equity_curve, balance

# ======================================================
#  🌐 EJECUCIÓN GLOBAL
# ======================================================

def run_portfolio():
    print(f"\n🌍 INICIANDO PORTFOLIO BACKTEST (V59 LOGIC) en {len(SYMBOLS)} ACTIVOS")
    print("="*60)
    
    all_trades = []
    portfolio_equity = pd.DataFrame()
    final_balances = {}
    
    for symbol in SYMBOLS:
        df = process_symbol(symbol)
        if df is not None:
            trades, equity_data, final_bal = backtest_symbol(symbol, df)
            
            # Guardar resultados
            all_trades.extend(trades)
            final_balances[symbol] = final_bal
            
            # Procesar equity curve para sumar al portafolio
            eq_df = pd.DataFrame(equity_data).set_index('timestamp')
            eq_df.rename(columns={'equity': symbol}, inplace=True)
            
            if portfolio_equity.empty:
                portfolio_equity = eq_df
            else:
                # Merge exterior para alinear fechas diferentes
                portfolio_equity = portfolio_equity.join(eq_df, how='outer')
    
    # Rellenar huecos (forward fill para mantener equity si no hay datos nuevos)
    portfolio_equity.ffill(inplace=True)
    portfolio_equity.fillna(INITIAL_BALANCE_PER_COIN, inplace=True) # Inicio
    
    # Sumar todo para Equity Total
    portfolio_equity['Total'] = portfolio_equity.sum(axis=1)
    
    # --- RESULTADOS ---
    print("\n" + "="*60)
    print("📊 REPORTE DE PORTFOLIO CONSOLIDADO")
    print("="*60)
    
    initial_total = INITIAL_BALANCE_PER_COIN * len(final_balances)
    final_total = sum(final_balances.values())
    total_ret = (final_total - initial_total) / initial_total * 100
    
    # Drawdown Global
    peak = portfolio_equity['Total'].cummax()
    dd = (portfolio_equity['Total'] - peak) / peak
    max_dd = dd.min() * 100
    
    print(f"💰 Capital Inicial:   ${initial_total:.2f}")
    print(f"💰 Capital Final:     ${final_total:.2f}")
    print(f"🚀 Retorno Total:     {total_ret:.2f}%")
    print(f"📉 Max Drawdown:      {max_dd:.2f}% (En el portafolio global)")
    
    if all_trades:
        trades_df = pd.DataFrame(all_trades)
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate Global:   {win:.2f}%")
        print(f"🧮 Total Trades:      {len(trades_df)}")
        print("\n📅 RENDIMIENTO ANUAL CONSOLIDADO:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        
        print("\n🏅 RENDIMIENTO POR ACTIVO:")
        for sym, bal in final_balances.items():
            ret = (bal - INITIAL_BALANCE_PER_COIN)/INITIAL_BALANCE_PER_COIN*100
            print(f"   {sym}: {ret:6.2f}%")
            
    else:
        print("⚠️ No se generaron trades en ningún activo.")

if __name__ == "__main__":
    run_portfolio()