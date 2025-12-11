#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
from datetime import timedelta

try:
    import talib
    HAS_TALIB = True
except:
    HAS_TALIB = False
    print("❌ TA-Lib no está instalado. Instálalo para usar V49.")

# ======================================================
#  🔥 CONFIG V49 – SUPERTREND (TREND FOLLOWING)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia Core (Supertrend Simulado) ----
# Queremos simular una tendencia diaria/4h usando velas de 1h.
# Period 24 = 1 Día de datos. Factor 3.0 = Desviación estándar amplia.
ST_PERIOD = 24          
ST_MULTIPLIER = 3.0     

# ---- Filtros ----
# En Trend Following puro, MENOS filtros es MEJOR.
# Queremos estar siempre en el mercado si hay tendencia.
ADX_FILTER = 20         # Solo operar si hay fuerza (ADX > 20)
BAD_HOURS = []          # En swing trading no importan las horas malas

# ---- Salidas ----
# La salida es el propio Supertrend (Trailing Stop técnico).
# No hay TP fijo. Dejamos correr hasta que la tendencia se rompa.
EXIT_HOURS = 336        # 14 días (Swing Trading real)

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.02   # 2% Riesgo por trade
COMMISSION = 0.0004     
SPREAD_PCT = 0.0004     
SLIPPAGE_PCT = 0.0006   
BASE_LATENCY = 0.0001

MIN_QTY = 0.01
QTY_PRECISION = 3 

# ======================================================
#  🧩 DATA LOADING
# ======================================================

def load_data(symbol):
    print(f"🔍 Cargando {symbol} ...")
    candidates = [f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv", f"{symbol}_{TIMEFRAME_STR}.csv"]
    paths = ["data", ".", "cpr_bot_v90/data"]
    
    df = None
    for name in candidates:
        for p in paths:
            path = os.path.join(p, name)
            if os.path.exists(path):
                print(f"📁 Archivo encontrado: {path}")
                df = pd.read_csv(path)
                break
        if df is not None: break

    if df is None: return None

    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert("UTC") 

    df.sort_values("timestamp", inplace=True)
    if 'volume' not in df.columns: df['volume'] = 1.0
    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  📐 INDICADORES (MANUAL SUPERTREND)
# ======================================================

def calc_indicators(df):
    print("📐 Calculando Supertrend V49...")

    if not HAS_TALIB: raise Exception("TA-Lib requerido.")

    # 1. ATR y ADX
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=ST_PERIOD)
    df['adx'] = talib.ADX(df['high'], df['low'], df['close'], timeperiod=14)
    
    # 2. CÁLCULO MANUAL SUPERTREND (Vectorizado Numba-style loop para precisión)
    # Supertrend es recursivo, difícil de vectorizar puro en pandas sin loops o librerías
    # Usaremos un loop simple optimizado.
    
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    atr = df['atr'].values
    
    # Arrays para resultados
    upper_band = np.zeros(len(df))
    lower_band = np.zeros(len(df))
    supertrend = np.zeros(len(df))
    trend = np.zeros(len(df)) # 1 up, -1 down
    
    # Init
    trend[0] = 1
    
    for i in range(1, len(df)):
        # Basic bands
        basic_upper = (high[i] + low[i]) / 2 + (ST_MULTIPLIER * atr[i])
        basic_lower = (high[i] + low[i]) / 2 - (ST_MULTIPLIER * atr[i])
        
        # Upper Band Logic
        if (basic_upper < upper_band[i-1]) or (close[i-1] > upper_band[i-1]):
            upper_band[i] = basic_upper
        else:
            upper_band[i] = upper_band[i-1]
            
        # Lower Band Logic
        if (basic_lower > lower_band[i-1]) or (close[i-1] < lower_band[i-1]):
            lower_band[i] = basic_lower
        else:
            lower_band[i] = lower_band[i-1]
            
        # Trend Logic
        # Si veníamos bajistas (-1) y cerramos arriba de Upper -> Cambio a Alcista
        if trend[i-1] == -1 and close[i] > upper_band[i-1]:
            trend[i] = 1
        # Si veníamos alcistas (1) y cerramos abajo de Lower -> Cambio a Bajista
        elif trend[i-1] == 1 and close[i] < lower_band[i-1]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]
            
        # Supertrend Line
        if trend[i] == 1:
            supertrend[i] = lower_band[i]
        else:
            supertrend[i] = upper_band[i]
            
    df['supertrend'] = supertrend
    df['trend'] = trend # 1 = Bullish, -1 = Bearish
    
    # Shift para no ver el futuro en la entrada
    # La señal de entrada es: Trend[i-1] era -1 y Trend[i] es 1.
    # Pero para ejecutar en backtest, miramos al cierre de 'i' y entramos en 'i+1'.
    
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  🚀 BACKTEST ENGINE – V49
# ======================================================

