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
# ⚙️ CONFIGURACIÓN V25 (TREND + CONFIRMATION)
# ==========================================
SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# --- ESTRATEGIA MODIFICADA ---
ATR_PERIOD = 135
ATR_SL_MULT = 1.1 
SAR_AF_START = 0.02
SAR_AF_MAX = 0.2
EXIT_HOURS = 9

# --- FILTROS ---
USE_EMA_FILTER = True
EMA_PERIOD = 168  # 168 horas = 1 semana (Tendencia de corto plazo)

# --- RIESGO Y MICROESTRUCTURA ---
INITIAL_BALANCE = 10000.0   
BASE_RISK_PCT = 0.02        
COMMISSION_RATE = 0.0006    
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
    df['gap_detected'] = df['time_diff'] > 7200
    
    df.reset_index(drop=True, inplace=True)
    return df

def calculate_indicators(df):
    print("🧮 Calculando indicadores (SAR Bullish + EMA)...")
    
    if HAS_TALIB:
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=ATR_PERIOD)
        df['sar'] = talib.SAR(df['high'], df['low'], acceleration=SAR_AF_START, maximum=SAR_AF_MAX)
        df['ema'] = talib.EMA(df['close'], timeperiod=EMA_PERIOD)
    else:
        # Fallback
        high = df['high']; low = df['low']; close = df['close'].shift(1)
        tr = pd.concat([high - low, (high - close).abs(), (low - close).abs()], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=ATR_PERIOD).mean()
        df['sar'] = df['close'].ewm(span=10).mean() # Fallback malo
        df['ema'] = df['close'].ewm(span=EMA_PERIOD).mean()

    # PDH Seguro
    df['date_only'] = df['timestamp'].dt.date
    daily_counts = df.groupby('date_only')['timestamp'].count()
    daily_highs = df.groupby('date_only')['high'].max()
    valid_days = daily_counts[daily_counts >= 23].index
    safe_daily_highs = daily_highs.loc[valid_days]
    safe_daily_highs_shifted = safe_daily_highs.copy()
    safe_daily_highs_shifted.index = safe_daily_highs_shifted.index + timedelta(days=1)
    df['pdh'] = df['date_only'].map(safe_daily_highs_shifted)
    
    df.dropna(subset=['atr', 'sar', 'ema', 'pdh'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ==========================================
# 🚀 MOTOR DE SIMULACIÓN V25
# ==========================================

def run_simulation(symbol):
    df = load_data(symbol)
    if df is None: return
    try: df = calculate_indicators(df)
    except Exception as e: print(e); return

    print(f"🚀 Iniciando Backtest V25 (Trend + Break&Hold) para {symbol}...")
    
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
    
    # Flag para entrar en la SIGUIENTE vela (Break & Hold)
    next_candle_entry = False
    next_candle_sl = 0.0

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
        ema = df.at[i, 'ema']
        is_gap = df.at[i, 'gap_detected']
        
        # Gestión de Cooldown por Gaps
        if is_gap:
            cooldown_counter = 24 
            next_candle_entry = False # Cancelar entrada si hay gap justo ahora
        
        if cooldown_counter > 0:
            cooldown_counter -= 1
        
        # Slippage Dinámico
        current_slippage_pct = SLIPPAGE_K * (atr / close) if close > 0 else 0.0005
        
        # --- 1. EJECUCIÓN DE ENTRADA (Break & Hold) ---
        # Si la vela ANTERIOR confirmó ruptura, entramos en el OPEN de ESTA vela
        if position is None and next_candle_entry and cooldown_counter == 0:
            
            # Precio de entrada es el OPEN actual
            # Aplicamos Slippage
            real_entry = open_p * (1 + current_slippage_pct)
            technical_sl = next_candle_sl
            risk_distance = real_entry - technical_sl
            
            if risk_distance > 0:
                # Gestión de Riesgo (Anti-Martingala)
                current_dd = (peak_balance - balance) / peak_balance
                adjusted_risk_pct = BASE_RISK_PCT * DD_BRAKE_FACTOR if current_dd > DD_BRAKE_THRESHOLD else BASE_RISK_PCT
                
                risk_amount_usd = balance * adjusted_risk_pct
                # Max contracts: Riesgo / Distancia, capado a 2x leverage
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
                
                # Reset flags
                next_candle_entry = False
            else:
                next_candle_entry = False

        # --- 2. GESTIÓN DE SALIDA ---
        if position == 'long':
            exit_price = None
            exit_reason = ""
            
            hit_sl = low <= sl_price
            hit_time = (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600
            
            if hit_sl:
                # Si abre con Gap abajo del SL, salimos al Open
                if open_p < sl_price:
                    exit_reason = "SL (Gap)"
                    raw_exit = open_p
                else:
                    exit_reason = "SL"
                    raw_exit = sl_price
                exit_price = raw_exit * (1 - current_slippage_pct)
            
            elif hit_time:
                exit_reason = "Time"
                exit_price = close * (1 - current_slippage_pct)
            
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
                    'risk_multiple': net_pnl / entry_risk_amount_usd if entry_risk_amount_usd else 0,
                })
                
                position = None
                next_candle_entry = False # No re-entrar inmediatamente en la misma lógica
                continue

        # --- 3. SEÑAL (CONFIRMACIÓN AL CIERRE) ---
        if position is None and not next_candle_entry and cooldown_counter == 0:
            # LÓGICA V25:
            # 1. Tendencia Alcista: SAR por DEBAJO del precio (SAR < Close)
            trend_sar_bull = sar < close
            
            # 2. Tendencia EMA: Precio por encima de EMA 168 (Semanal)
            if USE_EMA_FILTER:
                trend_ema_bull = close > ema
            else:
                trend_ema_bull = True
            
            # 3. TRIGGER: BREAK & HOLD
            # La vela actual CERRÓ por encima del PDH
            breakout_confirmed = close > pdh
            
            if trend_sar_bull and trend_ema_bull and breakout_confirmed:
                # Señal activada -> Entramos en la APERTURA de la próxima vela
                next_candle_entry = True
                
                # Definimos el SL ahora (usando el ATR de esta vela de señal)
                # Ojo: El SL real se calculará contra el precio de entrada, 
                # pero necesitamos una referencia base.
                # Opción: SL = PDH - ATR * Mult (Nivel técnico fijo)
                next_candle_sl = pdh - (atr * ATR_SL_MULT)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    if trades_df.empty: print("⚠️ Sin trades."); return

    total_ret = ((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100
    eq_series = pd.Series(equity_curve)
    max_dd_pct = ((eq_series - eq_series.cummax()) / eq_series.cummax()).min() * 100
    
    print("\n" + "="*50)
    print(f"📊 REPORTE FINAL V25 (Break & Hold): {symbol}")
    print("="*50)
    print(f"💰 Balance Final:    ${balance:.2f}")
    print(f"🚀 Retorno Total:    {total_ret:.2f}%")
    print(f"📉 Max Drawdown:     {max_dd_pct:.2f}%")
    print("-" * 50)
    
    # Win Rate
    win_rate = (len(trades_df[trades_df['net_pnl']>0]) / len(trades_df)) * 100
    print(f"✅ Win Rate:         {win_rate:.2f}%")
    print(f"🔢 Trades Totales:   {len(trades_df)}")

    print("📅 RENDIMIENTO POR AÑO:")
    stats = trades_df.groupby('year')['net_pnl'].agg(['sum', 'count'])
    print(stats)
    print("="*50 + "\n")

if __name__ == "__main__":
    run_simulation(SYMBOL)