print("🟢 INICIANDO SIMULACIÓN 'EVENT LOOP REAL' (Native TFs + State Memory)...")

import sys
import os
import pandas as pd
import glob
import numpy as np

PROJECT_ROOT = "/home/orangepi/bot_cpr"
if PROJECT_ROOT not in sys.path: sys.path.append(PROJECT_ROOT)

try:
    from bots.breakout.strategy import BreakoutBotStrategy
    print("✅ Estrategia importada.")
except ImportError as e:
    sys.exit(1)

# --- CONFIGURACIÓN ---
INITIAL_CAPITAL = 5000
MAX_OPEN_POSITIONS = 4 
DATA_DIR = os.path.join(PROJECT_ROOT, 'backtesting', 'data')

# Parámetros optimizados
PORTFOLIO = {
    '1000PEPE/USDT': {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.9}},
    'FET/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 2.0}},
    'WIF/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.6}},
    'DOGE/USDT':     {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.9}},
    # Activos Lentos (Native 4H)
    'SOL/USDT':      {'tf': '4h', 'params': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5}},
    'BTC/USDT':      {'tf': '4h', 'params': {'sl_atr': 1.5, 'tp_partial_atr': 2.0, 'trailing_dist_atr': 1.5, 'vol_multiplier': 1.1}}
}

def clean_columns(df):
    df.columns = [c.strip().capitalize() for c in df.columns]
    rename_map = {'Vol': 'Volume', 'Vol.': 'Volume', 'Op': 'Open', 'Hi': 'High', 'Lo': 'Low', 'Cl': 'Close'}
    df.rename(columns=rename_map, inplace=True)
    return df

