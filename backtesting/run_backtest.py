import sys
import os
import pandas as pd
import numpy as np
import ccxt
import time
from datetime import datetime
from tabulate import tabulate

# --- 1. IMPORTACIÓN DE LA ESTRATEGIA REAL (PRODUCCIÓN) ---
# Esto garantiza que usamos LA MISMA lógica que el bot en vivo
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bots.breakout.strategy import BreakoutBotStrategy

# Configuración
TIMEFRAME = '4h'
SINCE_STR = "2023-01-01 00:00:00" # Backtest desde 2023
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
FEE_TAKER = 0.0006 # 0.06% (0.05% Binance + 0.01% Slippage estimado)

if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

def fetch_full_history(symbol, timeframe, since_str):
    """Descarga datos usando la misma API de Futuros que producción."""
    safe_symbol = symbol.replace('/', '_').replace(':', '_')
    csv_path = os.path.join(DATA_DIR, f"{safe_symbol}_{timeframe}.csv")
    
    if os.path.exists(csv_path):
        print(f"📂 Cargando {symbol} desde caché...")
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        return df

    print(f"📥 Descargando historial de {symbol} (Futuros)...")
    exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
    exchange.load_markets()
    
    since = exchange.parse8601(since_str)
    all_ohlcv = []
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not ohlcv: break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            if ohlcv[-1][0] >= (exchange.milliseconds() - 4 * 3600 * 1000): break
            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            print(f"❌ Error descargando: {e}")
            break
    
    if not all_ohlcv: return pd.DataFrame()

    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df.to_csv(csv_path)
    return df

def run_fidelity_simulation(symbol, df, strategy_params):
    """
    Simulación de Alta Fidelidad: Replica el bucle while True del main_breakout.py
    """
    # 1. Instanciar la MISMA clase de estrategia
    strategy = BreakoutBotStrategy()
    strategy.sl_atr = strategy_params['sl_atr']
    strategy.tp_partial_atr = strategy_params['tp_partial_atr']
    strategy.trailing_dist_atr = strategy_params['trailing_dist_atr']
    strategy.vol_multiplier = strategy_params['vol_multiplier']

    # 2. Calcular indicadores (igual que en producción)
    df = strategy.calculate_indicators(df.copy())
    
    # Estado inicial (Simula el JSON vacío)
    state = {'status': 'WAITING_BREAKOUT'}
    
    trades = []
    equity = 1000.0  # Capital inicial simulado
    initial_equity = equity
    
    # --- BUCLE VELA A VELA (Simulando el paso del tiempo) ---
    # Empezamos en 200 para dar espacio a la EMA200
    for i in range(200, len(df)):
        # Simulamos que "df" es lo que el bot ve en ese momento (ohlcv limit=300)
        # Cortamos el dataframe hasta la vela 'i'
        current_window = df.iloc[:i+1]
        current_candle = current_window.iloc[-1]
        current_date = current_window.index[-1]
        
        # 3. Obtener Señal (Igual que main.py)
        signal = strategy.get_signal(current_window, state)
        action = signal['action']
        
        # --- REPLICANDO LA LÓGICA DE EJECUCIÓN DEL MAIN ---
        
        if action == 'ENTER_LONG':
            # main.py: exchange.create_order(...) + state.update(...)
            entry_price = signal['entry_price']
            
            # Costo de entrada
            cost = equity * FEE_TAKER
            equity -= cost
            
            state.update({
                'status': 'IN_POSITION',
                'entry_price': entry_price,
                'stop_loss': signal['stop_loss'],
                'tp_partial': signal['tp_partial'],
                'position_size_pct': 1.0,      # 100% de la posición
                'trailing_active': False,
                'atr_at_breakout': signal.get('atr_at_breakout', 0), # Replicamos persistencia
                'highest_price_post_tp': 0.0   # Replicamos lógica de trailing
            })
            
        elif action == 'EXIT_PARTIAL':
            # main.py: Vende 50% al precio de TP
            exit_price = state['tp_partial']
            entry_price = state['entry_price']
            
            # Cálculo PnL de la mitad de la posición
            position_value = (equity / 2) # Simplificación: Asumimos reinversión completa
            # Ajuste: En futuros usamos margen, aquí simulamos crecimiento de equity directo
            # PnL % = (Exit - Entry) / Entry
            pnl_pct = (exit_price - entry_price) / entry_price
            
            # Ganancia bruta en $
            gross_profit = (equity * 0.5) * pnl_pct
            
            # Costo de salida (sobre el 50% vendido)
            fee = (equity * 0.5) * FEE_TAKER
            
            equity += (gross_profit - fee)
            
            trades.append([current_date, symbol, 'PARTIAL_TP', pnl_pct*100, equity])
            
            # Actualización de Estado (Igual que main.py)
            state.update({
                'position_size_pct': 0.5,
                'stop_loss': signal['new_sl'], # Breakeven
                'trailing_active': True,
                'highest_price_post_tp': signal['highest_price_post_tp']
            })

        elif action == 'UPDATE_TRAILING':
            # main.py: Solo actualiza el JSON
            state['stop_loss'] = signal['new_sl']
            state['highest_price_post_tp'] = signal['highest_price_post_tp']

        elif action in ['EXIT_SL', 'EXIT_TRAILING']:
            # main.py: Cierra lo que queda (100% o 50%)
            exit_price = state['stop_loss']
            entry_price = state['entry_price']
            size_pct = state['position_size_pct'] # 1.0 o 0.5
            
            pnl_pct = (exit_price - entry_price) / entry_price
            
            # Cálculo PnL sobre la porción restante
            amount_involved = equity * size_pct # Si quedaba 50%, el profit/loss es sobre ese 50%
            # (Nota: 'equity' ya creció por el parcial anterior, así que esto simula interés compuesto)
            
            gross_profit = amount_involved * pnl_pct
            fee = amount_involved * FEE_TAKER
            
            equity += (gross_profit - fee)
            
            trades.append([current_date, symbol, action, pnl_pct*100, equity])
            
            # Reset de estado (Igual que main.py)
            state = {'status': 'COOLDOWN', 'last_exit_time': str(current_date)}

        # Manejo de estados intermedios (Waiting, Prepare Pullback)
        elif 'new_status' in signal:
            state['status'] = signal['new_status']
            if 'breakout_level' in signal: state['breakout_level'] = signal['breakout_level']
            if 'atr_at_breakout' in signal: state['atr_at_breakout'] = signal['atr_at_breakout']

        elif action == 'RESET_STATE':
             state['status'] = 'WAITING_BREAKOUT'

    return equity, len(trades)

