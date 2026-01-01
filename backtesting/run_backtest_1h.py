import sys
import os
import pandas as pd
import numpy as np
import ccxt
import time
from datetime import datetime
from tabulate import tabulate

# --- IMPORTACIÓN DE ESTRATEGIA ---
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bots.breakout.strategy import BreakoutBotStrategy

# --- CONFIGURACIÓN DEL EXPERIMENTO ---
TIMEFRAME = '1h'
# Descargamos desde 2023 para tener contexto histórico
SINCE_STR = "2022-01-01 00:00:00" 
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
FEE_TAKER = 0.0006 

if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

# AÑOS A TESTEAR (Separados para ver consistencia)
TEST_YEARS = [2022, 2023, 2024, 2025] 

# TUS CONFIGURACIONES GANADORAS
configs = {
    'DOGE/USDT': {'sl_atr': 2.0, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.8},
    'FET/USDT':  {'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 1.7},
    'WIF/USDT':  {'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.8},
    '1000PEPE/USDT': {'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.8},
    'AVAX/USDT': {'sl_atr': 2.5, 'tp_partial_atr': 5.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.8},
    'SOL/USDT':  {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5}
}

def fetch_full_history(symbol, timeframe, since_str):
    safe_symbol = symbol.replace('/', '_').replace(':', '_')
    csv_path = os.path.join(DATA_DIR, f"{safe_symbol}_{timeframe}_FULL.csv")
    
    # Si existe, cargamos y chequeamos si tiene datos viejos
    if os.path.exists(csv_path):
        print(f"📂 Cargando {symbol} desde caché...", end=" ")
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        # Si el caché empieza después de lo que pedimos, forzamos descarga nueva
        if df.index[0] > pd.to_datetime(since_str):
            print("Datos insuficientes (necesitamos 2023). Descargando de nuevo...")
        else:
            print("OK.")
            return df

    print(f"📥 Descargando historial completo {symbol}...")
    exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
    since = exchange.parse8601(since_str)
    all_ohlcv = []
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not ohlcv: break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            if ohlcv[-1][0] >= (exchange.milliseconds() - 3600 * 1000): break
            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            print(f"Error: {e}")
            break
    
    if not all_ohlcv: return pd.DataFrame()
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = df[~df.index.duplicated(keep='first')]
    df.to_csv(csv_path)
    return df

def run_fidelity_simulation(symbol, df, strategy_params):
    strategy = BreakoutBotStrategy()
    strategy.sl_atr = strategy_params['sl_atr']
    strategy.tp_partial_atr = strategy_params['tp_partial_atr']
    strategy.trailing_dist_atr = strategy_params['trailing_dist_atr']
    strategy.vol_multiplier = strategy_params['vol_multiplier']

    # Calculamos indicadores sobre TODO el dataframe para no perder EMAs al cortar años
    df = strategy.calculate_indicators(df.copy())
    
    state = {'status': 'WAITING_BREAKOUT'}
    trades = []
    equity = 1000.0
    
    # IMPORTANTE: Empezamos en 200 para tener indicadores listos
    for i in range(200, len(df)):
        current_window = df.iloc[:i+1]
        current_date = current_window.index[-1]
        
        signal = strategy.get_signal(current_window, state)
        action = signal['action']
        
        if action == 'ENTER_LONG':
            entry_price = signal['entry_price']
            equity -= equity * FEE_TAKER
            state.update({
                'status': 'IN_POSITION',
                'entry_price': entry_price,
                'stop_loss': signal['stop_loss'],
                'tp_partial': signal['tp_partial'],
                'position_size_pct': 1.0,
                'trailing_active': False,
                'atr_at_breakout': signal.get('atr_at_breakout', 0),
                'highest_price_post_tp': 0.0
            })
            
        elif action == 'EXIT_PARTIAL':
            exit_price = state['tp_partial']
            pnl_pct = (exit_price - state['entry_price']) / state['entry_price']
            gross_profit = (equity * 0.5) * pnl_pct
            fee = (equity * 0.5) * FEE_TAKER
            equity += (gross_profit - fee)
            
            state.update({
                'position_size_pct': 0.5,
                'stop_loss': signal['new_sl'],
                'trailing_active': True,
                'highest_price_post_tp': signal['highest_price_post_tp']
            })

        elif action == 'UPDATE_TRAILING':
            state['stop_loss'] = signal['new_sl']
            state['highest_price_post_tp'] = signal['highest_price_post_tp']

        elif action in ['EXIT_SL', 'EXIT_TRAILING']:
            exit_price = state['stop_loss']
            size_pct = state['position_size_pct']
            pnl_pct = (exit_price - state['entry_price']) / state['entry_price']
            
            amount = equity * size_pct
            gross_profit = amount * pnl_pct
            fee = amount * FEE_TAKER
            equity += (gross_profit - fee)
            
            state = {'status': 'COOLDOWN', 'last_exit_time': str(current_date)}

        elif 'new_status' in signal:
            state['status'] = signal['new_status']
            if 'breakout_level' in signal: state['breakout_level'] = signal['breakout_level']
            if 'atr_at_breakout' in signal: state['atr_at_breakout'] = signal['atr_at_breakout']
            
    return equity, len(trades) # (Nota: equity es acumulativo)

if __name__ == "__main__":
    print(f"🚀 BACKTEST ANUALIZADO (1H) - {SINCE_STR[:4]} a HOY")
    
    for year in TEST_YEARS:
        results = []
        print(f"\n📅 AÑO: {year}")
        print("="*60)
        
        total_profit = 0
        
        for symbol, params in configs.items():
            try:
                # 1. Obtenemos todo el historial
                df_full = fetch_full_history(symbol, TIMEFRAME, SINCE_STR)
                if df_full.empty: continue

                # 2. FILTRADO POR AÑO
                # Creamos una copia que solo contiene los datos de ESE año
                df_year = df_full[df_full.index.year == year].copy()
                
                if df_year.empty:
                    # Caso WIF/PEPE que quizas no existian a principios de 2023 en futuros
                    results.append([symbol, 0, "N/A", "N/A"])
                    continue

                # 3. Corremos simulación solo en ese año
                # Reiniciamos capital a $1000 cada año para comparar ROI limpio
                final_cap, _ = run_fidelity_simulation(symbol, df_year, params)
                
                roi = ((final_cap - 1000) / 1000) * 100
                profit = final_cap - 1000
                
                color_roi = f"\033[92m{roi:.2f}%\033[0m" if roi > 0 else f"\033[91m{roi:.2f}%\033[0m"
                results.append([symbol, f"${final_cap:.2f}", color_roi])
                total_profit += profit
                
            except Exception as e:
                # print(f"Error {symbol}: {e}") # Debug
                results.append([symbol, "ERROR", str(e)[:10]])

        print(tabulate(results, headers=['Par', 'Capital Final ($1000)', 'ROI %'], tablefmt='grid'))
        print(f"💰 PnL Año {year}: ${total_profit:.2f}")