# scalper_pro/lab_robustness.py
import pandas as pd
import numpy as np
import sys
import os
import ccxt
from datetime import datetime

# --- IMPORTACIÓN DE MÓDULOS DE PRODUCCIÓN ---
# Agregamos el directorio actual al path para poder importar los módulos hermanos
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from core.data_processor import DataProcessor
from strategies.strategy_v6_4 import StrategyV6_4

# ==========================================
# 1. MOTOR DE DESCARGA (Independiente de API Key)
# ==========================================
def fetch_history_for_backtest(total_candles=50000):
    """
    Descarga data histórica usando ccxt público (sin usar claves de config)
    para no gastar rate limit de la cuenta real o por seguridad.
    """
    exchange = ccxt.binance()
    print(f"📡 STRESS TEST: Descargando {total_candles} velas de {config.SYMBOL}...")
    
    ohlcv = exchange.fetch_ohlcv(config.SYMBOL, config.TIMEFRAME, limit=1000)
    all_ohlcv = ohlcv
    
    # Cálculo aproximado de tiempo
    timeframe_mins = int(config.TIMEFRAME.replace('m',''))
    timeframe_ms = timeframe_mins * 60 * 1000
    
    while len(all_ohlcv) < total_candles:
        oldest_timestamp = all_ohlcv[0][0]
        since_timestamp = oldest_timestamp - (1000 * timeframe_ms)
        try:
            new_batch = exchange.fetch_ohlcv(config.SYMBOL, config.TIMEFRAME, limit=1000, since=since_timestamp)
            # Filtrar solapamientos
            new_batch = [x for x in new_batch if x[0] < oldest_timestamp]
            
            if not new_batch: break
            all_ohlcv = new_batch + all_ohlcv
            
            if len(all_ohlcv) % 10000 == 0:
                print(f"   ... {len(all_ohlcv)} velas cargadas")
        except Exception as e:
            print(f"⚠️ Error descarga: {e}")
            break
            
    if len(all_ohlcv) > total_candles:
        all_ohlcv = all_ohlcv[-total_candles:]
        
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    # Convertir a float
    cols = ['open', 'high', 'low', 'close', 'volume']
    df[cols] = df[cols].astype(float)
    
    return df

# ==========================================
# 2. SIMULADOR DE GESTIÓN (Réplica exacta de main.py)
# ==========================================
def simulate_trade_management(df, entry_index, trade_data):
    """
    Replica EXACTAMENTE la lógica de salida del bucle while de main.py
    """
    entry_price = trade_data['entry_price']
    stop_loss = trade_data['stop_loss']
    direction = trade_data['type']
    atr = trade_data['atr']
    
    # Targets calculados igual que en main.py (State Manager)
    # TP1 y TP2 no son ordenes limit en el main, son chequeos logicos, 
    # pero usamos los ratios de config para calcular R.
    
    tp1_hit = False
    bars_held = 0
    
    # Iteramos vela a vela hacia el futuro
    # Límite 12 velas (1 hora) como en main.py
    for j in range(1, 13): 
        if entry_index + j >= len(df): break
        
        row = df.iloc[entry_index + j]
        bars_held = j
        
        # Precios de la vela actual
        curr_high = row['high']
        curr_low = row['low']
        curr_close = row['close']
        
        # Calcular R flotante al cierre (como hace el bot en tiempo real)
        sl_dist = abs(entry_price - stop_loss)
        if direction == 'LONG':
            pnl_r = (curr_close - entry_price) / sl_dist
            pnl_r_high = (curr_high - entry_price) / sl_dist # Para chequear TP intra-vela
            pnl_r_low = (curr_low - entry_price) / sl_dist   # Para chequear SL intra-vela
        else:
            pnl_r = (entry_price - curr_close) / sl_dist
            pnl_r_high = (entry_price - curr_low) / sl_dist  # Para chequear TP (precio baja)
            pnl_r_low = (entry_price - curr_high) / sl_dist  # Para chequear SL (precio sube)

        # --- LÓGICA V6.4 (IDÉNTICA A MAIN.PY) ---
        
        # 1. Failed Follow-Through (Barra 2)
        if bars_held == 2 and pnl_r < -0.10:
            return {"outcome": "FAILED_FT", "r_net": -0.15, "bars": j} # Costo fijo estimado

        # 2. Aggressive Stagnant (Barra 4)
        if bars_held == 4 and pnl_r < 0.25:
            return {"outcome": "STAGNANT", "r_net": 0.0, "bars": j} # Salida BE/Scratch

        # 3. Standard Stagnant (Barra 6)
        if bars_held == 6 and pnl_r < 0.20:
            return {"outcome": "STAGNANT_LATE", "r_net": -0.15, "bars": j}

        # 4. Time Stop (Barra 11)
        if bars_held >= 11:
            # Si salimos por tiempo, nos llevamos lo que hay (o min 0.5 si hubo TP1)
            final_r = max(pnl_r, 0.5) if tp1_hit else pnl_r
            return {"outcome": "TIME_STOP", "r_net": final_r, "bars": j}

        # 5. TP2 (3R) - Chequeo Intra-vela
        if pnl_r_high >= 3.0: # Asumimos fill en 3R
            return {"outcome": "TP2_HIT", "r_net": 3.0, "bars": j}

        # 6. Hard SL - Chequeo Intra-vela
        if pnl_r_low <= -1.1: # Slippage incluido
            return {"outcome": "SL_HIT", "r_net": -1.1, "bars": j}

        # TP1 Mental Update (Solo estado)
        if pnl_r_high >= 1.0:
            tp1_hit = True

    # Si se acaba la data o el loop
    return {"outcome": "FORCE_CLOSE", "r_net": pnl_r, "bars": bars_held}

