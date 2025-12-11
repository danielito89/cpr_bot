#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split

# ======================================================
#  🔥 CONFIG V48 – AI SMART CPR
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Configuración IA ----
PREDICTION_HORIZON = 4    # La IA predice movimiento a 4 horas
TARGET_PROFIT = 0.015     # Buscamos movimientos > 1.5%
CONFIDENCE_THRESHOLD = 0.55 # Solo entramos si la IA tiene > 55% de certeza

# ---- Gestión de Salida (Motor V47) ----
# Como la IA predice una subida a 4H, usamos salidas más dinámicas
SL_ATR_MULT = 1.5       
TP_ATR_MULT = 2.5       # Buscamos recorridos largos
EXIT_HOURS = 24         # Si en 24h no explotó, fuera

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.03   # 3% riesgo por trade
COMMISSION = 0.0004         
SLIPPAGE_PCT = 0.0006       
SPREAD_PCT = 0.0004
BASE_LATENCY = 0.0001

MIN_QTY = 0.01
BAD_HOURS = [3,4,5]

# ======================================================
#  1. CARGA Y PREPARACIÓN DE DATOS
# ======================================================

def prepare_data(symbol):
    print(f"🔍 Cargando datos para {symbol}...")
    candidates = [f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv", f"{symbol}_{TIMEFRAME_STR}.csv"]
    paths = ["data", ".", "cpr_bot_v90/data"]
    
    df = None
    for name in candidates:
        for p in paths:
            path = os.path.join(p, name)
            if os.path.exists(path):
                print(f"📁 Archivo encontrado: {path}")
                df = pd.read_csv(path)
                break
        if df is not None: break

    if df is None: return None, None

    # Limpieza básica
    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
    df.sort_values("timestamp", inplace=True)
    if 'volume' not in df.columns: df['volume'] = 1.0
    
    # -----------------------------------------------------
    # INGENIERÍA DE CARACTERÍSTICAS (FEATURES) - LO QUE VE LA IA
    # -----------------------------------------------------
    print("🧠 Generando Features (CPR, ATR, Volatilidad)...")
    
    # 1. Indicadores Técnicos
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], 14)
    df['atr_rel'] = df['atr'] / df['close']  # Feature IMPORTANTE #1
    df['rsi'] = talib.RSI(df['close'], 14)
    
    # 2. CPR & Pivots (Rolling 24h simulation)
    high_24 = df['high'].rolling(24).max().shift(1)
    low_24 = df['low'].rolling(24).min().shift(1)
    close_24 = df['close'].shift(1)
    
    pivot = (high_24 + low_24 + close_24) / 3
    bc = (high_24 + low_24) / 2
    tc = (pivot - bc) + pivot
    
    # Features de CPR
    df['cpr_width_pct'] = (tc - bc).abs() / df['close'] # Feature IMPORTANTE #2
    df['dist_pivot_pct'] = (df['close'] - pivot) / df['close']
    
    # 3. Volumen Relativo
    df['vol_ma'] = df['volume'].rolling(20).mean()
    df['rel_vol'] = df['volume'] / df['vol_ma']
    
    # 4. TARGET (ETIQUETADO)
    # 1 si el High futuro (4 velas) sube más de X%, 0 si no.
    future_high = df['high'].shift(-PREDICTION_HORIZON).rolling(PREDICTION_HORIZON).max()
    df['target'] = np.where(future_high > df['close'] * (1 + TARGET_PROFIT), 1, 0)
    
    # Limpieza de NaNs generados por rolling/shift
    df.dropna(inplace=True)
    
    # Dividir TRAIN (Pasado) y TEST (Futuro para backtest)
    # Usamos shuffle=False para respetar el tiempo
    train_size = int(len(df) * 0.70)
    df_train = df.iloc[:train_size].copy()
    df_test = df.iloc[train_size:].copy()
    
    print(f"📊 Dataset dividido: Train ({len(df_train)} velas) | Backtest ({len(df_test)} velas)")
    
    return df_train, df_test

# ======================================================
#  2. ENTRENAMIENTO DEL MODELO
# ======================================================

def train_brain(df_train):
    print("🤖 Entrenando Modelo Random Forest...")
    
    features = ['cpr_width_pct', 'dist_pivot_pct', 'rsi', 'atr_rel', 'rel_vol']
    X = df_train[features]
    y = df_train['target']
    
    # Modelo ligero pero robusto
    model = RandomForestClassifier(n_estimators=100, max_depth=7, min_samples_leaf=5, random_state=42, n_jobs=-1)
    model.fit(X, y)
    
    return model, features

# ======================================================
#  3. MOTOR DE BACKTEST (Solo corre en df_test)
# ======================================================

