import pandas as pd
import numpy as np
import sys
import os
import ccxt
from datetime import datetime, timedelta

# Importamos módulos de producción
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
from core.data_processor import DataProcessor
from strategies.strategy_v6_4 import StrategyV6_4

# --- CONFIGURACIÓN DE LA MÁQUINA DEL TIEMPO ---
TARGET_PAIRS = [
    'BTC/USDT'            # Los Nuevos Descubrimientos
]

# FECHAS A TESTEAR (Formato YYYY-MM-DD)
START_DATE = "2022-01-01" 
END_DATE   = "2022-06-31" 

TIMEFRAME = '5m'

def fetch_historical_data(symbol, start_str, end_str):
    exchange = ccxt.binance()
    
    # Convertir fechas a timestamp ms
    since = exchange.parse8601(f"{start_str}T00:00:00Z")
    end_ts = exchange.parse8601(f"{end_str}T23:59:59Z")
    
    print(f"\n⏳ Viajando a {start_str} para {symbol}...", end=' ')
    
    all_ohlcv = []
    
    while since < end_ts:
        try:
            # Descargamos lotes de 1000 velas
            ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=1000, since=since)
            if not ohlcv: break
            
            last_ts = ohlcv[-1][0]
            since = last_ts + 1 # Avanzamos el cursor
            
            # Filtramos los que se pasen del end_date
            ohlcv = [x for x in ohlcv if x[0] <= end_ts]
            all_ohlcv.extend(ohlcv)
            
            print(f"{len(all_ohlcv)} velas...", end='\r')
            
            if len(ohlcv) < 1000: break # Se acabaron los datos
            
        except Exception as e:
            print(f"Error: {e}")
            break
            
    if not all_ohlcv: return None

    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    cols = ['open', 'high', 'low', 'close', 'volume']
    df[cols] = df[cols].astype(float)
    
    print(f"✅ Completado: {len(df)} velas.")
    return df

def simulate_logic(df, strategy, symbol_name):
    processor = DataProcessor()
    
    # Indicadores
    try:
        df = processor.calculate_indicators(df)
    except: return [] # Error en cálculo (data insuficiente)

    trade_log = []
    last_idx = -999
    cooldown = 12
    
    # Optimizamos el loop
    # Convertimos a dict para velocidad, pandas iterrows es lento
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    times = df['timestamp']
    
    # Loop principal
    for i in range(500, len(df)):
        if i - last_idx < cooldown: continue
        
        # Simulamos pasarle el slice al bot (Data Frame slicing es necesario para indicadores complejos)
        current_slice = df.iloc[i-300 : i+1]
        zones = processor.get_volume_profile_zones(current_slice)
        
        trade = strategy.get_signal(current_slice, zones)
        
        if trade:
            entry_price = trade['entry_price']
            sl = trade['stop_loss']
            sl_dist = abs(entry_price - sl)
            if sl_dist == 0: sl_dist = entry_price * 0.01
            
            tp1_hit = False
            r_net = 0
            outcome = "HOLD"
            
            # Forward Loop (Miramos futuro hasta 12 velas / 1h)
            for j in range(1, 13): 
                if i+j >= len(df): break
                
                c_close = closes[i+j]
                c_high = highs[i+j]
                c_low = lows[i+j]
                
                # Calc R
                if trade['type'] == 'LONG':
                    curr_r = (c_close - entry_price) / sl_dist
                    high_r = (c_high - entry_price) / sl_dist
                    low_r = (c_low - entry_price) / sl_dist
                else:
                    curr_r = (entry_price - c_close) / sl_dist
                    high_r = (entry_price - c_low) / sl_dist 
                    low_r = (entry_price - c_high) / sl_dist 
                
                # Reglas V6.4 Exactas
                if j == 2 and curr_r < -0.10: outcome = "FAILED_FT"; r_net = -0.15; break
                if j == 4 and curr_r < 0.25: outcome = "STAGNANT"; r_net = 0.0; break
                if j == 6 and curr_r < 0.20: outcome = "STAGNANT_LATE"; r_net = -0.15; break
                if high_r >= 3.0: outcome = "TP2_HIT"; r_net = 3.0; break
                if low_r <= -1.1: outcome = "SL_HIT"; r_net = -1.1; break 
                
                if high_r >= 1.0: tp1_hit = True
                
                if j == 12: 
                    outcome = "TIME_STOP"
                    # Time Stop Logic: Si tocó TP1, salimos en BE+ (0.5), si no, lo que dé
                    r_net = max(curr_r, 0.5) if tp1_hit else curr_r
                    break
            
            fee = 0.05 # Fee estimado agresivo
            
            trade_log.append({
                'symbol': symbol_name, 
                'outcome': outcome,
                'r_net': r_net - fee,
                'date': times.iloc[i]
            })
            
            last_idx = i
            cooldown = 2 if 'STAGNANT' in outcome or 'FAILED' in outcome else 12
            
    return trade_log

def run_time_machine():
    strategy = StrategyV6_4()
    global_log = []
    
    print(f"\n🔮 MÁQUINA DEL TIEMPO V6.4 ({START_DATE} a {END_DATE}) 🔮")
    print(f"Filtros: Lunes-Viernes | 08:00 - 19:00 UTC")
    print("="*60)
    
    for symbol in TARGET_PAIRS:
        df = fetch_historical_data(symbol, START_DATE, END_DATE)
        
        if df is None or df.empty: continue
            
        print(f"   Simulando {symbol}...")
        results = simulate_logic(df, strategy, symbol)
        
        if not results:
            print(f"   ⚠️ {symbol}: 0 trades.")
            continue
            
        df_res = pd.DataFrame(results)
        total_r = df_res['r_net'].sum()
        
        print(f"   -> {symbol}: {len(df_res)} trades | R Neto: {total_r:.2f}")
        global_log.extend(results)

    if global_log:
        df_glob = pd.DataFrame(global_log)
        print("\n" + "="*60)
        print(f"🌎 RESULTADOS AÑO {START_DATE[:4]}")
        print("="*60)
        print(f"Total Trades: {len(df_glob)}")
        print(f"TOTAL R NETO: {df_glob['r_net'].sum():.2f} R")
        print(f"Expectancy:   {df_glob['r_net'].mean():.3f} R / trade")
        
        print("\n🏆 RANKING:")
        print(df_glob.groupby('symbol')['r_net'].sum().sort_values(ascending=False))
        
        print("\n📉 Drawdown Check (Peor racha):")
        # Calculo simple de racha negativa
        df_glob['cum_pnl'] = df_glob['r_net'].cumsum()
        print(f"Equity Final: {df_glob['cum_pnl'].iloc[-1]:.2f} R")
    else:
        print("\n❌ Ningún trade encontrado.")

if __name__ == "__main__":
    run_time_machine()