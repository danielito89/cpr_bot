import pandas as pd
import numpy as np

class MeanReversionStrategy:
    def __init__(self):
        self.bb_length = 20
        self.bb_mult = 2.0
        self.rsi_length = 14
        self.rsi_buy = 30
        self.rsi_sell = 70
        
        # Risk (Más conservador, targets cortos)
        self.sl_atr = 3.0
        self.tp_atr = 2.0  # Take Profit fijo rápido

    def calculate_indicators(self, df):
        df = df.copy()
        # Bollinger
        df['BB_Mid'] = df['Close'].rolling(self.bb_length).mean()
        std = df['Close'].rolling(self.bb_length).std()
        df['BB_Upper'] = df['BB_Mid'] + (std * self.bb_mult)
        df['BB_Lower'] = df['BB_Mid'] - (std * self.bb_mult)
        
        # RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(self.rsi_length).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_length).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # ATR
        df['TR'] = np.maximum(df['High'] - df['Low'], 
                              np.maximum(abs(df['High'] - df['Close'].shift()), 
                                         abs(df['Low'] - df['Close'].shift())))
        df['ATR'] = df['TR'].rolling(14).mean()
        return df

    def get_signal(self, window, state):
        curr = window.iloc[-1]
        
        # SALIDA
        if state.get('status') == 'IN_POSITION':
            # Salida por RSI alto o toque de banda superior
            if curr['RSI'] > self.rsi_sell or curr['High'] >= curr['BB_Upper']:
                return {'action': 'EXIT_TP'}
            # SL
            if curr['Low'] <= state['stop_loss']:
                return {'action': 'EXIT_SL'}
            return {'action': 'HOLD'}
            
        # ENTRADA (Contra-tendencia)
        # Precio toca banda inferior Y RSI está sobrevendido
        if curr['Low'] <= curr['BB_Lower'] and curr['RSI'] < self.rsi_buy:
             return {
                 'action': 'ENTER_LONG',
                 'stop_loss': curr['Close'] - (curr['ATR'] * self.sl_atr)
             }
        return {'action': 'HOLD'}

# --- SIMULADOR RÁPIDO INTEGRADO ---
if __name__ == "__main__":
    # Carga datos de SOL y BTC (que ya tienes descargados)
    # ... (Código de carga simplificado para validar la tesis)
    pass