# ==========================================
# 3. EJECUCIÓN DEL STRESS TEST
# ==========================================
def run_robustness_test():
    # 1. Inicializar Clases REALES
    processor = DataProcessor()
    strategy = StrategyV6_4()
    
    # 2. Datos
    df = fetch_history_for_backtest(total_candles=50000)
    print("⚙️ Calculando indicadores (Core Processor)...")
    df = processor.calculate_indicators(df)
    
    trade_log = []
    last_trade_idx = -999
    cooldown = 12
    
    print(f"\n⚡ EJECUTANDO VALIDACIÓN CON LÓGICA DE PRODUCCIÓN...")
    
    # Simulamos el bucle principal
    for i in range(500, len(df)):
        if i - last_trade_idx < cooldown: continue
        
        # Preparamos el "slice" de datos que vería el bot en vivo
        # En main.py el bot ve "todo hasta ahora".
        # Aquí pasamos el DF completo pero le decimos a la estrategia que mire el índice 'i'.
        # Como StrategyV6_4 usa .iloc[-1], necesitamos pasarle un subset que termine en i.
        
        # Optimización: Pasamos un slice pequeño (últimas 300 velas) para no matar la RAM/CPU
        # pero suficiente para Volume Profile (288 velas)
        current_slice = df.iloc[i-300 : i+1] 
        
        # Calcular Zonas en el momento (como hace main.py)
        zones = processor.get_volume_profile_zones(current_slice)
        
        # Pedir Señal a la Estrategia REAL
        trade_signal = strategy.get_signal(current_slice, zones)
        
        if trade_signal:
            # Si hay señal, simulamos la gestión
            result = simulate_trade_management(df, i, trade_signal)
            
            # Aplicar Smart Fees (V6.4)
            fee = 0.015 if result['outcome'] in ['EARLY_EXIT', 'STAGNANT', 'FAILED_FT', 'STAGNANT_LATE'] else 0.045
            final_r = result['r_net'] - fee
            
            trade_log.append({
                "time": trade_signal['time'],
                "type": trade_signal['type'],
                "outcome": result['outcome'],
                "r_net": final_r,
                "bars": result['bars']
            })
            
            last_trade_idx = i
            # Cooldown dinámico (replicar si lo usas en main, sino fijo)
            if result['outcome'] in ['EARLY_EXIT', 'STAGNANT', 'FAILED_FT']:
                cooldown = 2
            else:
                cooldown = 12

    # ==========================================
    # 4. REPORTING DE ROBUSTEZ
    # ==========================================
    if not trade_log:
        print("❌ No trades found.")
        return

    df_res = pd.DataFrame(trade_log)
    df_res['time'] = pd.to_datetime(df_res['time'])
    df_res.set_index('time', inplace=True)
    
    print("\n" + "="*60)
    print("📊 REPORTE DE ROBUSTEZ (LÓGICA VINCULADA)")
    print("="*60)
    print(f"Estrategia: {strategy.name}")
    print(f"Dataset:    {df_res.index[0]} -> {df_res.index[-1]}")
    print(f"Trades:     {len(df_res)}")
    print(f"R Neto Tot: {df_res['r_net'].sum():.2f} R")
    print(f"Expectancy: {df_res['r_net'].mean():.3f} R / trade")

    # A. ANÁLISIS MENSUAL
    monthly = df_res.resample('M')['r_net'].sum()
    print("\n📅 RENDIMIENTO MENSUAL:")
    print(monthly)
    months_neg = len(monthly[monthly < 0])
    print(f"Meses Negativos: {months_neg}")
    print(f"Peor Mes: {monthly.min():.2f} R")

    # B. RIESGO SECUENCIAL
    df_res['is_loss'] = df_res['r_net'] < 0
    loss_groups = (df_res['is_loss'] != df_res['is_loss'].shift()).cumsum()
    streaks = df_res.groupby(loss_groups)['is_loss'].sum()
    max_loss_streak = int(streaks.max()) if not streaks.empty else 0
    print(f"\n💀 Max Racha Perdedora: {max_loss_streak} trades")
    
    # C. STRESS TEST (SLIPPAGE)
    stress_penalty = 0.02
    stress_total = (df_res['r_net'] - stress_penalty).sum()
    print(f"\n📉 STRESS TEST (Slippage Agresivo): {stress_total:.2f} R")
    
    if stress_total > 0:
        print("✅ EL SISTEMA ES ROBUSTO.")
    else:
        print("⚠️ PRECAUCIÓN: El sistema sufre con slippage.")

    print("\nDistribución:")
    print(df_res['outcome'].value_counts())

if __name__ == "__main__":
    run_robustness_test()