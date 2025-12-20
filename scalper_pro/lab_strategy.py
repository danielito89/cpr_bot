import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta

# ---------------------------------------------------------
# 1. CÁLCULOS MATEMÁTICOS MANUALES (Sin librerías extra)
# ---------------------------------------------------------

def calculate_rsi_manual(series, period=14):
    """Calcula RSI usando Wilder's Smoothing sin pandas_ta"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)

    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_delta_cvd(df):
    """Calcula Delta (aprox) y CVD acumulado"""
    # Si Close >= Open, asumimos que predominó la compra (1), si no venta (-1)
    # Nota: Para mayor precision se puede usar (Close - Open) / (High - Low) * Vol
    df['direction'] = np.where(df['close'] >= df['open'], 1, -1)
    df['delta'] = df['volume'] * df['direction']
    df['cvd'] = df['delta'].cumsum() # Cumulative Volume Delta
    return df

def is_rejection_candle(row, factor=2.0):
    """Detecta si la vela tiene una mecha grande rechazando una zona"""
    body = abs(row['close'] - row['open'])
    wick_top = row['high'] - max(row['close'], row['open'])
    wick_bottom = min(row['close'], row['open']) - row['low']
    
    # Evitar división por cero en velas doji
    if body == 0: body = 0.000001
    
    # Retorna 1 si rechazo arriba (Bearish), -1 si rechazo abajo (Bullish), 0 neutro
    if wick_top > (body * factor):
        return 1 # Rechazo bajista (wick arriba largo)
    elif wick_bottom > (body * factor):
        return -1 # Rechazo alcista (wick abajo largo)
    return 0

def get_volume_profile_zones(df, lookback_bars=288):
    """
    Calcula VAH y VAL basado en las últimas 'lookback_bars'.
    288 barras de 5m = 24 horas.
    """
    # Tomamos el slice de datos para el perfil
    subset = df.iloc[-lookback_bars:]
    
    # Definimos rangos de precio (bins)
    price_min = subset['low'].min()
    price_max = subset['high'].max()
    
    if price_min == price_max: return None # Error data insuficiente
    
    bins = np.linspace(price_min, price_max, 100) # 100 niveles de precio
    
    # Asignamos volumen a los bins
    subset = subset.copy()
    subset['bin'] = pd.cut(subset['close'], bins=bins)
    vp = subset.groupby('bin', observed=False)['volume'].sum().reset_index()
    
    # Ordenamos por volumen para encontrar el Value Area (70%)
    total_volume = vp['volume'].sum()
    value_area_vol = total_volume * 0.70
    
    vp_sorted = vp.sort_values(by='volume', ascending=False)
    vp_sorted['cum_vol'] = vp_sorted['volume'].cumsum()
    
    # Filtramos las barras dentro del Value Area
    va_df = vp_sorted[vp_sorted['cum_vol'] <= value_area_vol]
    
    # Extraemos límites
    if va_df.empty: return None
    
    vah = va_df['bin'].apply(lambda x: x.right).max()
    val = va_df['bin'].apply(lambda x: x.left).min()
    poc = vp.loc[vp['volume'].idxmax(), 'bin'].mid
    
    return {'VAH': vah, 'VAL': val, 'POC': poc}

# ---------------------------------------------------------
# 2. DESCARGA DE DATOS Y SIMULACIÓN
# ---------------------------------------------------------

def run_lab_test():
    print("--- INICIANDO LABORATORIO EN ORANGE PI ---")
    exchange = ccxt.binance()
    symbol = 'BTC/USDT'
    timeframe = '5m'
    limit = 1000 # Últimas 1000 velas (aprox 3.5 días)

    print(f"Descargando {limit} velas de {symbol}...")
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    # 1. Calcular Indicadores Técnicos
    print("Calculando Indicadores (Manuales)...")
    df['RSI'] = calculate_rsi_manual(df['close'], 14)
    df = calculate_delta_cvd(df)
    
    # Variables de estado para simulación
    in_position = False
    balance = 1000 # Simulacion USD
    
    print("\n--- INICIANDO BACKTEST LÓGICO V2 ---")
    
    # Simulamos vela a vela (empezando desde la barra 300 para tener datos previos para el Perfil)
    for i in range(300, len(df)):
        current_row = df.iloc[i]
        prev_row = df.iloc[i-1]
        
        # Calculamos el Perfil de Volumen usando las 24h PREVIAS a la vela actual
        # (Para no hacer trampa viendo el futuro)
        # 288 velas de 5m = 24h
        past_data = df.iloc[i-288:i] 
        zones = get_volume_profile_zones(past_data)
        
        if not zones: continue
        
        vah = zones['VAH']
        val = zones['VAL']
        
        # --- LÓGICA DE ENTRADA (STRATEGY V2) ---
        
        rejection = is_rejection_candle(current_row)
        
        # 1. SEÑAL LONG: Precio toca VAL + RSI Bajo + Delta Positivo + Rechazo
        if current_row['low'] <= val and not in_position:
            # Condiciones
            cond_rsi = current_row['RSI'] < 40 # Sobreventa o cerca
            cond_delta = current_row['delta'] > 0 # Entra compra en el soporte
            cond_rejection = rejection == -1 # Wick largo hacia abajo
            # Divergencia CVD (Simple): El precio baja pero CVD sube en ultimas 3 velas
            cond_cvd = df['cvd'].iloc[i] > df['cvd'].iloc[i-3] 
            
            if cond_rsi and cond_delta and cond_rejection:
                print(f"[{current_row['timestamp']}] 🟢 LONG SIGNAL @ {current_row['close']}")
                print(f"   Razón: Toque VAL ({val:.2f}) + Rechazo + Delta Positivo")
                # Aquí iría la lógica de Take Profit / Stop Loss
        
        # 2. SEÑAL SHORT: Precio toca VAH + RSI Alto + Delta Negativo + Rechazo
        elif current_row['high'] >= vah and not in_position:
            # Condiciones
            cond_rsi = current_row['RSI'] > 60 # Sobrecompra o cerca
            cond_delta = current_row['delta'] < 0 # Entra venta en resistencia
            cond_rejection = rejection == 1 # Wick largo hacia arriba
            
            if cond_rsi and cond_delta and cond_rejection:
                print(f"[{current_row['timestamp']}] 🔴 SHORT SIGNAL @ {current_row['close']}")
                print(f"   Razón: Toque VAH ({vah:.2f}) + Rechazo + Delta Negativo")

if __name__ == "__main__":
    run_lab_test()