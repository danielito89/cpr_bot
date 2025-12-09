import pandas as pd
import numpy as np
import os
from datetime import timedelta

# Intentamos importar TA-Lib
try:
    import talib
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False
    print("⚠️ ADVERTENCIA: TA-Lib no encontrado. Usando cálculos nativos.")

# ==========================================
# ⚙️ CONFIGURACIÓN FINAL (V23)
# ==========================================
SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# --- ESTRATEGIA ---
ATR_PERIOD = 135
ATR_SL_MULT = 1.1
SAR_AF_START = 0.02
SAR_AF_MAX = 0.2
EXPIRATION_HOURS = 5
EXIT_HOURS = 9

# --- RIESGO Y MICROESTRUCTURA ---
INITIAL_BALANCE = 10000.0   
BASE_RISK_PCT = 0.02        
COMMISSION_RATE = 0.0006    
LATENCY_PENALTY = 0.0001    
SLIPPAGE_K = 0.05           
DD_BRAKE_THRESHOLD = 0.10   
DD_BRAKE_FACTOR = 0.5       

# ==========================================
# 🛠️ CARGA Y LIMPIEZA BLINDADA
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
    
    # --- 🛡️ VALIDACIÓN DE GAPS INTRADÍA (TU APORTE CRÍTICO) ---
    # Calculamos la diferencia en segundos entre velas consecutivas
    df['time_diff'] = df['timestamp'].diff().dt.total_seconds()
    
    # Umbral: Gap > 2 horas (7200s) es inaceptable para continuidad de indicadores
    GAP_THRESHOLD = 7200
    bad_rows = df[df['time_diff'] > GAP_THRESHOLD]
    
    if len(bad_rows) > 0:
        print(f"⚠️ ALERTA DE DATOS: Se detectaron {len(bad_rows)} gaps intradía graves (>2h).")
        print("   -> Estos saltos rompen la continuidad del ATR y SAR.")
        print("   -> Acción: Se invalidarán las señales inmediatamente posteriores a estos gaps.")
        
        # Marcamos una columna 'invalid_continuity' para no operar justo después del gap
        # hasta que los indicadores se estabilicen (ej. 24 velas después)
        df['gap_detected'] = df['time_diff'] > GAP_THRESHOLD
    else:
        df['gap_detected'] = False
        
    df.reset_index(drop=True, inplace=True)
    return df

