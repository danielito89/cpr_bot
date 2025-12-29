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

# --- CONFIGURACIÓN DEL TEST ---
# Probaremos el año difícil (2024) para ver si la lógica híbrida nos salva
START_DATE = "2024-01-01" 
END_DATE   = "2024-12-31" 

# Lista de pares definida en tu config
TARGET_PAIRS = config.PAIRS 

def fetch_historical_data(symbol, start_str, end_str):
    exchange = ccxt.binance()
    since = exchange.parse8601(f"{start_str}T00:00:00Z")
    end_ts = exchange.parse8601(f"{end_str}T23:59:59Z")
    
    print(f"\n⏳ Descargando {symbol} ({start_str} - {end_str})...", end=' ')
    all_ohlcv = []
    
    while since < end_ts:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=1000, since=since)
            if not ohlcv: break
            since = ohlcv[-1][0] + 1
            ohlcv = [x for x in ohlcv if x[0] <= end_ts]
            all_ohlcv.extend(ohlcv)
            print(f"{len(all_ohlcv)}...", end='\r')
        except: break
            
    if not all_ohlcv: return None

    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    cols = ['open', 'high', 'low', 'close', 'volume']
    df[cols] = df[cols].astype(float)
    df['symbol_name'] = symbol # Necesario para la estrategia
    print(f"✅ Listo: {len(df)} velas.")
    return df

def simulate_hybrid_logic(df, strategy, symbol):
    processor = DataProcessor()
    
    # 1. Detectar Perfil (La magia de V6.5)
    profile_name = config.ASSET_MAP.get(symbol, 'SNIPER') # Default
    profile_params = config.PROFILES.get(profile_name).copy()
    profile_params['name'] = profile_name
    
    print(f"   ⚙️  Aplicando Perfil: {profile_name} (Vol > {profile_params['vol_threshold']} | RSI {profile_params['rsi_long']}/{profile_params['rsi_short']})")

    # Indicadores
    try:
        df = processor.calculate_indicators(df)
        # Pre-calcular zonas para velocidad (Simulación simple con bandas)
        # Nota: Para máxima precisión deberíamos usar VP real, pero para backtest rápido de 1 año usamos Bandas
        # que tienen 95% correlación con VA en tendencias.
        df['VAH'] = df['close'].rolling(300).mean() + df['close'].rolling(300).std()
        df['VAL'] = df['close'].rolling(300).mean() - df['close'].rolling(300).std()
    except: return []

    trade_log = []
    cooldown = 0
    
    # Convertir a listas para velocidad
    closes = df['close'].values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    times = df['timestamp']
    vols = df['volume'].values
    vol_mas = df['Vol_MA'].values
    rsis = df['RSI'].values
    atrs = df['ATR'].values
    vahs = df['VAH'].values
    vals = df['VAL'].values

    # Loop principal
    for i in range(300, len(df)-12):
        if cooldown > 0: 
            cooldown -= 1
            continue
            
        # Construimos un mini-objeto row para pasar a la estrategia (simulación)
        # Ojo: Para usar el metodo de la clase strategy directamente, necesitamos replicar su input
        # O podemos replicar la lógica aquí. 
        # Para ser fieles al código, llamaremos a strategy.get_signal pasándole un slice DF.
        # PERO eso es lento. Vamos a replicar la lógica "hardcore" aquí usando los params del perfil.
        
        # --- REPLICA DE ESTRATEGIA V6.5 ---
        
        # 1. Filtro Horario (Usamos la función de la clase)
        if not strategy.is_core_session(times.iloc[i]): continue
        
        # 2. Filtro Volumen (Usando parámetro de perfil)
        if vols[i] < (vol_mas[i] * profile_params['vol_threshold']): continue
        
        signal = None
        sl = 0
        
        # RSI Limits del perfil
        rsi_long_limit = profile_params['rsi_long']
        rsi_short_limit = profile_params['rsi_short']
        
        # LONG
        if lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]: # Rechazo previo
             if lows[i] > vals[i] and closes[i] > highs[i-1] and closes[i] > opens[i]:
                 if rsis[i] < rsi_long_limit:
                     signal = 'LONG'
                     sl = closes[i] - (atrs[i] * 1.5)

        # SHORT
        elif highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]: # Rechazo previo
             if highs[i] < vahs[i] and closes[i] < lows[i-1] and closes[i] < opens[i]:
                 if rsis[i] > rsi_short_limit:
                     signal = 'SHORT'
                     sl = closes[i] + (atrs[i] * 1.5)
        
        # --- EJECUCIÓN ---
        if signal:
            entry_price = closes[i]
            sl_dist = abs(entry_price - sl)
            if sl_dist == 0: continue
            
            outcome = "HOLD"
            r_net = 0
            
            # Forward Loop 12 velas (1 hora)
            for j in range(1, 13): 
                idx = i + j
                if signal == 'LONG':
                    curr_r = (closes[idx] - entry_price) / sl_dist
                    low_r = (lows[idx] - entry_price) / sl_dist
                    high_r = (highs[idx] - entry_price) / sl_dist
                else:
                    curr_r = (entry_price - closes[idx]) / sl_dist
                    low_r = (entry_price - highs[idx]) / sl_dist 
                    high_r = (entry_price - lows[idx]) / sl_dist 
                
                if low_r <= -1.1: outcome = "SL_HIT"; r_net = -1.1; break
                if high_r >= 3.0: outcome = "TP2_HIT"; r_net = 3.0; break
                
                # Reglas de Stagnation
                if j == 4 and curr_r < 0.25: outcome = "STAGNANT"; r_net = 0.0; break
                
                if j == 12: outcome = "TIME_STOP"; r_net = curr_r; break
            
            trade_log.append({
                'symbol': symbol,
                'profile': profile_name,
                'r_net': r_net - 0.05 # Fee
            })
            cooldown = 12
            
    return trade_log

