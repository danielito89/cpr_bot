import pandas as pd
import numpy as np
import os
from datetime import timedelta

# Verificación de TA-Lib
try:
    import talib
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False
    print("⚠️ ADVERTENCIA CRÍTICA: TA-Lib no encontrado.")

# ==========================================
# ⚙️ CONFIGURACIÓN V33 (GOLD STANDARD)
# ==========================================
SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# --- ESTRATEGIA (SMART REVERSION) ---
BB_PERIOD = 20
BB_STD_DEV = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 30 
EMA_TREND_PERIOD = 200

# --- CONFIRMACIÓN ---
WAIT_CANDLES_MAX = 5    
MIN_CANDLE_STRENGTH = 0.6 

# --- SALIDAS Y ESTRUCTURA ---
TP_ATR_MULT = 2.2         
MIN_SL_PCT = 0.003        
EXIT_HOURS = 48         

# --- FILTROS ---
MAX_TRADES_PER_MONTH = 12
BAD_HOURS = [3, 4, 5]     # UTC

# --- RIESGO INSTITUCIONAL ---
INITIAL_BALANCE = 10000.0   
TARGET_VOLATILITY = 0.01    
BASE_VAR_PCT = 0.02         
COMMISSION_RATE = 0.0006    
BASE_LATENCY = 0.0001
DD_BRAKE_THRESHOLD = 0.10   
DD_BRAKE_FACTOR = 0.5       
MAX_LEVERAGE_BUFFER = 15.0  

# --- MICROESTRUCTURA ---
SLIPPAGE_K = 0.15           
EXIT_SLIPPAGE_MULT = 1.5    
SPREAD_MIN_USD = 0.10       
SPREAD_FACTOR = 0.002       

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
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')

    df.sort_values('timestamp', inplace=True)
    df.drop_duplicates(subset='timestamp', keep='first', inplace=True)
    df['time_diff'] = df['timestamp'].diff().dt.total_seconds()
    
    df.reset_index(drop=True, inplace=True)
    return df