def calculate_indicators(df):
    print("🧮 Calculando indicadores Bulletproof...")
    
    # 1. ATR 
    if HAS_TALIB:
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=ATR_PERIOD)
    else:
        high = df['high']; low = df['low']; close = df['close'].shift(1)
        tr = pd.concat([high - low, (high - close).abs(), (low - close).abs()], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=ATR_PERIOD).mean()

    # 2. SAR
    if HAS_TALIB:
        df['sar'] = talib.SAR(df['high'], df['low'], acceleration=SAR_AF_START, maximum=SAR_AF_MAX)
    else:
        # Fallback simple (EWM)
        df['sar'] = df['close'].ewm(span=10).mean()

    # 3. PDH (Daily High)
    print("   👉 Mapeando PDH seguro...")
    df['date_only'] = df['timestamp'].dt.date
    
    # Contamos velas reales por día
    daily_counts = df.groupby('date_only')['timestamp'].count()
    daily_highs = df.groupby('date_only')['high'].max()
    
    # Filtro estricto: Días con menos de 23 velas NO generan PDH confiable
    valid_days = daily_counts[daily_counts >= 23].index
    safe_daily_highs = daily_highs.loc[valid_days]
    
    safe_daily_highs_shifted = safe_daily_highs.copy()
    safe_daily_highs_shifted.index = safe_daily_highs_shifted.index + timedelta(days=1)
    
    df['pdh'] = df['date_only'].map(safe_daily_highs_shifted)
    
    # Limpieza final
    df.dropna(subset=['atr', 'sar'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ==========================================
# 🚀 MOTOR DE SIMULACIÓN (V23)
# ==========================================

def run_simulation(symbol):
    df = load_data(symbol)
    if df is None: return
    try: df = calculate_indicators(df)
    except Exception as e: print(e); return

    print(f"🚀 Iniciando Backtest V23 para {symbol}...")
    
    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance
    trades = []
    
    position = None 
    entry_price = 0.0
    sl_price = 0.0
    entry_time = None
    position_size_contracts = 0.0 
    
    entry_risk_amount_usd = 0.0 
    entry_comm_paid = 0.0       
    
    pending_active = False
    pending_trigger = 0.0
    pending_start_time = None

    # Contador para enfriamiento post-gap
    # Si hubo un gap, esperamos ATR_PERIOD velas para confiar en el ATR de nuevo
    cooldown_counter = 0

    for i in range(len(df)):
        ts = df.at[i, 'timestamp']
        high = df.at[i, 'high']
        low = df.at[i, 'low']
        close = df.at[i, 'close']
        open_p = df.at[i, 'open']
        pdh = df.at[i, 'pdh']
        atr = df.at[i, 'atr']
        sar = df.at[i, 'sar']
        is_gap = df.at[i, 'gap_detected']
        
        # --- LÓGICA DE COOLDOWN POR GAP ---
        if is_gap:
            # Si detectamos gap en esta vela (respecto a la anterior), activamos cooldown
            # Necesitamos recargar el buffer de indicadores (ej. 24h)
            cooldown_counter = 24 
            # Si teníamos orden pendiente, la matamos por seguridad
            if pending_active:
                pending_active = False
            # (Opcional: Si hubiera posición abierta, se gestiona con gap logic abajo, no se cierra forzado)
        
        if cooldown_counter > 0:
            cooldown_counter -= 1
            # Si estamos en enfriamiento, saltamos la búsqueda de nuevas señales
            # pero DEBEMOS gestionar posiciones abiertas
        
        # Slippage Dinámico
        current_slippage_pct = SLIPPAGE_K * (atr / close) if close > 0 else 0.0005
        effective_slippage_in = current_slippage_pct + LATENCY_PENALTY
        effective_slippage_out = current_slippage_pct

        # --- A. GESTIÓN SALIDA ---
        if position == 'long':
            exit_price = None
            exit_reason = ""
            
            hit_sl = low <= sl_price
            hit_time = (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600
            
            if hit_sl:
                if open_p < sl_price:
                    exit_reason = "SL (Gap)"
                    raw_exit = open_p
                else:
                    exit_reason = "SL"
                    raw_exit = sl_price
                exit_price = raw_exit * (1 - effective_slippage_out)
            
            elif hit_time:
                exit_reason = "Time"
                exit_price = close * (1 - effective_slippage_out)
            
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
                    'entry_time': entry_time, 'exit_time': ts, 'year': ts.year,
                    'type': exit_reason, 'net_pnl': net_pnl, 
                    'risk_multiple': net_pnl / entry_risk_amount_usd if entry_risk_amount_usd else 0,
                    'commissions': entry_comm_paid + exit_comm,
                    'slippage_pct_used': effective_slippage_in + effective_slippage_out
                })
                
                position = None
                pending_active = False
                continue

        # --- B. ORDEN PENDIENTE ---
        if position is None and pending_active:
            if (ts - pending_start_time).total_seconds() > EXPIRATION_HOURS * 3600:
                pending_active = False
            
            elif high >= pending_trigger:
                # Entrada Realista (Max de Open vs Trigger)
                base_execution_price = max(open_p, pending_trigger)
                real_entry = base_execution_price * (1 + effective_slippage_in)
                
                technical_sl = pending_trigger - (atr * ATR_SL_MULT)
                risk_distance = real_entry - technical_sl
                
                if risk_distance > 0:
                    # Drawdown Brake
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
                    pending_active = False
                else:
                    pending_active = False

        # --- C. SEÑAL (Solo si no hay Cooldown) ---
        if position is None and not pending_active and cooldown_counter == 0:
            if not np.isnan(pdh):
                if sar > close:
                    pending_active = True
                    pending_trigger = pdh
                    pending_start_time = ts

    # ==========================================
    # 📊 REPORTING
    # ==========================================
    trades_df = pd.DataFrame(trades)
    if trades_df.empty: print("⚠️ Sin trades."); return

    trades_df.to_csv(f"trade_log_v23_{symbol}.csv", index=False)
    
    total_ret = ((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100
    eq_series = pd.Series(equity_curve)
    max_dd_pct = ((eq_series - eq_series.cummax()) / eq_series.cummax()).min() * 100
    
    print("\n" + "="*50)
    print(f"📊 REPORTE FINAL V23 (Bulletproof): {symbol}")
    print("="*50)
    print(f"💰 Balance Final:    ${balance:.2f}")
    print(f"🚀 Retorno Total:    {total_ret:.2f}%")
    print(f"📉 Max Drawdown:     {max_dd_pct:.2f}%")
    print("-" * 50)
    
    win_rate = (len(trades_df[trades_df['net_pnl']>0]) / len(trades_df)) * 100
    print(f"✅ Win Rate:         {win_rate:.2f}%")
    print(f"🔢 Trades Totales:   {len(trades_df)}")

    print("\n📅 RENDIMIENTO POR AÑO:")
    stats = trades_df.groupby('year')['net_pnl'].agg(['sum', 'count'])
    print(stats)
    print("="*50 + "\n")

if __name__ == "__main__":
    run_simulation(SYMBOL)