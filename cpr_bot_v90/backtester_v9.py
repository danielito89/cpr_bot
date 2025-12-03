import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time

# ==========================================
# CONFIGURACIÓN GENERAL
# ==========================================
SYMBOL = 'ETH/USDT'
TIMEFRAME = '1h'
TRADING_START_DATE = "2022-01-01"  # Fecha donde quieres que el bot empiece a operar
BUFFER_DAYS = 25                   # Días extra hacia atrás para cargar indicadores previos
CAPITAL_INICIAL = 1000             # USD

# ==========================================
# 1. MOTOR DE DATOS (CON BUFFER)
# ==========================================
def descargar_datos_con_buffer(symbol, start_date_str, timeframe='1h', buffer_days=30):
    """
    Descarga datos desde (start_date - buffer) para asegurar que el primer día
    operativo tenga historial previo para calcular pivotes.
    """
    # Configurar Exchange (Usamos Binance como ejemplo público)
    exchange = ccxt.binance({'enableRateLimit': True})
    
    # Calcular fechas
    target_start = pd.to_datetime(start_date_str)
    download_start = target_start - timedelta(days=buffer_days)
    since_ts = int(download_start.timestamp() * 1000)
    
    print(f"\n📥 [DATA] Iniciando descarga...")
    print(f"   - Objetivo Operativo: {target_start.date()}")
    print(f"   - Descarga Real (Buffer): {download_start.date()} (Cargando {buffer_days} días extra)")

    all_ohlcv = []
    
    # Bucle simple de paginación para CCXT
    # NOTA: En producción, usa un while loop robusto. Aquí descargamos un bloque grande para el ejemplo.
    try:
        # Descargamos un lote grande (limitado por el exchange, usualmente 1000 velas)
        # Para backtests muy largos, necesitarás un bucle while 'since' < 'now'
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since_ts, limit=1000)
        all_ohlcv.extend(ohlcv)
        
        # Simulamos una segunda petición si es necesario (ejemplo simplificado)
        if len(ohlcv) == 1000:
            last_ts = ohlcv[-1][0]
            ohlcv2 = exchange.fetch_ohlcv(symbol, timeframe, since=last_ts, limit=1000)
            all_ohlcv.extend(ohlcv2)
            
    except Exception as e:
        print(f"❌ Error descargando datos: {e}")
        return pd.DataFrame(), target_start

    # Crear DataFrame
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    
    print(f"✅ Datos descargados: {len(df)} velas. Desde {df.index[0]} hasta {df.index[-1]}")
    return df, target_start

