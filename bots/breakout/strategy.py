import pandas as pd
import numpy as np

class BreakoutBotStrategy:
    def __init__(self):
        self.lookback = 20
        self.sl_atr = 1.5
        self.tp_partial_atr = 2.0
        self.trailing_dist_atr = 1.5
        self.vol_multiplier = 1.5
        self.sma_period = 200 

    def calculate_indicators(self, df):
        df = df.copy()
        
        # 1. Resistencia (Donchian High)
        df['Resistance'] = df['High'].rolling(window=self.lookback).max().shift(1)
        
        # 2. ATR
        df['tr0'] = abs(df['High'] - df['Low'])
        df['tr1'] = abs(df['High'] - df['Close'].shift())
        df['tr2'] = abs(df['Low'] - df['Close'].shift())
        df['TR'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()
        
        # 3. Volumen y Tendencia
        df['Vol_SMA'] = df['Volume'].rolling(window=20).mean()
        df['Trend_SMA'] = df['Close'].rolling(window=self.sma_period).mean()
        
        return df

    def get_signal(self, window, state_data):
        if len(window) < self.sma_period: 
            return {'action': 'HOLD'}
            
        curr = window.iloc[-1]
        status = state_data.get('status', 'WAITING_BREAKOUT')
        
        # --- 1. GESTIÓN DE SALIDAS (SI ESTAMOS DENTRO) ---
        if status == 'IN_POSITION':
            curr_price = curr['High']
            curr_low = curr['Low']
            
            # TP Parcial
            tp_part = state_data.get('tp_partial')
            size_pct = state_data.get('position_size_pct', 1.0)
            
            if size_pct == 1.0 and curr_price >= tp_part:
                return {
                    'action': 'EXIT_PARTIAL',
                    'new_sl': state_data['entry_price'], # Breakeven
                    'highest_price_post_tp': curr_price
                }
            
            # SL
            sl = state_data.get('stop_loss')
            if curr_low <= sl:
                return {'action': 'EXIT_SL'} if size_pct == 1.0 else {'action': 'EXIT_TRAILING'}
            
            # Trailing
            trailing_active = state_data.get('trailing_active', False)
            if trailing_active:
                highest = state_data.get('highest_price_post_tp', 0)
                if curr_price > highest:
                    new_highest = curr_price
                    new_sl = new_highest - (curr['ATR'] * self.trailing_dist_atr)
                    if new_sl > sl:
                        return {'action': 'UPDATE_TRAILING', 'new_sl': new_sl, 'highest_price_post_tp': new_highest}
            
            return {'action': 'HOLD'}

        # --- 2. GESTIÓN DE ENTRADAS (DIRECT BREAKOUT) ---
        # Si estamos esperando, analizamos si rompe YA.
        if status == 'WAITING_BREAKOUT' or status == 'COOLDOWN':
            
            # Gestión de Cooldown simple
            if status == 'COOLDOWN':
                 # Si pasaron 3 velas, reseteamos a WAITING_BREAKOUT internamente para evaluar
                 last_exit_str = state_data.get('last_exit_time')
                 if last_exit_str:
                     last_exit = pd.to_datetime(last_exit_str)
                     hours_diff = (curr.name - last_exit).total_seconds() / 3600
                     if hours_diff < 3: return {'action': 'HOLD'}

            res = curr['Resistance']
            vol_sma = curr['Vol_SMA']
            trend_sma = curr['Trend_SMA']
            
            if pd.isna(res) or pd.isna(vol_sma) or pd.isna(trend_sma): return {'action': 'HOLD'}
            
            # A) RUPTURA DE PRECIO
            is_breakout = curr['Close'] > res
            
            # B) FILTRO DE VOLUMEN (VITAL)
            is_volume_ok = curr['Volume'] > (vol_sma * self.vol_multiplier)
            
            # C) FILTRO DE TENDENCIA (EL SALVAVIDAS)
            # Solo compramos si estamos sobre la media de 200
            is_trend_ok = curr['Close'] > trend_sma

            if is_breakout and is_volume_ok and is_trend_ok:
                atr = curr['ATR']
                entry_price = curr['Close']
                
                return {
                    'action': 'ENTER_LONG',
                    'new_status': 'IN_POSITION',
                    'entry_price': entry_price,
                    'stop_loss': entry_price - (atr * self.sl_atr),
                    'tp_partial': entry_price + (atr * self.tp_partial_atr),
                    'atr_at_breakout': atr,
                    'position_size_pct': 1.0,
                    'trailing_active': False,
                    'highest_price_post_tp': 0.0
                }

        return {'action': 'HOLD'}