#!/usr/bin/env python3
# optimizer_v12_high_vol.py
# RANGO EXTENDIDO PARA "FRANCOTIRADOR DE VOLUMEN"
# Objetivo: Filtrar agresivamente el ruido buscando Volume Factor > 6.0

import pandas as pd
import numpy as np
from bayes_opt import BayesianOptimization
import warnings
import os

warnings.filterwarnings('ignore')

# --- CONFIGURACIÓN ---
FILE_PATH = 'data/mainnet_data_1m_ETHUSDT.csv'
MONTHS_TO_ANALYZE = 6
LEVERAGE = 20

# RANGOS ACTUALIZADOS (Buscando lo que pediste)
PBOUNDS = {
    'volume_factor': (2.0, 8.0),            # Subimos el piso
    'strict_volume_factor': (6.0, 25.0),    # ¡Aquí está el cambio clave! De 6 hasta 25
    'breakout_tp_mult': (3.0, 12.0),        # Targets más ambiciosos para movimientos fuertes
    'trailing_stop_trigger_atr': (2.0, 8.0) # Stops más holgados para dejar correr
}

# ==========================================
# 1. CARGA DE DATOS
# ==========================================
def load_data(path, months):
    if not os.path.exists(path):
        print(f"❌ No se encuentra: {path}")
        return None
    
    print(f"📂 Cargando dataset: {path}...")
    df = pd.read_csv(path)
    df.columns = [x.lower() for x in df.columns]
    
    if 'open_time' in df.columns:
        df['date'] = pd.to_datetime(df['open_time'])
    else:
        df['date'] = pd.to_datetime(df['timestamp'])
    
    cutoff_date = df['date'].max() - pd.DateOffset(months=months)
    df = df[df['date'] >= cutoff_date].copy().reset_index(drop=True)
    
    print(f"✅ Datos recortados a últimos {months} meses: {len(df)} velas.")
    return df

# ==========================================
# 2. LÓGICA VECTORIZADA (SHIFTED + HIGH VOL)
# ==========================================
def run_vectorized_backtest(df_in, volume_factor, strict_volume_factor, breakout_tp_mult, trailing_stop_trigger_atr):
    df = df_in.copy()
    
    # Indicadores
    df['vol_ma'] = df['volume'].rolling(20).mean()
    
    h_l = df['high'] - df['low']
    h_c = (df['high'] - df['close'].shift(1)).abs()
    l_c = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([h_l, h_c, l_c], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    df.dropna(inplace=True)
    
    # SEÑAL (Vela N) - Buscamos la explosión
    # Nota: Si strict > volume_factor, la condición de strict domina.
    signal_long = (df['volume'] > (df['vol_ma'] * strict_volume_factor)) & \
                  (df['close'] > df['open']) 
                  
    # EJECUCIÓN (Vela N+1)
    df['entry_long'] = signal_long.shift(1)
    trades = df[df['entry_long'] == True].copy()
    
    if len(trades) == 0:
        return -100, 0 # Penalización fuerte por no operar
    
    # CÁLCULO DE RESULTADOS (Vectorizado Pesimista)
    entry_price = trades['open']
    atr = trades['atr']
    
    tp_price = entry_price + (atr * breakout_tp_mult)
    sl_price = entry_price - (atr * trailing_stop_trigger_atr)
    
    hit_sl = trades['low'] <= sl_price
    hit_tp = trades['high'] >= tp_price
    
    pnl_sl = (sl_price - entry_price) / entry_price
    pnl_tp = (tp_price - entry_price) / entry_price
    pnl_close = (trades['close'] - entry_price) / entry_price
    
    trades['pnl'] = np.where(hit_sl, pnl_sl, 
                             np.where(hit_tp, pnl_tp, pnl_close))
    
    trades['pnl_net'] = (trades['pnl'] * LEVERAGE) - 0.0008
    
    total_pnl = trades['pnl_net'].sum()
    wins = (trades['pnl_net'] > 0).sum()
    total_trades = len(trades)
    
    # Filtro de Calidad: Queremos evitar overfitting con 2 trades de suerte
    if total_trades < 10: 
        return -50, total_trades

    # SCORE
    # Priorizamos PnL Total positivo
    return total_pnl, total_trades

# ==========================================
# 3. EJECUCIÓN
# ==========================================
GLOBAL_DF = None

def optimization_target(volume_factor, strict_volume_factor, breakout_tp_mult, trailing_stop_trigger_atr):
    score, _ = run_vectorized_backtest(GLOBAL_DF, volume_factor, strict_volume_factor, breakout_tp_mult, trailing_stop_trigger_atr)
    return score

if __name__ == "__main__":
    GLOBAL_DF = load_data(FILE_PATH, months=MONTHS_TO_ANALYZE)
    
    if GLOBAL_DF is not None:
        print("\n🦅 INICIANDO OPTIMIZACIÓN 'FRANCOTIRADOR' (Rango Vol: 6x - 25x)...")
        
        optimizer = BayesianOptimization(
            f=optimization_target,
            pbounds=PBOUNDS,
            random_state=42,
            verbose=2
        )
        
        optimizer.maximize(init_points=10, n_iter=60)
        
        print("\n🏆 MEJOR CONFIGURACIÓN ENCONTRADA:")
        print(optimizer.max['params'])