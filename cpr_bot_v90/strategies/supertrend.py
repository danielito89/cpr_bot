#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V100 – INSTITUTIONAL TREND (PORTFOLIO)
# ======================================================

# CANASTA DE ACTIVOS
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "1000PEPEUSDT"]
TIMEFRAME_STR = "1h"

# ---- Estrategia: GOLDEN CROSS 4H MEJORADO ----
FAST_EMA = 50
SLOW_EMA = 200

# ---- MEJORA 1: Filtro de Fuerza (ADX) ----
USE_ADX_FILTER = False
ADX_THRESHOLD = 25      # Solo operar si la tendencia es fuerte

# ---- MEJORA 4: Filtro Diario (The Tide) ----
USE_DAILY_FILTER = False
DAILY_EMA_PERIOD = 200  # Solo Longs si Precio > EMA 200 Diaria

# ---- Salidas ----
SL_ATR_MULT = 3.0       # Stop Loss Catastrófico

# ---- Risk & Microestructura ----
INITIAL_BALANCE_PER_COIN = 2000
FIXED_RISK_PCT = 0.05   
MAX_LEVER = 5           

COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001
MIN_QTY = 0.01

# ======================================================
#  1. FUNCIÓN DE CARGA
# ======================================================
def load_data(symbol):
    # Busca en las rutas de tu proyecto actual
    candidates = [
        f"data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"data/{symbol}_{TIMEFRAME_STR}.csv",
        f"cpr_bot_v90/data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv"
    ]
    
    df = None
    for path in candidates:
        if os.path.exists(path):
            print(f"   📄 Cargando {symbol} desde {path}...")
            df = pd.read_csv(path)
            break
            
    if df is None: return None

    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    # Fix Timezone
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
    
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)
    if 'volume' not in df.columns: df['volume'] = 1.0
    
    return df

# ======================================================
#  2. LÓGICA CORE MULTI-TIMEFRAME
# ======================================================
def process_symbol(symbol):
    df = load_data(symbol)
    if df is None: return None

    # --- A. TIME FRAME 4H (LA SEÑAL) ---
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    
    # Indicadores 4H
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    df_4h['adx'] = talib.ADX(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Señal Base (Cruce)
    df_4h['trend_up'] = np.where(df_4h['ema_fast'] > df_4h['ema_slow'], 1, 0)
    df_4h['prev_trend'] = df_4h['trend_up'].shift(1)
    
    df_4h['signal_buy'] = np.where((df_4h['trend_up'] == 1) & (df_4h['prev_trend'] == 0), 1, 0)
    df_4h['signal_sell'] = np.where((df_4h['trend_up'] == 0) & (df_4h['prev_trend'] == 1), 1, 0)

    # Shift 4H (Anti-Lookahead)
    df_4h_shifted = df_4h.shift(1)

    # --- B. TIME FRAME 1D (EL FILTRO MACRO) ---
    df_1d = df.resample('1D').apply(ohlc_dict).dropna()
    df_1d['ema_daily'] = talib.EMA(df_1d['close'], timeperiod=DAILY_EMA_PERIOD)
    # Filtro: Close > EMA Diario
    df_1d['daily_trend_ok'] = np.where(df_1d['close'] > df_1d['ema_daily'], 1, 0)
    
    # Shift 1D (Anti-Lookahead)
    df_1d_shifted = df_1d.shift(1)

    # --- C. MERGE TODO A 1H ---
    # 1. Unir 4H a 1H
    cols_4h = ['ema_fast', 'ema_slow', 'atr', 'adx', 'signal_buy', 'signal_sell']
    df_merged = df.join(df_4h_shifted[cols_4h], rsuffix='_4h')
    df_merged.ffill(inplace=True)
    
    # 2. Unir 1D a 1H
    cols_1d = ['daily_trend_ok']
    df_merged = df_merged.join(df_1d_shifted[cols_1d], rsuffix='_1d')
    df_merged.ffill(inplace=True)
    
    df_merged.dropna(inplace=True)
    df_merged.reset_index(inplace=True)
    
    return df_merged

# ======================================================
#  🚀 BACKTEST INDIVIDUAL
# ======================================================
def backtest_symbol(symbol, df):
    balance = INITIAL_BALANCE_PER_COIN
    peak_balance = balance 
    position = None 
    entry_price = 0; quantity = 0; sl = 0; entry_comm = 0
    
    trades = []
    equity_curve = [] 

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Datos Multi-TF
        atr_4h = row.atr
        adx_4h = row.adx
        daily_ok = row.daily_trend_ok == 1
        
        signal_buy = row.signal_buy == 1
        signal_sell = row.signal_sell == 1 # Death Cross
        
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # --- LÓGICA DE FILTRADO INSTITUCIONAL ---
        # 1. Filtro ADX (Opcional)
        filter_adx_pass = (adx_4h > ADX_THRESHOLD) if USE_ADX_FILTER else True
        
        # 2. Filtro Diario (Opcional)
        filter_daily_pass = daily_ok if USE_DAILY_FILTER else True

        # ENTRADA
        if position is None and signal_buy:
            
            # APLICAMOS LOS FILTROS AQUÍ
            if filter_adx_pass and filter_daily_pass:
                
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

        # GESTIÓN
        elif position == "long":
            exit_p = None
            # Salida solo por Estructura 4H (Death Cross) o SL
            if signal_sell: exit_p = o * (1 - SLIPPAGE_PCT)
            elif l <= sl: exit_p = sl * (1 - SLIPPAGE_PCT)
            
            if exit_p:
                pnl = (exit_p - entry) * quantity
                fee = exit_p * quantity * COMMISSION
                balance += (pnl - fee)
                if balance > peak_balance: peak_balance = balance
                trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm_paid-fee})
                position = None
        
        curr_eq = balance
        if position == 'long': curr_eq += (c - entry) * quantity
        equity_curve.append({'timestamp': ts, 'equity': curr_eq})

    return trades, equity_curve, balance

