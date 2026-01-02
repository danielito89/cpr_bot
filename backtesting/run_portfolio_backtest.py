import sys
import os
import pandas as pd
import numpy as np
from tabulate import tabulate

# --- IMPORTACIÓN DE ESTRATEGIA ---
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from bots.breakout.strategy import BreakoutBotStrategy

# --- CONFIGURACIÓN REALISTA ---
INITIAL_CAPITAL = 5000
RISK_PER_TRADE = 0.03
MAX_OPEN_POSITIONS = 4  # <--- EL FILTRO DE LA VERDAD
START_DATE = "2023-01-01"
END_DATE = "2025-12-31"
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')

# --- TU CONFIGURACIÓN GOLD ---
PORTFOLIO = {
    '1000PEPE/USDT': {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.9}},
    'FET/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 2.0}},
    'WIF/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.6}},
    'DOGE/USDT':     {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.9}},
    'SOL/USDT':      {'tf': '4h', 'params': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5}},
    'BTC/USDT':      {'tf': '4h', 'params': {'sl_atr': 1.5, 'tp_partial_atr': 2.0, 'trailing_dist_atr': 1.5, 'vol_multiplier': 1.1}}
}

def load_and_prep_data():
    """Carga y sincroniza todos los datos en una línea de tiempo horaria"""
    market_data = {}
    strategies = {}
    
    print("🛠️  Cargando datos y calculando indicadores...")
    
    for symbol, conf in PORTFOLIO.items():
        # Cargar CSV
        safe_symbol = symbol.replace('/', '_')
        path = os.path.join(DATA_DIR, f"{safe_symbol}_{conf['tf']}_FULL.csv")
        if not os.path.exists(path): path = os.path.join(DATA_DIR, f"{safe_symbol}_{conf['tf']}.csv")
        
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.columns = [c.capitalize() for c in df.columns] # High, Low...
        df = df.sort_index()
        
        # Calcular indicadores en su TF nativo
        strat = BreakoutBotStrategy()
        p = conf['params']
        strat.sl_atr = p['sl_atr']; strat.tp_partial_atr = p['tp_partial_atr']
        strat.trailing_dist_atr = p['trailing_dist_atr']; strat.vol_multiplier = p['vol_multiplier']
        
        df = strat.calculate_indicators(df)
        
        # TRUCO: Resamplear a 1H para sincronizar el bucle cronológico
        # Si es 4H, repetimos la fila 4 veces (ffill) para que el simulador horario pueda "ver" el estado
        df_1h = df.resample('1h').ffill()
        
        # Recortar fechas
        df_1h = df_1h[(df_1h.index >= pd.to_datetime(START_DATE)) & (df_1h.index <= pd.to_datetime(END_DATE))]
        
        market_data[symbol] = df_1h
        strategies[symbol] = strat

    return market_data, strategies

