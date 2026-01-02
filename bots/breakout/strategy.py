import pandas as pd
import numpy as np

class BreakoutBotStrategy:
    def __init__(self):
        self.lookback = 20
        # Stops más holgados para aguantar la volatilidad
        self.sl_atr = 2.0  
        self.tp_partial_atr = 4.0
        self.trailing_dist_atr = 2.5
        
        # EL FILTRO DE ORO: VOLUMEN
        # 1.5 es el equilibrio. 1.8 era muy estricto.
        self.vol_multiplier = 1.5 
        
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
        
        # 3. Volumen Promedio (SMA 20)
        df['Vol_SMA'] = df['Volume'].rolling(window=20).mean()
        
        # NOTA: Borramos Trend_SMA. No la queremos. Estorba.
        
        return df

    def get_signal(self, window, state_data):
        # Necesitamos mínimas velas para Vol_SMA y Resistance
        if len(window) < 25: return {'action': 'HOLD'}
            
        curr = window.iloc[-1]
        status = state_data.get('status', 'WAITING_BREAKOUT')
        
        # --- 1. SALIDAS ---
        if status == 'IN_POSITION':
            curr_high = curr['High']
            curr_low = curr['Low']
            
            # TP
            tp = state_data.get('tp_partial')
            size_pct = state_data.get('position_size_pct', 1.0)
            
            if size_pct == 1.0 and curr_high >= tp:
                return {
                    'action': 'EXIT_PARTIAL',
                    'new_sl': state_data['entry_price'], # Breakeven
                    'highest_price_post_tp': curr_high
                }
            
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

        # --- 2. ENTRADAS ---
        if status == 'WAITING_BREAKOUT' or status == 'COOLDOWN':
            
            res = curr['Resistance']
            vol_sma = curr['Vol_SMA']
            
            if pd.isna(res) or pd.isna(vol_sma): return {'action': 'HOLD'}
            
            # CONDICIÓN 1: PRECIO ROMPE RESISTENCIA
            if curr['Close'] > res:
                
                # CONDICIÓN 2: VOLUMEN CONFIRMA (El único filtro necesario)
                if curr['Volume'] > (vol_sma * self.vol_multiplier):
                    
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