if __name__ == "__main__":
    results = []
    
    # --- CONFIGURACIÓN DE RIESGO IDÉNTICA A CONFIG.PY ---
    # (Copia aquí tus valores finales para validar)
    configs = {
        'SOL/USDT':       {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5},
        'DOGE/USDT':      {'sl_atr': 1.0, 'tp_partial_atr': 3.0, 'trailing_dist_atr': 2.0, 'vol_multiplier': 1.5},
        'XPL/USDT':       {'sl_atr': 1.5, 'tp_partial_atr': 3.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.8},
        'XRP/USDT':       {'sl_atr': 1.0, 'tp_partial_atr': 2.0, 'trailing_dist_atr': 1.5, 'vol_multiplier': 1.5},
        'BNB/USDT':       {'sl_atr': 1.0, 'tp_partial_atr': 2.0, 'trailing_dist_atr': 1.5, 'vol_multiplier': 1.5}
        # Agrega pares para testear
    }

    print(f"🚀 INICIANDO BACKTEST DE ALTA FIDELIDAD (Fee: {FEE_TAKER*100}%)")
    print(f"📅 Desde: {SINCE_STR} | Timeframe: {TIMEFRAME}")
    
    for symbol, params in configs.items():
        try:
            df = fetch_full_history(symbol, TIMEFRAME, SINCE_STR)
            if df.empty:
                print(f"⚠️ Sin datos para {symbol}")
                continue

            final_cap, num_trades = run_fidelity_simulation(symbol, df, params)
            
            roi = ((final_cap - 1000) / 1000) * 100
            color_roi = f"\033[92m{roi:.2f}%\033[0m" if roi > 0 else f"\033[91m{roi:.2f}%\033[0m"
            
            results.append([symbol, num_trades, f"${final_cap:.2f}", color_roi])
            
        except Exception as e:
            print(f"Error en {symbol}: {e}")
            import traceback
            traceback.print_exc()

    print("\n📊 RESULTADOS (Capital Inicial $1000)")
    print(tabulate(results, headers=['Par', '# Trades', 'Capital Final', 'ROI %'], tablefmt='grid'))