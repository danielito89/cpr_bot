print("🟢 INICIANDO SCRIPT DE DEBUG... (Si lees esto, Python funciona)")

import sys
import os
import pandas as pd
import glob

# --- 1. CONFIGURACIÓN DE RUTAS A FUERZA BRUTA ---
# Asumimos que estás en tu Orange Pi
PROJECT_ROOT = "/home/orangepi/bot_cpr"
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)
    print(f"📂 Ruta añadida al sistema: {PROJECT_ROOT}")

try:
    from bots.breakout.strategy import BreakoutBotStrategy
    print("✅ Estrategia importada correctamente.")
except ImportError as e:
    print(f"❌ ERROR CRÍTICO: No se puede importar la estrategia.\n{e}")
    sys.exit(1)

# --- 2. CONFIGURACIÓN ---
INITIAL_CAPITAL = 5000
MAX_OPEN_POSITIONS = 3
DATA_DIR = os.path.join(PROJECT_ROOT, 'backtesting', 'data')

# Tu Portfolio Gold
PORTFOLIO = {
    '1000PEPE/USDT': {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.9}},
    'FET/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 2.0}},
    'WIF/USDT':      {'tf': '1h', 'params': {'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.6}},
    'DOGE/USDT':     {'tf': '1h', 'params': {'sl_atr': 2.0, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.9}},
    'SOL/USDT':      {'tf': '4h', 'params': {'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5}},
    'BTC/USDT':      {'tf': '4h', 'params': {'sl_atr': 1.5, 'tp_partial_atr': 2.0, 'trailing_dist_atr': 1.5, 'vol_multiplier': 1.1}}
}

def clean_columns(df):
    """Limpia y estandariza las columnas a Capitalizado (Open, High, Low...)"""
    df.columns = [c.strip().capitalize() for c in df.columns]
    # Mapeo forzoso si acaso vienen raros
    rename_map = {
        'Vol': 'Volume', 'Vol.': 'Volume', 
        'Op': 'Open', 'Hi': 'High', 'Lo': 'Low', 'Cl': 'Close'
    }
    df.rename(columns=rename_map, inplace=True)
    return df

def run_debug_sim():
    print(f"\n🔍 BUSCANDO DATOS EN: {DATA_DIR}")
    
    market_data = {}
    strategies = {}
    
    # 1. CARGA DE DATOS
    for symbol, conf in PORTFOLIO.items():
        safe_symbol = symbol.replace('/', '_')
        # Buscar cualquier archivo que coincida
        pattern = os.path.join(DATA_DIR, f"{safe_symbol}*.csv")
        files = glob.glob(pattern)
        
        if not files:
            print(f"⚠️  No hay CSV para {symbol}")
            continue
            
        # Tomamos el primero que encontremos (preferiblemente FULL)
        target_file = files[0]
        for f in files:
            if "FULL" in f: target_file = f
            
        print(f"   --> Cargando {os.path.basename(target_file)} ...", end=" ")
        
        try:
            df = pd.read_csv(target_file, index_col=0, parse_dates=True)
            df = clean_columns(df)
            
            # Chequeo de columnas vitales
            required = ['High', 'Low', 'Close', 'Volume']
            missing = [c for c in required if c not in df.columns]
            if missing:
                print(f"❌ MAL FORMATO. Faltan: {missing}. Columnas actuales: {list(df.columns)}")
                continue

            # Calcular Indicadores
            strat = BreakoutBotStrategy()
            p = conf['params']
            strat.sl_atr = p['sl_atr']
            strat.tp_partial_atr = p['tp_partial_atr']
            strat.trailing_dist_atr = p['trailing_dist_atr']
            strat.vol_multiplier = p['vol_multiplier']
            
            df = strat.calculate_indicators(df)
            
            # Sincronizar a 1H
            df_1h = df.resample('1h').ffill()
            # Filtrar fechas (2023-2025)
            df_1h = df_1h[(df_1h.index >= '2023-01-01') & (df_1h.index <= '2025-12-31')]
            
            market_data[symbol] = df_1h
            strategies[symbol] = strat
            print(f"✅ OK ({len(df_1h)} velas)")
            
        except Exception as e:
            print(f"❌ ERROR LEYENDO: {e}")

    if not market_data:
        print("\n⛔ SE DETIENE LA EJECUCIÓN: No hay datos válidos cargados.")
        return

    # 2. SIMULACIÓN SIMPLE
    print(f"\n🚀 EJECUTANDO SIMULACIÓN DE CUPO (Max {MAX_OPEN_POSITIONS} activos)...")
    
    full_timeline = sorted(list(set().union(*[df.index for df in market_data.values()])))
    wallet = INITIAL_CAPITAL
    active_positions = {} 
    
    trades_count = 0
    rejected_count = 0
    
    # Debug: Imprimir progreso cada 10%
    total_steps = len(full_timeline)
    
    for i, current_time in enumerate(full_timeline):
        
        # SALIDAS
        closed_ids = []
        for sym, pos in active_positions.items():
            df = market_data[sym]
            if current_time not in df.index: continue
            
            strat = strategies[sym]
            # Dummy state
            st = {
                'status': 'IN_POSITION', 'entry_price': pos['entry'], 'stop_loss': pos['sl'],
                'tp_partial': pos['tp'], 'position_size_pct': pos['size_pct'],
                'trailing_active': pos['trail'], 'highest_price_post_tp': pos['h_post']
            }
            
            try:
                # Usar iloc para obtener una fila como DataFrame
                idx = df.index.get_loc(current_time)
                # Truco: slice de 1 elemento mantiene formato DataFrame
                dummy_window = df.iloc[idx:idx+1] 
                
                signal = strat.get_signal(dummy_window, st)
                act = signal['action']
                
                profit = 0
                if act == 'EXIT_PARTIAL':
                    # Venta 50%
                    realized = (pos['coins'] * 0.5 * pos['tp']) - (pos['coins'] * 0.5 * pos['entry'])
                    wallet += realized
                    pos['coins'] *= 0.5
                    pos['size_pct'] = 0.5
                    pos['sl'] = signal['new_sl']
                    pos['trail'] = True
                    pos['h_post'] = signal['highest_price_post_tp']
                    active_positions[sym] = pos
                    
                elif act == 'UPDATE_TRAILING':
                    pos['sl'] = signal['new_sl']
                    pos['h_post'] = signal['highest_price_post_tp']
                    active_positions[sym] = pos
                    
                elif act in ['EXIT_SL', 'EXIT_TRAILING']:
                    realized = (pos['coins'] * pos['sl']) - (pos['coins'] * pos['entry'])
                    wallet += realized
                    closed_ids.append(sym)
                    trades_count += 1
            except Exception as e:
                # print(f"ErrSalida {sym}: {e}")
                pass

        for sym in closed_ids: del active_positions[sym]

        # ENTRADAS
        if len(active_positions) >= MAX_OPEN_POSITIONS:
            # Aquí podríamos contar cuántos rechazamos, pero por rendimiento lo saltamos
            continue
            
        for sym in PORTFOLIO.keys():
            if sym in active_positions: continue
            if sym not in market_data: continue
            if len(active_positions) >= MAX_OPEN_POSITIONS: break
            
            df = market_data[sym]
            if current_time not in df.index: continue
            
            try:
                # Lógica rápida de entrada manual para velocidad
                # Requerimos que la estrategia haya calculado 'Resistance' y 'Volume_OK'
                # Si strategy.py no guarda esas columnas, usamos get_signal
                
                # Usamos get_signal con ventana mínima
                idx = df.index.get_loc(current_time)
                if idx < 50: continue
                window = df.iloc[idx-50:idx+1]
                
                st_dummy = {'status': 'WAITING_BREAKOUT'}
                sig = strategies[sym].get_signal(window, st_dummy)
                
                if sig['action'] == 'ENTER_LONG':
                    entry = sig['entry_price']
                    sl = sig['stop_loss']
                    dist = abs(entry - sl)
                    if dist == 0: continue
                    
                    risk = wallet * 0.03
                    coins = risk / dist
                    notional = coins * entry
                    if notional > wallet * 0.4: coins = (wallet * 0.4) / entry
                    
                    active_positions[sym] = {
                        'entry': entry, 'sl': sl, 'tp': sig['tp_partial'],
                        'coins': coins, 'size_pct': 1.0, 'trail': False, 'h_post': 0.0
                    }
            except: pass

    # --- RESULTADOS ---
    roi = ((wallet - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    print("\n" + "="*40)
    print(f"📊 RESULTADO FINAL (DEBUG SIM)")
    print(f"💰 Capital Inicial: ${INITIAL_CAPITAL}")
    print(f"💰 Capital Final:   ${wallet:.2f}")
    print(f"📈 ROI Total:       {roi:.2f}%")
    print(f"🔢 Trades Cerrados: {trades_count}")
    print("="*40)

if __name__ == "__main__":
    run_debug_sim()