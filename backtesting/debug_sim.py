print("🟢 INICIANDO SIMULACIÓN 'FRANCOTIRADOR' (Score > 25 + Cluster Control)...")

import sys
import os
import pandas as pd
import glob
import numpy as np # Importante

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
RISK_PER_TRADE = 0.01 
DATA_DIR = os.path.join(PROJECT_ROOT, 'backtesting', 'data')

# --- CLUSTERS ---
CATEGORIES = {
    'SOL/USDT': 'L1', 'INJ/USDT': 'L1', 'NEAR/USDT': 'L1', 'SUI/USDT': 'L1', 'APT/USDT': 'L1',
    'FET/USDT': 'AI', 'RNDR/USDT': 'AI', 'ARKM/USDT': 'AI', 'WLD/USDT': 'AI',
    'DOGE/USDT': 'MEME', 'WIF/USDT': 'MEME', '1000PEPE/USDT': 'MEME', 'BONK/USDT': 'MEME',
    'BTC/USDT': 'MACRO'
}

PORTFOLIO = {k: {'tf': '4h'} for k in CATEGORIES.keys()}

def clean_columns(df):
    df.columns = [c.strip().capitalize() for c in df.columns]
    rename_map = {'Vol': 'Volume', 'Vol.': 'Volume', 'Op': 'Open', 'Hi': 'High', 'Lo': 'Low', 'Cl': 'Close'}
    df.rename(columns=rename_map, inplace=True)
    return df

