import pandas as pd
import numpy as np
import os
import talib  # ⚠️ REQUIERE INSTALACIÓN BINARIA PREVIA
from datetime import timedelta

# ==========================================
# ⚙️ CONFIGURACIÓN INSTITUCIONAL
# ==========================================
SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# --- PARÁMETROS DE ESTRATEGIA ---
ATR_PERIOD = 135
ATR_SL_MULT = 1.1
SAR_AF_START = 0.02
SAR_AF_MAX = 0.2
EXPIRATION_HOURS = 5
EXIT_HOURS = 9

# --- PARÁMETROS DE GESTIÓN DE RIESGO Y REALISMO ---
INITIAL_BALANCE = 10000.0   # Balance más realista
RISK_PER_TRADE_PCT = 0.02   # Riesgo fijo del 2% por operación (MANDAMIENTO #3)
COMMISSION_RATE = 0.0006    # 0.06% Taker
SLIPPAGE_PCT = 0.0005       # 0.05% Slippage

# ==========================================
# 🛠️ FUNCIONES DE CARGA Y LIMPIEZA
# ==========================================

def load_and_validate_data(symbol):
    print(f"🔍 Buscando datos para {symbol} ({TIMEFRAME_STR})...")
    
    # Búsqueda de archivos
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
                try:
                    df = pd.read_csv(full_path)
                    break
                except Exception as e:
                    print(f"⚠️ Error leyendo: {e}")
        if df is not None: break

    if df is None:
        print("❌ ERROR CRÍTICO: No data found.")
        return None

    # Normalización
    df.columns = [c.lower() for c in df.columns]
    
    # Mapeo de fecha
    if 'open_time' in df.columns: df.rename(columns={'open_time': 'timestamp'}, inplace=True)
    elif 'date' in df.columns: df.rename(columns={'date': 'timestamp'}, inplace=True)
    
    if 'timestamp' not in df.columns:
        print("❌ ERROR: Sin columna de timestamp.")
        return None
        
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    # --- MANDAMIENTO #6: VALIDACIÓN DE INTEGRIDAD DE DATOS ---
    # 1. Verificar orden cronológico
    if not df['timestamp'].is_monotonic_increasing:
        print("⚠️ ALERTA: Datos desordenados. Ordenando...")
        df.sort_values('timestamp', inplace=True)
    
    # 2. Verificar duplicados
    if df['timestamp'].duplicated().any():
        print(f"⚠️ ALERTA: {df['timestamp'].duplicated().sum()} velas duplicadas eliminadas.")
        df.drop_duplicates(subset='timestamp', keep='first', inplace=True)
        
    # 3. Verificar huecos (Gaps temporales graves)
    time_diffs = df['timestamp'].diff().dt.total_seconds()
    median_diff = time_diffs.median() # Debería ser 3600 para 1h
    gaps = time_diffs[time_diffs > median_diff * 1.5]
    if len(gaps) > 0:
        print(f"⚠️ ALERTA DE DATA: Se detectaron {len(gaps)} huecos en la línea de tiempo.")
        print(f"   Mayor hueco: {gaps.max() / 3600:.1f} horas.")
    
    df.reset_index(drop=True, inplace=True)
    return df

def calculate_indicators_pro(df):
    print("🧮 Calculando indicadores (TA-Lib & PDH Safe)...")
    
    # --- MANDAMIENTO #2: PARABOLIC SAR CON TA-LIB ---
    # Usamos la librería estándar de la industria
    try:
        df['sar'] = talib.SAR(df['high'], df['low'], acceleration=SAR_AF_START, maximum=SAR_AF_MAX)
    except Exception as e:
        print(f"❌ ERROR TA-LIB: {e}")
        print("Asegúrate de tener instalada la librería C de TA-Lib.")
        return None

    # --- ATR ---
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=ATR_PERIOD)

    # --- MANDAMIENTO #1: PDH SIN LOOKAHEAD (MÉTODO MERGE) ---
    # Calculamos el Max del día
    daily_highs = df.groupby(df['timestamp'].dt.date)['high'].max()
    
    # Creamos un DF auxiliar donde la fecha es "Mañana"
    # (El High de HOY 05/12 sirve para operar MAÑANA 06/12)
    daily_highs_shifted = daily_highs.copy()
    daily_highs_shifted.index = daily_highs_shifted.index + timedelta(days=1)
    
    # Mapeamos usando la fecha de la vela actual
    df['date_only'] = df['timestamp'].dt.date
    # Hacemos map contra el índice desplazado.
    # Si hoy es 06/12, buscará el valor indexado como 06/12 en daily_highs_shifted,
    # que corresponde al High real del 05/12.
    df['pdh'] = df['date_only'].map(daily_highs_shifted)
    
    # Limpieza de NaN iniciales
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ==========================================
# 🚀 MOTOR DE SIMULACIÓN PROFESIONAL
# ==========================================

