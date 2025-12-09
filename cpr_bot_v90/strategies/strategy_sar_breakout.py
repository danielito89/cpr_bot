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
# ⚙️ CONFIGURACIÓN V24 (TREND FILTER)
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

# --- FILTRO DE TENDENCIA (NUEVO) ---
# Usamos EMA 4800 en 1H para simular la EMA 200 Diaria (200 * 24 = 4800)
USE_TREND_FILTER = True
TREND_EMA_PERIOD = 4800 

# --- RIESGO Y MICROESTRUCTURA ---
INITIAL_BALANCE = 1000.0   
BASE_RISK_PCT = 0.02        
COMMISSION_RATE = 0.0006    
LATENCY_PENALTY = 0.0001    
SLIPPAGE_K = 0.05           
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
    
    # Gap Detection
    df['time_diff'] = df['timestamp'].diff().dt.total_seconds()
    GAP_THRESHOLD = 7200
    df['gap_detected'] = df['time_diff'] > GAP_THRESHOLD
    
    if df['gap_detected'].sum() > 0:
        print(f"⚠️ Gaps detectados: {df['gap_detected'].sum()} velas marcadas.")
        
    df.reset_index(drop=True, inplace=True)
    return df

def calculate_indicators(df):
    print("🧮 Calculando indicadores con Trend Filter...")
    
    if HAS_TALIB:
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=ATR_PERIOD)
        df['sar'] = talib.SAR(df['high'], df['low'], acceleration=SAR_AF_START, maximum=SAR_AF_MAX)
        # EMA de Tendencia
        df['trend_ema'] = talib.EMA(df['close'], timeperiod=TREND_EMA_PERIOD)
    else:
        # Fallback Nativo
        high = df['high']; low = df['low']; close = df['close'].shift(1)
        tr = pd.concat([high - low, (high - close).abs(), (low - close).abs()], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=ATR_PERIOD).mean()
        df['sar'] = df['close'].ewm(span=10).mean() # Fallback simple
        df['trend_ema'] = df['close'].ewm(span=TREND_EMA_PERIOD).mean()

    # PDH Seguro
    df['date_only'] = df['timestamp'].dt.date
    daily_counts = df.groupby('date_only')['timestamp'].count()
    daily_highs = df.groupby('date_only')['high'].max()
    valid_days = daily_counts[daily_counts >= 23].index
    safe_daily_highs = daily_highs.loc[valid_days]
    safe_daily_highs_shifted = safe_daily_highs.copy()
    safe_daily_highs_shifted.index = safe_daily_highs_shifted.index + timedelta(days=1)
    df['pdh'] = df['date_only'].map(safe_daily_highs_shifted)
    
    df.dropna(subset=['atr', 'sar', 'trend_ema'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ==========================================
# 🚀 MOTOR DE SIMULACIÓN V24
# ==========================================

def run_simulation(symbol):
    df = load_data(symbol)
    if df is None: return
    try: df = calculate_indicators(df)
    except Exception as e: print(e); return

    print(f"🚀 Iniciando Backtest V24 (Trend Filter) para {symbol}...")
    
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
    
    pending_active = False
    pending_trigger = 0.0
    pending_start_time = None

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
        trend_ema = df.at[i, 'trend_ema']
        is_gap = df.at[i, 'gap_detected']
        
        if is_gap:
            cooldown_counter = 24 
            if pending_active: pending_active = False
        
        if cooldown_counter > 0:
            cooldown_counter -= 1
        
        # Slippage Dinámico
        current_slippage_pct = SLIPPAGE_K * (atr / close) if close > 0 else 0.0005
        effective_slippage_in = current_slippage_pct + LATENCY_PENALTY
        effective_slippage_out = current_slippage_pct

        # --- GESTIÓN SALIDA ---
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
                
                # PnL Calc
                entry_value_nominal = position_size_contracts * entry_price # Costo nominal
                gross_pnl = exit_value - entry_value_nominal
                net_pnl = gross_pnl - entry_comm_paid - exit_comm
                
                balance_delta = gross_pnl - exit_comm
                balance += balance_delta
                
                if balance > peak_balance: peak_balance = balance
                equity_curve.append(balance)
                
                trades.append({
                    'year': ts.year,
                    'net_pnl': net_pnl, 
                    'commissions': entry_comm_paid + exit_comm,
                })
                
                position = None
                pending_active = False
                continue

        # --- ORDEN PENDIENTE ---
        if position is None and pending_active:
            if (ts - pending_start_time).total_seconds() > EXPIRATION_HOURS * 3600:
                pending_active = False
            
            elif high >= pending_trigger:
                # FILTRO DE TENDENCIA (Check final antes de ejecutar)
                # Si el precio cruzó la EMA hacia abajo violentamente, abortamos?
                # O confiamos en que el filtro se aplicó al crear la señal.
                # Lo aplicamos al CREAR la señal para ser más limpios.
                
                base_execution_price = max(open_p, pending_trigger)
                real_entry = base_execution_price * (1 + effective_slippage_in)
                technical_sl = pending_trigger - (atr * ATR_SL_MULT)
                risk_distance = real_entry - technical_sl
                
                if risk_distance > 0:
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

        # --- SEÑAL DE ENTRADA ---
        if position is None and not pending_active and cooldown_counter == 0:
            if not np.isnan(pdh):
                # CONDICIONES V24:
                # 1. SAR configuración (SAR > Close) -> Esto es "SAR Bajista" en teoría, para Breakout? 
                #    Revisando lógica original: "SAR en descenso" = Puntos Arriba del precio bajando.
                #    Correcto. Apostamos a reversión/breakout del PDH.
                
                sar_condition = sar > close
                
                # 2. TREND FILTER (NUEVO)
                # Solo tomamos Longs si estamos en Bull Market (Close > EMA 200 daily)
                if USE_TREND_FILTER:
                    trend_ok = close > trend_ema
                else:
                    trend_ok = True
                
                if sar_condition and trend_ok:
                    pending_active = True
                    pending_trigger = pdh
                    pending_start_time = ts

    # REPORTING SIMPLIFICADO
    trades_df = pd.DataFrame(trades)
    if trades_df.empty: print("⚠️ Sin trades."); return

    total_ret = ((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100
    eq_series = pd.Series(equity_curve)
    max_dd_pct = ((eq_series - eq_series.cummax()) / eq_series.cummax()).min() * 100
    
    print("\n" + "="*50)
    print(f"📊 REPORTE FINAL V24 (Trend Filter): {symbol}")
    print("="*50)
    print(f"💰 Balance Final:    ${balance:.2f}")
    print(f"🚀 Retorno Total:    {total_ret:.2f}%")
    print(f"📉 Max Drawdown:     {max_dd_pct:.2f}%")
    print("-" * 50)
    
    print("📅 RENDIMIENTO POR AÑO:")
    stats = trades_df.groupby('year')['net_pnl'].agg(['sum', 'count'])
    print(stats)
    print("="*50 + "\n")

if __name__ == "__main__":
    run_simulation(SYMBOL)