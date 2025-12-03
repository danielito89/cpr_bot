import pandas as pd
import numpy as np
import os
import sys
from datetime import datetime, timedelta

# ==========================================
# ⚙️ CONFIGURACIÓN DEL BACKTEST
# ==========================================
SYMBOL = 'ETHUSDT'          
TIMEFRAME = '1h'            
TRADING_START_DATE = "2023-01-01"  # <--- CAMBIA ESTO A TU FECHA DESEADA
BUFFER_DAYS = 25            # Días previos para calcular indicadores sin errores
CAPITAL_INICIAL = 1000      

# Ruta relativa: Asumimos que corres esto desde cpr_bot_v90/
# y los datos están en cpr_bot_v90/data/
DATA_FOLDER = "data"        

# ==========================================
# 1. CARGADOR DE DATOS LOCAL (El Fix)
# ==========================================
def cargar_datos_locales_con_buffer(symbol, start_date_str, buffer_days=20):
    """
    Busca el archivo local generado por download_data.py y recorta
    el DataFrame para incluir el 'buffer' de días previos.
    """
    # Nombre exacto que genera tu script download_data.py
    filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}.csv"
    
    # Construir ruta absoluta para evitar errores de "file not found"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, DATA_FOLDER, filename)
    
    print(f"\n📂 BUSCANDO ARCHIVO: {filepath}...")
    
    if not os.path.exists(filepath):
        print(f"❌ ERROR: No encuentro el archivo.")
        print(f"   Asegúrate de haber corrido 'python download_data.py' antes.")
        sys.exit(1)

    # Cargar CSV
    try:
        df = pd.read_csv(filepath)
        df.columns = [col.lower() for col in df.columns] # Normalizar a minúsculas
        
        # Detectar columna de fecha
        col_fecha = 'open_time' if 'open_time' in df.columns else 'timestamp'
        if col_fecha not in df.columns:
            # Intento de fallback por si el índice es la fecha
            if isinstance(df.index, pd.DatetimeIndex):
                pass
            else:
                # Si 'date' existe
                col_fecha = 'date' if 'date' in df.columns else col_fecha
        
        if col_fecha in df.columns:
            df[col_fecha] = pd.to_datetime(df[col_fecha])
            df.set_index(col_fecha, inplace=True)
        
        print(f"✅ Archivo cargado. Historial total: {len(df)} velas.")
        print(f"   Rango disponible: {df.index[0]} -> {df.index[-1]}")
        
    except Exception as e:
        print(f"❌ Error crítico leyendo el CSV: {e}")
        sys.exit(1)

    # Recorte Inteligente (Buffer)
    target_start = pd.to_datetime(start_date_str)
    start_with_buffer = target_start - timedelta(days=buffer_days)
    
    # Filtrar: Tomamos desde (Fecha Inicio - Buffer) en adelante
    df_filtrado = df[df.index >= start_with_buffer].copy()
    
    if df_filtrado.empty:
        print(f"❌ ERROR: Tu CSV termina antes de la fecha requerida ({start_with_buffer}).")
        print(f"   Tu CSV llega hasta: {df.index[-1]}")
        sys.exit(1)

    print(f"✂️  DATOS LISTOS:")
    print(f"   - Buffer inicia (Cálculos): {df_filtrado.index[0]}")
    print(f"   - Trading inicia (Dinero):  {target_start}")
    print(f"   - Velas a procesar:         {len(df_filtrado)}")
    
    return df_filtrado, target_start

# ==========================================
# 2. MOTOR DE BACKTEST V9.3
# ==========================================
def backtest_v9_3(df, target_start_date):
    print("\n⚙️  CALCULANDO PIVOTES VECTORIZADOS...")
    
    # A. Pre-cálculo de Pivotes (Diario)
    daily_df = df.resample('1D').agg({
        'high': 'max',
        'low': 'min',
        'close': 'last'
    })

    # Shift(1): Datos de AYER para operar HOY
    daily_df['prev_high'] = daily_df['high'].shift(1)
    daily_df['prev_low'] = daily_df['low'].shift(1)
    daily_df['prev_close'] = daily_df['close'].shift(1)
    
    # Fórmulas Camarilla/Standard
    daily_df['P'] = (daily_df['prev_high'] + daily_df['prev_low'] + daily_df['prev_close']) / 3
    daily_df['R1'] = (2 * daily_df['P']) - daily_df['prev_low']
    daily_df['S1'] = (2 * daily_df['P']) - daily_df['prev_high']
    
    daily_df.dropna(inplace=True)

    # B. Variables
    balance = CAPITAL_INICIAL
    position = None 
    entry_price = 0
    trades_history = []
    
    print(f"🚀 INICIANDO SIMULACIÓN...")
    
    # C. Bucle Principal
    for current_time, row in df.iterrows():
        # Si estamos en zona de buffer, NO operamos (solo acumulamos indicadores si hubiera)
        if current_time < target_start_date:
            continue
            
        price = row['close']
        current_date_str = str(current_time.date())
        
        # Buscar Pivote
        try:
            day_stats = daily_df.loc[current_date_str]
            pivot = day_stats['P']
            r1 = day_stats['R1']
        except KeyError:
            continue
            
        # --- ESTRATEGIA (Ejemplo) ---
        # Long si cruza P hacia arriba
        if position is None:
            if row['close'] > pivot and row['open'] < pivot:
                position = 'LONG'
                entry_price = price
        
        elif position == 'LONG':
            # TP en R1, SL 2%
            take_profit = r1
            stop_loss = entry_price * 0.98
            
            if price >= take_profit or price <= stop_loss:
                pnl_pct = (price - entry_price) / entry_price
                pnl_usd = balance * pnl_pct
                balance += pnl_usd
                
                trades_history.append({
                    'date': current_time,
                    'type': 'LONG',
                    'pnl_usd': pnl_usd,
                    'reason': 'TP' if price >= take_profit else 'SL'
                })
                position = None

    generar_reporte(trades_history, CAPITAL_INICIAL, balance)

def generar_reporte(trades, start_cap, end_cap):
    if not trades:
        print("\n⚠️ 0 Trades realizados.")
        return

    df_t = pd.DataFrame(trades)
    total_trades = len(df_t)
    wins = df_t[df_t['pnl_usd'] > 0]
    
    win_rate = (len(wins) / total_trades) * 100
    total_pnl = end_cap - start_cap
    pf = df_t[df_t['pnl_usd']>0]['pnl_usd'].sum() / abs(df_t[df_t['pnl_usd']<=0]['pnl_usd'].sum()) if len(df_t[df_t['pnl_usd']<=0]) > 0 else 999

    print("\n" + "="*45)
    print(f"📊 REPORTE LOCAL - {SYMBOL}")
    print("="*45)
    print(f"💰 PnL Neto:          ${total_pnl:.2f}")
    print(f"🎲 Trades Totales:    {total_trades}")
    print(f"✅ Win Rate:          {win_rate:.2f}%")
    print(f"⚖️ Profit Factor:     {pf:.2f}")
    print("="*45 + "\n")

# ==========================================
# EJECUCIÓN
# ==========================================
if __name__ == "__main__":
    # Cargar archivo local
    df_data, fecha_real = cargar_datos_locales_con_buffer(SYMBOL, TRADING_START_DATE, BUFFER_DAYS)
    
    if df_data is not None and not df_data.empty:
        backtest_v9_3(df_data, fecha_real)