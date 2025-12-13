#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🐻 CONFIG V70 – THE BEAR HUNTER (SHORT ONLY)
# ======================================================

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"]
TIMEFRAME_STR = "1h"

# ---- Parámetros de Estrategia ----
# CAPA 1 & 2: Tendencia
EMA_FAST = 50
EMA_SLOW = 200

# CAPA 4 & 5: Salidas (R-Multiples)
SL_ATR_BUFFER = 0.5     # Buffer sobre el High de la vela de rechazo
RISK_REWARD_RATIO = 1.5 # Buscamos ganar 1.5 veces lo arriesgado (TP Fijo)
MAX_HOLD_BARS = 24      # (Opcional) Si en 24h no cae, salir. El short debe ser rápido.

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.015  # ⚠️ RIESGO BAJO (1.5%) para Shorts
MAX_LEVER = 3           # Apalancamiento bajo para shorts (seguridad)

COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001
MIN_QTY = 0.01

# ======================================================
#  1. CARGA Y PREPARACIÓN (MULTI-TIMEFRAME)
# ======================================================
def load_data(symbol):
    # Rutas de búsqueda
    candidates = [
        f"data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"data/{symbol}_{TIMEFRAME_STR}.csv",
        f"cpr_bot_v90/data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"{symbol}_{TIMEFRAME_STR}.csv" # Fallback local
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
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
    
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)
    if 'volume' not in df.columns: df['volume'] = 1.0
    
    return df

def process_symbol(symbol):
    df = load_data(symbol)
    if df is None: return None

    # --- A. MACRO: 1D (FILTRO DE RÉGIMEN) ---
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_1d = df.resample('1D').apply(ohlc_dict).dropna()
    df_1d['ema50_d'] = talib.EMA(df_1d['close'], timeperiod=EMA_FAST)
    df_1d['ema200_d'] = talib.EMA(df_1d['close'], timeperiod=EMA_SLOW)
    
    # Condición Capa 1: Bear Market Regime
    df_1d['bear_regime'] = np.where(df_1d['ema50_d'] < df_1d['ema200_d'], 1, 0)
    
    # Shift 1D para no ver futuro
    df_1d_shifted = df_1d.shift(1)[['bear_regime']]

    # --- B. TACTICAL: 4H (SETUP & TRIGGER) ---
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    df_4h['ema50_4h'] = talib.EMA(df_4h['close'], timeperiod=EMA_FAST)
    df_4h['ema200_4h'] = talib.EMA(df_4h['close'], timeperiod=EMA_SLOW)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Condición Capa 2: Tendencia 4H Bajista Clara
    # EMA 50 < 200 AND Precio < EMA 50 (No queremos shortear si el precio ya rompió la media hacia arriba)
    trend_down_4h = (df_4h['ema50_4h'] < df_4h['ema200_4h'])
    
    # Condición Capa 3: Trigger "Touch & Reject"
    # El precio (High) tocó la EMA 50, pero cerró ABAJO.
    # High >= EMA50 AND Close < EMA50
    rejection = (df_4h['high'] >= df_4h['ema50_4h']) & (df_4h['close'] < df_4h['ema50_4h'])
    
    df_4h['signal_short'] = np.where(trend_down_4h & rejection, 1, 0)
    
    # Shift 4H
    cols_4h = ['signal_short', 'atr', 'high', 'ema50_4h'] # High necesario para SL
    df_4h_shifted = df_4h.shift(1)[cols_4h].rename(columns={'high': 'signal_candle_high'})

    # --- C. MERGE A 1H (EJECUCIÓN) ---
    df_merged = df.join(df_1d_shifted, rsuffix='_1d')
    df_merged = df_merged.join(df_4h_shifted, rsuffix='_4h')
    
    # Llenar huecos hacia adelante (la señal de las 08:00 vale hasta las 12:00)
    df_merged.ffill(inplace=True)
    df_merged.dropna(inplace=True)
    df_merged.reset_index(inplace=True)
    
    return df_merged

