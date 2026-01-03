print("🟢 INICIANDO SIMULACIÓN 'PLAN DE RESCATE' (Fix PnL + Wallet + Slope)...")

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
MAX_OPEN_POSITIONS = 4 
DATA_DIR = os.path.join(PROJECT_ROOT, 'backtesting', 'data')

# Parámetros optimizados (Volvemos a tu lista ganadora)
PORTFOLIO = {
    '1000PEPE/USDT': {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.9}},
    'FET/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 2.0}},
    'WIF/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.6}},
    'DOGE/USDT':     {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.9}},
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

    # 2. SIMULACIÓN REALISTA
    full_timeline = sorted(list(set().union(*[df.index for df in market_data.values()])))
    
    # FIX 2: Wallet gestiona el capital DISPONIBLE
    wallet = INITIAL_CAPITAL 
    
    active_positions = {} 
    trades_history = []
    
    print(f"\n🚀 EJECUTANDO SIMULACIÓN CON FIXES ({len(full_timeline)} pasos)...")
    
    for i, current_time in enumerate(full_timeline):
        if i % 10000 == 0: print(f"   ... {int(i/len(full_timeline)*100)}%")

        # A) SALIDAS
        closed_ids = []
        for sym, pos in active_positions.items():
            df = market_data[sym]
            if current_time not in df.index: continue
            
            curr = df.loc[current_time] # Usamos loc para acceder rápido a High/Low
            strat = strategies[sym]
            
            st = {
                'status': 'IN_POSITION', 'entry_price': pos['entry'], 'stop_loss': pos['sl'],
                'tp_partial': pos['tp'], 'position_size_pct': pos['size_pct'],
                'trailing_active': pos['trail'], 'highest_price_post_tp': pos['h_post']
            }
            
            # Recreamos la señal pasando una ventana mínima
            idx = df.index.get_loc(current_time)
            window = df.iloc[max(0, idx-60):idx+1]
            signal = strat.get_signal(window, st)
            act = signal['action']

            # --- EJECUCIÓN DE SALIDA (PnL REALISTA) ---
            if act == 'EXIT_PARTIAL':
                # TP suele ejecutarse bien (Limit), asumimos precio TP
                exit_price = pos['tp'] 
                
                # Calculamos profit de la mitad vendida
                coins_sold = pos['coins'] * 0.5
                revenue = coins_sold * exit_price
                cost_basis = coins_sold * pos['entry']
                realized = revenue - cost_basis
                
                # FIX 2: Devolvemos capital al wallet (costo + ganancia)
                # Aquí simplificamos: devolvemos lo "arriesgado" proporcionalmente + profit
                capital_released = (pos['risk_blocked'] * 0.5) + realized
                wallet += capital_released
                
                # Actualizamos posición
                pos['coins'] -= coins_sold
                pos['risk_blocked'] *= 0.5 # Liberamos mitad del riesgo bloqueado
                pos['size_pct'] = 0.5
                pos['sl'] = signal['new_sl']
                pos['trail'] = True
                pos['h_post'] = signal['highest_price_post_tp']
                active_positions[sym] = pos
                trades_history.append([current_time, sym, 'TP1', realized])

            elif act in ['EXIT_SL', 'EXIT_TRAILING']:
                # FIX 1: PEOR ESCENARIO EN SLIPPAGE
                # Si el Low de la vela perforó el SL, asumimos que salimos en el Low (peor caso)
                # O en el SL, lo que sea PEOR (más bajo).
                exit_price = min(curr['Low'], pos['sl'])
                
                coins_left = pos['coins']
                revenue = coins_left * exit_price
                cost_basis = coins_left * pos['entry']
                realized = revenue - cost_basis
                
                # Devolvemos lo que queda del riesgo bloqueado + profit (que será negativo)
                wallet += pos['risk_blocked'] + realized
                
                closed_ids.append(sym)
                trades_history.append([current_time, sym, act, realized])
                
            elif act == 'UPDATE_TRAILING':
                pos['sl'] = signal['new_sl']
                pos['h_post'] = signal['highest_price_post_tp']
                active_positions[sym] = pos

        for sym in closed_ids: del active_positions[sym]

        # B) ENTRADAS
        if len(active_positions) >= MAX_OPEN_POSITIONS: continue
        
        # Sorteo simple para no privilegiar siempre al primero de la lista
        # check_list = list(PORTFOLIO.keys()); np.random.shuffle(check_list)
        
        for sym in PORTFOLIO.keys():
            if sym in active_positions: continue
            if sym not in market_data: continue
            if len(active_positions) >= MAX_OPEN_POSITIONS: break
            
            df = market_data[sym]
            if current_time not in df.index: continue
            
            idx = df.index.get_loc(current_time)
            if idx < 60: continue
            window = df.iloc[idx-60 : idx+1]
            
            st_dummy = {'status': 'WAITING_BREAKOUT', 'last_exit_time': str(current_time - pd.Timedelta(hours=20))} 
            # Truco: last_exit viejo para primer trade
            
            try:
                # Recuperar si hubo salida reciente real
                # (Aquí simplificamos, el cooldown interno de strategy se encarga si pasamos el estado correcto
                # pero como es debug_sim sin memoria compleja, confiamos en que el volumen filtra)
                
                signal = strategies[sym].get_signal(window, st_dummy)
                
                if signal['action'] == 'ENTER_LONG':
                    entry = signal['entry_price']
                    sl = signal['stop_loss']
                    dist = abs(entry - sl)
                    
                    if dist > 0:
                        # FIX 2: Gestión de Riesgo sobre Wallet DISPONIBLE (o Total, pero bloqueando)
                        # Usamos 3% del wallet actual como riesgo
                        risk_amount = wallet * 0.03
                        
                        # Si no hay suficiente dinero en wallet para cubrir el riesgo (muy raro), ajustamos
                        if risk_amount > wallet: risk_amount = wallet

                        coins = risk_amount / dist
                        notional = coins * entry
                        
                        # Cap de seguridad: No meter más del 40% del wallet en una sola
                        if notional > wallet * 0.4:
                            coins = (wallet * 0.4) / entry
                            risk_amount = coins * dist # Recalculamos riesgo real
                        
                        # BLOQUEAMOS CAPITAL
                        wallet -= risk_amount
                        
                        active_positions[sym] = {
                            'entry': entry, 'sl': sl, 'tp': signal['tp_partial'],
                            'coins': coins, 'size_pct': 1.0, 'trail': False, 
                            'h_post': 0.0,
                            'risk_blocked': risk_amount # Guardamos cuánto bloqueamos para devolverlo luego
                        }
            except: pass

    roi = ((wallet - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    print("\n" + "="*40)
    print(f"📊 RESULTADO FINAL (PLAN DE RESCATE)")
    print(f"💰 Capital Final: ${wallet:.2f}")
    print(f"📈 ROI Total:     {roi:.2f}%")
    print(f"🔢 Trades:        {len(trades_history)}")
    print("="*40)

if __name__ == "__main__":
    run_debug_sim()