import pandas as pd
from datetime import datetime

class StrategyV6_5:
    def __init__(self):
        self.name = "Hydra V6.5 (Hybrid Engine)"

    def is_core_session(self, timestamp):
        """
        Filtro de Horario:
        Opera Lunes a Viernes (0-4)
        Horario Extendido: 08:00 a 19:00 UTC (Cubre Londres + NY)
        """
        # 1. Filtro de Días (Sábado=5, Domingo=6 -> OFF)
        if timestamp.weekday() >= 5:
            return False

        # 2. Filtro de Hora
        hour = timestamp.hour
        return 8 <= hour <= 19

    def get_signal(self, df, zones, params):
        """
        Calcula señales recibiendo parámetros dinámicos (Sniper/Flow).
        """
        if df.empty or not zones:
            return None

        # Datos actuales y previos
        row = df.iloc[-1]
        prev = df.iloc[-2]
        
        # --- 1. FILTRO DE HORARIO ---
        if not self.is_core_session(row['timestamp']):
            return None

        # --- 2. FILTRO DE VOLUMEN (Dinámico) ---
        # El umbral (1.2 o 0.6) viene en 'params'
        vol_threshold = params.get('vol_threshold', 0.9)
        
        if row['volume'] < (row['Vol_MA'] * vol_threshold):
            return None

        # --- 3. ESTRUCTURA DE MERCADO (Volume Profile) ---
        val = zones['VAL']
        vah = zones['VAH']
        
        signal_type = None
        stop_loss = 0.0
        
        # Extraemos límites RSI del perfil
        rsi_limit_long = params.get('rsi_long', 45)
        rsi_limit_short = params.get('rsi_short', 55)

        # --- LONG SETUP ---
        # Precio estaba bajo VAL y recupera el nivel con fuerza
        if prev['low'] <= val and prev['close'] > val: # Rechazo previo o recuperación
            # Confirmación de la vela actual (Cierra arriba del open y del high previo)
            if row['low'] > val and row['close'] > prev['high'] and row['close'] > row['open']:
                # Filtro RSI
                if row['RSI'] < rsi_limit_long:
                    signal_type = 'LONG'
                    # SL Estructural: Un poco por debajo del cierre (1.5 ATR)
                    stop_loss = row['close'] - (row['ATR'] * 1.5)

        # --- SHORT SETUP ---
        # Precio estaba sobre VAH y pierde el nivel con fuerza
        elif prev['high'] >= vah and prev['close'] < vah:
            # Confirmación
            if row['high'] < vah and row['close'] < prev['low'] and row['close'] < row['open']:
                # Filtro RSI
                if row['RSI'] > rsi_limit_short:
                    signal_type = 'SHORT'
                    # SL Estructural
                    stop_loss = row['close'] + (row['ATR'] * 1.5)

        # --- RETORNO DE SEÑAL ---
        if signal_type:
            return {
                'strategy': self.name,
                'symbol': df['symbol_name'].iloc[0] if 'symbol_name' in df.columns else "UNKNOWN",
                'type': signal_type,
                'entry_price': row['close'],
                'stop_loss': stop_loss,
                'atr': row['ATR'],
                'timestamp': row['timestamp'],
                'profile_name': params.get('name', 'UNKNOWN'),
                'risk_type': params.get('risk_type', 'STANDARD'),
                'tp_target': params.get('tp_target', 1.5)
            }

        return None