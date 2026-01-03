import pandas as pd
import numpy as np

class BreakoutBotStrategy:
    def __init__(self):
        # Parámetros Squeeze
        self.bb_length = 20
        self.bb_mult = 2.0
        self.kc_length = 20
        self.kc_mult = 1.5
        
        # Parámetros Salida
        self.sl_atr = 2.0
        self.tp_partial_atr = 4.0
        self.trailing_dist_atr = 3.0
        
        # Filtros
        self.adx_threshold = 20 # Tendencia mínima
        self.adx_period = 14
        
        # Cooldown Estructural (Velas)
        self.cooldown_candles = 10 

    def calculate_indicators(self, df):
        df = df.copy()
        
        # 1. ATR y TR
        df['tr0'] = abs(df['High'] - df['Low'])
        df['tr1'] = abs(df['High'] - df['Close'].shift())
        df['tr2'] = abs(df['Low'] - df['Close'].shift())
        df['TR'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()
        
        # 2. Bollinger Bands (BB)
        df['BB_Mid'] = df['Close'].rolling(window=self.bb_length).mean()
        df['BB_Std'] = df['Close'].rolling(window=self.bb_length).std()
        df['BB_Upper'] = df['BB_Mid'] + (df['BB_Std'] * self.bb_mult)
        df['BB_Lower'] = df['BB_Mid'] - (df['BB_Std'] * self.bb_mult)
        
        # 3. Keltner Channels (KC)
        # Usamos SMA para la linea media de KC (igual que BB)
        df['KC_Upper'] = df['BB_Mid'] + (df['ATR'] * self.kc_mult)
        df['KC_Lower'] = df['BB_Mid'] - (df['ATR'] * self.kc_mult)
        
        # 4. SQUEEZE DETECTOR
        # True si las bandas de Bollinger están DENTRO de las de Keltner
        df['Squeeze_On'] = (df['BB_Upper'] < df['KC_Upper']) & (df['BB_Lower'] > df['KC_Lower'])
        
        # 5. ADX (Fuerza de tendencia)
        # Cálculo simplificado de ADX para no depender de talib
        up = df['High'] - df['High'].shift(1)
        down = df['Low'].shift(1) - df['Low']
        pos_dm = np.where((up > down) & (up > 0), up, 0.0)
        neg_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = df['TR']
        
        # Suavizado Wilder
        def wilder_smooth(series, period):
            return series.ewm(alpha=1/period, adjust=False).mean()

        tr_smooth = wilder_smooth(pd.Series(tr), self.adx_period)
        pos_dm_smooth = wilder_smooth(pd.Series(pos_dm), self.adx_period)
        neg_dm_smooth = wilder_smooth(pd.Series(neg_dm), self.adx_period)
        
        pos_di = 100 * (pos_dm_smooth / tr_smooth)
        neg_di = 100 * (neg_dm_smooth / tr_smooth)
        dx = 100 * abs(pos_di - neg_di) / (pos_di + neg_di)
        df['ADX'] = wilder_smooth(dx, self.adx_period)

        # 6. Momentum (Linear Regression o simple Delta)
        # Usamos cambio de precio vs hace 5 periodos como proxy de momentum
        df['Momentum'] = df['Close'] - df['Close'].shift(5)

        return df

    def get_signal(self, window, state_data):
        if len(window) < 30: return {'action': 'HOLD'}
            
        curr = window.iloc[-1]
        prev = window.iloc[-2]
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

        # --- ENTRADAS (SQUEEZE BREAKOUT) ---
        if status == 'WAITING_BREAKOUT' or status == 'COOLDOWN':
            # Cooldown
            if status == 'COOLDOWN':
                 last_exit = pd.to_datetime(state_data.get('last_exit_time'))
                 if (curr.name - last_exit).total_seconds() / 3600 < (self.cooldown_candles * 4): # *4 si es 4H
                     return {'action': 'HOLD'}

            # LÓGICA DE ENTRADA:
            # 1. Venimos de un Squeeze? (Miramos si hubo squeeze en las últimas 5 velas)
            recent_squeeze = window['Squeeze_On'].iloc[-6:-1].any()
            
            # 2. El Squeeze se rompió? (Ahora Squeeze es False y las bandas se abren)
            squeeze_fired = (not curr['Squeeze_On']) and recent_squeeze
            
            # 3. Confirmación de Trend (ADX)
            adx_ok = curr['ADX'] > self.adx_threshold
            
            # 4. Momentum Positivo (Precio rompe Upper BB o Momentum > 0)
            momentum_up = curr['Close'] > curr['BB_Upper'] or (curr['Momentum'] > 0 and curr['Close'] > curr['BB_Mid'])

            if squeeze_fired and adx_ok and momentum_up:
                
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