import pandas as pd
import numpy as np
import os
from datetime import timedelta

# Verificación de TA-Lib (Esencial para BB y RSI)
try:
    import talib
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False
    print("⚠️ ADVERTENCIA CRÍTICA: TA-Lib no encontrado. Esta estrategia lo necesita.")

# ==========================================
# ⚙️ CONFIGURACIÓN V26 (BB + RSI SNIPER)
# ==========================================
SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# --- ESTRATEGIA: REVERSIÓN A LA MEDIA ---
BB_PERIOD = 20
BB_STD_DEV = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 30  # Nivel de compra

# --- SALIDAS ---
ATR_SL_MULT = 1.5  # Un poco más holgado para dejar respirar la caída
EXIT_HOURS = 24    # Tiempo máximo de vida del trade (1 día)

# --- RIESGO Y MICROESTRUCTURA ---
INITIAL_BALANCE = 10000.0   
BASE_RISK_PCT = 0.02        
COMMISSION_RATE = 0.0006    
SLIPPAGE_K = 0.05           
LATENCY_PENALTY = 0.0001
DD_BRAKE_THRESHOLD = 0.10   
DD_BRAKE_FACTOR = 0.5       

# ==========================================
# 🛠️ CARGA Y PROCESAMIENTO
# ==========================================

def load_data(symbol):
    print(f"🔍 Buscando datos para {symbol} ({TIMEFRAME_STR})...")
    possible_filenames = [
        f"mainnet_data_{TIMEFRAME_STR}_{symbol}_2020-2021.csv",
        f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"{symbol}_{TIMEFRAME_STR}.csv"
    ]
    search_paths = ["data", "cpr_bot_v90/data", "."]
    
    df = None
    for filename in possible_filenames:
        for path in search_paths:
            full_path = os.path.join(path, filename)
            if os.path.exists(full_path):
                print(f"✅ Archivo encontrado: {full_path}")
                df = pd.read_csv(full_path)
                break
        if df is not None: break

    if df is None: return None

    df.columns = [c.lower() for c in df.columns]
    if 'open_time' in df.columns: df.rename(columns={'open_time': 'timestamp'}, inplace=True)
    elif 'date' in df.columns: df.rename(columns={'date': 'timestamp'}, inplace=True)
    
    if 'timestamp' not in df.columns: return None
        
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.sort_values('timestamp', inplace=True)
    df.drop_duplicates(subset='timestamp', keep='first', inplace=True)
    
    # Gap Detection (Seguimos protegiéndonos de datos corruptos)
    df['time_diff'] = df['timestamp'].diff().dt.total_seconds()
    df['gap_detected'] = df['time_diff'] > 7200
    
    df.reset_index(drop=True, inplace=True)
    return df

