import sys
import os
import pandas as pd
import numpy as np
import yfinance as yf
from tabulate import tabulate

# Hack para importar la estrategia desde ../bots/breakout/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bots.breakout.strategy import BreakoutBotStrategy

# Mapeo de Símbolos Binance -> Yahoo Finance
SYMBOLS_MAP = {
    'BTC/USDT': 'BTC-USD',
    'ETH/USDT': 'ETH-USD',
    'SOL/USDT': 'SOL-USD',
    'BNB/USDT': 'BNB-USD',
    'DOGE/USDT': 'DOGE-USD'
}

def download_data(yahoo_symbol, start="2022-01-01", end="2024-04-01"):
    print(f"📥 Descargando {yahoo_symbol}...")
    df = yf.download(yahoo_symbol, start=start, end=end, interval="1h", progress=False)
    
    # Resamplear a 4H (Yahoo da 1h, lo convertimos a 4h para el bot)
    df_4h = df.resample('4H').agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }).dropna()
    
    return df_4h

def run_simulation(symbol, df, strategy_params):
    # Instanciar estrategia con parámetros específicos
    strategy = BreakoutBotStrategy(
        atr_period=14, lookback=20
    )
    # Sobreescribir con parámetros personalizados
    strategy.sl_atr = strategy_params['sl_atr']
    strategy.tp_partial_atr = strategy_params['tp_partial_atr']
    strategy.trailing_dist_atr = strategy_params['trailing_dist_atr']
    strategy.vol_multiplier = strategy_params['vol_multiplier']

    # Pre-calcular indicadores
    df = strategy.calculate_indicators(df.copy())
    
    state = {'status': 'WAITING_BREAKOUT'}
    trades = []
    equity = 1000.0  # Capital inicial por moneda
    
    # Loop de simulación
    for i in range(200, len(df)):
        # Simular "ventana" de datos hasta el momento actual
        window = df.iloc[:i+1]
        
        signal = strategy.get_signal(window, state)
        action = signal['action']
        current_date = df.index[i]
        
        if action == 'ENTER_LONG':
            state.update({
                'status': 'IN_POSITION',
                'entry_price': signal['entry_price'],
                'stop_loss': signal['stop_loss'],
                'tp_partial': signal['tp_partial'],
                'position_size_pct': 1.0,
                'trailing_active': False,
                'highest_price_post_tp': 0.0
            })
            
        elif action == 'EXIT_PARTIAL':
            price = signal.get('new_sl', 0) # Aproximación, usamos el precio de trigger
            # En realidad salimos al precio TP, aquí simplificamos visualización
            pnl_pct = (state['tp_partial'] - state['entry_price']) / state['entry_price']
            profit = (equity * 0.5) * pnl_pct
            equity += profit
            trades.append([current_date, symbol, 'PARTIAL_TP', pnl_pct*100])
            
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
            entry_price = state['entry_price']
            
            # Calculamos PnL sobre el tamaño restante (100% o 50%)
            size = state['position_size_pct']
            pnl_pct = (exit_price - entry_price) / entry_price
            
            # Ajuste de equity
            amount_invested = 1000.0 * size # Simplificado
            profit = amount_invested * pnl_pct
            equity += profit
            
            trades.append([current_date, symbol, action, pnl_pct*100])
            
            state = {
                'status': 'COOLDOWN', 
                'last_exit_time': str(current_date)
            }
            
        elif action == 'RESET_STATE':
             state['status'] = 'WAITING_BREAKOUT'

    return equity, len(trades)

# --- EJECUCIÓN PRINCIPAL ---
if __name__ == "__main__":
    results = []
    
    # Configuraciones a probar (Simulando config.py)
    configs = {
        'BTC/USDT': {'sl_atr': 1.0, 'tp_partial_atr': 2.5, 'trailing_dist_atr': 1.5, 'vol_multiplier': 1.5},
        'ETH/USDT': {'sl_atr': 1.2, 'tp_partial_atr': 3.0, 'trailing_dist_atr': 2.0, 'vol_multiplier': 1.6},
        'SOL/USDT': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.8},
    }

    print("🚀 INICIANDO BACKTEST (2022 - 2024)...")
    
    for symbol, params in configs.items():
        yahoo_sym = SYMBOLS_MAP.get(symbol)
        if not yahoo_sym: continue
        
        try:
            df = download_data(yahoo_sym)
            final_cap, num_trades = run_simulation(symbol, df, params)
            roi = ((final_cap - 1000) / 1000) * 100
            
            results.append([symbol, num_trades, f"${final_cap:.2f}", f"{roi:.2f}%"])
        except Exception as e:
            print(f"Error en {symbol}: {e}")

    print("\n📊 RESULTADOS FINALES (Capital Inicial $1000)")
    print(tabulate(results, headers=['Par', '# Trades', 'Capital Final', 'ROI %'], tablefmt='grid'))