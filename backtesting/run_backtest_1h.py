import sys
import os
import pandas as pd
import numpy as np
import ccxt
import time
from datetime import datetime
from tabulate import tabulate

# --- IMPORTACIÓN DE ESTRATEGIA (Mismo Cerebro que 4H) ---
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bots.breakout.strategy import BreakoutBotStrategy

# --- CONFIGURACIÓN DEL EXPERIMENTO 1H ---
TIMEFRAME = '1h'  # <--- VELOCIDAD RÁPIDA
SINCE_STR = "2024-01-01 00:00:00" # Solo mercado reciente
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
FEE_TAKER = 0.0006 

if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

# --- CONFIGURACIONES ESPECÍFICAS PARA 1H ---
# Teoría: En 1H hay más ruido. Necesitamos:
# 1. SL más holgado (ATR 2.0) para aguantar mechas.
# 2. Volumen MUY alto (2.0x) para evitar falsas rupturas.
configs = {
    'DOGE/USDT': {
        'sl_atr': 2.0, 
        'tp_partial_atr': 4.0, 
        'trailing_dist_atr': 2.5, 
        'vol_multiplier': 1.8
    },
    'FET/USDT': {
        'sl_atr': 2.0, 
        'tp_partial_atr': 6.0, 
        'trailing_dist_atr': 3.0, 
        'vol_multiplier': 1.7
    },
    'WIF/USDT': {
        'sl_atr': 2.5, 
        'tp_partial_atr': 4.0, 
        'trailing_dist_atr': 3.5, 
        'vol_multiplier': 1.8 # WIF en 1H es una locura, filtro máximo
    },
    '1000PEPE/USDT': {
        'sl_atr': 2.5, 
        'tp_partial_atr': 6.0, 
        'trailing_dist_atr': 3.5, 
        'vol_multiplier': 1.8
    },
    'AVAX/USDT': {
        'sl_atr': 2.5, 
        'tp_partial_atr': 5.0, 
        'trailing_dist_atr': 3.5, 
        'vol_multiplier': 1.8
    }
}

def fetch_full_history(symbol, timeframe, since_str):
    safe_symbol = symbol.replace('/', '_').replace(':', '_')
    csv_path = os.path.join(DATA_DIR, f"{safe_symbol}_{timeframe}_2024.csv")
    
    if os.path.exists(csv_path):
        print(f"📂 Cargando {symbol} ({timeframe}) desde caché...")
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        return df

    print(f"📥 Descargando {symbol} {timeframe} (Futuros)...")
    exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
    
    since = exchange.parse8601(since_str)
    all_ohlcv = []
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not ohlcv: break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            # Paramos si llegamos a hoy
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
    # Instanciamos la estrategia REAL
    strategy = BreakoutBotStrategy()
    strategy.sl_atr = strategy_params['sl_atr']
    strategy.tp_partial_atr = strategy_params['tp_partial_atr']
    strategy.trailing_dist_atr = strategy_params['trailing_dist_atr']
    strategy.vol_multiplier = strategy_params['vol_multiplier']

    df = strategy.calculate_indicators(df.copy())
    state = {'status': 'WAITING_BREAKOUT'}
    
    trades = []
    equity = 1000.0
    
    # Bucle vela a vela
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
            
            trades.append([current_date, symbol, 'PARTIAL', pnl_pct*100, equity])
            
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
            
            trades.append([current_date, symbol, action, pnl_pct*100, equity])
            state = {'status': 'COOLDOWN', 'last_exit_time': str(current_date)}

        elif 'new_status' in signal:
            state['status'] = signal['new_status']
            if 'breakout_level' in signal: state['breakout_level'] = signal['breakout_level']
            if 'atr_at_breakout' in signal: state['atr_at_breakout'] = signal['atr_at_breakout']
            
    return equity, len(trades)

if __name__ == "__main__":
    results = []
    print(f"🚀 BACKTEST BREAKOUT RÁPIDO (1H) - 2024")
    print(f"🌊 Vol Multiplier: High (Filtro de Ruido)")
    
    for symbol, params in configs.items():
        try:
            df = fetch_full_history(symbol, TIMEFRAME, SINCE_STR)
            if df.empty: continue

            final_cap, num_trades = run_fidelity_simulation(symbol, df, params)
            
            roi = ((final_cap - 1000) / 1000) * 100
            color_roi = f"\033[92m{roi:.2f}%\033[0m" if roi > 0 else f"\033[91m{roi:.2f}%\033[0m"
            results.append([symbol, num_trades, f"${final_cap:.2f}", color_roi])
            
        except Exception as e:
            print(f"Error {symbol}: {e}")

    print("\n📊 RESULTADOS 1H (Capital Inicial $1000)")
    print(tabulate(results, headers=['Par', '# Trades', 'Capital Final', 'ROI %'], tablefmt='grid'))