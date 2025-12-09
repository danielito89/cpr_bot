import pandas as pd
import numpy as np
import os

# ==========================================
# ⚙️ CONFIGURACIÓN DE DATOS
# ==========================================
DATA_FOLDER = 'data'      # Nombre de la carpeta donde están los CSV
INTERVAL = '1h'           # Temporalidad por defecto

# ==========================================
# ⚙️ PARÁMETROS DE LA ESTRATEGIA (OPTIMIZABLES)
# ==========================================
ATR_PERIOD = 135          # Periodo para el ATR (Volatilidad lenta)
ATR_SL_MULT = 1.1         # Multiplicador del ATR para el Stop Loss
SAR_AF_START = 0.02       # Parabolic SAR: Paso inicial
SAR_AF_MAX = 0.2          # Parabolic SAR: Maximo paso
EXPIRATION_HOURS = 5      # Horas para cancelar la orden pendiente si no se ejecuta
EXIT_HOURS = 9            # Time Exit: Horas para cerrar el trade si sigue abierto
INITIAL_BALANCE = 1000    # Balance inicial simulado

# ==========================================
# 🛠️ FUNCIONES AUXILIARES
# ==========================================
def calculate_atr(df, period):
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    
    # True Range
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # ATR (Media móvil simple para ligereza y compatibilidad)
    return tr.rolling(window=period).mean()

def run_strategy(symbol):
    # 1. CONSTRUCCIÓN DE LA RUTA DEL ARCHIVO
    # Asumimos formato: data/ETHUSDT_1h.csv
    filename = f"{symbol}_{INTERVAL}.csv"
    file_path = os.path.join(DATA_FOLDER, filename)
    
    if not os.path.exists(file_path):
        print(f"❌ ERROR: No se encuentra el archivo: {file_path}")
        print(f"   Asegúrate de haber descargado los datos en la carpeta '{DATA_FOLDER}'")
        return

    print(f"📂 Cargando datos para {symbol} desde: {file_path}...")
    df = pd.read_csv(file_path)
    
    # Limpieza básica de nombres de columnas
    df.columns = [c.lower() for c in df.columns]
    
    # Manejo de fechas
    if 'timestamp' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp'])
    elif 'date' in df.columns: 
        df['timestamp'] = pd.to_datetime(df['date'])
        
    # 2. CÁLCULO DE INDICADORES (Pre-bucle)
    print("🧮 Calculando indicadores base (ATR & PDH)...")
    
    # ATR
    df['atr'] = calculate_atr(df, ATR_PERIOD)
    
    # Previous Daily High (PDH)
    # Mapeamos el High del día anterior a cada hora del día actual
    df_daily = df.set_index('timestamp').resample('D')['high'].max().shift(1)
    df['date_only'] = df['timestamp'].dt.date
    df['pdh'] = df['date_only'].map(df_daily.index.to_series().dt.date.map(df_daily))
    
    # Limpiamos NaNs iniciales (necesarios por el periodo del ATR)
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)

    # 3. BUCLE PRINCIPAL (Simulación SAR y Lógica)
    print(f"🚀 Iniciando Backtest SAR Breakout para {symbol}...")
    
    balance = INITIAL_BALANCE
    trades = []
    
    # Variables SAR (Iniciamos asumiendo tendencia bajista para arrancar)
    sar = df.iloc[0]['high'] 
    ep = df.iloc[0]['low']   
    af = SAR_AF_START
    trend = -1 # -1: Bajista (SAR arriba), 1: Alcista
    
    # Variables de Estrategia
    position = None 
    entry_price = 0
    sl_price = 0
    entry_idx = 0
    
    pending_active = False
    pending_trigger = 0
    pending_start_idx = 0

    sar_values = [] # Debug

    for i in range(len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i-1] if i > 0 else row
        
        high = row['high']
        low = row['low']
        close = row['close']
        pdh = row['pdh']
        atr = row['atr']
        
        # --- A. CÁLCULO MANUAL SAR ---
        if i > 0:
            sar = sar + af * (ep - sar)
            
            reversal = False
            if trend == -1: # Bajista
                if high > sar: 
                    trend = 1
                    sar = ep 
                    af = SAR_AF_START
                    ep = high
                    reversal = True
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
                    reversal = True
                else:
                    if high > ep: 
                        ep = high
                        af = min(af + SAR_AF_START, SAR_AF_MAX)
                    if sar > prev_row['low']:
                        sar = prev_row['low']
        
        sar_values.append(sar)

        # --- B. LÓGICA DE TRADING ---
        
        # 1. GESTIÓN POSICIÓN
        if position == 'long':
            # Stop Loss
            if low <= sl_price:
                pnl = (sl_price - entry_price) * (balance / entry_price) 
                balance += pnl
                trades.append({'symbol': symbol, 'type': 'SL', 'entry': entry_price, 'exit': sl_price, 'pnl': pnl, 'bars': i - entry_idx})
                position = None
                pending_active = False
                continue
            
            # Time Exit
            if (i - entry_idx) >= EXIT_HOURS:
                pnl = (close - entry_price) * (balance / entry_price)
                balance += pnl
                trades.append({'symbol': symbol, 'type': 'Time', 'entry': entry_price, 'exit': close, 'pnl': pnl, 'bars': i - entry_idx})
                position = None
                pending_active = False
                continue

        # 2. GESTIÓN ORDEN PENDIENTE
        if position is None and pending_active:
            # Caducidad
            if (i - pending_start_idx) > EXPIRATION_HOURS:
                pending_active = False 
            
            # Ejecución (Trigger)
            elif high >= pending_trigger:
                position = 'long'
                entry_price = pending_trigger
                sl_price = entry_price - (atr * ATR_SL_MULT)
                entry_idx = i
                pending_active = False 
                # print(f"🟢 ENTRY {symbol} at {entry_price:.2f}")

        # 3. BUSCAR SEÑAL
        # Condición: Tendencia SAR Bajista (trend == -1) y precio bajo el PDH
        if position is None and not pending_active and trend == -1:
            if sar > close:
                pending_active = True
                pending_trigger = pdh
                pending_start_idx = i

    # --- REPORTE ---
    print("\n" + "="*40)
    print(f"📊 REPORTE FINAL: {symbol}")
    print("="*40)
    print(f"Balance Inicial: ${INITIAL_BALANCE}")
    print(f"Balance Final:   ${balance:.2f}")
    
    total_trades = len(trades)
    winners = len([t for t in trades if t['pnl'] > 0])
    win_rate = (winners / total_trades * 100) if total_trades > 0 else 0
    
    print(f"Total Trades:    {total_trades}")
    print(f"Win Rate:        {win_rate:.2f}%")
    
    if total_trades > 0:
        avg_pnl = sum([t['pnl'] for t in trades]) / total_trades
        print(f"Avg PnL per trade: ${avg_pnl:.2f}")
        
        # Calcular Retorno Total
        total_return = ((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100
        print(f"Retorno Total:   {total_return:.2f}%")

    return balance

# ==========================================
# 🏁 EJECUCIÓN SIMPLE
# ==========================================
if __name__ == "__main__":
    # AQUÍ CAMBIAS EL NOMBRE DEL PAR QUE QUIERES PROBAR
    # Asegúrate de tener el archivo en data/ETHUSDT_1h.csv (o el par que elijas)
    
    run_strategy("ETHUSDT") 
    
    # Si quisieras probar otro, descomenta:
    # run_strategy("BTCUSDT")