def calculate_indicators(df):
    print("🧮 Calculando indicadores (Bollinger + RSI)...")
    
    if HAS_TALIB:
        # ATR para Stop Loss
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
        
        # RSI
        df['rsi'] = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
        
        # Bollinger Bands
        # matype=0 es SMA (Standard)
        upper, middle, lower = talib.BBANDS(df['close'], timeperiod=BB_PERIOD, nbdevup=BB_STD_DEV, nbdevdn=BB_STD_DEV, matype=0)
        df['bb_upper'] = upper
        df['bb_middle'] = middle # Esta es la SMA 20
        df['bb_lower'] = lower
        
    else:
        print("❌ ERROR: Se requiere TA-Lib para Bollinger Bands correcto.")
        return None

    df.dropna(subset=['atr', 'rsi', 'bb_lower'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ==========================================
# 🚀 MOTOR DE SIMULACIÓN V26 (SNIPER)
# ==========================================

def run_simulation(symbol):
    df = load_data(symbol)
    if df is None: return
    try: df = calculate_indicators(df)
    except Exception as e: print(e); return

    print(f"🚀 Iniciando Backtest V26 (Mean Reversion) para {symbol}...")
    
    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance
    trades = []
    
    position = None 
    entry_time = None
    position_size_contracts = 0.0 
    entry_risk_amount_usd = 0.0 
    entry_comm_paid = 0.0       
    entry_price = 0.0
    sl_price = 0.0
    
    cooldown_counter = 0

    for i in range(len(df)):
        ts = df.at[i, 'timestamp']
        high = df.at[i, 'high']
        low = df.at[i, 'low']
        close = df.at[i, 'close']
        open_p = df.at[i, 'open']
        atr = df.at[i, 'atr']
        rsi = df.at[i, 'rsi']
        
        # Bandas
        bb_lower = df.at[i, 'bb_lower']
        bb_middle = df.at[i, 'bb_middle']
        
        is_gap = df.at[i, 'gap_detected']
        
        # Gestión de Cooldown
        if is_gap: cooldown_counter = 24
        if cooldown_counter > 0: cooldown_counter -= 1
        
        # Slippage Dinámico (Importante en caídas fuertes)
        current_slippage_pct = SLIPPAGE_K * (atr / close) if close > 0 else 0.0005
        
        # --- GESTIÓN DE POSICIÓN ---
        if position == 'long':
            exit_price = None
            exit_reason = ""
            
            # 1. Stop Loss Check
            hit_sl = low <= sl_price
            
            # 2. Time Exit Check
            hit_time = (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600
            
            # 3. TAKE PROFIT: Toque de la Banda Media (Mean Reversion)
            # Si el High de la vela tocó la media, salimos ahí.
            hit_tp = high >= bb_middle
            
            # Lógica de Prioridad (OHLC Simplificada)
            # En reversión a la media, el SL suele estar lejos. El TP está cerca.
            # Asumimos que si toca TP, salimos bien. Si toca ambos, priorizamos SL por seguridad.
            
            if hit_sl:
                # Gap Protection
                raw_exit = open_p if open_p < sl_price else sl_price
                exit_price = raw_exit * (1 - current_slippage_pct)
                exit_reason = "SL"
            
            elif hit_tp:
                # Salimos al precio de la Banda Media (o al Open si abrimos ya por encima, raro)
                # Ojo: No podemos salir "al Open" si la condición es High >= Middle.
                # Salimos al nivel de la banda media.
                raw_exit = bb_middle
                # Si el Open ya estaba arriba de la media, salimos al Open (Gap a favor)
                if open_p > bb_middle:
                    raw_exit = open_p
                
                exit_price = raw_exit * (1 - current_slippage_pct)
                exit_reason = "TP (Mean)"
                
            elif hit_time:
                exit_price = close * (1 - current_slippage_pct)
                exit_reason = "Time"
            
            if exit_price:
                exit_value = position_size_contracts * exit_price
                exit_comm = exit_value * COMMISSION_RATE
                
                entry_value_nominal = position_size_contracts * entry_price 
                gross_pnl = exit_value - entry_value_nominal
                net_pnl = gross_pnl - entry_comm_paid - exit_comm
                
                balance_delta = gross_pnl - exit_comm
                balance += balance_delta
                
                if balance > peak_balance: peak_balance = balance
                equity_curve.append(balance)
                
                trades.append({
                    'year': ts.year,
                    'net_pnl': net_pnl, 
                    'type': exit_reason
                })
                
                position = None
                continue

        # --- BÚSQUEDA DE SEÑAL ---
        if position is None and cooldown_counter == 0:
            
            # CONDICIONES DE ENTRADA (SNIPER)
            # 1. Precio EXTREMO: Cierre por debajo de la Banda Inferior
            #    (Significa que estadísticamente está a 2 desviaciones de lo normal)
            cond_price = close < bb_lower
            
            # 2. RSI SOBREVENDIDO: Menor a 30
            cond_rsi = rsi < RSI_OVERSOLD
            
            if cond_price and cond_rsi:
                # --- ENTRADA ---
                # Entramos al CIERRE de esta vela (o Open de la siguiente, simulamos Open Next)
                # Como backtest OHLC, asumimos entrada al Close de esta vela + Slippage
                # (Es lo más realista si operamos al cierre de la barra)
                
                base_entry = close
                real_entry = base_entry * (1 + current_slippage_pct + LATENCY_PENALTY)
                
                # Stop Loss Dinámico basado en ATR
                technical_sl = real_entry - (atr * ATR_SL_MULT)
                
                risk_distance = real_entry - technical_sl
                
                if risk_distance > 0:
                    # Gestión de Riesgo Anti-Martingala
                    current_dd = (peak_balance - balance) / peak_balance
                    adjusted_risk_pct = BASE_RISK_PCT * DD_BRAKE_FACTOR if current_dd > DD_BRAKE_THRESHOLD else BASE_RISK_PCT
                    
                    risk_amount_usd = balance * adjusted_risk_pct
                    qty_contracts = min(risk_amount_usd / risk_distance, (balance * 2) / real_entry)
                    
                    entry_comm = (qty_contracts * real_entry) * COMMISSION_RATE
                    
                    position = 'long'
                    entry_price = real_entry
                    sl_price = technical_sl
                    position_size_contracts = qty_contracts
                    entry_time = ts
                    entry_risk_amount_usd = risk_amount_usd
                    entry_comm_paid = entry_comm
                    
                    balance -= entry_comm

    # REPORTING
    trades_df = pd.DataFrame(trades)
    if trades_df.empty: print("⚠️ Sin trades."); return

    total_ret = ((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100
    eq_series = pd.Series(equity_curve)
    max_dd_pct = ((eq_series - eq_series.cummax()) / eq_series.cummax()).min() * 100
    
    print("\n" + "="*50)
    print(f"📊 REPORTE FINAL V26 (BB + RSI): {symbol}")
    print("="*50)
    print(f"💰 Balance Final:    ${balance:.2f}")
    print(f"🚀 Retorno Total:    {total_ret:.2f}%")
    print(f"📉 Max Drawdown:     {max_dd_pct:.2f}%")
    print("-" * 50)
    
    win_rate = (len(trades_df[trades_df['net_pnl']>0]) / len(trades_df)) * 100
    print(f"✅ Win Rate:         {win_rate:.2f}%")
    print(f"🔢 Trades Totales:   {len(trades_df)}")

    print("📅 RENDIMIENTO POR AÑO:")
    stats = trades_df.groupby('year')['net_pnl'].agg(['sum', 'count'])
    print(stats)
    print("="*50 + "\n")

if __name__ == "__main__":
    run_simulation(SYMBOL)