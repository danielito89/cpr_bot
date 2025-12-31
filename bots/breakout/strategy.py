import pandas as pd
import numpy as np

class BreakoutBotStrategy:
    def __init__(self, atr_period=14, lookback=20):
        self.atr_period = atr_period
        self.lookback = lookback
        self.vol_multiplier = 1.5
        self.atr_pullback_factor = 0.5
        self.trend_filter_period = 200 
        
        # Risk Management
        self.sl_atr = 1.0              
        self.tp_partial_atr = 2.5      
        self.trailing_dist_atr = 1.5   
        self.cooldown_candles = 2      
    
    # ... (calculate_indicators es igual al anterior) ...
    def calculate_indicators(self, df):
        df['Resistance'] = df['High'].rolling(window=self.lookback).max().shift(1)
        df['Vol_SMA'] = df['Volume'].rolling(window=20).mean()
        df['Trend_SMA'] = df['Close'].rolling(window=self.trend_filter_period).mean()
        
        # ATR Manual Robusto
        df['tr0'] = abs(df['High'] - df['Low'])
        df['tr1'] = abs(df['High'] - df['Close'].shift())
        df['tr2'] = abs(df['Low'] - df['Close'].shift())
        df['TR'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        df['ATR'] = df['TR'].rolling(window=self.atr_period).mean()
        return df

    def get_signal(self, df, state_data):
        curr = df.iloc[-1]
        current_state = state_data.get('status', 'WAITING_BREAKOUT')
        
        # --- 0. GESTIÓN DE COOLDOWN ---
        if current_state == 'COOLDOWN':
            last_trade_time = pd.to_datetime(state_data.get('last_exit_time'))
            candles_passed = (curr.name - last_trade_time).total_seconds() / (4 * 3600)
            if candles_passed >= self.cooldown_candles:
                return {'action': 'RESET_STATE', 'new_status': 'WAITING_BREAKOUT'}
            return {'action': 'HOLD'}

        # --- 1. WAITING_BREAKOUT (Sin cambios) ---
        if current_state == 'WAITING_BREAKOUT':
            is_breakout = curr['Close'] > curr['Resistance']
            is_vol_valid = curr['Volume'] > (curr['Vol_SMA'] * self.vol_multiplier)
            is_trend_bullish = curr['Close'] > curr['Trend_SMA']
            
            if is_breakout and is_vol_valid and is_trend_bullish:
                return {
                    'action': 'PREPARE_PULLBACK', 'new_status': 'WAITING_PULLBACK',
                    'breakout_level': curr['Resistance'], 'atr_at_breakout': curr['ATR']
                }

        # --- 2. WAITING_PULLBACK (Sin cambios) ---
        elif current_state == 'WAITING_PULLBACK':
            level = state_data['breakout_level']
            atr = state_data['atr_at_breakout']
            
            if curr['Close'] > level + (2 * atr): # Anti-FOMO
                return {'action': 'CANCEL_FOMO', 'new_status': 'WAITING_BREAKOUT'}
            
            buy_zone = level + (atr * self.atr_pullback_factor)
            
            # Condición Reforzada: Tocar zona Y cerrar sobre soporte
            if curr['Low'] <= buy_zone and curr['Close'] > level:
                entry_price = curr['Close']
                # SL: Mínimo entre Estructura y Volatilidad
                sl_structure = level - (0.25 * atr)
                sl_volatility = entry_price - (atr * self.sl_atr)
                stop_loss = min(sl_structure, sl_volatility)
                tp_partial = entry_price + (atr * self.tp_partial_atr)
                
                return {
                    'action': 'ENTER_LONG', 'new_status': 'IN_POSITION',
                    'entry_price': entry_price, 'stop_loss': stop_loss,
                    'tp_partial': tp_partial, 'trailing_active': False,
                    'position_size_pct': 1.0, 'highest_price_post_tp': 0.0
                }

        # --- 3. IN_POSITION (CORREGIDO: BUG 1 y 2) ---
        elif current_state == 'IN_POSITION':
            entry = state_data['entry_price']
            sl = state_data['stop_loss'] # PUNTO ÚNICO DE VERDAD
            tp_part = state_data['tp_partial']
            atr = curr['ATR']
            
            # A) CHEQUEO UNIVERSAL DE STOP LOSS (Primero y Único)
            if curr['Low'] <= sl:
                return {'action': 'EXIT_SL', 'new_status': 'COOLDOWN'}
            
            # B) CHEQUEO TAKE PROFIT PARCIAL (Solo si estamos al 100%)
            if state_data['position_size_pct'] == 1.0 and curr['High'] >= tp_part:
                new_sl = entry * 1.002 # Breakeven
                return {
                    'action': 'EXIT_PARTIAL',
                    'new_sl': new_sl,
                    'trailing_active': True,
                    'highest_price_post_tp': curr['High'] # Iniciamos tracking
                }
            
            # C) ACTUALIZACIÓN DE TRAILING (Solo si ya cobramos parcial)
            if state_data.get('trailing_active'):
                # Lógica corregida: Comparar contra el máximo previo registrado
                prev_highest = state_data.get('highest_price_post_tp', 0)
                current_high = curr['High']
                
                if current_high > prev_highest:
                    # Hacemos Update del Trailing
                    proposed_sl = current_high - (atr * self.trailing_dist_atr)
                    current_sl = state_data['stop_loss']
                    
                    # El SL solo sube
                    new_sl = max(current_sl, proposed_sl)
                    
                    return {
                        'action': 'UPDATE_TRAILING',
                        'new_sl': new_sl,
                        'highest_price_post_tp': current_high
                    }
                
                # Si no hay nuevo máximo, mantenemos todo igual ('HOLD')

        return {'action': 'HOLD'}