# ======================================================
#  🌐 EJECUCIÓN GLOBAL
# ======================================================
def run_portfolio():
    print(f"\n🌍 V100 INSTITUTIONAL TREND - PORTFOLIO BACKTEST")
    print(f"   ADX Filter: {USE_ADX_FILTER} (> {ADX_THRESHOLD})")
    print(f"   Daily Filter: {USE_DAILY_FILTER} (Price > EMA200 Daily)")
    print("="*60)
    
    all_trades = []
    portfolio_equity = pd.DataFrame()
    final_balances = {}
    
    for symbol in SYMBOLS:
        df = process_symbol(symbol)
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
    
    # RESULTADOS
    print("\n" + "="*60)
    print("📊 REPORTE V100 CONSOLIDADO")
    print("="*60)
    
    initial_total = INITIAL_BALANCE_PER_COIN * len(final_balances)
    final_total = sum(final_balances.values())
    
    if initial_total == 0:
        print("⚠️ No hay datos iniciales.")
        return

    total_ret = (final_total - initial_total) / initial_total * 100
    
    peak = portfolio_equity['Total'].cummax()
    dd = (portfolio_equity['Total'] - peak) / peak
    max_dd = dd.min() * 100
    
    print(f"💰 Inicial:   ${initial_total:.2f}")
    print(f"💰 Final:     ${final_total:.2f}")
    print(f"🚀 Retorno:   {total_ret:.2f}%")
    print(f"📉 Max DD:    {max_dd:.2f}%")
    
    if all_trades:
        trades_df = pd.DataFrame(all_trades)
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:  {win:.2f}%")
        print(f"🧮 Trades:    {len(trades_df)}")
        print("\n📅 RENDIMIENTO ANUAL:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        
        print("\n🏅 POR ACTIVO:")
        for sym, bal in final_balances.items():
            ret = (bal - INITIAL_BALANCE_PER_COIN)/INITIAL_BALANCE_PER_COIN*100
            print(f"   {sym}: {ret:6.2f}%")
            
    else:
        print("⚠️ No se generaron trades (Filtros demasiado estrictos).")

if __name__ == "__main__":
    run_portfolio()