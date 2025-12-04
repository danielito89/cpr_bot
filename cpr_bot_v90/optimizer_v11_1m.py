#!/usr/bin/env python3
# optimizer_v11_1m.py
# OPTIMIZACIÓN BAYESIANA PARA 1M (Vectorizada pero Realista)
# - Entradas desplazadas al Open siguiente (N+1)
# - Resolución de conflictos pesimista (SL gana a TP)
# - Optimizado para velocidad en Orange Pi

import pandas as pd
import numpy as np
from bayes_opt import BayesianOptimization
import time
import warnings
import os

# Ignorar warnings de pandas
warnings.filterwarnings('ignore')

# --- CONFIGURACIÓN ---
FILE_PATH = 'data/mainnet_data_1m_ETHUSDT.csv'
MONTHS_TO_ANALYZE = 6  # Analizar solo los últimos X meses para velocidad y relevancia
CAPITAL = 1000
LEVERAGE = 20

# Rangos de búsqueda (Ajustados para 1m scalping)
PBOUNDS = {
    'volume_factor': (1.1, 4.0),           # Factor relativo al promedio
    'strict_volume_factor': (1.5, 6.0),    # Filtro de trampas
    'breakout_tp_mult': (1.5, 6.0),        # TP en ATRs
    'trailing_stop_trigger_atr': (1.0, 4.0)# Distancia SL/Trailing
}

# ==========================================
# 1. CARGA DE DATOS OPTIMIZADA
# ==========================================
def load_data(path, months):
    if not os.path.exists(path):
        print(f"❌ No se encuentra: {path}")
        return None
    
    print(f"📂 Cargando dataset: {path}...")
    df = pd.read_csv(path)
    df.columns = [x.lower() for x in df.columns]
    
    # Convertir fecha y filtrar últimos meses
    if 'open_time' in df.columns:
        df['date'] = pd.to_datetime(df['open_time'])
    else:
        df['date'] = pd.to_datetime(df['timestamp'])
    
    cutoff_date = df['date'].max() - pd.DateOffset(months=months)
    df = df[df['date'] >= cutoff_date].copy().reset_index(drop=True)
    
    print(f"✅ Datos recortados a últimos {months} meses: {len(df)} velas.")
    return df

