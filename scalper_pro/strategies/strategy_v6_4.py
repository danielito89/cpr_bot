# strategies/strategy_v6_4.py
import pandas as pd
import numpy as np
from datetime import datetime
import sys
import os

# Importar configuración
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

class StrategyV6_4:
    def __init__(self):
        self.name = "Velocity Sniper V6.4"

    def is_core_session(self, timestamp):
        """Sesión 13:00 - 18:59 UTC"""
        hour = timestamp.hour
        return 13 <= hour <= 18

    def get_signal(self, df, zones):
        """
        Analiza las últimas velas cerradas para buscar entrada.
        Retorna: dict con datos de entrada o None
        """
        if df is None or df.empty or zones is None:
            return None

        # Trabajamos con la última vela CERRADA (iloc[-1])
        # y la anterior a esa (iloc[-2]) que sería la vela de Setup.
        row = df.iloc[-1]       # Vela de Confirmación
        prev_row = df.iloc[-2]  # Vela de Setup (Rejection)
        
        # 1. Filtros Globales (Sesión y ATR)
        if not self.is_core_session(row['timestamp']):
            return None
        
        if row['ATR'] < row['ATR_Threshold']:
            return None # Mercado muerto

        # Zonas
        vah = zones['VAH']
        val = zones['VAL']
        
        # 2. Análisis Vela Setup (i-1)
        setup_long = (prev_row['low'] <= val) and (prev_row['close'] > val)
        setup_short = (prev_row['high'] >= vah) and (prev_row['close'] < vah)
        
        if not (setup_long or setup_short):
            return None

        # 3. Análisis de Calidad (Vela Actual i)
        c_range = row['high'] - row['low']
        if c_range == 0: return None
        
        c_body = abs(row['close'] - row['open'])
        body_strength = c_body / c_range
        
        # Filtro Convicción (> 40% cuerpo)
        if body_strength < 0.40:
            return None

        # Filtro Expansión Mínima (> 0.25 ATR)
        expansion = abs(row['close'] - prev_row['close'])
        if expansion < (row['ATR'] * 0.25):
            return None

        # Filtro Volumen (> 80% del promedio)
        if row['volume'] < (row['Vol_MA'] * 0.8):
            return None

        # 4. Decisión Final
        signal = None
        stop_loss = 0.0
        
        # --- LONG ---
        if setup_long:
            # Acceptance: Low > VAL | Close > Prev High | Delta > 0
            if (row['low'] > val and 
                row['close'] > prev_row['high'] and 
                row['delta_norm'] > 0):
                
                if row['RSI'] < config.RSI_LONG_THRESHOLD:
                    signal = 'LONG'
                    stop_loss = row['close'] - (row['ATR'] * 1.5) # SL Estructural

        # --- SHORT ---
        elif setup_short:
            # Acceptance: High < VAH | Close < Prev Low | Delta < 0
            if (row['high'] < vah and 
                row['close'] < prev_row['low'] and 
                row['delta_norm'] < 0):
                
                if row['RSI'] > config.RSI_SHORT_THRESHOLD:
                    signal = 'SHORT'
                    stop_loss = row['close'] + (row['ATR'] * 1.5) # SL Estructural

        if signal:
            return {
                'type': signal,
                'entry_price': row['close'],
                'stop_loss': stop_loss,
                'atr': row['ATR'],
                'time': row['timestamp']
            }
        
        return None