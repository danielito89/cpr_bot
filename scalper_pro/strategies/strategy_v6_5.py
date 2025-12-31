import pandas as pd
from datetime import datetime

class StrategyV6_5:
    def __init__(self):
        self.name = "Hydra V7 (Dual Engine)" # Actualizamos nombre

    def is_core_session(self, timestamp):
        if timestamp.weekday() >= 5: return False
        return 8 <= timestamp.hour <= 19

    def get_signal(self, df, zones, params):
        if df.empty or not zones: return None

        row = df.iloc[-1]
        prev = df.iloc[-2]
        
        # 1. Filtro Básico (Horario y Volumen)
        if not self.is_core_session(row['timestamp']):
            return None
            
        vol_threshold = params.get('vol_threshold', 0.9)
        if row['volume'] < (row['Vol_MA'] * vol_threshold):
            return None

        # Datos
        val = zones['VAL']
        vah = zones['VAH']
        mode = params.get('mode', 'REVERSION') # Default a la vieja confiable
        
        # ======================================================================
        # 🛡️ MOTOR 1: REVERSIÓN (Tu estrategia actual)
        # ======================================================================
        if mode == 'REVERSION':
            rsi_long = params.get('rsi_long', 40)
            rsi_short = params.get('rsi_short', 60)

            # LONG (Rechazo de VAL)
            if prev['low'] <= val and prev['close'] > val:
                if row['low'] > val and row['close'] > prev['high'] and row['close'] > row['open']:
                    if row['RSI'] < rsi_long:
                        return self._build_trade('LONG', row, params, 'REVERSION')

            # SHORT (Rechazo de VAH)
            elif prev['high'] >= vah and prev['close'] < vah:
                if row['high'] < vah and row['close'] < prev['low'] and row['close'] < row['open']:
                    if row['RSI'] > rsi_short:
                        return self._build_trade('SHORT', row, params, 'REVERSION')

        # ======================================================================
        # 🚀 MOTOR 2: BREAKOUT (La nueva bestia)
        # ======================================================================
        elif mode == 'BREAKOUT':
            # Para breakout, queremos RSI a favor de la tendencia (Momentum)
            # Long: RSI > 55 (Fuerza). Short: RSI < 45 (Debilidad)
            rsi_min_bull = params.get('rsi_long', 55)
            rsi_max_bear = params.get('rsi_short', 45)

            # LONG BREAKOUT (Rompe VAH y cierra afuera con fuerza)
            # Condición: Cierre actual > VAH y Cierre previo < VAH (Cruce limpio)
            # OJO: Exigimos vela verde fuerte (Close > Open)
            if row['close'] > vah and row['close'] > row['open']:
                # Confirmación RSI (No queremos entrar con RSI 90, pero sí > 55)
                if row['RSI'] > rsi_min_bull and row['RSI'] < 80: 
                    return self._build_trade('LONG', row, params, 'BREAKOUT')

            # SHORT BREAKOUT (Rompe VAL y cierra abajo con fuerza)
            elif row['close'] < val and row['close'] < row['open']:
                # Confirmación RSI
                if row['RSI'] < rsi_max_bear and row['RSI'] > 20:
                    return self._build_trade('SHORT', row, params, 'BREAKOUT')

        return None

    def _build_trade(self, type_side, row, params, setup_type):
        """Helper para construir el objeto trade"""
        is_long = (type_side == 'LONG')
        
        # Stop Loss Dinámico
        # Reversion: 1.5 ATR (Damos aire para respirar)
        # Breakout: 1.0 ATR (Si falla la ruptura, salimos rápido)
        sl_mult = params.get('sl_atr', 1.5)
        
        stop_loss = row['close'] - (row['ATR'] * sl_mult) if is_long else row['close'] + (row['ATR'] * sl_mult)
        
        return {
            'strategy': self.name,
            'type': type_side,
            'entry_price': row['close'],
            'stop_loss': stop_loss,
            'atr': row['ATR'],
            'timestamp': row['timestamp'],
            'profile_name': params.get('name', 'UNKNOWN'),
            'risk_type': params.get('risk_type', 'STANDARD'),
            'tp_target': params.get('tp_target', 1.5),
            'setup_type': setup_type # Para saber en logs qué lógica entró
        }

        return None