import pandas as pd
import numpy as np

class BreakoutBotStrategy:
    def __init__(self):
        self.lookback = 20
        self.sl_atr = 2.0
        self.tp_partial_atr = 4.0
        self.trailing_dist_atr = 2.5
        
        # Filtros
        self.vol_multiplier = 1.5 
        self.sma_period = 50 
        self.cooldown_candles = 12 

    def calculate_indicators(self, df):
        df = df.copy()
        
        # 1. Resistencia
        df['Resistance'] = df['High'].rolling(window=self.lookback).max().shift(1)
        
        # 2. ATR & Volatilidad
        df['tr0'] = abs(df['High'] - df['Low'])
        df['tr1'] = abs(df['High'] - df['Close'].shift())
        df['tr2'] = abs(df['Low'] - df['Close'].shift())
        df['TR'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()
        
        # FIX DE EDGE: Promedio de ATR para detectar expansión
        df['ATR_SMA'] = df['ATR'].rolling(window=20).mean()
        
        # 3. Vol & Trend
        df['Vol_SMA'] = df['Volume'].rolling(window=20).mean()
        df['Trend_SMA'] = df['Close'].rolling(window=self.sma_period).mean()
        
        # Slope (Pendiente)
        df['SMA_Slope'] = df['Trend_SMA'].diff(3)
        
        return df

    def get_signal(self, window, state_data):
        if len(window) < self.sma_period: return {'action': 'HOLD'}
            
        curr = window.iloc[-1]
        status = state_data.get('status', 'WAITING_BREAKOUT')
        
        # --- SALIDAS ---
        if status == 'IN_POSITION':
            curr_high = curr['High']
            curr_low = curr['Low']
            
            # TP
            tp = state_data.get('tp_partial')
            size_pct = state_data.get('position_size_pct', 1.0)
            if size_pct == 1.0 and curr_high >= tp:
                return {'action': 'EXIT_PARTIAL', 'new_sl': state_data['entry_price'], 'highest_price_post_tp': curr_high}
            
            # SL
            sl = state_data.get('stop_loss')
            if curr_low <= sl:
                return {'action': 'EXIT_SL'} if size_pct == 1.0 else {'action': 'EXIT_TRAILING'}
            
            # Trailing
            if state_data.get('trailing_active'):
                highest = state_data.get('highest_price_post_tp', 0)
                if curr_high > highest:
                    new_high = curr_high
                    new_sl = new_high - (curr['ATR'] * self.trailing_dist_atr)
                    if new_sl > sl:
                        return {'action': 'UPDATE_TRAILING', 'new_sl': new_sl, 'highest_price_post_tp': new_high}
            return {'action': 'HOLD'}

        # --- ENTRADAS ---
        # 1. Gestión de Cooldown (Ahora sí funcionará porque state_data persiste)
        if status == 'COOLDOWN':
             last_exit_str = state_data.get('last_exit_time')
             if last_exit_str:
                 last_exit = pd.to_datetime(last_exit_str)
                 # Calculamos horas pasadas
                 hours_passed = (curr.name - last_exit).total_seconds() / 3600
                 
                 # Si estamos en TF de 4H, cada vela son 4 horas. Ajustamos lógica.
                 # Pero simplificamos: cooldown es en HORAS absolutas.
                 if hours_passed < (self.cooldown_candles): 
                     return {'action': 'HOLD'}
                 
             # Si pasó el tiempo, reseteamos estado internamente para evaluar
             # (No retornamos RESET_STATE para ahorrar un ciclo, evaluamos directo abajo)
             status = 'WAITING_BREAKOUT'

        # 2. Condiciones de Entrada
        res = curr['Resistance']
        vol_sma = curr['Vol_SMA']
        slope = curr['SMA_Slope']
        atr = curr['ATR']
        atr_sma = curr['ATR_SMA']
        
        if pd.isna(res) or pd.isna(vol_sma) or pd.isna(slope): return {'action': 'HOLD'}
        
        # A) Breakout Precio
        if curr['Close'] > res:
            # B) Volumen
            if curr['Volume'] > (vol_sma * self.vol_multiplier):
                # C) Tendencia (Slope Positivo)
                if slope > 0:
                    # D) FIX DE EDGE: Expansión de Volatilidad
                    # Queremos que el ATR actual sea mayor que el promedio (movimiento explosivo)
                    # o al menos que no esté colapsando.
                    if atr > atr_sma:
                        
                        return {
                            'action': 'ENTER_LONG',
                            'new_status': 'IN_POSITION',
                            'entry_price': curr['Close'],
                            'stop_loss': curr['Close'] - (atr * self.sl_atr),
                            'tp_partial': curr['Close'] + (atr * self.tp_partial_atr),
                            'atr_at_breakout': atr,
                            'position_size_pct': 1.0,
                            'trailing_active': False,
                            'highest_price_post_tp': 0.0
                        }
        return {'action': 'HOLD'}