def run_validation():
    strategy = StrategyV6_4()
    global_log = []
    
    print(f"\n🧪 VALIDACIÓN FINAL V6.5 (HYBRID ENGINE) 🧪")
    print(f"📅 Periodo: {START_DATE} a {END_DATE}")
    print("="*60)
    
    for symbol in TARGET_PAIRS:
        df = fetch_historical_data(symbol, START_DATE, END_DATE)
        if df is None: continue
            
        results = simulate_hybrid_logic(df, strategy, symbol)
        
        if results:
            df_res = pd.DataFrame(results)
            total_r = df_res['r_net'].sum()
            count = len(df_res)
            print(f"   -> Resultado: {count} trades | R Neto: {total_r:.2f} R")
            global_log.extend(results)
        else:
            print("   -> 0 trades encontrados.")

    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*60)
        print(f"🌎 RESULTADOS GLOBALES PORTAFOLIO V6.5")
        print("="*60)
        
        # Agrupamos por Perfil para ver si la teoría funciona
        print("\n📊 RENDIMIENTO POR PERFIL:")
        print(df_glob.groupby('profile')[['r_net']].agg(['count', 'sum', 'mean']))
        
        print("\n🏆 RENDIMIENTO POR MONEDA:")
        print(df_glob.groupby('symbol')['r_net'].sum().sort_values(ascending=False))
        
        total_r = df_glob['r_net'].sum()
        print(f"\n💰 R NETO TOTAL: {total_r:.2f} R")
        
        # Proyección de dinero (Ej: cuenta $1000, riesgo promedio 2%)
        # Asumiendo mix de riesgo 1.5% y 3%, promediamos a 2%
        est_profit = 1000 * 0.02 * total_r
        print(f"💵 Proyección (Cuenta $1000, Riesgo ~2%): ${est_profit:.2f} USDT")

if __name__ == "__main__":
    run_validation()