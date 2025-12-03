import pandas as pd
import numpy as np
import os
from datetime import datetime, timedelta

# ==========================================
# ⚙️ CONFIGURACIÓN DEL BACKTEST
# ==========================================
SYMBOL = 'ETHUSDT'          # El par exacto que descargaste (sin barra /)
TIMEFRAME = '1h'            # Timeframe de tu CSV (1h)
TRADING_START_DATE = "2023-01-01"  # FECHA DONDE QUIERES EMPEZAR A OPERAR
BUFFER_DAYS = 20            # Días extra hacia atrás para cargar la "previa"
CAPITAL_INICIAL = 1000      # USD

# Rutas relativas a donde corre el script (cpr_bot_v90/)
DATA_FOLDER = "data"        

# ==========================================
# 1. CARGADOR DE DATOS (Modo Precisión Local)
# ==========================================
def cargar_datos_locales_con_buffer(symbol, start_date_str, buffer_days=20):
    """
    Busca el archivo exacto generado por download_data.py y recorta
    el DataFrame para incluir el 'buffer' de días previos necesario para los pivotes.
    """
    # 1. Construir nombre de archivo exacto según download_data.py
    # Formato esperado: mainnet_data_1h_ETHUSDT.csv
    filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}.csv"
    filepath = os.path.join(DATA_FOLDER, filename)
    
    print(f"\n📂 BUSCANDO ARCHIVO: {filepath}...")
    
    if not os.path.exists(filepath):
        print(f"❌ ERROR: No encuentro el archivo. ¿Ejecutaste download_data.py primero?")
        print(f"   Ruta buscada: {os.path.abspath(filepath)}")
        return pd.DataFrame(), None

    # 2. Cargar CSV
    try:
        df = pd.read_csv(filepath)
        # Normalizar columnas (minúsculas)
        df.columns = [col.lower() for col in df.columns]
        
        # Detectar columna de fecha (open_time o timestamp)
        col_fecha = 'open_time' if 'open_time' in df.columns else 'timestamp'
        df[col_fecha] = pd.to_datetime(df[col_fecha])
        df.set_index(col_fecha, inplace=True)
        
        print(f"✅ Archivo cargado. Total histórico: {len(df)} velas.")
        print(f"   Rango total: {df.index[0]} -> {df.index[-1]}")
        
    except Exception as e:
        print(f"❌ Error leyendo el CSV: {e}")
        return pd.DataFrame(), None

    # 3. Calcular Fechas de Corte (La Cirugía)
    target_start = pd.to_datetime(start_date_str)
    
    # Restamos X días a la fecha de inicio para tener datos previos (Buffer)
    start_with_buffer = target_start - timedelta(days=buffer_days)
    
    # 4. Filtrar DataFrame
    # Nos quedamos con los datos desde (Fecha Inicio - Buffer) en adelante
    df_filtrado = df[df.index >= start_with_buffer].copy()
    
    if df_filtrado.empty:
        print(f"❌ ERROR: Tu CSV termina antes de la fecha de inicio requerida ({start_with_buffer}).")
        return pd.DataFrame(), None

    print(f"✂️  DATOS RECORTADOS PARA BACKTEST:")
    print(f"   - Buffer inicia:      {df_filtrado.index[0]} (Calculamos indicadores aquí)")
    print(f"   - Trading real inicia: {target_start} (Aquí empieza el dinero)")
    print(f"   - Velas a procesar:   {len(df_filtrado)}")
    
    return df_filtrado, target_start

