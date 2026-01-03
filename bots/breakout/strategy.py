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
        
        # Filtros ADX
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
        df['KC_Upper'] = df['BB_Mid'] + (df['ATR'] * self.kc_mult)
        df['KC_Lower'] = df['BB_Mid'] - (df['ATR'] * self.kc_mult)
        
        # --- FIX 1: SQUEEZE RELATIVO (CRYPTO ADAPTED) ---
        # No exigimos que BB esté 100% dentro de KC.
        # Exigimos que el ancho de BB sea menor al 85% del ancho de KC.
        bb_width = df['BB_Upper'] - df['BB_Lower']
        kc_width = df['KC_Upper'] - df['KC_Lower']
        
        # Evitamos división por cero o NaNs
        kc_width = kc_width.replace(0, np.nan) 
        
        # True si hay contracción relativa
        df['Squeeze_On'] = bb_width < (kc_width * 0.85)
        
        # 4. ADX (Cálculo standard Wilder)
        up = df['High'] - df['High'].shift(1)
        down = df['Low'].shift(1) - df['Low']
        pos_dm = np.where((up > down) & (up > 0), up, 0.0)
        neg_dm = np.where((down > up) & (down > 0), down, 0.0)
        
        def wilder_smooth(series, period):
            return series.ewm(alpha=1/period, adjust=False).mean()

        tr_smooth = wilder_smooth(df['TR'], self.adx_period)
        pos_dm_smooth = wilder_smooth(pd.Series(pos_dm, index=df.index), self.adx_period)
        neg_dm_smooth = wilder_smooth(pd.Series(neg_dm, index=df.index), self.adx_period)
        
        pos_di = 100 * (pos_dm_smooth / tr_smooth)
        neg_di = 100 * (neg_dm_smooth / tr_smooth)
        dx = 100 * abs(pos_di - neg_di) / (pos_di + neg_di)
        df['ADX'] = wilder_smooth(dx, self.adx_period)

        return df

    def get_signal(self, window, state_data):
        if len(window) < 30: return {'action': 'HOLD'}
            
        curr = window.iloc[-1]
        prev = window.iloc[-2] # Necesitamos la vela anterior para comparar
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

        # --- ENTRADAS (CRYPTO SQUEEZE) ---
        if status == 'WAITING_BREAKOUT' or status == 'COOLDOWN':
            # Cooldown check
            if status == 'COOLDOWN':
                 last_exit = pd.to_datetime(state_data.get('last_exit_time'))
                 # En 4H, cada vela es 1 unidad de cooldown_candles si el simulador pasa velas.
                 # Pero debug_sim pasa tiempo real. 10 velas * 4 horas = 40 horas.
                 if (curr.name - last_exit).total_seconds() / 3600 < (self.cooldown_candles * 4): 
                     return {'action': 'HOLD'}

            # 1. FIX 4: Ventana de Squeeze más larga (12 velas hacia atrás)
            # Buscamos si hubo ALGÚN momento de compresión recientemente
            # -13:-1 mira las ultimas 12 velas antes de la actual
            recent_squeeze = window['Squeeze_On'].iloc[-13:-1].any()
            
            # El disparo ocurre si venimos de squeeze y ahora estamos rompiendo
            if not recent_squeeze: return {'action': 'HOLD'}
            
            # 2. FIX 2: ADX Rising (Nacimiento de tendencia)
            # No pedimos > 25, pedimos que esté subiendo
            adx_rising = curr['ADX'] > prev['ADX']
            
            # 3. FIX 3: Momentum Temprano
            # Precio sube vs vela anterior Y está sobre la media (zona alcista)
            momentum_up = (curr['Close'] > prev['Close']) and (curr['Close'] > curr['BB_Mid'])
            
            # 4. Confirmación extra: Que el precio esté por encima de la banda superior?
            # Opcional. Tu Fix 3 dice "Momentum temprano", así que con momentum_up basta.
            # Pero para seguridad, pedimos que NO esté colapsando la volatilidad (BB Width expandiendose levemente es bueno)
            
            if adx_rising and momentum_up:
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