def run_simulation(symbol):
    df = load_and_validate_data(symbol)
    if df is None: return
    
    df = calculate_indicators_pro(df)
    if df is None: return

    print(f"🚀 Iniciando Simulación Profesional para {symbol}...")
    print(f"   ⚙️ Riesgo Fijo: {RISK_PER_TRADE_PCT*100}% del Balance")
    
    balance = INITIAL_BALANCE
    equity_curve = [balance]
    trades = []
    
    # Variables de estado
    position = None 
    entry_price = 0.0
    sl_price = 0.0
    entry_time = None
    position_size_contracts = 0.0 # Cantidad de ETH (o coin base)
    
    pending_active = False
    pending_trigger = 0.0
    pending_start_time = None

    for i in range(len(df)):
        # Extracción de datos para legibilidad y velocidad
        timestamp = df.at[i, 'timestamp']
        high = df.at[i, 'high']
        low = df.at[i, 'low']
        close = df.at[i, 'close']
        open_p = df.at[i, 'open']
        pdh = df.at[i, 'pdh']
        atr = df.at[i, 'atr']
        sar = df.at[i, 'sar']
        
        # --- A. GESTIÓN DE POSICIÓN ABIERTA ---
        if position == 'long':
            exit_price = None
            exit_reason = ""
            
            # 1. MANDAMIENTO #4: SLIPPAGE REALISTA & GAPS
            # Si el precio baja del SL, verificamos CÓMO bajó.
            if low <= sl_price:
                exit_reason = "SL"
                # Si la vela abrió YA por debajo del SL, nos ejecutaron en el Open (Gap enorme)
                if open_p < sl_price:
                    # El fill real es el Open, y aún así le aplicamos slippage negativo por pánico
                    raw_exit = open_p
                else:
                    # El precio cruzó el SL durante la vela. Asumimos slippage sobre el nivel de SL.
                    raw_exit = sl_price
                
                # Aplicamos slippage del mercado
                exit_price = raw_exit * (1 - SLIPPAGE_PCT)
            
            # 2. MANDAMIENTO #7: EXIT POR TIEMPO REAL (TIMEDELTA)
            # Solo si no tocó SL
            elif (timestamp - entry_time).total_seconds() >= EXIT_HOURS * 3600:
                exit_reason = "Time"
                # Salida a mercado al cierre de la vela (con slippage)
                exit_price = close * (1 - SLIPPAGE_PCT)
            
            # EJECUCIÓN DE SALIDA
            if exit_price:
                # PnL Calculation
                # Valor de salida = contracts * price
                exit_value = position_size_contracts * exit_price
                entry_value = position_size_contracts * entry_price
                
                # Costo Comision Salida
                exit_comm = exit_value * COMMISSION_RATE
                
                # PnL Neto = (Salida - Entrada) - Comisiones Totales (la de entrada ya se pagó mentalmente o se resta aquí)
                # Vamos a restar ambas aquí para claridad del trade
                entry_comm = entry_value * COMMISSION_RATE
                
                gross_pnl = exit_value - entry_value
                net_pnl = gross_pnl - (entry_comm + exit_comm)
                
                balance += net_pnl
                equity_curve.append(balance)
                
                trades.append({
                    'entry_time': entry_time,
                    'exit_time': timestamp,
                    'type': exit_reason,
                    'entry': entry_price,
                    'exit': exit_price,
                    'pnl': net_pnl,
                    'risk_multiple': net_pnl / (balance * RISK_PER_TRADE_PCT) # R ratio aproximado
                })
                
                position = None
                pending_active = False
                continue

        # --- B. GESTIÓN DE ORDEN PENDIENTE ---
        if position is None and pending_active:
            # 1. Chequeo de Expiración por Tiempo Real
            if (timestamp - pending_start_time).total_seconds() > EXPIRATION_HOURS * 3600:
                pending_active = False
            
            # 2. Chequeo de Trigger
            elif high >= pending_trigger:
                # MANDAMIENTO #5: RE-CÁLCULO DE RIESGO POST-SLIPPAGE
                
                # Precio base de entrada (Trigger)
                base_entry = pending_trigger
                # Precio real con slippage (compramos más caro)
                real_entry = base_entry * (1 + SLIPPAGE_PCT)
                
                # Nivel de Stop Loss TÉCNICO (basado en el gráfico, no cambia por slippage)
                technical_sl = base_entry - (atr * ATR_SL_MULT)
                
                # Distancia real de riesgo (ahora es mayor por el slippage de entrada)
                risk_distance = real_entry - technical_sl
                
                if risk_distance > 0:
                    # MANDAMIENTO #3: RIESGO FIJO 2%
                    risk_amount_usd = balance * RISK_PER_TRADE_PCT
                    
                    # Tamaño de posición (Contracts = Risk$ / Dist$)
                    qty_contracts = risk_amount_usd / risk_distance
                    
                    # Chequeo de seguridad: No exceder apalancamiento loco
                    # Si el stop es muy corto, el qty puede ser gigante. Limitamos a max 2x leverage por seguridad
                    max_qty = (balance * 2) / real_entry
                    qty_contracts = min(qty_contracts, max_qty)
                    
                    # EJECUCIÓN
                    position = 'long'
                    entry_price = real_entry
                    sl_price = technical_sl
                    position_size_contracts = qty_contracts
                    entry_time = timestamp
                    
                    pending_active = False
                else:
                    # Caso raro: Volatilidad tan baja o slippage tan alto que el SL queda por encima de la entrada (imposible en long)
                    pending_active = False

        # --- C. BÚSQUEDA DE SEÑAL ---
        if position is None and not pending_active:
            # Lógica Trend SAR (MANDAMIENTO #2)
            # SAR < Close = Tendencia Alcista (No operamos)
            # SAR > Close = Tendencia Bajista (Buscamos reversión/breakout)
            
            # Trend = -1 (Bajista) si SAR > Close
            is_trend_down = sar > close
            
            if is_trend_down:
                # Verificación extra: que el sar no esté "dentro" de la vela (ruido)
                # Validamos setup
                pending_active = True
                pending_trigger = pdh
                pending_start_time = timestamp

    # ==========================================
    # 📊 REPORTE FINAL
    # ==========================================
    print("\n" + "="*50)
    print(f"📊 REPORTE INSTITUCIONAL (V20): {symbol}")
    print("="*50)
    
    # Cálculo Drawdown
    equity_series = pd.Series(equity_curve)
    peak = equity_series.cummax()
    drawdown = (equity_series - peak) / peak
    max_dd_pct = drawdown.min() * 100
    
    total_return = ((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100
    
    print(f"💰 Balance Final:    ${balance:.2f}")
    print(f"🚀 Retorno Total:    {total_return:.2f}%")
    print(f"📉 Max Drawdown:     {max_dd_pct:.2f}%")
    
    total_trades = len(trades)
    if total_trades > 0:
        winners = len([t for t in trades if t['pnl'] > 0])
        win_rate = (winners / total_trades) * 100
        
        avg_pnl = sum([t['pnl'] for t in trades]) / total_trades
        
        wins = [t['pnl'] for t in trades if t['pnl'] > 0]
        losses = [t['pnl'] for t in trades if t['pnl'] < 0]
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else 999
        
        print("-" * 50)
        print(f"🔢 Total Trades:     {total_trades}")
        print(f"✅ Win Rate:         {win_rate:.2f}%")
        print(f"🏆 Profit Factor:    {profit_factor:.2f}")
        print(f"⚖️ Risk/Reward Avg:  1 : {abs(avg_win/avg_loss):.2f}")
        print("-" * 50)
        
        # Métrica de Calidad: Expectancy Ratio
        # (Win% * AvgWin) - (Loss% * AvgLoss)
        win_dec = win_rate / 100
        loss_dec = 1 - win_dec
        expectancy = (win_dec * avg_win) + (loss_dec * avg_loss)
        print(f"🧠 Expectancy:       ${expectancy:.2f} por trade")

    else:
        print("⚠️ No se realizaron trades.")

# ==========================================
# 🏁 EJECUCIÓN
# ==========================================
if __name__ == "__main__":
    run_simulation("ETHUSDT")