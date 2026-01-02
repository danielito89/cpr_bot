import pandas as pd
import numpy as np

class BreakoutBotStrategy:
    def __init__(self):
        self.lookback = 20
        self.sl_atr = 1.5
        self.tp_partial_atr = 2.0
        self.trailing_dist_atr = 1.5
        self.vol_multiplier = 1.5
        self.sma_period = 200 # Periodo de tendencia

    def calculate_indicators(self, df):
        df = df.copy()
        
        # 1. Resistencia (Donchian Channel High)
        # Shift(1) es VITAL para no mirar el futuro (usamos el maximo de ayer, no de hoy)
        df['Resistance'] = df['High'].rolling(window=self.lookback).max().shift(1)
        
        # 2. ATR para Stops
        df['tr0'] = abs(df['High'] - df['Low'])
        df['tr1'] = abs(df['High'] - df['Close'].shift())
        df['tr2'] = abs(df['Low'] - df['Close'].shift())
        df['TR'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()
        
        # 3. Volumen Promedio (SMA 20)
        df['Vol_SMA'] = df['Volume'].rolling(window=20).mean()
        
        # 4. Tendencia (SMA 200)
        df['Trend_SMA'] = df['Close'].rolling(window=self.sma_period).mean()
        
        return df

    def get_signal(self, window, state_data):
        if len(window) < self.sma_period: # Necesitamos datos suficientes
            return {'action': 'HOLD'}
            
        curr = window.iloc[-1]
        
        # Estado actual
        status = state_data.get('status', 'WAITING_BREAKOUT')
        
        # --- LÓGICA DE SALIDA (SI ESTAMOS DENTRO) ---
        if status == 'IN_POSITION':
            curr_price = curr['High'] # Usamos High para ver si tocó TP
            curr_low = curr['Low']    # Usamos Low para ver si tocó SL
            
            # 1. Chequeo de TP Parcial
            tp_part = state_data.get('tp_partial')
            # Fix: Aseguramos que position_size_pct exista
            size_pct = state_data.get('position_size_pct', 1.0) 
            
            if size_pct == 1.0 and curr_price >= tp_part:
                # Calcular nuevo SL (Breakeven)
                entry = state_data['entry_price']
                new_sl = entry # Breakeven simple
                
                # Opcional: Si el precio subió mucho, el trailing empieza desde el TP
                # Pero por ahora simple: Breakeven.
                
                return {
                    'action': 'EXIT_PARTIAL',
                    'new_sl': new_sl,
                    'highest_price_post_tp': curr_price
                }
            
            # 2. Chequeo de Trailing Stop
            sl = state_data.get('stop_loss')
            if curr_low <= sl:
                # Determinar si es SL total o del remanente
                return {'action': 'EXIT_SL'} if size_pct == 1.0 else {'action': 'EXIT_TRAILING'}
            
            # 3. Actualizar Trailing (Solo si ya tomamos parcial)
            trailing_active = state_data.get('trailing_active', False)
            if trailing_active:
                highest = state_data.get('highest_price_post_tp', 0)
                if curr_price > highest:
                    # Nuevo máximo detectado
                    new_highest = curr_price
                    # Calculamos nuevo SL basado en ATR de cuando entramos (o actual, a gusto)
                    # Usamos ATR actual para ser dinámicos
                    atr = curr['ATR']
                    new_sl = new_highest - (atr * self.trailing_dist_atr)
                    
                    # EL SL SOLO PUEDE SUBIR
                    if new_sl > sl:
                        return {
                            'action': 'UPDATE_TRAILING',
                            'new_sl': new_sl,
                            'highest_price_post_tp': new_highest
                        }
            
            return {'action': 'HOLD'}

        # --- LÓGICA DE ENTRADA (BREAKOUT) ---
        if status == 'WAITING_BREAKOUT' or status == 'COOLDOWN':
            # Condiciones:
            # 1. Cierre > Resistencia
            # 2. Volumen > Promedio * Multiplicador
            
            res = curr['Resistance']
            vol_sma = curr['Vol_SMA']
            
            # Validación de datos nulos
            if pd.isna(res) or pd.isna(vol_sma):
                return {'action': 'HOLD'}
            
            is_breakout_price = curr['Close'] > res
            is_volume_ok = curr['Volume'] > (vol_sma * self.vol_multiplier)
            
            # 🔥🔥🔥 MODIFICACIÓN CRÍTICA: FILTRO DE TENDENCIA DESACTIVADO 🔥🔥🔥
            # is_trend_ok = curr['Close'] > curr['Trend_SMA'] 
            # (Comentamos la línea de arriba y forzamos True)
            is_trend_ok = True 

            if is_breakout_price and is_volume_ok and is_trend_ok:
                atr = curr['ATR']
                entry_price = curr['Close']
                
                stop_loss = entry_price - (atr * self.sl_atr)
                tp_partial = entry_price + (atr * self.tp_partial_atr)
                
                return {
                    'action': 'ENTER_LONG',
                    'entry_price': entry_price,
                    'stop_loss': stop_loss,
                    'tp_partial': tp_partial,
                    'atr_at_breakout': atr,
                    'new_status': 'IN_POSITION'
                }
                
        return {'action': 'HOLD'}