def run_simulation(model, features, df_test):
    print("\n🚀 Iniciando Simulación V48 (IA Powered) en datos NO VISTOS...")
    
    # Reseteamos índice para el loop
    df = df_test.reset_index(drop=True)
    
    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance
    
    position = None
    entry_price = 0; qty = 0; sl = 0; tp = 0; entry_time = None
    
    trades = []
    
    # Pre-calculamos probabilidades para velocidad (Vectorizado)
    # Esto simula que en cada vela, el bot consulta a la IA
    print("🔮 Generando predicciones de IA para todo el periodo de prueba...")
    X_test = df[features]
    # predict_proba devuelve array [[prob_0, prob_1], ...]
    # Tomamos la columna 1 (Probabilidad de subida)
    probs = model.predict_proba(X_test)[:, 1]
    df['ai_prob'] = probs

    # --- LOOP VELA A VELA ---
    for i in range(len(df)):
        row = df.iloc[i]
        ts = row.timestamp
        c = row.close
        o = row.open
        h = row.high
        l = row.low
        atr = row.atr
        
        # IA Signal
        ai_signal = row.ai_prob
        
        # Costos Dinámicos (Simplificado V47)
        total_friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY
        
        # ==========================
        # 1. BÚSQUEDA DE ENTRADA
        # ==========================
        if position is None:
            if ts.hour not in BAD_HOURS:
                
                # LA SEÑAL ES PURAMENTE IA
                # Si la IA tiene una certeza mayor al Threshold, disparamos.
                if ai_signal > CONFIDENCE_THRESHOLD:
                    
                    # Entry
                    entry_price = o * (1 + total_friction)
                    
                    # SL / TP basados en volatilidad (V47 Logic)
                    sl_dist = atr * SL_ATR_MULT
                    tp_dist = atr * TP_ATR_MULT
                    
                    sl = entry_price - sl_dist
                    tp = entry_price + tp_dist
                    
                    risk_dist = entry_price - sl
                    
                    if risk_dist > 0:
                        # Sizing
                        risk_usd = balance * FIXED_RISK_PCT
                        qty = risk_usd / risk_dist
                        
                        if qty >= MIN_QTY:
                            cost = qty * entry_price * COMMISSION
                            balance -= cost
                            
                            position = "long"
                            entry_time = ts
                            
                            # Intra-candle Check
                            if l <= sl:
                                exit_p = sl * (1 - SLIPPAGE_PCT)
                                pnl = (exit_p - entry_price) * qty
                                fee = exit_p * qty * COMMISSION
                                balance += (pnl - fee)
                                trades.append({'pnl': pnl - cost - fee, 'year': ts.year})
                                position = None
                            
                            elif h >= tp:
                                exit_p = tp * (1 - SLIPPAGE_PCT)
                                pnl = (exit_p - entry_price) * qty
                                fee = exit_p * qty * COMMISSION
                                balance += (pnl - fee)
                                trades.append({'pnl': pnl - cost - fee, 'year': ts.year})
                                position = None

        # ==========================
        # 2. GESTIÓN DE SALIDA
        # ==========================
        elif position == "long":
            exit_p = None
            reason = None
            
            if l <= sl:
                exit_p = sl * (1 - SLIPPAGE_PCT)
                reason = "SL"
            elif h >= tp:
                exit_p = tp * (1 - SLIPPAGE_PCT)
                reason = "TP"
            elif (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600:
                exit_p = c * (1 - SLIPPAGE_PCT)
                reason = "Time"
            
            # IA EXIT ALERT (Opcional): Si la IA dice que la prob baja mucho, salir?
            # Por ahora mantenemos salidas fijas para no sobre-complicar
            
            if exit_p:
                pnl = (exit_p - entry_price) * qty
                fee = exit_p * qty * COMMISSION
                net = pnl - fee # (entry cost ya restado)
                
                balance += (pnl - fee)
                trades.append({'pnl': net, 'year': ts.year})
                position = None
        
        equity_curve.append(balance)

    # ==========================
    # REPORTING
    # ==========================
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V48 (AI SMART CPR) - TEST DATA ONLY")
    print(f"   (Solo operando en datos desconocidos para la IA)")
    print("="*55)
    print(f"💰 Balance Final:   ${balance:.2f}")
    print(f"📈 Retorno Total:   {total_ret:.2f}%")
    
    if not trades_df.empty:
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:        {win:.2f}%")
        print(f"🧮 Total Trades:    {len(trades_df)}")
        print("\n📅 Rendimiento (Solo Periodo Test):")
        print(trades_df.groupby("year")["pnl"].agg(["sum", "count"]))
    else:
        print("⚠️ No hubo trades suficientes en el periodo de prueba.")

# ======================================================
# EJECUCIÓN
# ======================================================
if __name__ == "__main__":
    # 1. Preparar datos
    df_train, df_test = prepare_data(SYMBOL)
    
    if df_train is not None:
        # 2. Entrenar IA
        model, features = train_brain(df_train)
        
        # 3. Simular en datos nuevos
        run_simulation(model, features, df_test)