import pandas as pd
import numpy as np

class BreakoutBotStrategy:
    def __init__(self):
        # Parámetros Squeeze
        self.bb_length = 20
        self.bb_mult = 2.0
        self.kc_length = 20
        self.kc_mult = 1.5
        
        # --- OPT 3: DEJANDO CORRER GANANCIAS ---
        self.sl_atr = 2.0
        self.tp_partial_atr = 5.5      # Antes 4.0 -> Buscamos Home Runs
        self.trailing_dist_atr = 2.5   # Antes 3.0 -> Aseguramos un poco más ajustado al subir
        
        self.adx_period = 14
        self.cooldown_candles = 10 

    def calculate_indicators(self, df):
        df = df.copy()
        
        # 1. ATR
        df['tr0'] = abs(df['High'] - df['Low'])
        df['tr1'] = abs(df['High'] - df['Close'].shift())
        df['tr2'] = abs(df['Low'] - df['Close'].shift())
        df['TR'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()
        
        # 2. Bollinger Bands
        df['BB_Mid'] = df['Close'].rolling(window=self.bb_length).mean()
        df['BB_Std'] = df['Close'].rolling(window=self.bb_length).std()
        df['BB_Upper'] = df['BB_Mid'] + (df['BB_Std'] * self.bb_mult)
        df['BB_Lower'] = df['BB_Mid'] - (df['BB_Std'] * self.bb_mult)
        
        # --- OPT 1: DETECCIÓN DE EXPANSIÓN DE BANDAS ---
        df['BB_Width'] = df['BB_Upper'] - df['BB_Lower']
        # Cambio del ancho respecto a la vela anterior
        df['BB_Width_Change'] = df['BB_Width'] - df['BB_Width'].shift(1)
        
        # 3. Keltner Channels
        df['KC_Upper'] = df['BB_Mid'] + (df['ATR'] * self.kc_mult)
        df['KC_Lower'] = df['BB_Mid'] - (df['ATR'] * self.kc_mult)
        
        # Squeeze Relativo (0.85 factor)
        bb_width = df['BB_Width']
        kc_width = df['KC_Upper'] - df['KC_Lower']
        kc_width = kc_width.replace(0, np.nan)
        df['Squeeze_On'] = bb_width < (kc_width * 0.85)
        
        # 4. ADX & Momentum
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

        # --- ENTRADAS (HYDRA SQUEEZE 2.0) ---
        if status == 'WAITING_BREAKOUT' or status == 'COOLDOWN':
            if status == 'COOLDOWN':
                 last_exit = pd.to_datetime(state_data.get('last_exit_time'))
                 if (curr.name - last_exit).total_seconds() / 3600 < (self.cooldown_candles * 4): 
                     return {'action': 'HOLD'}

            # 1. Venimos de Squeeze reciente?
            recent_squeeze = window['Squeeze_On'].iloc[-13:-1].any()
            if not recent_squeeze: return {'action': 'HOLD'}
            
            # 2. ADX Rising
            adx_rising = curr['ADX'] > prev['ADX']
            
            # 3. Momentum Temprano (Precio sube y está en zona alta)
            momentum_up = (curr['Close'] > prev['Close']) and (curr['Close'] > curr['BB_Mid'])
            
            # 4. OPT 1: BANDAS EXPANDIÉNDOSE (Confirmación Física)
            # La boca del cocodrilo se tiene que abrir.
            bb_expanding = curr['BB_Width_Change'] > 0
            
            if adx_rising and momentum_up and bb_expanding:
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