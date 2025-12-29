import joblib
import pandas as pd
import numpy as np
import os

class CortexBrain:
    def __init__(self):
        self.model = None
        self.model_path = os.path.join(os.path.dirname(__file__), 'cortex_model_v1.joblib')
        self.load_brain()

    def load_brain(self):
        try:
            self.model = joblib.load(self.model_path)
            print("🧠 CORTEX V7 Cargado Exitosamente")
        except Exception as e:
            print(f"⚠️ Error cargando CORTEX: {e}")
            self.model = None

    def predict_profile(self, df):
        """
        Recibe el DataFrame actual (últimas velas) y decide el perfil.
        Retorna: 'SNIPER', 'FLOW' o 'WAIT'
        """
        if self.model is None or df.empty:
            return 'SNIPER' # Fallback seguro por defecto
            
        try:
            # Calcular Features (DEBEN SER IDÉNTICOS AL MINER)
            row = df.iloc[-1].copy()
            
            # Recalculamos features on-the-fly para la última vela
            # Nota: Necesitamos algunas velas previas para los promedios
            
            # Volatility
            high_low = df['high'] - df['low']
            # Simplificación para inferencia rápida: ATR rolling simple
            df['ATR'] = high_low.rolling(14).mean()
            feat_volatility = df['ATR'].iloc[-1] / df['close'].iloc[-1]
            
            # Vol Ratio
            vol_ma = df['volume'].rolling(20).mean().iloc[-1]
            feat_vol_ratio = df['volume'].iloc[-1] / vol_ma
            
            # Trend Dev (SMA 50)
            sma50 = df['close'].rolling(50).mean().iloc[-1]
            feat_trend_dev = (df['close'].iloc[-1] - sma50) / df['close'].iloc[-1]
            
            # RSI (Ya suele venir calculado en df['RSI'] por el DataProcessor)
            # Calculamos feature compatible (RSI/100 o raw según como entrenamos)
            # En el miner usamos RSI tal cual (0-100) -> 'feat_rsi'
            feat_rsi = df['RSI'].iloc[-1]
            
            # Armar vector X
            X_live = pd.DataFrame([{
                'feat_volatility': feat_volatility,
                'feat_vol_ratio': feat_vol_ratio,
                'feat_rsi': feat_rsi,
                'feat_trend_dev': feat_trend_dev
            }])
            
            # Predecir
            prediction = self.model.predict(X_live)[0]
            
            if prediction == 0: return 'SNIPER'
            if prediction == 1: return 'FLOW'
            return 'WAIT' # La IA dice que el mercado está feo
            
        except Exception as e:
            print(f"⚠️ Error Inferencia CORTEX: {e}")
            return 'SNIPER' # Fallback