def run_debug_sim():
    market_data = {}
    strategies = {}
    
    print("\n🛠️ CARGANDO DATOS 4H...")
    for symbol in PORTFOLIO.keys():
        safe_symbol = symbol.replace('/', '_')
        pattern = os.path.join(DATA_DIR, f"{safe_symbol}_4h*.csv")
        files = glob.glob(pattern)
        
        if not files:
            pattern_1h = os.path.join(DATA_DIR, f"{safe_symbol}_1h*.csv")
            files = glob.glob(pattern_1h)
            tf_source = '1h'
        else:
            tf_source = '4h'

        if not files: continue
        target_file = next((f for f in files if "FULL" in f), files[0])
        
        try:
            df = pd.read_csv(target_file, index_col=0, parse_dates=True)
            df = clean_columns(df)
            
            if tf_source == '1h':
                logic = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
                df = df.resample('4h').agg(logic).dropna()
            
            strat = BreakoutBotStrategy()
            df = strat.calculate_indicators(df)
            df = df[(df.index >= '2023-01-01') & (df.index <= '2025-12-31')]
            
            market_data[symbol] = df
            strategies[symbol] = strat
            print(f"✅ {symbol} cargado.")
        except: pass

    if not market_data: return

    # REGIMEN BTC
    btc_regime = pd.Series(True, index=pd.date_range('2023-01-01', '2025-12-31', freq='4h'))
    if 'BTC/USDT' in market_data:
        btc_df = market_data['BTC/USDT']
        btc_sma = btc_df['Close'].rolling(window=200).mean()
        btc_is_bullish = btc_df['Close'] > btc_sma
        btc_regime = btc_is_bullish.reindex(btc_regime.index, method='ffill').fillna(False)

    full_timeline = sorted(list(set().union(*[df.index for df in market_data.values()])))
    wallet = INITIAL_CAPITAL
    bot_memory = {sym: {'status': 'WAITING_BREAKOUT', 'last_exit_time': None} for sym in PORTFOLIO}
    active_positions = {} 
    trades_history = []
    
    print(f"\n🚀 SIMULACIÓN FRANCOTIRADOR ({len(full_timeline)} velas 4H)...")
    
    for i, current_time in enumerate(full_timeline):
        
        is_macro_bullish = True
        try:
            if 'BTC/USDT' in market_data:
                idx = btc_regime.index.get_indexer([current_time], method='pad')[0]
                if idx != -1: is_macro_bullish = btc_regime.iloc[idx]
        except: pass

        # A) SALIDAS
        closed_ids = []
        for sym, pos in active_positions.items():
            df = market_data[sym]
            if current_time not in df.index: continue
            
            curr = df.loc[current_time]
            strat = strategies[sym]
            st = {
                'status': 'IN_POSITION', 'entry_price': pos['entry'], 'stop_loss': pos['sl'],
                'tp_partial': pos['tp'], 'position_size_pct': pos['size_pct'],
                'trailing_active': pos['trail'], 'highest_price_post_tp': pos['h_post']
            }
            
            idx = df.index.get_loc(current_time)
            window = df.iloc[max(0, idx-50):idx+1]
            signal = strat.get_signal(window, st)
            act = signal['action']

            if act == 'EXIT_PARTIAL':
                exit_price = pos['tp']
                realized = (pos['coins'] * 0.5 * exit_price) - (pos['coins'] * 0.5 * pos['entry'])
                wallet += (pos['risk_blocked'] * 0.5) + realized
                pos['coins'] *= 0.5; pos['risk_blocked'] *= 0.5; pos['size_pct'] = 0.5
                pos['sl'] = signal['new_sl']; pos['trail'] = True; pos['h_post'] = signal['highest_price_post_tp']
                active_positions[sym] = pos
                trades_history.append([current_time, sym, 'TP1', realized])

            elif act in ['EXIT_SL', 'EXIT_TRAILING']:
                exit_price = min(curr['Low'], pos['sl'])
                realized = (pos['coins'] * exit_price) - (pos['coins'] * pos['entry'])
                wallet += pos['risk_blocked'] + realized
                closed_ids.append(sym)
                trades_history.append([current_time, sym, act, realized])
                bot_memory[sym] = {'status': 'COOLDOWN', 'last_exit_time': str(current_time)}

            elif act == 'UPDATE_TRAILING':
                pos['sl'] = signal['new_sl']; pos['h_post'] = signal['highest_price_post_tp']
                active_positions[sym] = pos

        for sym in closed_ids: del active_positions[sym]

        # B) ENTRADAS
        if len(active_positions) >= MAX_OPEN_POSITIONS: continue
        if not is_macro_bullish: continue

        active_categories = [CATEGORIES.get(s, 'UNKNOWN') for s in active_positions.keys()]
            
        for sym in PORTFOLIO.keys():
            if sym in active_positions: continue
            if sym not in market_data: continue
            if sym == 'BTC/USDT': continue
            
            my_cat = CATEGORIES.get(sym, 'UNKNOWN')
            if my_cat in active_categories: continue

            if len(active_positions) >= MAX_OPEN_POSITIONS: break
            
            df = market_data[sym]
            if current_time not in df.index: continue
            
            idx = df.index.get_loc(current_time)
            if idx < 50: continue
            window = df.iloc[idx-50 : idx+1]
            st_mem = bot_memory.get(sym, {'status': 'WAITING_BREAKOUT'})
            
            try:
                signal = strategies[sym].get_signal(window, st_mem)
                
                if signal['action'] == 'ENTER_LONG':
                    entry = signal['entry_price']
                    sl = signal['stop_loss']
                    dist = abs(entry - sl)
                    if dist > 0:
                        risk_amt = wallet * RISK_PER_TRADE
                        if risk_amt > wallet: risk_amt = wallet # Safety
                        
                        coins = risk_amt / dist
                        notional = coins * entry
                        
                        if notional > wallet * 0.3: 
                            coins = (wallet * 0.3) / entry; risk_amt = coins * dist
                        
                        wallet -= risk_amt
                        active_positions[sym] = {
                            'entry': entry, 'sl': sl, 'tp': signal['tp_partial'],
                            'coins': coins, 'size_pct': 1.0, 'trail': False, 
                            'h_post': 0.0, 'risk_blocked': risk_amt
                        }
                        active_categories.append(my_cat)
            except: pass

    roi = ((wallet - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    print("\n" + "="*40)
    print(f"📊 RESULTADO FINAL (SCORE > 25)")
    print(f"💰 Capital Final: ${wallet:.2f}")
    print(f"📈 ROI Total:     {roi:.2f}%")
    print(f"🔢 Trades:        {len(trades_history)}")
    print("="*40)

if __name__ == "__main__":
    run_debug_sim()