def run_debug_sim():
    market_data = {}
    strategies = {}
    
    # 1. CARGA DE DATOS (NATIVA, SIN RESAMPLE)
    for symbol, conf in PORTFOLIO.items():
        safe_symbol = symbol.replace('/', '_')
        pattern = os.path.join(DATA_DIR, f"{safe_symbol}*.csv")
        files = glob.glob(pattern)
        if not files: continue
        target_file = next((f for f in files if "FULL" in f), files[0])
        try:
            df = pd.read_csv(target_file, index_col=0, parse_dates=True)
            df = clean_columns(df)
            
            strat = BreakoutBotStrategy()
            p = conf['params']
            strat.sl_atr = p['sl_atr']; strat.tp_partial_atr = p['tp_partial_atr']
            strat.trailing_dist_atr = p['trailing_dist_atr']; strat.vol_multiplier = p['vol_multiplier']
            
            # Calculamos indicadores en TF nativo
            df = strat.calculate_indicators(df)
            
            # Filtramos fechas
            df = df[(df.index >= '2023-01-01') & (df.index <= '2025-12-31')]
            
            market_data[symbol] = df
            strategies[symbol] = strat
            print(f"✅ {symbol} cargado ({conf['tf']}).")
        except: pass

    if not market_data: return

    # 2. CONSTRUCCIÓN DE TIMELINE MAESTRO
    # Unimos todos los índices. Esto crea una línea de tiempo paso a paso.
    # Si es 13:00, PEPE(1h) tendrá dato, pero SOL(4h) no.
    full_timeline = sorted(list(set().union(*[df.index for df in market_data.values()])))
    
    wallet = INITIAL_CAPITAL
    
    # --- MEMORIA PERSISTENTE DEL BOT ---
    # Aquí guardamos el estado de CADA par. Esto sobrevive al loop.
    # state_key: 'status', 'last_exit_time', etc.
    bot_memory = {sym: {'status': 'WAITING_BREAKOUT', 'last_exit_time': None} for sym in PORTFOLIO}
    
    # Gestión de posiciones vivas
    active_positions = {} 
    trades_history = []
    
    print(f"\n🚀 EJECUTANDO SIMULACIÓN EVENT-DRIVEN ({len(full_timeline)} eventos)...")
    
    for i, current_time in enumerate(full_timeline):
        if i % 10000 == 0: print(f"   ... {int(i/len(full_timeline)*100)}%")

        # A) CHEQUEO DE SALIDAS (Solo para posiciones activas)
        closed_ids = []
        for sym, pos in active_positions.items():
            df = market_data[sym]
            
            # FIX 1: INTEGRIDAD DE TIMEFRAME
            # Si para este simbolo, en este instante de tiempo, no hay vela cerrada -> SALTAR.
            # (Evita el ffill fantasma)
            if current_time not in df.index: continue
            
            curr = df.loc[current_time]
            strat = strategies[sym]
            
            # Recuperamos estado desde la posición activa
            # (Nota: Mientras está activa, el estado vive en 'active_positions')
            st = {
                'status': 'IN_POSITION', 'entry_price': pos['entry'], 'stop_loss': pos['sl'],
                'tp_partial': pos['tp'], 'position_size_pct': pos['size_pct'],
                'trailing_active': pos['trail'], 'highest_price_post_tp': pos['h_post']
            }
            
            idx = df.index.get_loc(current_time)
            window = df.iloc[max(0, idx-60):idx+1]
            signal = strat.get_signal(window, st)
            act = signal['action']

            # --- EJECUCIÓN ---
            if act == 'EXIT_PARTIAL':
                exit_price = pos['tp']
                coins_sold = pos['coins'] * 0.5
                realized = (coins_sold * exit_price) - (coins_sold * pos['entry'])
                
                # Devolución de capital parcial
                capital_released = (pos['risk_blocked'] * 0.5) + realized
                wallet += capital_released
                
                pos['coins'] -= coins_sold
                pos['risk_blocked'] *= 0.5
                pos['size_pct'] = 0.5
                pos['sl'] = signal['new_sl']
                pos['trail'] = True
                pos['h_post'] = signal['highest_price_post_tp']
                active_positions[sym] = pos
                trades_history.append([current_time, sym, 'TP1', realized])

            elif act in ['EXIT_SL', 'EXIT_TRAILING']:
                # PnL Realista
                exit_price = min(curr['Low'], pos['sl'])
                coins_left = pos['coins']
                realized = (coins_left * exit_price) - (coins_left * pos['entry'])
                
                wallet += pos['risk_blocked'] + realized
                closed_ids.append(sym)
                trades_history.append([current_time, sym, act, realized])
                
                # FIX 2: ACTUALIZAR MEMORIA CON COOLDOWN
                # Al cerrar, escribimos en la memoria persistente que acabamos de salir
                bot_memory[sym] = {
                    'status': 'COOLDOWN',
                    'last_exit_time': str(current_time)
                }

            elif act == 'UPDATE_TRAILING':
                pos['sl'] = signal['new_sl']
                pos['h_post'] = signal['highest_price_post_tp']
                active_positions[sym] = pos

        for sym in closed_ids: del active_positions[sym]

        # B) CHEQUEO DE ENTRADAS
        if len(active_positions) >= MAX_OPEN_POSITIONS: continue
        
        for sym in PORTFOLIO.keys():
            if sym in active_positions: continue
            if sym not in market_data: continue
            if len(active_positions) >= MAX_OPEN_POSITIONS: break
            
            df = market_data[sym]
            
            # FIX 1: INTEGRIDAD DE TIMEFRAME
            if current_time not in df.index: continue
            
            idx = df.index.get_loc(current_time)
            if idx < 60: continue
            window = df.iloc[idx-60 : idx+1]
            
            # FIX 2: LEEMOS LA MEMORIA PERSISTENTE
            # Aquí recuperamos si está en COOLDOWN o WAITING
            current_state = bot_memory.get(sym, {'status': 'WAITING_BREAKOUT'})
            
            try:
                signal = strategies[sym].get_signal(window, current_state)
                
                # Actualizamos memoria si hay cambio de estado interno (raro en breakout directo, pero útil)
                if 'new_status' in signal and signal['new_status'] != 'IN_POSITION':
                     # Si la estrategia quisiera cambiar a un estado intermedio (ej: waiting confirmation)
                     # lo guardaríamos aquí.
                     pass 

                if signal['action'] == 'ENTER_LONG':
                    entry = signal['entry_price']
                    sl = signal['stop_loss']
                    dist = abs(entry - sl)
                    
                    if dist > 0:
                        # Riesgo 2% para ser más conservadores tras la racha de pérdidas
                        risk_amount = wallet * 0.02 
                        if risk_amount > wallet: risk_amount = wallet
                        
                        coins = risk_amount / dist
                        notional = coins * entry
                        if notional > wallet * 0.3: # Max 30% por trade
                            coins = (wallet * 0.3) / entry
                            risk_amount = coins * dist
                        
                        wallet -= risk_amount
                        
                        active_positions[sym] = {
                            'entry': entry, 'sl': sl, 'tp': signal['tp_partial'],
                            'coins': coins, 'size_pct': 1.0, 'trail': False, 
                            'h_post': 0.0, 'risk_blocked': risk_amount
                        }
                        # Al entrar, el estado pasa a ser gestionado por 'active_positions'
                        # En bot_memory podemos poner un placeholder
                        bot_memory[sym] = {'status': 'IN_POSITION'}
            except: pass

    roi = ((wallet - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    print("\n" + "="*40)
    print(f"📊 RESULTADO FINAL (NATIVE + STATE MEMORY)")
    print(f"💰 Capital Final: ${wallet:.2f}")
    print(f"📈 ROI Total:     {roi:.2f}%")
    print(f"🔢 Trades:        {len(trades_history)}")
    print("="*40)

if __name__ == "__main__":
    run_debug_sim()