def run_backtest(symbol):
    df = load_data(symbol)
    if df is None: return
    df = calc_indicators(df)

    print(f"🚀 Iniciando Backtest V49 (Supertrend) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]

    position = None # None, 'long'
    entry = 0; quantity = 0; sl = 0
    entry_time = None
    entry_comm_paid = 0.0
    
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Supertrend Values
        current_trend = row.trend
        st_value = row.supertrend
        prev_trend = df.at[i-1, 'trend'] if i > 0 else 0
        
        # Costos
        total_entry_cost = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # ============================================================
        # 1) BÚSQUEDA DE ENTRADA (LONG ONLY)
        # ============================================================
        # Si la tendencia cambió a Alcista (1) en esta vela cerrada
        # Entramos en la APERTURA de la siguiente (simulada aquí como 'o' actual? 
        # No, iteramos vela cerrada 'i'. La entrada real sería en 'i+1' Open.
        # Pero el loop recorre 'i'.
        # Lógica correcta: Si en 'i-1' la tendencia cambió a Bullish, entramos en Open 'i'.
        
        is_bullish_flip = (prev_trend == 1) and (df.at[i-2, 'trend'] == -1) if i > 1 else False
        # Simplificación: Si estamos en tendencia alcista y no tenemos posición -> Entrar.
        # (Esto permite re-entradas si nos sacó el stop pero la tendencia sigue)
        
        # Señal: Cierre anterior cruzó Supertrend
        signal_buy = (prev_trend == 1) # Ya estamos en verde desde el cierre anterior
        
        # Filtro ADX
        adx_ok = row.adx > ADX_FILTER
        
        if position is None:
            if signal_buy and adx_ok:
                
                # --- EJECUCIÓN ---
                entry_price = o * (1 + total_entry_cost)
                
                # SL Inicial: La línea del Supertrend actual
                # (Es un trailing stop natural)
                sl_price = st_value
                
                # Sizing: Distancia al Supertrend
                risk_dist = entry_price - sl_price
                
                if risk_dist > 0:
                    risk_capital = balance * FIXED_RISK_PCT
                    qty = risk_capital / risk_dist 
                    
                    # Cap de apalancamiento
                    max_qty = (balance * MAX_LEVER) / entry_price
                    qty = min(qty, max_qty)
                    
                    if qty >= MIN_QTY:
                        entry_comm = qty * entry_price * COMMISSION
                        balance -= entry_comm

                        position = "long"
                        entry = entry_price
                        sl = sl_price
                        quantity = qty
                        entry_time = ts
                        entry_comm_paid = entry_comm
                        
                        # Intra-candle Check
                        if l <= sl:
                            exit_price = sl * (1 - SLIPPAGE_PCT)
                            pnl = (exit_price - entry) * qty
                            fee = exit_price * qty * COMMISSION
                            
                            balance += (pnl - fee)
                            net = pnl - entry_comm - fee
                            trades.append({"year": ts.year, "pnl": net, "type": "SL Intra"})
                            position = None

        # ============================================================
        # 2) GESTIÓN DE POSICIÓN
        # ============================================================
        elif position == "long":
            
            exit_price = None
            reason = None
            
            # Actualizar Trailing Stop (Supertrend Line)
            # El Supertrend solo sube en tendencia alcista, nunca baja
            if st_value > sl:
                sl = st_value

            # A) Cambio de Tendencia (Trend Flip)
            # Si la tendencia ya es -1 (Bajista), salimos
            if current_trend == -1:
                exit_price = o * (1 - SLIPPAGE_PCT) # Salimos al Open porque el flip ocurrió al cierre previo
                reason = "Trend Flip"
            
            # B) Stop Loss (El precio tocó la línea Supertrend intradía)
            elif l <= sl:
                exit_raw = o if o < sl else sl # Gap protection
                exit_price = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "SL (Supertrend)"

            if exit_price:
                pnl = (exit_price - entry) * quantity
                exit_comm = exit_price * quantity * COMMISSION
                
                balance += (pnl - exit_comm)
                net = pnl - entry_comm_paid - exit_comm

                trades.append({
                    "year": entry_time.year, "pnl": net, "type": reason
                })
                
                position = None

        # Equity Update
        curr_eq = balance
        if position == "long":
            curr_eq += (c - entry) * quantity
        equity_curve.append(curr_eq)

    # REPORT
    eq_series = pd.Series(equity_curve)
    if len(eq_series) > 0:
        dd = (eq_series - eq_series.cummax()) / eq_series.cummax()
        max_dd = dd.min() * 100
    else: max_dd = 0

    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    trades_df = pd.DataFrame(trades)

    print("\n" + "="*55)
    print(f"📊 RESULTADOS FINALES V49 – SUPERTREND: {symbol}")
    print("="*55)
    print(f"💰 Balance Final:   ${balance:.2f}")
    print(f"📈 Retorno Total:   {total_ret:.2f}%")
    print(f"📉 Max DD:          {max_dd:.2f}%\n")

    if not trades_df.empty:
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:        {win:.2f}%")
        print(f"🧮 Total Trades:    {len(trades_df)}\n")
        try:
            print("📅 RENDIMIENTO POR AÑO:")
            print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        except: pass
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)