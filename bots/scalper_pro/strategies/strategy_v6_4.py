import pandas as pd
import config
from datetime import datetime

class StrategyV6_4:
    def __init__(self):
        self.name = "V6.5 Velocity Sniper (Multi-Profile)"

    def is_core_session(self, timestamp):
        """
        Filtro de Régimen Temporal:
        1. Solo Lunes a Viernes (0-4)
        2. Solo Horario Bancario Extendido (08:00 - 19:00 UTC)
        """
        # 1. Filtro de Días (Sábado=5, Domingo=6)
        if timestamp.weekday() >= 5:
            return False

        # 2. Filtro de Hora (Londres + NY)
        hour = timestamp.hour
        return 8 <= hour <= 18

    def get_signal(self, df, zones, params):
        """
        Calcula señales basadas en Volume Profile + Estructura.
        Recibe 'params' dinámicos según el perfil (SNIPER vs FLOW).
        """
        if df.empty or not zones:
            return None

        # Datos actuales y previos
        row = df.iloc[-1]
        prev = df.iloc[-2]
        
        # --- 1. FILTROS DE RÉGIMEN ---
        
        # A. Filtro de Tiempo
        if not self.is_core_session(row['timestamp']):
            return None

        # B. Filtro de Volumen (Dinámico por Perfil)
        # BTC requiere 1.2, AVAX requiere 0.6
        vol_threshold = params.get('vol_threshold', 0.9)
        
        if row['volume'] < (row['Vol_MA'] * vol_threshold):
            return None

        # --- 2. LÓGICA DE ESTRUCTURA (VAH/VAL) ---
        
        val = zones['VAL']
        vah = zones['VAH']
        
        signal_type = None
        stop_loss = 0.0

        # Parámetros RSI dinámicos
        rsi_limit_long = params.get('rsi_long', 45)
        rsi_limit_short = params.get('rsi_short', 55)

        # --- LONG SETUP ---
        # 1. El precio estaba abajo o tocando el VAL
        # 2. Recupera el nivel y cierra adentro
        # 3. Confirma con fuerza (Close > Open y Close > High previo)
        if prev['low'] <= val and prev['close'] > val: # Rechazo previo
            if row['low'] > val and row['close'] > prev['high'] and row['close'] > row['open']:
                # 4. Filtro RSI
                if row['RSI'] < rsi_limit_long:
                    signal_type = 'LONG'
                    # SL Estructural o por ATR
                    stop_loss = row['close'] - (row['ATR'] * 1.5)

        # --- SHORT SETUP ---
        # 1. El precio estaba arriba o tocando el VAH
        # 2. Pierde el nivel y cierra adentro
        # 3. Confirma debilidad
        elif prev['high'] >= vah and prev['close'] < vah: # Rechazo previo
            if row['high'] < vah and row['close'] < prev['low'] and row['close'] < row['open']:
                # 4. Filtro RSI
                if row['RSI'] > rsi_limit_short:
                    signal_type = 'SHORT'
                    stop_loss = row['close'] + (row['ATR'] * 1.5)

        # --- RETORNO DE SEÑAL ---
        if signal_type:
            return {
                'strategy': 'V6.5',
                'symbol': df['symbol_name'].iloc[0] if 'symbol_name' in df.columns else "UNKNOWN",
                'type': signal_type,
                'entry_price': row['close'],
                'stop_loss': stop_loss,
                'atr': row['ATR'],
                'timestamp': row['timestamp'],
                'profile_name': params.get('name', 'UNKNOWN'), # SNIPER o FLOW
                'risk_type': params.get('risk_type', 'STANDARD') # PREMIUM o STANDARD
            }

        return None