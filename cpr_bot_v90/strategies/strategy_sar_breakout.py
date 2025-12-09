import pandas as pd
import numpy as np
import os

# ==========================================
# ⚙️ PARÁMETROS DE LA ESTRATEGIA
# ==========================================
ATR_PERIOD = 135          # Periodo para el ATR
ATR_SL_MULT = 1.1         # Multiplicador del ATR para el Stop Loss
SAR_AF_START = 0.02       # Parabolic SAR: Paso inicial
SAR_AF_MAX = 0.2          # Parabolic SAR: Maximo paso
EXPIRATION_HOURS = 5      # Horas para cancelar orden pendiente
EXIT_HOURS = 9            # Horas para cerrar trade por tiempo
INITIAL_BALANCE = 1000    # Balance inicial
TIMEFRAME_STR = "1h"      # String para buscar en el nombre del archivo

# ==========================================
# 🛠️ FUNCIONES DE CARGA Y CÁLCULO
# ==========================================

def load_data_smart(symbol):
    """
    Carga datos buscando en varias carpetas y normaliza el nombre de la columna fecha.
    """
    print(f"🔍 Buscando datos para {symbol} ({TIMEFRAME_STR})...")
    
    possible_filenames = [
        f"mainnet_data_{TIMEFRAME_STR}_{symbol}_2020-2021.csv",
        f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"{symbol}_{TIMEFRAME_STR}.csv"
    ]
    
    # Rutas típicas
    search_paths = ["data", "cpr_bot_v90/data", "."]
    
    for filename in possible_filenames:
        for path in search_paths:
            full_path = os.path.join(path, filename)
            if os.path.exists(full_path):
                print(f"✅ Archivo encontrado: {full_path}")
                try:
                    df = pd.read_csv(full_path)
                    
                    # 1. Normalizar nombres de columnas a minúsculas
                    # (Open_Time -> open_time, High -> high, etc.)
                    df.columns = [c.lower() for c in df.columns]
                    
                    # 2. CORRECCIÓN CLAVE: Renombrar columna de fecha a 'timestamp'
                    # Tu downloader usa 'open_time', lo estandarizamos aquí.
                    if 'open_time' in df.columns:
                        df.rename(columns={'open_time': 'timestamp'}, inplace=True)
                    elif 'date' in df.columns:
                        df.rename(columns={'date': 'timestamp'}, inplace=True)
                    
                    # 3. Convertir a objetos datetime
                    if 'timestamp' in df.columns:
                        df['timestamp'] = pd.to_datetime(df['timestamp'])
                    else:
                        print(f"⚠️ Advertencia: No se encontró columna de fecha en {filename}")
                        continue # Intentar siguiente archivo si este está mal

                    return df
                except Exception as e:
                    print(f"⚠️ Error leyendo {full_path}: {e}")

    print(f"❌ ERROR CRÍTICO: No se encontró ningún archivo de datos válido para {symbol}.")
    return None

