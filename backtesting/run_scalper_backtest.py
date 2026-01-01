import pandas as pd
import numpy as np
import ccxt
import os
import sys

# ==============================================================================
# 🎛️ CONFIGURACIÓN V7 (TREND FILTERED MEAN REVERSION)
# ==============================================================================

START_DATE = "2024-01-01 00:00:00"
END_DATE   = "2024-12-31 23:59:59"

PROFILES = {
    'TREND_SCALPER': { 
        'vol_threshold': 1.0,   
        'rsi_long': 35,         # Solo comprar si está MUY sobrevendido
        'rsi_short': 65,        # Solo vender si está MUY sobrecomprado
        'tp_mult': 2.0,         # TP más conservador
        'sl_atr': 1.5,          # Stop estándar
        'cooldown': 8          
    }
}

TEST_MAP = {
    'SOL/USDT': 'TREND_SCALPER',
    'BTC/USDT': 'TREND_SCALPER',
    'ETH/USDT': 'TREND_SCALPER',
    'DOGE/USDT': 'TREND_SCALPER',
    'AVAX/USDT': 'TREND_SCALPER'
}

# ==============================================================================
# ⚙️ MOTOR
# ==============================================================================

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

def fetch_futures_data(symbol):
    safe_symbol = symbol.replace('/', '_')
    csv_path = os.path.join(DATA_DIR, f"{safe_symbol}_5m_2024.csv")
    if os.path.exists(csv_path):
        print(f"📂 Cargando {symbol}...")
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        return df
    # (Omitimos la descarga porque ya deberías tener los datos en caché de las pruebas anteriores)
    return pd.DataFrame() 

def calculate_indicators(df):
    df = df.copy()
    
    # 1. EMA 200 (FILTRO DE TENDENCIA MAESTRO)
    df['EMA200'] = df['close'].ewm(span=200, adjust=False).mean()
    
    # 2. RSI 14
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # 3. ATR 14
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    df['ATR'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
    
    # 4. Vol MA
    df['Vol_MA'] = df['volume'].rolling(20).mean()

    # 5. Bandas Estructurales (300 periodos)
    df['rolling_mean'] = df['close'].rolling(300).mean()
    df['rolling_std'] = df['close'].rolling(300).std()
    df['VAH'] = df['rolling_mean'] + (df['rolling_std'] * 2)
    df['VAL'] = df['rolling_mean'] - (df['rolling_std'] * 2)
    
    return df.dropna()

def run_simulation():
    print(f"\n🧪 SCALPER BACKTEST 5.0 (TREND FILTERED)")
    print("="*60)
    
    total_r = 0
    
    for symbol, profile_name in TEST_MAP.items():
        df = fetch_futures_data(symbol)
        if df.empty: continue
        
        df = df.loc[START_DATE:END_DATE]
        df = calculate_indicators(df)
        params = PROFILES[profile_name]
        
        trades = []
        cooldown = 0
        
        # Vectores
        closes = df['close'].values; opens = df['open'].values
        highs = df['high'].values; lows = df['low'].values
        vols = df['volume'].values; vol_mas = df['Vol_MA'].values
        rsis = df['RSI'].values; atrs = df['ATR'].values
        vahs = df['VAH'].values; vals = df['VAL'].values
        ema200 = df['EMA200'].values # Nuevo Vector
        
        for i in range(300, len(df)-12):
            if cooldown > 0: cooldown -= 1; continue

            # Filtro Volumen
            if vols[i] < (vol_mas[i] * params['vol_threshold']): continue
            
            signal = None
            sl_price = 0
            
            # --- LÓGICA CON FILTRO DE TENDENCIA ---
            
            # LONG:
            # 1. Precio > EMA 200 (Tendencia Alcista)
            # 2. Reversión en la banda inferior (Comprar el Dip)
            if closes[i] > ema200[i]: 
                touched_prev = lows[i-1] <= vals[i-1]
                confirm_curr = closes[i] > highs[i-1] and closes[i] > opens[i]
                
                if touched_prev and confirm_curr:
                     if rsis[i] < params['rsi_long']:
                         signal = 'LONG'
                         sl_price = closes[i] - (atrs[i] * params['sl_atr'])

            # SHORT:
            # 1. Precio < EMA 200 (Tendencia Bajista)
            # 2. Reversión en la banda superior (Vender el rebote)
            elif closes[i] < ema200[i]:
                touched_prev_high = highs[i-1] >= vahs[i-1]
                confirm_curr_low = closes[i] < lows[i-1] and closes[i] < opens[i]

                if touched_prev_high and confirm_curr_low:
                     if rsis[i] > params['rsi_short']:
                         signal = 'SHORT'
                         sl_price = closes[i] + (atrs[i] * params['sl_atr'])
            
            # --- SIMULACIÓN (CON BREAKEVEN) ---
            if signal:
                entry = closes[i]
                risk = abs(entry - sl_price)
                if risk == 0: continue
                
                tp_dist = risk * params['tp_mult']
                tp_price = entry + tp_dist if signal == 'LONG' else entry - tp_dist
                be_trigger = entry + risk if signal == 'LONG' else entry - risk
                
                outcome_r = 0
                sl_moved_to_be = False 
                
                for j in range(1, 288): 
                    idx = i + j
                    if idx >= len(closes): break
                    
                    current_low = lows[idx]; current_high = highs[idx]

                    if signal == 'LONG':
                        if current_low <= sl_price: 
                            outcome_r = 0.0 if sl_moved_to_be else -1.0; break
                        if current_high >= tp_price: 
                            outcome_r = params['tp_mult']; break
                        if not sl_moved_to_be and current_high >= be_trigger:
                            sl_price = entry; sl_moved_to_be = True
                    else: 
                        if current_high >= sl_price: 
                            outcome_r = 0.0 if sl_moved_to_be else -1.0; break
                        if current_low <= tp_price: 
                            outcome_r = params['tp_mult']; break
                        if not sl_moved_to_be and current_low <= be_trigger:
                            sl_price = entry; sl_moved_to_be = True
                    
                    if j == 287: # Time Stop
                        pnl = (closes[idx]-entry) if signal=='LONG' else (entry-closes[idx])
                        outcome_r = pnl/risk

                r_net = outcome_r - 0.05 
                trades.append(r_net)
                cooldown = params['cooldown']
        
        if trades:
            net_r = sum(trades)
            win_rate = len([x for x in trades if x > 0])/len(trades)
            print(f" -> {symbol: <10}: {len(trades):3d} trades | WR: {win_rate:.0%} | R Neto: {net_r:+.2f} R")
            total_r += net_r
        else:
            print(f" -> {symbol: <10}:   0 trades")

    print("-" * 60)
    print(f"💰 RESULTADO FINAL: {total_r:+.2f} R")
    print("=" * 60)

if __name__ == "__main__":
    run_simulation()