# ==========================================
# 2. GENERADOR DE REPORTE (KPIs)
# ==========================================
def generar_reporte_profesional(trades, start_cap, end_cap):
    if not trades:
        print("\n⚠️ No se realizaron operaciones. Revisa la lógica o los datos.")
        return

    df_trades = pd.DataFrame(trades)
    
    # Cálculos básicos
    total_trades = len(df_trades)
    wins = df_trades[df_trades['pnl_usd'] > 0]
    losses = df_trades[df_trades['pnl_usd'] <= 0]
    
    win_rate = (len(wins) / total_trades) * 100
    total_pnl = end_cap - start_cap
    roi = (total_pnl / start_cap) * 100
    
    avg_win = wins['pnl_usd'].mean() if not wins.empty else 0
    avg_loss = losses['pnl_usd'].mean() if not losses.empty else 0
    
    # Profit Factor
    gross_profit = wins['pnl_usd'].sum()
    gross_loss = abs(losses['pnl_usd'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss != 0 else 999
    
    # Máximo Drawdown (Simulado sobre el balance final de cada trade)
    # Crea una serie de balances acumulados
    equity_curve = [start_cap]
    current_bal = start_cap
    for pnl in df_trades['pnl_usd']:
        current_bal += pnl
        equity_curve.append(current_bal)
    
    equity_series = pd.Series(equity_curve)
    peak = equity_series.cummax()
    drawdown = (equity_series - peak) / peak
    max_drawdown = drawdown.min() * 100

    print("\n" + "="*45)
    print(f"📊 REPORTE DE RESULTADOS V9.3 - {SYMBOL}")
    print("="*45)
    print(f"💰 Balance Inicial:   ${start_cap:.2f}")
    print(f"💰 Balance Final:     ${end_cap:.2f}")
    print(f"📈 PnL Neto:          ${total_pnl:.2f} ({roi:.2f}%)")
    print(f"📉 Max Drawdown:      {max_drawdown:.2f}%")
    print("-" * 45)
    print(f"🎲 Trades Totales:    {total_trades}")
    print(f"✅ Win Rate:          {win_rate:.2f}%")
    print(f"⚖️ Profit Factor:     {profit_factor:.2f} (Objetivo > 1.5)")
    print("-" * 45)
    print(f"🟢 Promedio Ganancia: ${avg_win:.2f}")
    print(f"🔴 Promedio Pérdida:  ${avg_loss:.2f}")
    print("="*45 + "\n")

# ==========================================
# 3. MOTOR DE BACKTEST (Lógica de Pivotes)
# ==========================================
def backtest_v9_3(df, target_start_date):
    print("\n⚙️  PROCESANDO DATOS Y CALCULANDO PIVOTES...")
    
    # --- A. PRE-CÁLCULO DE PIVOTES DIARIOS (La Solución) ---
    # Resampleamos a días para obtener OHLC D1
    daily_df = df.resample('1D').agg({
        'high': 'max',
        'low': 'min',
        'close': 'last'
    })

    # SHIFT(1): Usamos los datos de AYER para calcular los niveles de HOY
    # Si hoy es 2 de Enero, usamos High/Low/Close del 1 de Enero.
    daily_df['prev_high'] = daily_df['high'].shift(1)
    daily_df['prev_low'] = daily_df['low'].shift(1)
    daily_df['prev_close'] = daily_df['close'].shift(1)
    
    # Fórmulas de Pivotes (Standard)
    daily_df['P'] = (daily_df['prev_high'] + daily_df['prev_low'] + daily_df['prev_close']) / 3
    daily_df['R1'] = (2 * daily_df['P']) - daily_df['prev_low']
    daily_df['S1'] = (2 * daily_df['P']) - daily_df['prev_high']
    
    # Eliminamos los días del inicio que no tienen "ayer" (los NaNs iniciales)
    daily_df.dropna(inplace=True)

    # --- B. INICIALIZACIÓN DE VARIABLES ---
    balance = CAPITAL_INICIAL
    position = None     # 'LONG', 'SHORT', None
    entry_price = 0
    trades_history = []
    
    # Filtramos el DF para iterar SOLO desde la fecha oficial de inicio
    operational_df = df[df.index >= target_start_date]
    
    if operational_df.empty:
        print("❌ Error Crítico: No hay datos después de la fecha de inicio.")
        return

    print(f"🚀 INICIANDO LOOP DE TRADING ({len(operational_df)} velas)...")
    
    # --- C. BUCLE VELA A VELA ---
    for current_time, row in operational_df.iterrows():
        price = row['close']
        current_date_str = str(current_time.date())
        
        # 1. BUSCAR PIVOTES DEL DÍA
        try:
            day_stats = daily_df.loc[current_date_str]
            pivot = day_stats['P']
            r1 = day_stats['R1']
            s1 = day_stats['S1']
        except KeyError:
            # Si falta data de ese día específico, saltamos
            continue
            
        # 2. ESTRATEGIA (Ejemplo simple de Pivotes)
        # =========================================
        
        # ENTRADA LONG: Si el precio cruza hacia arriba el Pivote
        if position is None:
            # Condición: Precio mayor al pivote Y apertura menor (cruce)
            if row['close'] > pivot and row['open'] < pivot:
                position = 'LONG'
                entry_price = price
                # print(f"   [LONG] {current_time} @ {price:.2f} | P: {pivot:.2f}")
        
        # GESTIÓN DE POSICIÓN (Salida)
        elif position == 'LONG':
            # Take Profit en R1  OR  Stop Loss del 2%
            take_profit = r1
            stop_loss = entry_price * 0.98 
            
            if price >= take_profit or price <= stop_loss:
                # Calcular PnL
                pnl_pct = (price - entry_price) / entry_price
                pnl_usd = balance * pnl_pct
                
                # Actualizar Balance
                balance += pnl_usd
                
                # Guardar Trade
                trades_history.append({
                    'date': current_time,
                    'type': 'LONG',
                    'entry': entry_price,
                    'exit': price,
                    'pnl_usd': pnl_usd,
                    'reason': 'TP' if price >= take_profit else 'SL'
                })
                
                position = None # Reset
                # print(f"   [CLOSE] {current_time} @ {price:.2f} | PnL: ${pnl_usd:.2f}")

    # --- D. FINALIZAR ---
    generar_reporte_profesional(trades_history, CAPITAL_INICIAL, balance)

# ==========================================
# EJECUCIÓN
# ==========================================
if __name__ == "__main__":
    # 1. Cargar datos con el "colchón" de seguridad
    df_data, fecha_inicio_real = descargar_datos_con_buffer(SYMBOL, TRADING_START_DATE, TIMEFRAME, BUFFER_DAYS)
    
    # 2. Ejecutar Backtest si hay datos
    if not df_data.empty:
        backtest_v9_3(df_data, fecha_inicio_real)