print("🟢 INICIANDO SCRIPT DE DEBUG V2... (Corregido Window Size)")

import sys
import os
import pandas as pd
import glob

# --- 1. CONFIGURACIÓN DE RUTAS ---
PROJECT_ROOT = "/home/orangepi/bot_cpr"
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

try:
    from bots.breakout.strategy import BreakoutBotStrategy
    print("✅ Estrategia importada.")
except ImportError as e:
    print(f"❌ ERROR: No se puede importar la estrategia.\n{e}")
    sys.exit(1)

# --- 2. CONFIGURACIÓN ---
INITIAL_CAPITAL = 5000
MAX_OPEN_POSITIONS = 3 # Límite de cupos
DATA_DIR = os.path.join(PROJECT_ROOT, 'backtesting', 'data')

# Portfolio Gold (Con PEPE y DOGE ajustados a 1.8)
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
    print(f"\n🔍 BUSCANDO DATOS EN: {DATA_DIR}")
    
    market_data = {}
    strategies = {}
    
    # 1. CARGA DE DATOS
    for symbol, conf in PORTFOLIO.items():
        safe_symbol = symbol.replace('/', '_')
        pattern = os.path.join(DATA_DIR, f"{safe_symbol}*.csv")
        files = glob.glob(pattern)
        
        if not files:
            print(f"⚠️  No hay CSV para {symbol}")
            continue
            
        target_file = files[0]
        for f in files:
            if "FULL" in f: target_file = f
            
        print(f"   --> {symbol}:", end=" ")
        
        try:
            df = pd.read_csv(target_file, index_col=0, parse_dates=True)
            df = clean_columns(df)
            
            strat = BreakoutBotStrategy()
            p = conf['params']
            strat.sl_atr = p['sl_atr']
            strat.tp_partial_atr = p['tp_partial_atr']
            strat.trailing_dist_atr = p['trailing_dist_atr']
            strat.vol_multiplier = p['vol_multiplier']
            
            # Calculamos indicadores globales
            df = strat.calculate_indicators(df)
            
            # Sincronizar a 1H y filtrar fechas
            df_1h = df.resample('1h').ffill()
            df_1h = df_1h[(df_1h.index >= '2023-01-01') & (df_1h.index <= '2025-12-31')]
            
            market_data[symbol] = df_1h
            strategies[symbol] = strat
            print(f"✅ Cargado ({len(df_1h)} velas)")
            
        except Exception as e:
            print(f"❌ ERROR: {e}")

    if not market_data: return

    # 2. SIMULACIÓN
    print(f"\n🚀 EJECUTANDO SIMULACIÓN (Window Fix: 300 velas)...")
    
    full_timeline = sorted(list(set().union(*[df.index for df in market_data.values()])))
    wallet = INITIAL_CAPITAL
    active_positions = {} 
    
    trades_count = 0
    trades_history = []
    
    # Barra de progreso simple
    total_steps = len(full_timeline)
    
    for i, current_time in enumerate(full_timeline):
        if i % 5000 == 0: print(f"   ... Progreso: {int(i/total_steps*100)}%")

        # --- A) SALIDAS ---
        closed_ids = []
        for sym, pos in active_positions.items():
            df = market_data[sym]
            if current_time not in df.index: continue
            
            strat = strategies[sym]
            st = {
                'status': 'IN_POSITION', 'entry_price': pos['entry'], 'stop_loss': pos['sl'],
                'tp_partial': pos['tp'], 'position_size_pct': pos['size_pct'],
                'trailing_active': pos['trail'], 'highest_price_post_tp': pos['h_post']
            }
            
            # --- FIX: Ventana de salida también grande por si acaso ---
            idx = df.index.get_loc(current_time)
            # Pasamos 300 velas hacia atrás, aunque para salida solo mira la última.
            # Esto evita el rechazo por len(window) < 200
            start_idx = max(0, idx - 300)
            window = df.iloc[start_idx : idx+1]
            
            try:
                signal = strat.get_signal(window, st)
                act = signal['action']
                
                profit = 0
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
                    trades_count += 1
                    trades_history.append([current_time, sym, act, realized])

            except Exception as e: pass

        for sym in closed_ids: del active_positions[sym]

        # --- B) ENTRADAS ---
        # Si la cartera está llena, no buscamos entradas
        if len(active_positions) >= MAX_OPEN_POSITIONS: continue
            
        for sym in PORTFOLIO.keys():
            if sym in active_positions: continue
            if sym not in market_data: continue
            if len(active_positions) >= MAX_OPEN_POSITIONS: break
            
            df = market_data[sym]
            if current_time not in df.index: continue
            
            try:
                idx = df.index.get_loc(current_time)
                # --- FIX CRÍTICO: VENTANA DE 300 VELAS ---
                # Antes era 50, y la estrategia pide min 200.
                if idx < 300: continue 
                window = df.iloc[idx-300 : idx+1]
                
                st_dummy = {'status': 'WAITING_BREAKOUT'}
                sig = strategies[sym].get_signal(window, st_dummy)
                
                if sig['action'] == 'ENTER_LONG':
                    entry = sig['entry_price']
                    sl = sig['stop_loss']
                    dist = abs(entry - sl)
                    if dist == 0: continue
                    
                    risk = wallet * 0.03 # 3% riesgo
                    coins = risk / dist
                    notional = coins * entry
                    if notional > wallet * 0.4: coins = (wallet * 0.4) / entry
                    
                    active_positions[sym] = {
                        'entry': entry, 'sl': sl, 'tp': sig['tp_partial'],
                        'coins': coins, 'size_pct': 1.0, 'trail': False, 'h_post': 0.0
                    }
                    # print(f"   --> BUY {sym} @ {entry}") # Debug opcional
            except: pass

    # --- RESULTADOS ---
    roi = ((wallet - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    
    print("\n" + "="*40)
    print(f"📊 RESULTADO FINAL (REALISTA)")
    print(f"💰 Capital Inicial: ${INITIAL_CAPITAL}")
    print(f"💰 Capital Final:   ${wallet:.2f}")
    print(f"📈 ROI Total:       {roi:.2f}%")
    print(f"🔢 Trades Cerrados: {trades_count}")
    
    if trades_history:
        print("\n📜 Últimos 5 Trades:")
        for t in trades_history[-5:]:
            print(f"   {t[0]} | {t[1]} | {t[2]} | ${t[3]:.2f}")
    print("="*40)

if __name__ == "__main__":
    run_debug_sim()