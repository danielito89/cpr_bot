import pandas as pd
import numpy as np
import sys
import os
import ccxt

# Importamos módulos de producción
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
from core.data_processor import DataProcessor
from strategies.strategy_v6_4 import StrategyV6_4

# --- CONFIGURACIÓN DEL LABORATORIO ---
# Incluimos los validados + los memes a testear
TARGET_PAIRS = [
    'BTC/USDT',      # Benchmark
    'ETH/USDT',      # Benchmark
    'SOL/USDT',      # Alta Volatilidad
    'BNB/USDT',      # Validado
    'DOGE/USDT',     # Meme King
    '1000PEPE/USDT', # Meme Volatility (Ojo: usa 1000PEPE en Futuros)
    'WIF/USDT'       # Solana Meme Trend
]

TIMEFRAME = '5m'
TOTAL_CANDLES = 30000  # Aprox 3 meses recientes

def fetch_data(symbol):
    exchange = ccxt.binance()
    print(f"\n📡 Descargando {TOTAL_CANDLES} velas de {symbol}...")
    
    # Intentamos descargar. Si falla (ej: símbolo incorrecto), retornamos None
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=1000)
    except Exception as e:
        print(f"⚠️ Error descargando {symbol}: {e}")
        return None

    all_ohlcv = ohlcv
    
    # Estimación de tiempo para el loop
    tf_min = 5
    ms_chunk = 1000 * tf_min * 60 * 1000
    
    while len(all_ohlcv) < TOTAL_CANDLES:
        oldest = all_ohlcv[0][0]
        since = oldest - ms_chunk
        try:
            batch = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=1000, since=since)
            batch = [x for x in batch if x[0] < oldest]
            if not batch: break
            all_ohlcv = batch + all_ohlcv
            print(f"   Buffer: {len(all_ohlcv)} velas...", end='\r')
        except: break
    
    if len(all_ohlcv) > TOTAL_CANDLES: all_ohlcv = all_ohlcv[-TOTAL_CANDLES:]
    
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    cols = ['open', 'high', 'low', 'close', 'volume']
    df[cols] = df[cols].astype(float)
    return df

def simulate_logic(df, strategy, symbol_name):
    processor = DataProcessor()
    # Calculamos indicadores usando la lógica V6.4 real
    df = processor.calculate_indicators(df)
    
    trade_log = []
    last_idx = -999
    cooldown = 12
    
    # Loop de simulación
    for i in range(500, len(df)):
        if i - last_idx < cooldown: continue
        
        # Slice para Volume Profile
        current_slice = df.iloc[i-300 : i+1]
        zones = processor.get_volume_profile_zones(current_slice)
        
        # Señal
        trade = strategy.get_signal(current_slice, zones)
        
        if trade:
            # Simulación de Gestión (Simplificada V6.4 para velocidad)
            outcome = "HOLD"
            entry_price = trade['entry_price']
            sl = trade['stop_loss']
            sl_dist = abs(entry_price - sl)
            if sl_dist == 0: sl_dist = entry_price * 0.01 # Evitar div/0
            
            tp1_hit = False
            r_net = 0
            bars_duration = 0
            
            # Forward Loop (Miramos el futuro para ver el resultado)
            for j in range(1, 13): # 1 hora max
                if i+j >= len(df): break
                c = df.iloc[i+j]
                bars_duration = j
                
                # Calc R
                if trade['type'] == 'LONG':
                    curr_r = (c['close'] - entry_price) / sl_dist
                    high_r = (c['high'] - entry_price) / sl_dist
                    low_r = (c['low'] - entry_price) / sl_dist
                else:
                    curr_r = (entry_price - c['close']) / sl_dist
                    high_r = (entry_price - c['low']) / sl_dist 
                    low_r = (entry_price - c['high']) / sl_dist 
                
                # Reglas de Salida V6.4
                if j == 2 and curr_r < -0.10: outcome = "FAILED_FT"; r_net = -0.15; break
                if j == 4 and curr_r < 0.25: outcome = "STAGNANT"; r_net = 0.0; break
                if j == 6 and curr_r < 0.20: outcome = "STAGNANT_LATE"; r_net = -0.15; break
                
                # Targets
                if high_r >= 3.0: outcome = "TP2_HIT"; r_net = 3.0; break
                if low_r <= -1.1: outcome = "SL_HIT"; r_net = -1.1; break # Slippage incluido
                
                # TP1 State
                if high_r >= 1.0: tp1_hit = True
                
                # Time Stop
                if j == 12: 
                    outcome = "TIME_STOP"
                    r_net = max(curr_r, 0.5) if tp1_hit else curr_r
                    break
            
            # Fees (Smart Fee logic)
            fee = 0.015 if outcome in ['EARLY_EXIT', 'STAGNANT', 'FAILED_FT', 'STAGNANT_LATE'] else 0.045
            
            # --- CORRECCIÓN DEL BUG ---
            # Guardamos 'symbol_name' (string) directamente, no df[...]
            trade_log.append({
                'symbol': symbol_name, 
                'outcome': outcome,
                'r_net': r_net - fee,
                'bars': bars_duration
            })
            
            last_idx = i
            cooldown = 2 if 'STAGNANT' in outcome or 'FAILED' in outcome else 12
            
    return trade_log

def run_multipair_lab():
    strategy = StrategyV6_4()
    global_log = []
    
    print(f"\n🧪 ORANGE PI LAB: MULTIPAIR VALIDATOR (V6.4) 🧪")
    print("="*60)
    
    for symbol in TARGET_PAIRS:
        df = fetch_data(symbol)
        
        if df is None or df.empty:
            print(f"   ⚠️ Saltando {symbol} por falta de datos.")
            continue
            
        print(f"   Procesando {symbol}...")
        # Pasamos el nombre del símbolo explícitamente
        results = simulate_logic(df, strategy, symbol)
        
        if not results:
            print(f"   ⚠️ {symbol}: 0 trades encontrados (mercado muy tranquilo).")
            continue
            
        df_res = pd.DataFrame(results)
        total_r = df_res['r_net'].sum()
        expectancy = df_res['r_net'].mean()
        
        print(f"   -> {symbol}: {len(df_res)} trades | R Neto: {total_r:.2f} | Exp: {expectancy:.2f}R")
        global_log.extend(results)

    # REPORTE GLOBAl
    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*60)
        print("🌎 RESULTADOS GLOBALES DE PORTAFOLIO")
        print("="*60)
        print(f"Total Trades: {len(df_glob)}")
        print(f"TOTAL R NETO: {df_glob['r_net'].sum():.2f} R")
        print(f"Expectancy:   {df_glob['r_net'].mean():.3f} R / trade")
        
        print("\n🏆 RANKING POR PAR (R NETO):")
        # Ahora el groupby funcionará perfecto porque 'symbol' es string
        print(df_glob.groupby('symbol')['r_net'].sum().sort_values(ascending=False))
        
        print("\n📊 Distribución de Outcomes:")
        print(df_glob['outcome'].value_counts())
    else:
        print("\n❌ Ningún par generó trades.")

if __name__ == "__main__":
    run_multipair_lab()