# ======================================================
#  🚀 BACKTEST ENGINE (SHORT ONLY)
# ======================================================
def backtest_symbol(symbol, df):
    balance = INITIAL_BALANCE
    peak_balance = balance 
    position = None 
    entry_price = 0; quantity = 0; sl = 0; tp = 0; entry_comm = 0
    bars_held = 0
    
    trades = []
    equity_curve = [] 

    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Filtros
        bear_regime = row.bear_regime == 1
        signal_short = row.signal_short == 1
        
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # --- ENTRADA (SOLO SHORT) ---
        if position is None:
            # Solo si Régimen Diario es Bajista Y Señal 4H activa
            if bear_regime and signal_short:
                
                # Ejecución al Open de la siguiente vela de 1H tras el cierre de 4H
                # (Como ya hicimos shift, 'signal_short' es 1 si la vela 4H previa fue rechazo)
                
                real_entry = o * (1 - friction) # Venta: precio baja por slippage
                
                # CAPA 4: SL Estructural
                # High de la vela de rechazo + Buffer ATR
                ref_high = row.signal_candle_high
                atr = row.atr
                
                sl_price = ref_high + (atr * SL_ATR_BUFFER)
                
                # Protección: Si el SL está por debajo de la entrada (raro pero posible en gaps), forzar mínimo
                if sl_price <= real_entry: sl_price = real_entry + atr
                
                risk_dist = sl_price - real_entry
                
                # CAPA 5: TP Fijo (Riesgo Asimétrico)
                tp_dist = risk_dist * RISK_REWARD_RATIO
                tp_price = real_entry - tp_dist
                
                if risk_dist > 0:
                    risk_usd = balance * FIXED_RISK_PCT # Uso Balance actual (Conservador)
                    qty = min(risk_usd / risk_dist, (balance * MAX_LEVER) / real_entry)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * real_entry * COMMISSION
                        balance -= entry_comm
                        position = 'short'
                        quantity = qty
                        sl = sl_price
                        tp = tp_price
                        entry_price = real_entry
                        bars_held = 0
                        
                        # Intra-candle crash check (Short Squeeze instantáneo)
                        if h >= sl:
                            exit_p = sl * (1 + SLIPPAGE_PCT)
                            pnl = (entry_price - exit_p) * qty
                            fee = exit_p * qty * COMMISSION
                            balance += (pnl - fee)
                            trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm-fee, 'type': 'SL Instant'})
                            position = None

        # --- GESTIÓN ---
        elif position == "short":
            exit_p = None
            reason = None
            bars_held += 1
            
            # 1. Stop Loss Hit
            if h >= sl:
                exit_p = sl * (1 + SLIPPAGE_PCT)
                reason = "SL"
            
            # 2. Take Profit Hit
            elif l <= tp:
                exit_p = tp * (1 - SLIPPAGE_PCT) # Compra para cerrar
                reason = "TP Target"
            
            # 3. Time Stop (Opcional - los shorts no deben envejecer)
            elif bars_held > MAX_HOLD_BARS:
                exit_p = c * (1 + SLIPPAGE_PCT)
                reason = "Time Stop"
            
            if exit_p:
                pnl = (entry_price - exit_p) * quantity # PnL Short: Entrada - Salida
                fee = exit_p * quantity * COMMISSION
                balance += (pnl - fee)
                trades.append({'symbol': symbol, 'year': ts.year, 'pnl': pnl-entry_comm-fee, 'type': reason})
                position = None
        
        curr_eq = balance
        if position == 'short': 
            # Mark to market short
            curr_eq += (entry_price - c) * quantity
        equity_curve.append({'timestamp': ts, 'equity': curr_eq})

    return trades, equity_curve, balance

# ======================================================
#  RUNNER
# ======================================================
def run_portfolio():
    print(f"\n🐻 V70 BEAR HUNTER (SHORT ONLY) - BACKTEST")
    print(f"   Regime: Daily EMA50 < EMA200")
    print(f"   Setup: 4H EMA50 < 200 + Rejection")
    print(f"   Risk: {FIXED_RISK_PCT*100}% per trade | Reward Target: {RISK_REWARD_RATIO}R")
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
    portfolio_equity.fillna(INITIAL_BALANCE, inplace=True)
    portfolio_equity['Total'] = portfolio_equity.sum(axis=1)
    
    # Reporte
    initial_total = INITIAL_BALANCE * len(final_balances)
    final_total = sum(final_balances.values())
    total_ret = (final_total - initial_total) / initial_total * 100
    
    peak = portfolio_equity['Total'].cummax()
    dd = (portfolio_equity['Total'] - peak) / peak
    max_dd = dd.min() * 100
    
    print("\n" + "="*60)
    print(f"💰 Inicial:   ${initial_total:.2f}")
    print(f"💰 Final:     ${final_total:.2f}")
    print(f"🚀 Retorno:   {total_ret:.2f}%")
    print(f"📉 Max DD:    {max_dd:.2f}%")
    
    if all_trades:
        trades_df = pd.DataFrame(all_trades)
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:  {win:.2f}%")
        print(f"🧮 Trades:    {len(trades_df)}")
        print("\n📅 RENDIMIENTO ANUAL (Busca ganancias en 2022):")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        
        print("\n🏅 POR ACTIVO:")
        for sym, bal in final_balances.items():
            ret = (bal - INITIAL_BALANCE)/INITIAL_BALANCE*100
            print(f"   {sym}: {ret:6.2f}%")

if __name__ == "__main__":
    run_portfolio()