def run_realistic_sim():
    market_data, strategies = load_and_prep_data()
    
    # Crear línea de tiempo maestra (unión de todos los índices)
    full_timeline = sorted(list(set().union(*[df.index for df in market_data.values()])))
    
    wallet = INITIAL_CAPITAL
    equity_curve = []
    
    # Estado de cartera
    active_positions = {} # {symbol: {data_dict}}
    rejected_trades = 0
    trades_log = []
    
    print(f"🚀 INICIANDO SIMULACIÓN CRONOLÓGICA (Strict Mode)")
    print(f"🔒 Límite de Cupos: {MAX_OPEN_POSITIONS} activos simultáneos")
    print("="*60)

    # --- BUCLE CRONOLÓGICO (HORA A HORA) ---
    for current_time in full_timeline:
        
        # 1. GESTIONAR POSICIONES ABIERTAS (Check Exit)
        symbols_to_remove = []
        
        for symbol in list(active_positions.keys()):
            pos = active_positions[symbol]
            df = market_data[symbol]
            
            # Verificar si tenemos datos para esta hora
            if current_time not in df.index: continue
            
            candle = df.loc[current_time]
            strat = strategies[symbol]
            
            # Reconstruir estado para la estrategia
            state_for_strat = {
                'status': 'IN_POSITION',
                'entry_price': pos['entry_price'],
                'stop_loss': pos['stop_loss'],
                'tp_partial': pos['tp_partial'],
                'position_size_pct': pos['position_size_pct'],
                'trailing_active': pos['trailing_active'],
                'highest_price_post_tp': pos['highest_price_post_tp']
            }
            
            # Simular paso de estrategia (Check SL/TP)
            # Creamos una ventana dummy solo con la vela actual para chequear salidas
            # (En realidad la estrategia mira high/low de la vela actual)
            # Hack: pasamos un DF de 1 fila
            dummy_window = df.loc[[current_time]]
            
            signal = strat.get_signal(dummy_window, state_for_strat)
            action = signal['action']
            
            profit = 0
            closed = False
            
            if action == 'EXIT_PARTIAL':
                # Venta del 50%
                coins_sold = pos['coins'] * 0.5
                revenue = coins_sold * pos['tp_partial']
                cost = coins_sold * pos['entry_price']
                profit = revenue - cost - (revenue * 0.0006)
                
                pos['coins'] -= coins_sold
                pos['position_size_pct'] = 0.5
                pos['stop_loss'] = signal['new_sl']
                pos['trailing_active'] = True
                pos['highest_price_post_tp'] = signal['highest_price_post_tp']
                
                wallet += profit
                active_positions[symbol] = pos # Actualizar
                trades_log.append([current_time, symbol, "TP1", profit])

            elif action == 'UPDATE_TRAILING':
                pos['stop_loss'] = signal['new_sl']
                pos['highest_price_post_tp'] = signal['highest_price_post_tp']
                active_positions[symbol] = pos

            elif action in ['EXIT_SL', 'EXIT_TRAILING']:
                # Venta del resto
                revenue = pos['coins'] * pos['stop_loss']
                cost = pos['coins'] * pos['entry_price']
                profit = revenue - cost - (revenue * 0.0006)
                
                wallet += profit
                trades_log.append([current_time, symbol, action, profit])
                symbols_to_remove.append(symbol)

        # Limpiar cerradas
        for s in symbols_to_remove:
            del active_positions[s]

        # 2. BUSCAR NUEVAS ENTRADAS (Solo si hay cupo)
        # Randomizamos el orden de chequeo para no favorecer siempre alfabéticamente a '1000PEPE'
        # aunque en 1H la diferencia es mínima.
        check_order = list(PORTFOLIO.keys())
        # np.random.shuffle(check_order) # Opcional: realismo puro
        
        for symbol in check_order:
            if symbol in active_positions: continue # Ya tengo este
            
            # EL FILTRO DE LA VERDAD:
            if len(active_positions) >= MAX_OPEN_POSITIONS:
                # Si hubiera señal aquí, sería rechazada.
                # Para saber si hubo señal rechazada tendríamos que correr la lógica,
                # pero por eficiencia, simplemente no analizamos.
                continue

            df = market_data[symbol]
            if current_time not in df.index: continue
            
            # Necesitamos ventana de contexto para calcular señal de entrada
            # (La estrategia necesita las velas anteriores para ver el breakout)
            # Esto es lento en bucle, así que usamos un truco:
            # Ya pre-calculamos indicadores. Solo miramos la columna 'breakout_logic' si la tuvieramos,
            # o corremos get_signal rapido.
            
            # Optimizacion: Mirar si Close > Resistance (precalculado)
            # Pero strategy.py lo hace interno.
            # Cortamos ventana hasta current_time
            # Para no matar la CPU, asumimos que indicators ya tiene todo.
            # Solo necesitamos pasarle la fila actual y saber si Close > Resistance[1]
            
            row = df.loc[current_time]
            
            # Lógica manual rápida para filtrar (Replica BreakoutBotStrategy)
            # Resistance está en la columna 'Resistance' (si la guardamos en prep)
            # Si no, re-ejecutamos get_signal (más lento pero seguro)
            
            # Para el backtest realista vamos a confiar en get_signal con ventana mínima
            # El df ya tiene indicadores.
            
            # Tomamos las ultimas 2 filas (suficiente para ver cruce o estado)
            # loc slice es inclusivo
            try:
                # Necesitamos iloc para cortar hacia atras
                idx_loc = df.index.get_loc(current_time)
                if idx_loc < 50: continue 
                window = df.iloc[idx_loc-50 : idx_loc+1]
                
                state_dummy = {'status': 'WAITING_BREAKOUT'}
                signal = strategies[symbol].get_signal(window, state_dummy)
                
                if signal['action'] == 'ENTER_LONG':
                    # --- ENTRADA CONFIRMADA ---
                    entry_price = signal['entry_price']
                    sl = signal['stop_loss']
                    dist = abs(entry_price - sl)
                    if dist == 0: continue
                    
                    risk_amt = wallet * RISK_PER_TRADE
                    size_coins = risk_amt / dist
                    notional = size_coins * entry_price
                    
                    # Cap de seguridad 40% cuenta
                    if notional > wallet * 0.4:
                        size_coins = (wallet * 0.4) / entry_price
                    
                    # Ejecutar
                    fee = (size_coins * entry_price) * 0.0006
                    wallet -= fee
                    
                    active_positions[symbol] = {
                        'entry_price': entry_price,
                        'stop_loss': sl,
                        'tp_partial': signal['tp_partial'],
                        'coins': size_coins,
                        'position_size_pct': 1.0,
                        'trailing_active': False,
                        'highest_price_post_tp': 0.0
                    }
            except Exception as e:
                pass

    # --- REPORTE FINAL ---
    print("\n📜 ÚLTIMOS TRADES:")
    print(tabulate(trades_log[-5:], headers=['Fecha', 'Par', 'Evento', 'PnL']))
    
    total_profit = wallet - INITIAL_CAPITAL
    roi = (total_profit / INITIAL_CAPITAL) * 100
    
    print("\n" + "="*40)
    print(f"💰 CAPITAL FINAL (Realista): ${wallet:.2f}")
    print(f"📉 ROI AJUSTADO (Con Cupos): {roi:.2f}%")
    print(f"🛑 Trades Rechazados por Cupo: (Implícito en menor ROI)")
    print("="*40)

if __name__ == "__main__":
    run_realistic_sim()