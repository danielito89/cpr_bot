print("🟢 INICIANDO SIMULACIÓN 'MEAN REVERSION' (Futuros Data)...")

import sys
import os
import pandas as pd
import glob
import numpy as np

PROJECT_ROOT = "/home/orangepi/bot_cpr"
if PROJECT_ROOT not in sys.path: sys.path.append(PROJECT_ROOT)

# --- CONFIGURACIÓN ---
INITIAL_CAPITAL = 1000
MAX_OPEN_POSITIONS = 3
RISK_PER_TRADE = 0.05  # Reversión tiene alto WinRate, permite más riesgo (5%)
DATA_DIR = os.path.join(PROJECT_ROOT, 'backtesting', 'data_futures') # Usamos la carpeta de futuros

# PARES MADUROS (No usamos Memes aquí, esos no revierten, mueren)
PORTFOLIO = ['SOLUSDT', 'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ADAUSDT']

class MeanReversionStrategy:
    def __init__(self):
        self.bb_length = 20
        self.bb_mult = 2.5    # Exigente: Solo caídas fuertes (2.5 std)
        self.rsi_length = 14
        self.rsi_buy = 30     # Sobreventa
        self.rsi_sell = 55    # Salida rápida (apenas cruza la mitad)
        
        # Risk Management de Emergencia
        self.sl_atr = 4.0     # Stop Loss amplio (para aguantar la mecha)

    def calculate_indicators(self, df):
        df = df.copy()
        # Bollinger
        df['BB_Mid'] = df['Close'].rolling(self.bb_length).mean()
        df['BB_Std'] = df['Close'].rolling(self.bb_length).std()
        df['BB_Lower'] = df['BB_Mid'] - (df['BB_Std'] * self.bb_mult)
        
        # RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(self.rsi_length).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_length).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # ATR para Stop de Emergencia
        df['tr0'] = abs(df['High'] - df['Low'])
        df['tr1'] = abs(df['High'] - df['Close'].shift())
        df['tr2'] = abs(df['Low'] - df['Close'].shift())
        df['TR'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        df['ATR'] = df['TR'].rolling(14).mean()
        
        return df

    def get_signal(self, window, state):
        curr = window.iloc[-1]
        
        # --- SALIDA ---
        if state.get('status') == 'IN_POSITION':
            # 1. Take Profit Lógico: Regreso a la media o RSI sano
            if curr['Close'] > curr['BB_Mid'] or curr['RSI'] > self.rsi_sell:
                return {'action': 'EXIT_PROFIT'}
            
            # 2. Stop Loss de Emergencia (Si el activo colapsa de verdad)
            if curr['Low'] <= state['stop_loss']:
                return {'action': 'EXIT_SL'}
                
            return {'action': 'HOLD'}
            
        # --- ENTRADA ---
        # "Catch the Knife" inteligente
        # Precio por debajo de la banda inferior Y RSI en el suelo
        if curr['Close'] < curr['BB_Lower'] and curr['RSI'] < self.rsi_buy:
             atr = curr['ATR']
             return {
                 'action': 'ENTER_LONG',
                 'stop_loss': curr['Close'] - (atr * self.sl_atr)
             }
        return {'action': 'HOLD'}

def run_simulation():
    market_data = {}
    strategies = {}
    
    print("\n🛠️ CARGANDO DATOS FUTUROS 4H...")
    for symbol in PORTFOLIO:
        # Buscamos el archivo _FUTURES.csv
        pattern = os.path.join(DATA_DIR, f"{symbol}*_FUTURES.csv")
        files = glob.glob(pattern)
        if not files: 
            print(f"⚠️ {symbol}: No data.")
            continue
            
        try:
            df = pd.read_csv(files[0], index_col=0, parse_dates=True)
            # Limpieza básica
            strat = MeanReversionStrategy()
            df = strat.calculate_indicators(df)
            df = df[(df.index >= '2023-01-01') & (df.index <= '2025-12-31')]
            market_data[symbol] = df
            strategies[symbol] = strat
            print(f"✅ {symbol} cargado.")
        except: pass

    if not market_data: return

    full_timeline = sorted(list(set().union(*[df.index for df in market_data.values()])))
    wallet = INITIAL_CAPITAL
    active_positions = {}
    trades_history = []
    
    # Stats
    symbol_stats = {sym: {'pnl': 0, 'trades': 0, 'wins': 0} for sym in PORTFOLIO}
    
    print(f"\n🚀 EJECUTANDO 'THE SHIELD' ({len(full_timeline)} velas)...")
    
    for current_time in full_timeline:
        # A) SALIDAS
        closed_ids = []
        for sym, pos in active_positions.items():
            df = market_data[sym]
            if current_time not in df.index: continue
            
            curr = df.loc[current_time]
            strat = strategies[sym]
            
            st = {'status': 'IN_POSITION', 'stop_loss': pos['sl']}
            
            # Ventana pequeña
            idx = df.index.get_loc(current_time)
            window = df.iloc[max(0, idx-30):idx+1]
            signal = strat.get_signal(window, st)
            
            if signal['action'] in ['EXIT_PROFIT', 'EXIT_SL']:
                exit_price = curr['Close'] if signal['action'] == 'EXIT_PROFIT' else min(curr['Low'], pos['sl'])
                realized = (pos['coins'] * exit_price) - (pos['coins'] * pos['entry'])
                wallet += pos['risk_blocked'] + realized
                
                symbol_stats[sym]['pnl'] += realized
                symbol_stats[sym]['trades'] += 1
                if realized > 0: symbol_stats[sym]['wins'] += 1
                
                trades_history.append(realized)
                closed_ids.append(sym)

        for sym in closed_ids: del active_positions[sym]

        # B) ENTRADAS
        if len(active_positions) >= MAX_OPEN_POSITIONS: continue
        
        for sym in market_data:
            if sym in active_positions: continue
            df = market_data[sym]
            if current_time not in df.index: continue
            
            idx = df.index.get_loc(current_time)
            if idx < 30: continue
            window = df.iloc[idx-30 : idx+1]
            
            strat = strategies[sym]
            signal = strat.get_signal(window, {'status': 'WAITING'})
            
            if signal['action'] == 'ENTER_LONG':
                entry = window.iloc[-1]['Close']
                sl = signal['stop_loss']
                dist = abs(entry - sl)
                
                if dist > 0:
                    # Riesgo 5% (Mean Reversion es High Winrate)
                    risk_amt = wallet * RISK_PER_TRADE
                    coins = risk_amt / dist
                    # Cap de seguridad por posición (50% wallet)
                    if (coins * entry) > wallet * 0.5:
                        coins = (wallet * 0.5) / entry
                        risk_amt = coins * dist
                    
                    wallet -= risk_amt
                    active_positions[sym] = {
                        'entry': entry, 'sl': sl, 'coins': coins, 'risk_blocked': risk_amt
                    }

    # REPORTE
    roi = ((wallet - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    print("\n" + "="*50)
    print(f"📊 RESULTADO MEAN REVERSION (SOL/BTC/ETH)")
    print(f"💰 Capital Final: ${wallet:.2f}")
    print(f"📈 ROI Total:     {roi:.2f}%")
    print("="*50)
    
    print("\n📋 POR ACTIVO:")
    for sym, s in symbol_stats.items():
        if s['trades'] > 0:
            wr = (s['wins'] / s['trades']) * 100
            print(f"{sym:<10} | PnL: {s['pnl']:<10.2f} | WR: {wr:.1f}% | Trades: {s['trades']}")

if __name__ == "__main__":
    run_simulation()