# ==========================================
# 2. MOTOR DE BACKTEST V9.3 (Lógica Pivotes)
# ==========================================
def backtest_v9_3(df, target_start_date):
    print("\n⚙️  CALCULANDO PIVOTES VECTORIZADOS...")
    
    # --- A. PRE-CÁLCULO DE PIVOTES (Vectorizado) ---
    # Convertimos velas de 1H a Días para sacar High/Low/Close diario
    daily_df = df.resample('1D').agg({
        'high': 'max',
        'low': 'min',
        'close': 'last'
    })

    # [PRECISIÓN] Desplazamiento (Shift)
    # Los datos de HOY se calculan con el High/Low/Close de AYER.
    daily_df['prev_high'] = daily_df['high'].shift(1)
    daily_df['prev_low'] = daily_df['low'].shift(1)
    daily_df['prev_close'] = daily_df['close'].shift(1)
    
    # Fórmulas Standard
    daily_df['P'] = (daily_df['prev_high'] + daily_df['prev_low'] + daily_df['prev_close']) / 3
    daily_df['R1'] = (2 * daily_df['P']) - daily_df['prev_low']
    daily_df['S1'] = (2 * daily_df['P']) - daily_df['prev_high']
    
    # Limpiamos NaNs generados por el shift en el primer día del buffer
    daily_df.dropna(inplace=True)

    # --- B. INICIALIZACIÓN ---
    balance = CAPITAL_INICIAL
    position = None 
    entry_price = 0
    trades_history = []
    
    # Filtramos para mostrar solo el loop operativo (sin el buffer) en los logs
    # Pero el iterrows recorrerá todo lo que le pasemos, así que controlamos adentro.
    
    print(f"🚀 INICIANDO SIMULACIÓN DESDE {target_start_date}...")
    
    # --- C. BUCLE VELA A VELA ---
    for current_time, row in df.iterrows():
        
        # [PRECISIÓN] Si estamos en el periodo de Buffer, NO operamos, solo pasamos.
        # (Aunque aquí ya tenemos pivotes pre-calculados, esto asegura respetar la fecha de inicio)
        if current_time < target_start_date:
            continue
            
        price = row['close']
        # Usamos solo la fecha (YYYY-MM-DD) para buscar el pivote correspondiente
        current_date_str = str(current_time.date())
        
        # 1. BUSCAR PIVOTES DEL DÍA EN EL MAPA PRE-CALCULADO
        try:
            day_stats = daily_df.loc[current_date_str]
            pivot = day_stats['P']
            r1 = day_stats['R1']
            s1 = day_stats['S1']
        except KeyError:
            # Si falta un día en el medio (ej. mantenimiento exchange), saltamos sin error.
            continue
            
        # 2. ESTRATEGIA SIMPLE DE TEST
        # =========================================
        
        # ENTRADA LONG: Precio cruza Pivot hacia arriba
        if position is None:
            if row['close'] > pivot and row['open'] < pivot:
                position = 'LONG'
                entry_price = price
        
        # GESTIÓN DE SALIDA (TP / SL)
        elif position == 'LONG':
            take_profit = r1
            stop_loss = entry_price * 0.98  # SL 2%
            
            if price >= take_profit or price <= stop_loss:
                pnl_pct = (price - entry_price) / entry_price
                pnl_usd = balance * pnl_pct
                balance += pnl_usd
                
                trades_history.append({
                    'date': current_time,
                    'type': 'LONG',
                    'entry': entry_price,
                    'exit': price,
                    'pnl_usd': pnl_usd,
                    'reason': 'TP' if price >= take_profit else 'SL'
                })
                position = None

    # --- D. REPORTE FINAL ---
    generar_reporte(trades_history, CAPITAL_INICIAL, balance)

def generar_reporte(trades, start_cap, end_cap):
    if not trades:
        print("\n⚠️ 0 Trades realizados. Revisa si el precio cruzó el pivote alguna vez.")
        return

    df_t = pd.DataFrame(trades)
    total_trades = len(df_t)
    wins = df_t[df_t['pnl_usd'] > 0]
    losses = df_t[df_t['pnl_usd'] <= 0]
    
    win_rate = (len(wins) / total_trades) * 100
    total_pnl = end_cap - start_cap
    
    gross_profit = wins['pnl_usd'].sum()
    gross_loss = abs(losses['pnl_usd'].sum())
    pf = gross_profit / gross_loss if gross_loss != 0 else 999

    print("\n" + "="*45)
    print(f"📊 REPORTE DE RESULTADOS - {SYMBOL}")
    print(f"   Periodo Analizado: {TRADING_START_DATE} en adelante")
    print("="*45)
    print(f"💰 Balance Inicial:   ${start_cap:.2f}")
    print(f"💰 Balance Final:     ${end_cap:.2f}")
    print(f"📈 PnL Neto:          ${total_pnl:.2f}")
    print("-" * 45)
    print(f"🎲 Trades Totales:    {total_trades}")
    print(f"✅ Win Rate:          {win_rate:.2f}%")
    print(f"⚖️ Profit Factor:     {pf:.2f}")
    print("="*45 + "\n")

# ==========================================
# EJECUCIÓN
# ==========================================
if __name__ == "__main__":
    # 1. Cargamos TU archivo local con la lógica precisa del buffer
    df_data, fecha_real = cargar_datos_locales_con_buffer(SYMBOL, TRADING_START_DATE, BUFFER_DAYS)
    
    # 2. Corremos el backtest si cargó bien
    if df_data is not None and not df_data.empty:
        backtest_v9_3(df_data, fecha_real)