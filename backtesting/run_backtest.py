import sys
import os
import pandas as pd
import numpy as np
import ccxt
import time
from datetime import datetime
from tabulate import tabulate

# Hack para importar la estrategia desde ../bots/breakout/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
try:
    from bots.breakout.strategy import BreakoutBotStrategy
except ImportError:
    # Fallback por si la estructura de carpetas varía ligeramente
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
    from bots.breakout.strategy import BreakoutBotStrategy

# Configuración
TIMEFRAME = '4h'
SINCE_STR = "2023-06-01 00:00:00"
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')

# Crear carpeta de datos si no existe
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

def fetch_full_history(symbol, timeframe, since_str):
    """
    Descarga historial completo de Binance usando paginación.
    Guarda un CSV en caché para no descargar cada vez.
    """
    safe_symbol = symbol.replace('/', '_')
    csv_path = os.path.join(DATA_DIR, f"{safe_symbol}_{timeframe}.csv")
    
    # Si ya existe el CSV, lo cargamos
    if os.path.exists(csv_path):
        print(f"📂 Cargando {symbol} desde caché local...")
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        return df

    print(f"📥 Descargando historial completo de {symbol} desde Binance...")
    exchange = ccxt.binance({'enableRateLimit': True})
    
    since = exchange.parse8601(since_str)
    all_ohlcv = []
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not ohlcv:
                break
            
            all_ohlcv.extend(ohlcv)
            last_timestamp = ohlcv[-1][0]
            since = last_timestamp + 1
            
            # Si la última vela es reciente (menos de 4h), paramos
            if last_timestamp >= (exchange.milliseconds() - 4 * 3600 * 1000):
                break
                
            print(f"   ... {len(all_ohlcv)} velas")
            time.sleep(exchange.rateLimit / 1000)
            
        except Exception as e:
            print(f"❌ Error descargando: {e}")
            break
    
    if not all_ohlcv:
        return pd.DataFrame()

    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df.to_csv(csv_path)
    return df

def run_simulation(symbol, df, strategy_params):
    # Instanciar estrategia
    strategy = BreakoutBotStrategy(atr_period=14, lookback=20)
    
    # Inyectar parámetros
    strategy.sl_atr = strategy_params['sl_atr']
    strategy.tp_partial_atr = strategy_params['tp_partial_atr']
    strategy.trailing_dist_atr = strategy_params['trailing_dist_atr']
    strategy.vol_multiplier = strategy_params['vol_multiplier']

    # Pre-calcular indicadores
    df = strategy.calculate_indicators(df.copy())
    
    state = {'status': 'WAITING_BREAKOUT'}
    trades = []
    equity = 1000.0
    
    # SIMULACIÓN VELA A VELA
    for i in range(201, len(df)):
        window = df.iloc[:i+1]
        signal = strategy.get_signal(window, state)
        action = signal['action']
        current_date = df.index[i]
        
        # --- Lógica de Ejecución Simulada ---
        
        # 1. MANEJO DE ESTADOS PREVIOS A LA ENTRADA (ESTO FALTABA)
        if action == 'PREPARE_PULLBACK':
            state.update({
                'status': signal['new_status'],
                'breakout_level': signal['breakout_level'],
                'atr_at_breakout': signal['atr_at_breakout']
            })
            
        elif action == 'CANCEL_FOMO':
            state['status'] = signal['new_status']

        # 2. ENTRADA AL MERCADO
        elif action == 'ENTER_LONG':
            cost = 1000 * 0.001 # 0.1% fees
            equity -= cost 
            state.update({
                'status': 'IN_POSITION',
                'entry_price': signal['entry_price'],
                'stop_loss': signal['stop_loss'],
                'tp_partial': signal['tp_partial'],
                'position_size_pct': 1.0,
                'trailing_active': False,
                'highest_price_post_tp': 0.0
            })
            
        # 3. GESTIÓN DE SALIDAS
        elif action == 'EXIT_PARTIAL':
            exit_price = state['tp_partial']
            entry_price = state['entry_price']
            
            pnl_pct = (exit_price - entry_price) / entry_price
            profit = (1000.0 * 0.5) * pnl_pct
            cost = (1000.0 * 0.5) * 0.001 
            
            equity += (profit - cost)
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
            
            size_pct = state['position_size_pct']
            pnl_pct = (exit_price - entry_price) / entry_price
            
            profit = (1000.0 * size_pct) * pnl_pct
            cost = (1000.0 * size_pct) * 0.001
            
            equity += (profit - cost)
            trades.append([current_date, symbol, action, pnl_pct*100])
            
            state = {'status': 'COOLDOWN', 'last_exit_time': str(current_date)}
            
        elif action == 'RESET_STATE':
             state['status'] = 'WAITING_BREAKOUT'

    return equity, len(trades)

if __name__ == "__main__":
    results = []
    
    # CONFIGURACIONES DE RIESGO
    configs = {
        '1000PEPE/USDT': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5}, 
        'ZEC/USDT': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5},
        'SOL/USDT': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5},
        'XRP/USDT': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5},
        'DOGE/USDT': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5},
        'BNB/USDT': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5},
    }
    
    # Nota: Bajé ligeramente el 'vol_multiplier' de 1.5 a 1.3/1.4 para ser un poco más permisivo en backtest 
    # y verificar que hay operaciones.

    print(f"🚀 INICIANDO BACKTEST 4H (Desde {SINCE_STR})...")
    
    for symbol, params in configs.items():
        try:
            df = fetch_full_history(symbol, TIMEFRAME, SINCE_STR)
            if df.empty:
                print(f"⚠️ Sin datos para {symbol}")
                continue

            final_cap, num_trades = run_simulation(symbol, df, params)
            
            roi = ((final_cap - 1000) / 1000) * 100
            color_roi = f"\033[92m{roi:.2f}%\033[0m" if roi > 0 else f"\033[91m{roi:.2f}%\033[0m"
            
            results.append([symbol, num_trades, f"${final_cap:.2f}", color_roi])
            
        except Exception as e:
            print(f"Error crítico en {symbol}: {e}")

    print("\n📊 RESULTADOS FINALES (Capital Inicial $1000 | Fees Incluidos)")
    print(tabulate(results, headers=['Par', '# Trades', 'Capital Final', 'ROI %'], tablefmt='grid'))