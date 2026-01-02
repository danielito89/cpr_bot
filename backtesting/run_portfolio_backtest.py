import sys
import os
import pandas as pd
import numpy as np
import ccxt
from datetime import datetime
from tabulate import tabulate

# --- IMPORTACIÓN DE ESTRATEGIA ---
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bots.breakout.strategy import BreakoutBotStrategy

# --- 1. CONFIGURACIÓN DEL PORTAFOLIO ---
INITIAL_CAPITAL = 5000  
RISK_PER_TRADE = 0.03   # 3% riesgo por trade
START_DATE = "2023-01-01" 
END_DATE = "2025-12-31"   

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

# --- 2. EL EJÉRCITO HÍBRIDO ---
PORTFOLIO = {
    # --- DIVISIÓN RÁPIDA (1H) ---
    'DOGE/USDT': {
        'tf': '1h', 
        'params': {'sl_atr': 2.0, 'tp_partial_atr': 3.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 2.0}
    },
    'FET/USDT': {
        'tf': '1h',
        'params': {'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 1.8}
    },
    'WIF/USDT': {
        'tf': '1h',
        'params': {'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.8}
    },
    '1000PEPE/USDT': {
        'tf': '1h',
        'params': {'sl_atr': 2.5, 'tp_partial_atr': 5.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.6}
    },

    # --- DIVISIÓN LENTA (4H) ---
    'SOL/USDT': {
        'tf': '4h',
        'params': {'sl_atr': 1.5, 'tp_partial_atr': 3.5, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5}
    },
    'BTC/USDT': {
        'tf': '4h',
        'params': {'sl_atr': 1.5, 'tp_partial_atr': 2.0, 'trailing_dist_atr': 1.5, 'vol_multiplier': 1.1} 
    }
}

# --- FUNCIONES DE CARGA DE DATOS ---
def get_data(symbol, timeframe):
    safe_symbol = symbol.replace('/', '_').replace(':', '_')
    path_full = os.path.join(DATA_DIR, f"{safe_symbol}_{timeframe}_FULL.csv")
    path_norm = os.path.join(DATA_DIR, f"{safe_symbol}_{timeframe}.csv") 
    
    if os.path.exists(path_full): path = path_full
    elif os.path.exists(path_norm): path = path_norm
    else: return pd.DataFrame() 

    df = pd.read_csv(path, index_col=0, parse_dates=True)
    
    # NORMALIZAR A CAPITALIZADO (Open, High, Low, Close, Volume)
    df.columns = [c.capitalize() for c in df.columns]
    
    # Filtramos por fechas
    df = df[(df.index >= pd.to_datetime(START_DATE)) & (df.index <= pd.to_datetime(END_DATE))]
    return df

# --- MOTOR DE SIMULACIÓN DE PORTAFOLIO ---
def run_portfolio():
    print(f"🚀 INICIANDO SIMULACIÓN DE PORTAFOLIO HÍBRIDO")
    print(f"💰 Capital: ${INITIAL_CAPITAL} | Riesgo: {RISK_PER_TRADE*100}% | Fechas: {START_DATE} a {END_DATE}")
    print("="*80)

    market_data = {}
    strategies = {}
    states = {}
    
    print("🛠️ Preparando indicadores...")
    for symbol, config in PORTFOLIO.items():
        df = get_data(symbol, config['tf'])
        if df.empty: 
            print(f"⚠️ Warning: No data for {symbol} ({config['tf']})")
            continue
            
        strat = BreakoutBotStrategy()
        p = config['params']
        strat.sl_atr = p['sl_atr']; strat.tp_partial_atr = p['tp_partial_atr']
        strat.trailing_dist_atr = p['trailing_dist_atr']; strat.vol_multiplier = p['vol_multiplier']
        
        df_indicators = strat.calculate_indicators(df.copy())
        
        market_data[symbol] = df_indicators
        strategies[symbol] = strat
        states[symbol] = {'status': 'WAITING_BREAKOUT'}

    # Variables de la Billetera
    wallet = INITIAL_CAPITAL
    trades_log = []
    final_stats = []
    
    # Bucle por activo
    for symbol, config in PORTFOLIO.items():
        if symbol not in market_data: continue
        
        df = market_data[symbol]
        strat = strategies[symbol]
        state = states[symbol]
        
        symbol_pnl = 0
        symbol_trades = 0
        
        # Empezamos en 200
        for i in range(200, len(df)):
            current_window = df.iloc[:i+1]
            current_date = current_window.index[-1]
            
            # (No necesitamos current_close aquí, lo saca la estrategia)
            
            signal = strat.get_signal(current_window, state)
            action = signal['action']
            
            # --- EJECUCIÓN CON GESTIÓN DE RIESGO ---
            if action == 'ENTER_LONG':
                entry_price = signal['entry_price']
                sl_price = signal['stop_loss']
                dist_sl = abs(entry_price - sl_price)
                if dist_sl == 0: continue

                # Size based on Risk
                risk_amt = wallet * RISK_PER_TRADE
                position_size_coins = risk_amt / dist_sl
                position_notional = position_size_coins * entry_price
                
                # Safety check: Max 50% de cuenta en un solo trade
                if position_notional > wallet * 0.5:
                     position_size_coins = (wallet * 0.5) / entry_price
                     position_notional = position_size_coins * entry_price

                state.update({
                    'status': 'IN_POSITION',
                    'entry_price': entry_price,
                    'stop_loss': sl_price,
                    'tp_partial': signal['tp_partial'],
                    'size_coins': position_size_coins,
                    'coins_remaining': position_size_coins,
                    # --- FIX: AGREGAMOS LA CLAVE QUE FALTABA ---
                    'position_size_pct': 1.0, 
                    'trailing_active': False,
                    'highest_price_post_tp': 0.0,
                    'atr_at_breakout': signal.get('atr_at_breakout', 0)
                })
                
                fee = position_notional * 0.0006
                wallet -= fee
                symbol_pnl -= fee

            elif action == 'EXIT_PARTIAL':
                exit_price = state['tp_partial']
                coins_to_sell = state['size_coins'] * 0.5
                
                revenue = coins_to_sell * exit_price
                cost_basis = coins_to_sell * state['entry_price']
                profit = revenue - cost_basis
                fee = revenue * 0.0006
                
                wallet += (profit - fee)
                symbol_pnl += (profit - fee)
                symbol_trades += 1
                
                trades_log.append([current_date, symbol, 'TP1', f"${profit:.2f}"])
                
                state.update({
                    'coins_remaining': state['coins_remaining'] - coins_to_sell,
                    'stop_loss': signal['new_sl'],
                    # --- FIX: ACTUALIZAMOS EL PCT PARA QUE LA ESTRATEGIA SEPA QUE ES RESTO ---
                    'position_size_pct': 0.5,
                    'trailing_active': True,
                    'highest_price_post_tp': signal['highest_price_post_tp']
                })

            elif action == 'UPDATE_TRAILING':
                state['stop_loss'] = signal['new_sl']
                state['highest_price_post_tp'] = signal['highest_price_post_tp']

            elif action in ['EXIT_SL', 'EXIT_TRAILING']:
                exit_price = state['stop_loss']
                coins_remaining = state['coins_remaining']
                
                revenue = coins_remaining * exit_price
                cost_basis = coins_remaining * state['entry_price']
                profit = revenue - cost_basis
                fee = revenue * 0.0006
                
                wallet += (profit - fee)
                symbol_pnl += (profit - fee)
                symbol_trades += 1
                
                res_type = 'SL' if action == 'EXIT_SL' else 'TRAIL'
                color = "\033[92m" if profit > 0 else "\033[91m"
                trades_log.append([current_date, symbol, res_type, f"{color}${profit:.2f}\033[0m"])
                
                state = {'status': 'COOLDOWN', 'last_exit_time': str(current_date)}

            elif 'new_status' in signal:
                state['status'] = signal['new_status']
                if 'breakout_level' in signal: state['breakout_level'] = signal['breakout_level']
                if 'atr_at_breakout' in signal: state['atr_at_breakout'] = signal['atr_at_breakout']

        roi_sym = (symbol_pnl / INITIAL_CAPITAL) * 100
        final_stats.append([symbol, config['tf'], symbol_trades, f"${symbol_pnl:.2f}", f"{roi_sym:.2f}%"])

    print("\n📜 ÚLTIMOS 10 TRADES EJECUTADOS:")
    trades_log.sort(key=lambda x: x[0])
    print(tabulate(trades_log[-10:], headers=['Fecha', 'Par', 'Evento', 'PnL'], tablefmt='simple'))

    print("\n📊 RESULTADO POR ACTIVO (Gestión de Riesgo: 3% por trade)")
    print(tabulate(final_stats, headers=['Par', 'TF', '# Ops', 'PnL Neto', 'ROI Contrib'], tablefmt='grid'))
    
    total_profit = wallet - INITIAL_CAPITAL
    total_roi = (total_profit / INITIAL_CAPITAL) * 100
    
    print("\n" + "="*40)
    print(f"💰 CAPITAL FINAL: ${wallet:.2f}")
    print(f"📈 ROI TOTAL (2023-2025): {total_roi:.2f}%")
    print(f"💵 PnL NETO: ${total_profit:.2f}")
    print("="*40)

if __name__ == "__main__":
    run_portfolio()