def calculate_indicators(df):
    print("🧮 Calculando indicadores V33...")
    
    if HAS_TALIB:
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
        df['rsi'] = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
        u, m, l = talib.BBANDS(df['close'], timeperiod=BB_PERIOD, nbdevup=BB_STD_DEV, nbdevdn=BB_STD_DEV, matype=0)
        df['bb_upper'] = u; df['bb_middle'] = m; df['bb_lower'] = l
        df['ema_trend'] = talib.EMA(df['close'], timeperiod=EMA_TREND_PERIOD)
    else:
        print("❌ ERROR: Se requiere TA-Lib.")
        return None

    df['atr_smooth'] = df['atr'].ewm(alpha=0.2).mean()

    price_jump = abs(df['open'] - df['close'].shift(1))
    atr_threshold = df['atr'].shift(1) * 2
    time_gap = df['time_diff'] > 5400
    price_gap = price_jump > atr_threshold
    
    df['gap_detected'] = time_gap | price_gap
    df['low_5'] = df['low'].rolling(window=5).min().shift(1)

    df.dropna(subset=['atr', 'atr_smooth', 'rsi', 'bb_lower', 'ema_trend', 'low_5'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ==========================================
# 🚀 MOTOR DE SIMULACIÓN V33
# ==========================================

def run_simulation(symbol):
    df = load_data(symbol)
    if df is None: return
    try: df = calculate_indicators(df)
    except Exception as e: print(e); return

    print(f"🚀 Iniciando Backtest V33 (Gold Standard) para {symbol}...")
    
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
    tp_struct_price = 0.0 
    
    waiting_confirmation = False
    wait_counter = 0
    trigger_next_open = False
    next_open_sl = 0.0

    cooldown_counter = 0
    trades_this_month = 0
    current_month = -1

    for i in range(len(df)):
        # --- INIT FLAG INTRA-CANDLE (FIX #1 Parte B) ---
        trade_active_this_candle = False

        # Datos
        ts = df.at[i, 'timestamp']
        high = df.at[i, 'high']
        low = df.at[i, 'low']
        close = df.at[i, 'close']
        open_p = df.at[i, 'open']
        atr = df.at[i, 'atr']
        atr_smooth = df.at[i, 'atr_smooth'] 
        rsi = df.at[i, 'rsi']
        
        bb_upper = df.at[i, 'bb_upper']
        bb_lower = df.at[i, 'bb_lower']
        ema_trend = df.at[i, 'ema_trend']
        struct_low = df.at[i, 'low_5']
        
        if open_p == close and close == high and high == low:
            equity_curve.append(equity_curve[-1])
            continue

        if ts.month != current_month:
            current_month = ts.month
            trades_this_month = 0
            
        if df.at[i, 'gap_detected']: 
            cooldown_counter = 24
            waiting_confirmation = False
            trigger_next_open = False
        if cooldown_counter > 0: cooldown_counter -= 1
        
        # Costos
        rel_vol_smooth = atr_smooth / close if close > 0 else 0.01
        rel_vol_inst = atr / close if close > 0 else 0.01
        
        current_slippage_pct = SLIPPAGE_K * rel_vol_inst
        dynamic_latency = BASE_LATENCY + (rel_vol_inst * 0.1)
        spread_usd = max(SPREAD_MIN_USD, atr * SPREAD_FACTOR)
        spread_pct = spread_usd / close

        # =========================================================
        # FASE 1: EVENT LOOP DE ENTRADA (INTRA-CANDLE)
        # =========================================================
        if trigger_next_open and position is None:
            is_bad_hour = ts.hour in BAD_HOURS
            is_gap_down = open_p < next_open_sl
            is_monthly_limit = trades_this_month >= MAX_TRADES_PER_MONTH
            is_gap_detected = df.at[i, 'gap_detected']

            if not is_bad_hour and not is_gap_down and not is_monthly_limit and not is_gap_detected:
                
                fill_penalty = 0.25 * current_slippage_pct
                total_friction_pct = dynamic_latency + spread_pct + current_slippage_pct + fill_penalty
                real_entry = open_p * (1 + total_friction_pct)
                
                # Sizing Defensivo
                sl_execution_pad = current_slippage_pct * 0.5 
                technical_sl = next_open_sl
                sl_for_sizing = technical_sl * (1 - sl_execution_pad)
                
                min_sl_level = real_entry * (1 - MIN_SL_PCT)
                if sl_for_sizing > min_sl_level: sl_for_sizing = min_sl_level
                
                risk_distance = real_entry - sl_for_sizing
                
                if risk_distance > 0:
                    var_factor = min(1.0, TARGET_VOLATILITY / rel_vol_smooth)
                    current_dd = (peak_balance - balance) / peak_balance
                    dd_factor = DD_BRAKE_FACTOR if current_dd > DD_BRAKE_THRESHOLD else 1.0
                    
                    final_risk_pct = BASE_VAR_PCT * var_factor * dd_factor
                    risk_amount_usd = balance * final_risk_pct
                    
                    max_contracts = (balance * MAX_LEVERAGE_BUFFER) / real_entry
                    qty_contracts = min(risk_amount_usd / risk_distance, max_contracts)
                    
                    liquidity_impact = min(0.003, qty_contracts / 50000)
                    real_entry = real_entry * (1 + liquidity_impact)
                    
                    entry_comm = (qty_contracts * real_entry) * COMMISSION_RATE
                    tp_struct_price = real_entry + (atr * TP_ATR_MULT) 

                    position = 'long'
                    entry_price = real_entry
                    sl_price = technical_sl
                    position_size_contracts = qty_contracts
                    entry_time = ts
                    entry_risk_amount_usd = risk_amount_usd
                    entry_comm_paid = entry_comm
                    
                    balance -= entry_comm
                    trades_this_month += 1
                    
                    # INTRA-CANDLE EXIT CHECK
                    exit_slippage = current_slippage_pct * EXIT_SLIPPAGE_MULT
                    
                    if low <= sl_price:
                        exit_price = sl_price * (1 - exit_slippage)
                        exit_reason = "SL (Intra-Candle)"
                        
                        exit_value = position_size_contracts * exit_price
                        exit_comm = exit_value * COMMISSION_RATE
                        gross_pnl = exit_value - (position_size_contracts * entry_price)
                        net_pnl = gross_pnl - entry_comm_paid - exit_comm
                        
                        balance += (gross_pnl - exit_comm)
                        trades.append({
                            'year': ts.year, 'net_pnl': net_pnl, 'type': exit_reason,
                            'friction': total_friction_pct + liquidity_impact
                        })
                        position = None 
                        
                        # --- FIX #1 Parte A: Flag activada ---
                        trade_active_this_candle = True
                        
            trigger_next_open = False 

        # =========================================================
        # FASE 2: GESTIÓN DE POSICIÓN
        # =========================================================
        if position == 'long' and not trade_active_this_candle:
            exit_price = None
            exit_reason = ""
            
            hit_sl = low <= sl_price
            hit_tp = high >= tp_struct_price 
            hit_time = (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600
            
            exit_slippage = current_slippage_pct * EXIT_SLIPPAGE_MULT
            
            if hit_sl:
                raw_exit = open_p if open_p < sl_price else sl_price
                exit_price = raw_exit * (1 - exit_slippage)
                exit_reason = "SL"
            
            elif hit_tp:
                # --- FIX #2: TP con Gap Logic ---
                raw_exit = max(open_p, tp_struct_price)
                exit_price = raw_exit * (1 - exit_slippage)
                exit_reason = "TP (Struct)"
            
            elif hit_time:
                exit_price = close * (1 - exit_slippage)
                exit_reason = "Time"
            
            if exit_price:
                exit_value = position_size_contracts * exit_price
                
                # --- FIX #2 Confirmación: Comisión sobre el valor de salida real ---
                exit_comm = exit_value * COMMISSION_RATE
                
                entry_value_nominal = position_size_contracts * entry_price 
                gross_pnl = exit_value - entry_value_nominal
                net_pnl = gross_pnl - entry_comm_paid - exit_comm
                
                balance += (gross_pnl - exit_comm)
                if balance > peak_balance: peak_balance = balance
                
                trades.append({
                    'year': ts.year, 'net_pnl': net_pnl, 'type': exit_reason,
                    'friction': 0 
                })
                position = None
                trade_active_this_candle = True

        # =========================================================
        # FASE 3: BÚSQUEDA DE SEÑAL
        # =========================================================
        if position is None and not trigger_next_open and cooldown_counter == 0:
            if ts.hour not in BAD_HOURS:
                if close > ema_trend:
                    if (close < bb_lower) and (rsi < RSI_OVERSOLD):
                        waiting_confirmation = True
                        wait_counter = 0 
                    
                    if waiting_confirmation:
                        wait_counter += 1
                        is_green = close > open_p
                        
                        prev_low = df.at[i-1, 'low'] if i > 0 else 0
                        is_gap_fake = open_p < prev_low
                        prev_red = df.at[i-1, 'close'] < df.at[i-1, 'open'] if i > 0 else False
                        
                        candle_range = high - low
                        if candle_range > 0:
                            pos_in_candle = (close - low) / candle_range
                            is_strong = pos_in_candle > MIN_CANDLE_STRENGTH
                        else: is_strong = False
                        
                        if is_green and prev_red and is_strong and not is_gap_fake:
                            trigger_next_open = True
                            next_open_sl = struct_low
                            waiting_confirmation = False 
                        elif wait_counter >= WAIT_CANDLES_MAX:
                            waiting_confirmation = False
                else:
                    waiting_confirmation = False

        # =========================================================
        # FASE 4: EQUITY UPDATE (FIX #1 Parte C)
        # =========================================================
        if not trade_active_this_candle:
            # Si no hubo cierre, calculamos PnL latente
            current_equity = balance
            if position == 'long':
                unrealized_pnl = (close - entry_price) * position_size_contracts
                unrealized_exit_fee = (close * position_size_contracts) * COMMISSION_RATE
                current_equity += (unrealized_pnl - unrealized_exit_fee)
            equity_curve.append(current_equity)
        else:
            # Si hubo cierre, el balance ya incluye el PnL realizado.
            # No agregamos 'current_equity' fantasma, usamos el real.
            equity_curve.append(balance)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    if trades_df.empty: print("⚠️ Sin trades."); return

    eq_series = pd.Series(equity_curve)
    peak = eq_series.cummax()
    drawdown = (eq_series - peak) / peak
    max_dd_pct = drawdown.min() * 100
    
    total_ret = ((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100
    
    print("\n" + "="*50)
    print(f"📊 REPORTE FINAL V33 (GOLD STANDARD): {symbol}")
    print("="*50)
    print(f"💰 Balance Final:    ${balance:.2f}")
    print(f"🚀 Retorno Total:    {total_ret:.2f}%")
    print(f"📉 Max Drawdown:     {max_dd_pct:.2f}% (Mark-to-Market)")
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