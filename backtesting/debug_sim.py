print("🟢 INICIANDO SIMULACIÓN PULLBACK... (Con Memoria de Estado)")

import sys
import os
import pandas as pd
import glob

PROJECT_ROOT = "/home/orangepi/bot_cpr"
if PROJECT_ROOT not in sys.path: sys.path.append(PROJECT_ROOT)

try:
    from bots.breakout.strategy import BreakoutBotStrategy
    print("✅ Estrategia importada.")
except ImportError as e:
    sys.exit(1)

# --- CONFIGURACIÓN ---
INITIAL_CAPITAL = 5000
MAX_OPEN_POSITIONS = 3 
DATA_DIR = os.path.join(PROJECT_ROOT, 'backtesting', 'data')

# Mismos parámetros ajustados
PORTFOLIO = {
    '1000PEPE/USDT': {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.8}},
    'FET/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 2.0}},
    'WIF/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.6}},
    'DOGE/USDT':     {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.8}},
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
    
    # 1. CARGA DE DATOS
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
            
            df = strat.calculate_indicators(df)
            df_1h = df.resample('1h').ffill()
            df_1h = df_1h[(df_1h.index >= '2023-01-01') & (df_1h.index <= '2025-12-31')]
            market_data[symbol] = df_1h
            strategies[symbol] = strat
            print(f"✅ {symbol} cargado.")
        except: pass

    if not market_data: return

    # 2. SIMULACIÓN
    full_timeline = sorted(list(set().union(*[df.index for df in market_data.values()])))
    wallet = INITIAL_CAPITAL
    active_positions = {} 
    
    # --- MEMORIA DEL SIMULADOR (NUEVO) ---
    # Aquí guardamos el estado de las monedas que NO están en posición pero están esperando (Pullback/Cooldown)
    pending_states = {sym: {'status': 'WAITING_BREAKOUT'} for sym in PORTFOLIO.keys()}
    
    trades_history = []
    
    print(f"\n🚀 EJECUTANDO SIMULACIÓN PULLBACK ({len(full_timeline)} pasos)...")
    
    for i, current_time in enumerate(full_timeline):
        if i % 5000 == 0: print(f"   ... {int(i/len(full_timeline)*100)}%")

        # A) GESTIÓN DE POSICIONES ABIERTAS (Salidas)
        closed_ids = []
        for sym, pos in active_positions.items():
            df = market_data[sym]
            if current_time not in df.index: continue
            
            strat = strategies[sym]
            # Reconstruimos el estado completo para la estrategia
            st = {
                'status': 'IN_POSITION', 
                'entry_price': pos['entry'], 'stop_loss': pos['sl'], 'tp_partial': pos['tp'], 
                'position_size_pct': pos['size_pct'], 'trailing_active': pos['trail'], 'highest_price_post_tp': pos['h_post']
            }
            
            idx = df.index.get_loc(current_time)
            if idx < 300: continue
            window = df.iloc[idx-300 : idx+1] # Ventana grande
            
            try:
                signal = strat.get_signal(window, st)
                act = signal['action']
                
                if act == 'EXIT_PARTIAL':
                    realized = (pos['coins'] * 0.5 * pos['tp']) - (pos['coins'] * 0.5 * pos['entry'])
                    wallet += realized
                    pos['coins'] *= 0.5
                    pos['size_pct'] = 0.5
                    pos['sl'] = signal['new_sl']
                    pos['trail'] = True
                    pos['h_post'] = signal['highest_price_post_tp']
                    active_positions[sym] = pos
                    trades_history.append([current_time, sym, 'TP1', realized])
                    
                elif act == 'UPDATE_TRAILING':
                    pos['sl'] = signal['new_sl']
                    pos['h_post'] = signal['highest_price_post_tp']
                    active_positions[sym] = pos
                    
                elif act in ['EXIT_SL', 'EXIT_TRAILING']:
                    realized = (pos['coins'] * pos['sl']) - (pos['coins'] * pos['entry'])
                    wallet += realized
                    closed_ids.append(sym)
                    trades_history.append([current_time, sym, act, realized])
                    # Al cerrar, pasamos a estado COOLDOWN en la memoria pendiente
                    pending_states[sym] = {'status': 'COOLDOWN', 'last_exit_time': str(current_time)}

            except: pass

        for sym in closed_ids: del active_positions[sym]

        # B) BUSQUEDA DE ENTRADAS (Gestión de Estados Pendientes)
        # Iteramos TODOS los pares, incluso si no tenemos cupo, para actualizar sus estados (ej: Breakout -> Pullback)
        # Pero solo ejecutamos la compra si hay cupo.
        
        for sym in PORTFOLIO.keys():
            if sym in active_positions: continue # Ya está dentro, ignorar
            if sym not in market_data: continue
            
            df = market_data[sym]
            if current_time not in df.index: continue
            
            try:
                idx = df.index.get_loc(current_time)
                if idx < 300: continue
                window = df.iloc[idx-300 : idx+1]
                
                # --- RECUPERAMOS EL ESTADO DE LA MEMORIA ---
                current_st = pending_states.get(sym, {'status': 'WAITING_BREAKOUT'})
                
                signal = strategies[sym].get_signal(window, current_st)
                
                # --- ACTUALIZACIÓN DE ESTADO (CRÍTICO) ---
                if 'new_status' in signal:
                    # Actualizamos la memoria para la siguiente vela
                    new_st_data = current_st.copy()
                    new_st_data['status'] = signal['new_status']
                    
                    # Guardamos datos extra si la estrategia los manda (breakout_level, etc)
                    if 'breakout_level' in signal: new_st_data['breakout_level'] = signal['breakout_level']
                    if 'atr_at_breakout' in signal: new_st_data['atr_at_breakout'] = signal['atr_at_breakout']
                    
                    pending_states[sym] = new_st_data
                
                # --- EJECUCIÓN DE ENTRADA ---
                if signal['action'] == 'ENTER_LONG':
                    # Aquí aplicamos el filtro de cupos
                    if len(active_positions) < MAX_OPEN_POSITIONS:
                        entry = signal['entry_price']
                        sl = signal['stop_loss']
                        dist = abs(entry - sl)
                        if dist > 0:
                            risk = wallet * 0.03
                            coins = risk / dist
                            notional = coins * entry
                            if notional > wallet * 0.4: coins = (wallet * 0.4) / entry
                            
                            active_positions[sym] = {
                                'entry': entry, 'sl': sl, 'tp': signal['tp_partial'],
                                'coins': coins, 'size_pct': 1.0, 'trail': False, 'h_post': 0.0
                            }
                            # Al entrar, reseteamos el estado pendiente para el futuro
                            pending_states[sym] = {'status': 'IN_POSITION'} # Placeholder
                    else:
                        # Si no hay cupo, técnicamente perdimos el trade. 
                        # Podríamos mantenerlo en WAITING_PULLBACK o resetearlo. 
                        # Por simplicidad, dejamos que la estrategia decida en la prox vela (o cancele por FOMO)
                        pass
                
                elif signal['action'] == 'RESET_STATE':
                     pending_states[sym] = {'status': 'WAITING_BREAKOUT'}

            except: pass

    roi = ((wallet - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    print("\n" + "="*40)
    print(f"📊 RESULTADO FINAL (Estrategia PULLBACK)")
    print(f"💰 Capital Final: ${wallet:.2f}")
    print(f"📈 ROI Total:     {roi:.2f}%")
    print(f"🔢 Trades:        {len(trades_history)}")
    print("="*40)

if __name__ == "__main__":
    run_debug_sim()