# core/data_processor.py
import pandas as pd
import numpy as np
import sys
import os

# Importar configuración
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

class DataProcessor:
    def __init__(self):
        pass

    def calculate_indicators(self, df):
        """
        Aplica los indicadores de la Estrategia V6.4
        """
        if df is None or df.empty:
            return df

        # 1. RSI Manual (14 periodos)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        rs = avg_gain / avg_loss
        df['RSI'] = 100 - (100 / (1 + rs))

        # 2. Delta Normalizado & CVD (Proxy de Volumen de Compra/Venta)
        range_candle = (df['high'] - df['low']).replace(0, 0.000001)
        df['delta_norm'] = ((df['close'] - df['open']) / range_candle) * df['volume']
        df['cvd'] = df['delta_norm'].cumsum()

        # 3. ATR (14) y ATR Threshold (Percentil 0.25 - V6.4)
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        
        df['ATR'] = tr.rolling(window=14).mean()
        # Calculamos el percentil sobre las ultimas 500 velas
        df['ATR_Threshold'] = df['ATR'].rolling(window=500).quantile(config.ATR_PERCENTILE)

        # 4. Volume Moving Average (20) - Para filtro V6.4
        df['Vol_MA'] = df['volume'].rolling(window=config.VOLUME_MA_PERIOD).mean()

        return df

    def get_volume_profile_zones(self, df, lookback_bars=288):
        """
        Calcula VAH y VAL usando Volume Profile simplificado.
        Lookback 288 velas = 24 horas en M5.
        """
        if len(df) < lookback_bars:
            return None

        subset = df.iloc[-lookback_bars:].copy()
        price_min = subset['low'].min()
        price_max = subset['high'].max()
        
        if price_min == price_max: return None

        # Crear 100 niveles de precio (bins)
        bins = np.linspace(price_min, price_max, 100)
        subset['bin'] = pd.cut(subset['close'], bins=bins)
        
        # Agrupar volumen por nivel de precio
        vp = subset.groupby('bin', observed=False)['volume'].sum().reset_index()
        
        # Calcular Value Area (70%)
        total_volume = vp['volume'].sum()
        value_area_vol = total_volume * 0.70
        
        vp_sorted = vp.sort_values(by='volume', ascending=False)
        vp_sorted['cum_vol'] = vp_sorted['volume'].cumsum()
        
        va_df = vp_sorted[vp_sorted['cum_vol'] <= value_area_vol]
        
        if va_df.empty: return None
        
        # Determinar límites
        vah = va_df['bin'].apply(lambda x: x.right).max()
        val = va_df['bin'].apply(lambda x: x.left).min()
        
        return {'VAH': vah, 'VAL': val}