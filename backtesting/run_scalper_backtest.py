import pandas as pd
import numpy as np
import ccxt
import os
import sys
from datetime import datetime

# ==============================================================================
# 🎛️ ZONA DE CONFIGURACIÓN (JUEGA AQUÍ)
# ==============================================================================

# 1. FECHAS
START_DATE = "2024-01-01 00:00:00"
END_DATE   = "2024-12-31 23:59:59"

# 2. FILTRO HORARIO (UTC)
# Dejar vacío [] para 24/7
# London Open aprox 07:00 UTC | NY Open aprox 13:00 UTC
# Ejemplo: Solo operar de 8 AM a 4 PM UTC:
# TRADING_HOURS = list(range(8, 17)) 
TRADING_HOURS = [8,9,10,11,12,13,14,15,16,17,18,19,20] # 24/7 por defecto

# 3. PERFILES DE ESTRATEGIA (Tus "Perillas")
PROFILES = {
    'SNIPER': { # Para BTC/ETH
        'vol_threshold': 1.2,   # Exigir 20% más volumen que la media
        'rsi_long': 40,         # Sobreventa fuerte
        'rsi_short': 60,        # Sobrecompra fuerte
        'tp_mult': 3,         # TP moderado
        'sl_atr': 1.0,          # Stop ajustado
        'cooldown': 12          # 1 hora de espera
    },
    'FLOW': {   # Para SOL
        'vol_threshold': 0.6,   # Más permisivo
        'rsi_long': 50,
        'rsi_short': 50,
        'tp_mult': 2.0,         # Scalping rápido
        'sl_atr': 1.5,          # Más aire
        'cooldown': 12           # 30 min de espera
    }
}

# 4. MAPA DE ACTIVOS
TEST_MAP = {
    'BTC/USDT': 'SNIPER',
    'ETH/USDT': 'SNIPER',
    'SOL/USDT': 'FLOW'
}

# ==============================================================================
# ⚙️ MOTOR DE BACKTEST (FIDEDIGNO A PRODUCCIÓN)
# ==============================================================================

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

def fetch_futures_data(symbol):
    """Descarga datos de FUTUROS (igual que el bot real) con caché."""
    safe_symbol = symbol.replace('/', '_')
    csv_path = os.path.join(DATA_DIR, f"{safe_symbol}_5m_2024.csv")
    
    if os.path.exists(csv_path):
        print(f"📂 Cargando {symbol} desde caché local...")
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        return df

    print(f"📥 Descargando FUTUROS de {symbol} (Binance)...")
    # IMPORTANTE: defaultType='future' para coincidir con producción
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
            # Pequeña pausa para no saturar si descargas mucho
        except Exception as e:
            print(f"Error: {e}")
            break
            
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = df[~df.index.duplicated(keep='first')] # Limpiar duplicados
    df.to_csv(csv_path)
    return df

def calculate_indicators(df):
    """Replica la lógica de DataProcessor"""
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
    
    # 4. Bandas (Simulación Bollinger/Volume Profile)
    # Usamos Bollinger 2SD como proxy rápido de VAH/VAL
    df['rolling_mean'] = df['close'].rolling(20).mean() # Scalping usa medias más rápidas a veces
    df['rolling_std'] = df['close'].rolling(20).std()
    df['VAH'] = df['rolling_mean'] + (df['rolling_std'] * 2)
    df['VAL'] = df['rolling_mean'] - (df['rolling_std'] * 2)
    
    return df.dropna()

def run_simulation():
    print(f"\n🧪 SCALPER BACKTEST 2.0 (FUTUROS)")
    print(f"⏰ Horario Trading: {'24/7' if not TRADING_HOURS else TRADING_HOURS}")
    print("="*60)
    
    total_r = 0
    
    for symbol, profile_name in TEST_MAP.items():
        # 1. Datos
        df = fetch_futures_data(symbol)
        if df.empty: continue
        
        # Filtro de Fechas
        df = df.loc[START_DATE:END_DATE]
        df = calculate_indicators(df)
        params = PROFILES[profile_name]
        
        trades = []
        cooldown = 0
        
        # Vectores para velocidad
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
        
        for i in range(50, len(df)-12):
            if cooldown > 0: 
                cooldown -= 1
                continue

            # --- FILTRO HORARIO ---
            if TRADING_HOURS:
                if timestamps[i].hour not in TRADING_HOURS:
                    continue

            # --- LÓGICA DE ESTRATEGIA ---
            
            # Filtro Volumen
            if vols[i] < (vol_mas[i] * params['vol_threshold']): continue
            
            signal = None
            sl_price = 0
            
            # LONG: Cierre > VAL y Cierre > Open (Vela Verde en Soporte)
            # Agregamos validación extra: RSI no saturado
            if lows[i] <= vals[i] and closes[i] > vals[i]:
                if closes[i] > opens[i]: 
                    if rsis[i] < params['rsi_long']: # Comprar barato
                        signal = 'LONG'
                        sl_price = closes[i] - (atrs[i] * params['sl_atr'])

            # SHORT: Cierre < VAH y Cierre < Open (Vela Roja en Resistencia)
            elif highs[i] >= vahs[i] and closes[i] < vahs[i]:
                if closes[i] < opens[i]:
                    if rsis[i] > params['rsi_short']: # Vender caro
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
                
                # Miramos 12 velas al futuro (1 hora)
                for j in range(1, 13):
                    idx = i + j
                    if idx >= len(closes): break
                    
                    if signal == 'LONG':
                        if lows[idx] <= sl_price: outcome_r = -1.0; break
                        if highs[idx] >= tp_price: outcome_r = params['tp_mult']; break
                    else:
                        if highs[idx] >= sl_price: outcome_r = -1.0; break
                        if lows[idx] <= tp_price: outcome_r = params['tp_mult']; break
                        
                    # Salida por tiempo (Time Stop)
                    if j == 12:
                        current_pnl = (closes[idx] - entry) if signal == 'LONG' else (entry - closes[idx])
                        outcome_r = current_pnl / risk
                
                # Fees simulados (Futures Taker Fee ~0.05% x 2)
                # Aproximamos restando un pequeño R (ej. 0.05R)
                r_net = outcome_r - 0.05 
                
                trades.append(r_net)
                cooldown = params['cooldown']
        
        # Reporte por moneda
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