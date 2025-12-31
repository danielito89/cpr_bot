import pandas as pd
from datetime import datetime

class StrategyV6_5:
    def __init__(self):
        self.name = "Hydra V6.5 (Reversion Engine)"

    def is_core_session(self, timestamp):
        # Filtro de Hora: Lunes a Viernes, 8 a 19 UTC
        if timestamp.weekday() >= 5: return False
        return 8 <= timestamp.hour <= 19

    def get_signal(self, df, zones, params):
        if df.empty or not zones: return None

        row = df.iloc[-1]
        prev = df.iloc[-2]
        symbol = params.get('symbol_name', 'Unknown')
        
        # 1. Filtro Volumen
        vol_threshold = params.get('vol_threshold', 0.9)
        if row['volume'] < (row['Vol_MA'] * vol_threshold):
            # Log opcional de bajo volumen
            # if row['volume'] > (row['Vol_MA'] * vol_threshold * 0.8):
            #    print(f"💤 {symbol}: Volumen bajo...")
            return None

        # 2. Filtro Horario
        if not self.is_core_session(row['timestamp']):
            return None

        # Datos Técnicos
        val = zones['VAL']
        vah = zones['VAH']
        rsi_long = params.get('rsi_long', 40)
        rsi_short = params.get('rsi_short', 60)

        # --- LÓGICA DE REVERSIÓN (SNIPER/FLOW) ---
        
        # LONG (Rechazo de VAL)
        # Precio previo estaba abajo o en el borde, precio actual cierra arriba
        if prev['low'] <= val and prev['close'] > val:
            # Confirmación: Vela Verde y supera el High anterior
            if row['low'] > val and row['close'] > prev['high'] and row['close'] > row['open']:
                if row['RSI'] < rsi_long:
                    return self._build_trade('LONG', row, params)

        # SHORT (Rechazo de VAH)
        elif prev['high'] >= vah and prev['close'] < vah:
            # Confirmación: Vela Roja y pierde el Low anterior
            if row['high'] < vah and row['close'] < prev['low'] and row['close'] < row['open']:
                if row['RSI'] > rsi_short:
                    return self._build_trade('SHORT', row, params)
        
        return None

    def _build_trade(self, type_side, row, params):
        is_long = (type_side == 'LONG')
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
            'tp_target': params.get('tp_target', 1.5)
        }

        return None