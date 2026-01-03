import pandas as pd
import numpy as np

class BreakoutBotStrategy:
    def __init__(self):
        self.lookback = 20
        self.sl_atr = 2.0         # Stops más anchos para aguantar mechas
        self.tp_partial_atr = 4.0
        self.trailing_dist_atr = 2.5
        self.vol_multiplier = 1.5 
        
        # FIX 3: Usaremos Slope (Pendiente), no posición absoluta
        self.sma_period = 50 
        
        # FIX 4: Cooldown agresivo para evitar overtrading (12 horas)
        self.cooldown_candles = 12 

    def calculate_indicators(self, df):
        df = df.copy()
        
        # 1. Resistencia
        df['Resistance'] = df['High'].rolling(window=self.lookback).max().shift(1)
        
        # 2. ATR
        df['tr0'] = abs(df['High'] - df['Low'])
        df['tr1'] = abs(df['High'] - df['Close'].shift())
        df['tr2'] = abs(df['Low'] - df['Close'].shift())
        df['TR'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()
        
        # 3. Vol & Trend
        df['Vol_SMA'] = df['Volume'].rolling(window=20).mean()
        df['Trend_SMA'] = df['Close'].rolling(window=self.sma_period).mean()
        
        # FIX 3: Calculamos la PENDIENTE (Slope) de la SMA
        # Si la SMA de hoy es mayor que la de hace 3 horas, es alcista.
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
        # FIX 4: Gestión de Cooldown Estricta
        if status == 'COOLDOWN':
             last_exit = pd.to_datetime(state_data.get('last_exit_time'))
             # Si pasaron menos de 12 horas, no operamos
             if (curr.name - last_exit).total_seconds() / 3600 < self.cooldown_candles: 
                 return {'action': 'HOLD'}
             # Si ya pasó el tiempo, evaluamos entrada normalmente abajo

        # CONDICIONES DE ENTRADA
        res = curr['Resistance']
        vol_sma = curr['Vol_SMA']
        slope = curr['SMA_Slope']
        
        if pd.isna(res) or pd.isna(vol_sma) or pd.isna(slope): return {'action': 'HOLD'}
        
        # 1. Breakout Precio
        if curr['Close'] > res:
            # 2. Volumen
            if curr['Volume'] > (vol_sma * self.vol_multiplier):
                # 3. FIX 3: Filtro de Pendiente (Slope)
                # Solo entramos si la media móvil está subiendo (inercia positiva)
                if slope > 0:
                    
                    atr = curr['ATR']
                    entry = curr['Close']
                    return {
                        'action': 'ENTER_LONG',
                        'new_status': 'IN_POSITION',
                        'entry_price': entry,
                        'stop_loss': entry - (atr * self.sl_atr),
                        'tp_partial': entry + (atr * self.tp_partial_atr),
                        'atr_at_breakout': atr,
                        'position_size_pct': 1.0,
                        'trailing_active': False,
                        'highest_price_post_tp': 0.0
                    }
        return {'action': 'HOLD'}