import pandas as pd
from datetime import datetime

class StrategyV6_5:
    def __init__(self):
        self.name = "Hydra V6.5 (Hybrid Engine)"

    def is_core_session(self, timestamp):
        # Filtro de Hora: 8 a 19 UTC (Ajustable)
        if timestamp.weekday() >= 5: return False
        return 8 <= timestamp.hour <= 19

    def get_signal(self, df, zones, params):
        if df.empty or not zones: return None

        row = df.iloc[-1]
        prev = df.iloc[-2]
        symbol = params.get('symbol_name', 'Unknown') # Asegúrate de pasar esto desde main
        
        # --- DIAGNÓSTICO EN VIVO ---
        # Imprimiremos solo si hay ALGO de volumen, para no llenar el log de basura
        vol_threshold = params.get('vol_threshold', 0.9)
        vol_ma = row['Vol_MA']
        
        # 1. Chequeo de Volumen
        if row['volume'] < (vol_ma * vol_threshold):
            # Si el volumen es MUY bajo, ni avisamos. 
            # Si está cerca (ej: >80% del threshold), avisamos para saber que está vivo.
            if row['volume'] > (vol_ma * vol_threshold * 0.8):
                print(f"💤 {symbol}: Volumen insuficiente ({row['volume']:.2f} vs Req: {(vol_ma*vol_threshold):.2f})")
            return None

        # 2. Chequeo de Horario
        if not self.is_core_session(row['timestamp']):
            # print(f"🌙 {symbol}: Fuera de horario") # Descomentar si quieres ver esto
            return None

        # Si llegamos aquí, HAY VOLUMEN y HORARIO. Buscamos patrón técnico.
        val = zones['VAL']
        vah = zones['VAH']
        rsi_long = params.get('rsi_long', 45)
        rsi_short = params.get('rsi_short', 55)
        
        # --- DEBUG DE NIVELES ---
        # print(f"👀 {symbol} Analizando... Close: {row['close']} | VAL: {val:.2f} | VAH: {vah:.2f} | RSI: {row['RSI']:.2f}")

        # LONG SETUP
        if prev['low'] <= val and prev['close'] > val:
            if row['low'] > val and row['close'] > prev['high'] and row['close'] > row['open']:
                if row['RSI'] < rsi_long:
                    return self._build_trade('LONG', row, params)
                else:
                    print(f"⚠️ {symbol} LONG rechazado por RSI alto ({row['RSI']:.2f} > {rsi_long})")
            else:
                pass # Falló confirmación de vela

        # SHORT SETUP
        elif prev['high'] >= vah and prev['close'] < vah:
            if row['high'] < vah and row['close'] < prev['low'] and row['close'] < row['open']:
                if row['RSI'] > rsi_short:
                    return self._build_trade('SHORT', row, params)
                else:
                    print(f"⚠️ {symbol} SHORT rechazado por RSI bajo ({row['RSI']:.2f} < {rsi_short})")
        
        return None

    def _build_trade(self, type_side, row, params):
        """Helper para construir el objeto trade"""
        is_long = (type_side == 'LONG')
        sl_mult = 1.5
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
            'tp_target': params.get('tp_target', 1.5)
        }

        return None