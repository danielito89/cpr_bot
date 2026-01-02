print("🟢 INICIANDO SIMULACIÓN AGRESIVA (DIRECT BREAKOUT)...")

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
MAX_OPEN_POSITIONS = 4  # Damos más espacio para que entren los memes
DATA_DIR = os.path.join(PROJECT_ROOT, 'backtesting', 'data')

# --- PARÁMETROS AGRESIVOS (Vol 1.5 standard) ---
PORTFOLIO = {
    '1000PEPE/USDT': {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.5}},
    'FET/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 1.5}},
    'WIF/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.5}},
    'DOGE/USDT':     {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5}},
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
    trades_history = []
    
    print(f"\n🚀 EJECUTANDO SIMULACIÓN AGRESIVA ({len(full_timeline)} pasos)...")
    
    for i, current_time in enumerate(full_timeline):
        if i % 10000 == 0: print(f"   ... {int(i/len(full_timeline)*100)}%")

        # A) SALIDAS
        closed_ids = []
        for sym, pos in active_positions.items():
            df = market_data[sym]
            if current_time not in df.index: continue
            
            strat = strategies[sym]
            st = {
                'status': 'IN_POSITION', 
                'entry_price': pos['entry'], 'stop_loss': pos['sl'], 'tp_partial': pos['tp'], 
                'position_size_pct': pos['size_pct'], 'trailing_active': pos['trail'], 'highest_price_post_tp': pos['h_post']
            }
            
            idx = df.index.get_loc(current_time)
            # Solo necesitamos la última vela para salir
            window = df.iloc[max(0, idx-50) : idx+1] 
            
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
            except: pass

        for sym in closed_ids: del active_positions[sym]

        # B) ENTRADAS (Directas)
        if len(active_positions) >= MAX_OPEN_POSITIONS: continue
            
        for sym in PORTFOLIO.keys():
            if sym in active_positions: continue
            if sym not in market_data: continue
            if len(active_positions) >= MAX_OPEN_POSITIONS: break
            
            df = market_data[sym]
            if current_time not in df.index: continue
            
            try:
                idx = df.index.get_loc(current_time)
                if idx < 50: continue
                window = df.iloc[idx-50 : idx+1]
                
                st_dummy = {'status': 'WAITING_BREAKOUT'} # Siempre reseteamos a Waiting porque es Directo
                
                signal = strategies[sym].get_signal(window, st_dummy)
                
                if signal['action'] == 'ENTER_LONG':
                    entry = signal['entry_price']
                    sl = signal['stop_loss']
                    dist = abs(entry - sl)
                    if dist > 0:
                        # Gestión de riesgo simple: 3% riesgo fijo
                        risk = wallet * 0.03
                        coins = risk / dist
                        notional = coins * entry
                        if notional > wallet * 0.4: coins = (wallet * 0.4) / entry
                        
                        active_positions[sym] = {
                            'entry': entry, 'sl': sl, 'tp': signal['tp_partial'],
                            'coins': coins, 'size_pct': 1.0, 'trail': False, 'h_post': 0.0
                        }
            except: pass

    roi = ((wallet - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    print("\n" + "="*40)
    print(f"📊 RESULTADO FINAL (Directo + Agresivo)")
    print(f"💰 Capital Final: ${wallet:.2f}")
    print(f"📈 ROI Total:     {roi:.2f}%")
    print(f"🔢 Trades:        {len(trades_history)}")
    print("="*40)

if __name__ == "__main__":
    run_debug_sim()