# ==========================================
# 2. LÓGICA VECTORIZADA (SHIFTED)
# ==========================================
def run_vectorized_backtest(df_in, volume_factor, strict_volume_factor, breakout_tp_mult, trailing_stop_trigger_atr):
    # Trabajamos con copia para no ensuciar memoria global
    df = df_in.copy()
    
    # --- A. INDICADORES (Numpy es más rápido que Pandas puros a veces) ---
    # SMA Volumen (20 periodos)
    df['vol_ma'] = df['volume'].rolling(20).mean()
    
    # ATR (14 periodos)
    h_l = df['high'] - df['low']
    h_c = (df['high'] - df['close'].shift(1)).abs()
    l_c = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([h_l, h_c, l_c], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    # Limpieza de NaN iniciales
    df.dropna(inplace=True)
    
    # --- B. SEÑALES (Vela N) ---
    # Lógica simplificada de volumen (CPR es complejo de vectorizar, usamos Volatilidad como proxy)
    # Señal: Volumen > Promedio * Factor  Y  Volumen > Promedio * StrictFactor
    # Y Vela Alcista (Close > Open) para Longs (Simplificación para optimizador)
    
    # NOTA: Aquí simplificamos la lógica CPR para que corra rápido. 
    # Buscamos parámetros de VOLUMEN y ATR que gestionen el riesgo.
    
    signal_long = (df['volume'] > (df['vol_ma'] * volume_factor)) & \
                  (df['volume'] > (df['vol_ma'] * strict_volume_factor)) & \
                  (df['close'] > df['open']) # Vela verde fuerte
                  
    # --- C. EJECUCIÓN (Vela N+1) ---
    # Desplazamos la señal 1 vela hacia adelante. 
    # Si signal en t=0, entramos en Open de t=1
    df['entry_long'] = signal_long.shift(1)
    
    # Filtramos solo las filas donde entramos
    trades = df[df['entry_long'] == True].copy()
    
    if len(trades) == 0:
        return 0, 0 # Sin trades
    
    # --- D. CÁLCULO DE RESULTADOS (Vectorizado Pesimista) ---
    # Entry Price = Open de la vela actual (porque shifteamos la señal)
    entry_price = trades['open']
    atr = trades['atr'] # ATR capturado al momento de entrada (aprox correcto)
    
    # Definir Niveles
    tp_price = entry_price + (atr * breakout_tp_mult)
    sl_price = entry_price - (atr * trailing_stop_trigger_atr)
    
    # Verificar qué pasó en ESA vela (Intra-bar check simplificado)
    # Low toca SL?
    hit_sl = trades['low'] <= sl_price
    # High toca TP?
    hit_tp = trades['high'] >= tp_price
    
    # Lógica Pesimista: Si toca ambos, es SL. Si solo TP, es Win. Si solo SL, es Loss.
    # Si no toca ninguno en la primera vela... (Aquí está la limitación de la vectorización pura)
    # *TRUCO:* Para optimizar rápido, asumimos que si no toca TP en la primera vela de explosión,
    # cerramos al Cierre de esa vela (Time decay) o usamos un proxy.
    # Para ser más exactos sin bucles, usamos el resultado de la vela de entrada como proxy de la fuerza.
    
    # Matriz de PnL
    # Caso 1: Toca SL (o ambos) -> Loss fijo del tamaño del SL
    pnl_sl = (sl_price - entry_price) / entry_price
    
    # Caso 2: Toca TP (y no SL) -> Win fijo
    pnl_tp = (tp_price - entry_price) / entry_price
    
    # Caso 3: No toca nada -> Salimos al Close (Scalping rápido)
    pnl_close = (trades['close'] - entry_price) / entry_price
    
    # Asignar PnL
    trades['pnl'] = np.where(hit_sl, pnl_sl, 
                             np.where(hit_tp, pnl_tp, pnl_close))
    
    # Aplicar Apalancamiento y Comisiones (aprox 0.04% in + 0.04% out)
    trades['pnl_net'] = (trades['pnl'] * LEVERAGE) - 0.0008
    
    # --- E. MÉTRICAS ---
    total_pnl = trades['pnl_net'].sum()
    wins = (trades['pnl_net'] > 0).sum()
    total_trades = len(trades)
    
    win_rate = wins / total_trades if total_trades > 0 else 0
    
    # Función Objetivo: Combinación de PnL y WinRate
    # Queremos PnL alto pero penalizamos si el WinRate es bajo (<40%)
    score = total_pnl 
    if win_rate < 0.4:
        score = score * 0.5 # Penalización
        
    return score, total_trades

# ==========================================
# 3. FUNCIÓN OBJETIVO PARA BAYESIAN OPT
# ==========================================
GLOBAL_DF = None # Variable global para no pasar el DF gigante cada vez

def optimization_target(volume_factor, strict_volume_factor, breakout_tp_mult, trailing_stop_trigger_atr):
    score, _ = run_vectorized_backtest(GLOBAL_DF, volume_factor, strict_volume_factor, breakout_tp_mult, trailing_stop_trigger_atr)
    return score

# ==========================================
# 4. EJECUCIÓN PRINCIPAL
# ==========================================
if __name__ == "__main__":
    # 1. Cargar datos
    GLOBAL_DF = load_data(FILE_PATH, months=MONTHS_TO_ANALYZE)
    
    if GLOBAL_DF is not None:
        print("\n🧠 INICIANDO OPTIMIZACIÓN EN 1M (MODO FAST)...")
        print("   Este modo usa aproximaciones vectorizadas para velocidad.")
        print("   Objetivo: Encontrar la zona 'caliente' de parámetros.")
        
        optimizer = BayesianOptimization(
            f=optimization_target,
            pbounds=PBOUNDS,
            random_state=1,
            verbose=2
        )
        
        # Más iteraciones porque es rápido
        optimizer.maximize(
            init_points=10,
            n_iter=50, 
        )
        
        print("\n🏆 MEJORES PARÁMETROS (APROXIMADOS):")
        print(optimizer.max['params'])
        print("\n👉 AHORA: Usa estos valores en 'backtester_v14.py' para validarlos con realismo total.")