def calculate_atr(df, period):
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def run_strategy(symbol):
    # 1. CARGA DE DATOS
    df = load_data_smart(symbol)
    
    if df is None or df.empty:
        return

    # 2. CÁLCULO DE INDICADORES
    print("🧮 Calculando indicadores base (ATR & PDH)...")
    
    # ATR
    df['atr'] = calculate_atr(df, ATR_PERIOD)
    
    # PDH (Previous Daily High)
    # IMPORTANTE: Ahora 'timestamp' existe seguro gracias a la corrección en load_data_smart
    df_daily = df.set_index('timestamp').resample('D')['high'].max().shift(1)
    df['date_only'] = df['timestamp'].dt.date
    df['pdh'] = df['date_only'].map(df_daily.index.to_series().dt.date.map(df_daily))
    
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)

    # 3. BUCLE PRINCIPAL
    print(f"🚀 Iniciando Backtest SAR Breakout para {symbol}...")
    
    balance = INITIAL_BALANCE
    trades = []
    
    # Estado SAR
    sar = df.iloc[0]['high'] 
    ep = df.iloc[0]['low']   
    af = SAR_AF_START
    trend = -1 # -1: Bajista
    
    # Estado Trading
    position = None 
    entry_price = 0
    sl_price = 0
    entry_idx = 0
    
    pending_active = False
    pending_trigger = 0
    pending_start_idx = 0

    for i in range(len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i-1] if i > 0 else row
        
        high = row['high']
        low = row['low']
        close = row['close']
        pdh = row['pdh']
        atr = row['atr']
        
        # --- A. CÁLCULO SAR ---
        if i > 0:
            sar = sar + af * (ep - sar)
            
            if trend == -1: # Bajista
                if high > sar: 
                    trend = 1
                    sar = ep 
                    af = SAR_AF_START
                    ep = high
                else:
                    if low < ep: 
                        ep = low
                        af = min(af + SAR_AF_START, SAR_AF_MAX)
                    if sar < prev_row['high']:
                        sar = prev_row['high']     
            elif trend == 1: # Alcista
                if low < sar: 
                    trend = -1
                    sar = ep
                    af = SAR_AF_START
                    ep = low
                else:
                    if high > ep: 
                        ep = high
                        af = min(af + SAR_AF_START, SAR_AF_MAX)
                    if sar > prev_row['low']:
                        sar = prev_row['low']

        # --- B. TRADING ---
        
        # GESTIÓN POSICIÓN
        if position == 'long':
            # SL Check
            if low <= sl_price:
                pnl = (sl_price - entry_price) * (balance / entry_price) 
                balance += pnl
                trades.append({'type': 'SL', 'entry': entry_price, 'exit': sl_price, 'pnl': pnl})
                position = None
                pending_active = False
                continue
            
            # Time Exit Check
            if (i - entry_idx) >= EXIT_HOURS:
                pnl = (close - entry_price) * (balance / entry_price)
                balance += pnl
                trades.append({'type': 'Time', 'entry': entry_price, 'exit': close, 'pnl': pnl})
                position = None
                pending_active = False
                continue

        # GESTIÓN ORDEN PENDIENTE
        if position is None and pending_active:
            # Expiración
            if (i - pending_start_idx) > EXPIRATION_HOURS:
                pending_active = False 
            
            # Trigger
            elif high >= pending_trigger:
                position = 'long'
                entry_price = pending_trigger
                sl_price = entry_price - (atr * ATR_SL_MULT)
                entry_idx = i
                pending_active = False 

        # BUSCAR SEÑAL (SETUP)
        # Tendencia Bajista + SAR sobre precio + SAR descendente (implícito en tendencia bajista)
        if position is None and not pending_active and trend == -1:
            if sar > close:
                pending_active = True
                pending_trigger = pdh
                pending_start_idx = i

    # --- REPORTE MEJORADO CON DRAWDOWN ---
    print("\n" + "="*40)
    print(f"📊 REPORTE FINAL: {symbol}")
    print("="*40)
    
    # Cálculo de Drawdown
    equity_curve = []
    running_balance = INITIAL_BALANCE
    max_drawdown_pct = 0
    peak_balance = INITIAL_BALANCE
    
    # Reconstruimos la curva de equidad trade por trade
    for t in trades:
        running_balance += t['pnl']
        equity_curve.append(running_balance)
        
        # Actualizar pico
        if running_balance > peak_balance:
            peak_balance = running_balance
        
        # Calcular caída desde el pico
        dd = (peak_balance - running_balance) / peak_balance * 100
        if dd > max_drawdown_pct:
            max_drawdown_pct = dd

    print(f"Balance Final:   ${balance:.2f}")
    print(f"💰 Retorno Total: {((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100:.2f}%")
    print(f"📉 Max Drawdown:  {max_drawdown_pct:.2f}%  <-- DATO CLAVE")
    print("-" * 40)
    
    total_trades = len(trades)
    winners = len([t for t in trades if t['pnl'] > 0])
    win_rate = (winners / total_trades * 100) if total_trades > 0 else 0
    
    print(f"Total Trades:    {total_trades}")
    print(f"Win Rate:        {win_rate:.2f}%")
    
    if total_trades > 0:
        avg_pnl = sum([t['pnl'] for t in trades]) / total_trades
        print(f"Avg PnL per trade: ${avg_pnl:.2f}")
        
        # Ratio Promedio Ganancia/Pérdida
        avg_win = np.mean([t['pnl'] for t in trades if t['pnl'] > 0])
        avg_loss = np.mean([t['pnl'] for t in trades if t['pnl'] < 0])
        if avg_loss != 0:
            print(f"Risk/Reward Real: 1 : {abs(avg_win/avg_loss):.2f}")

    return balance

# ==========================================
# 🏁 EJECUCIÓN
# ==========================================
if __name__ == "__main__":
    run_strategy("ETHUSDT")