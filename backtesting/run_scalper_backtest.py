import pandas as pd
import numpy as np
import ccxt
import os
import sys
from datetime import datetime

# ==============================================================================
# 🎛️ ZONA DE CONFIGURACIÓN (RESTAURADA)
# ==============================================================================

START_DATE = "2024-01-01 00:00:00"
END_DATE   = "2024-12-31 23:59:59"

# Horario (Opcional, dejar vacío para 24/7)
TRADING_HOURS = [] 

PROFILES = {
    'SNIPER': { # BTC/ETH
        'vol_threshold': 1.0,   
        'rsi_long': 40,         
        'rsi_short': 60,        
        'tp_mult': 3.0,         # TP Largo (3R)
        'sl_atr': 1.5,          # SL Estructural
        'cooldown': 12          
    },
    'FLOW': {   # SOL
        'vol_threshold': 0.8,   
        'rsi_long': 45,
        'rsi_short': 55,
        'tp_mult': 2.0,         
        'sl_atr': 1.5,
        'cooldown': 6           
    }
}

TEST_MAP = {
    'BTC/USDT': 'SNIPER',
    'ETH/USDT': 'SNIPER',
    'SOL/USDT': 'FLOW'
}

# ==============================================================================
# ⚙️ MOTOR DE BACKTEST (LÓGICA V6.5 PURA)
# ==============================================================================

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

def fetch_futures_data(symbol):
    """Descarga datos de FUTUROS con caché."""
    safe_symbol = symbol.replace('/', '_')
    csv_path = os.path.join(DATA_DIR, f"{safe_symbol}_5m_2024.csv")
    
    if os.path.exists(csv_path):
        print(f"📂 Cargando {symbol} desde caché local...")
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        return df

    print(f"📥 Descargando FUTUROS de {symbol}...")
    exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
    
    since = exchange.parse8601(f"{START_DATE}Z")
    end_ts = exchange.parse8601(f"{END_DATE}Z")
    all_ohlcv = []
    
    while since < end_ts:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=1000, since=since)
            if not ohlcv: break
            since = ohlcv[-1][0] + 1
            all_ohlcv.extend(ohlcv)
        except Exception as e:
            print(f"Error: {e}")
            break
            
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = df[~df.index.duplicated(keep='first')]
    df.to_csv(csv_path)
    return df

def calculate_indicators(df):
    """
    FIX 3: Indicadores Estructurales (Lentos)
    """
    df = df.copy()
    
    # 1. Vol MA
    df['Vol_MA'] = df['volume'].rolling(20).mean()
    
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
    
    # 4. Bandas (FIX 3: Vuelven a ser de 300 periodos)
    # Esto busca reversión a la media estructural, no ruido de bollinger
    df['rolling_mean'] = df['close'].rolling(300).mean()
    df['rolling_std'] = df['close'].rolling(300).std()
    
    # Usamos 2 desviaciones (puedes probar 2.5 para ser más sniper aún)
    df['VAH'] = df['rolling_mean'] + (df['rolling_std'] * 2)
    df['VAL'] = df['rolling_mean'] - (df['rolling_std'] * 2)
    
    return df.dropna()

def run_simulation():
    print(f"\n🧪 SCALPER BACKTEST 3.0 (LOGIC FIX)")
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
        closes = df['close'].values
        opens = df['open'].values
        highs = df['high'].values
        lows = df['low'].values
        vols = df['volume'].values
        vol_mas = df['Vol_MA'].values
        rsis = df['RSI'].values
        atrs = df['ATR'].values
        vahs = df['VAH'].values
        vals = df['VAL'].values
        timestamps = df.index
        
        # Empezamos en 300 para asegurar bandas estables
        for i in range(300, len(df)-12):
            if cooldown > 0: 
                cooldown -= 1
                continue

            if TRADING_HOURS and timestamps[i].hour not in TRADING_HOURS:
                continue

            # Filtro Volumen
            if vols[i] < (vol_mas[i] * params['vol_threshold']): continue
            
            signal = None
            sl_price = 0
            
            # --- FIX 1: LÓGICA DE CONFIRMACIÓN (2 VELAS) ---
            
            # LONG:
            # 1. Vela ANTERIOR (i-1): Pinchó la banda de abajo (Rechazo) pero cerró cerca o dentro.
            # 2. Vela ACTUAL (i): Es alcista (Close > Open) y rompe el máximo anterior.
            touched_prev = lows[i-1] <= vals[i-1] and closes[i-1] > vals[i-1]
            confirm_curr = closes[i] > highs[i-1] and closes[i] > opens[i]
            
            if touched_prev and confirm_curr:
                 if rsis[i] < params['rsi_long']:
                     signal = 'LONG'
                     sl_price = closes[i] - (atrs[i] * params['sl_atr'])

            # SHORT:
            # 1. Vela ANTERIOR: Pinchó arriba.
            # 2. Vela ACTUAL: Bajista y rompe mínimo anterior.
            touched_prev_high = highs[i-1] >= vahs[i-1] and closes[i-1] < vahs[i-1]
            confirm_curr_low = closes[i] < lows[i-1] and closes[i] < opens[i]

            if touched_prev_high and confirm_curr_low:
                 if rsis[i] > params['rsi_short']:
                     signal = 'SHORT'
                     sl_price = closes[i] + (atrs[i] * params['sl_atr'])
            
            # --- SIMULACIÓN DE RESULTADO ---
            if signal:
                entry = closes[i]
                risk = abs(entry - sl_price)
                if risk == 0: continue
                
                tp_dist = risk * params['tp_mult']
                tp_price = entry + tp_dist if signal == 'LONG' else entry - tp_dist
                
                outcome_r = 0
                trade_duration = 0
                
                # FIX 4: SIN TIME EXIT FORZADO (Solo TP o SL)
                # Buscamos hasta 288 velas (24 horas) para dar espacio
                for j in range(1, 288): 
                    idx = i + j
                    if idx >= len(closes): break
                    
                    if signal == 'LONG':
                        if lows[idx] <= sl_price: outcome_r = -1.0; break
                        if highs[idx] >= tp_price: outcome_r = params['tp_mult']; break
                    else:
                        if highs[idx] >= sl_price: outcome_r = -1.0; break
                        if lows[idx] <= tp_price: outcome_r = params['tp_mult']; break
                    
                    # Si pasan 24h, cerramos al mercado (Time Stop de Emergencia)
                    if j == 287:
                        current_pnl = (closes[idx] - entry) if signal == 'LONG' else (entry - closes[idx])
                        outcome_r = current_pnl / risk
                
                # Fees
                r_net = outcome_r - 0.05 
                
                trades.append(r_net)
                cooldown = params['cooldown'] # Este cooldown ahora sí sirve porque hay menos señales
        
        # Reporte
        if trades:
            net_r_symbol = sum(trades)
            win_rate = len([x for x in trades if x > 0]) / len(trades)
            print(f" -> {symbol: <10} [{profile_name}]: {len(trades):3d} trades | WR: {win_rate:.0%} | R Neto: {net_r_symbol:+.2f} R")
            total_r += net_r_symbol
        else:
            print(f" -> {symbol: <10} [{profile_name}]:   0 trades")

    print("-" * 60)
    print(f"💰 RESULTADO FINAL CARTERA: {total_r:+.2f} R")
    print("=" * 60)

if __name__ == "__main__":
    run_simulation()