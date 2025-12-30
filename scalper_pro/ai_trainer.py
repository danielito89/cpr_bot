import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
import joblib
import os

# Los mismos pares
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT', 'LTC/USDT']

def train_specialists():
    print("🧠 ENTRENANDO ESPECIALISTAS (1 MODELO POR PAR)...")
    
    for pair in PAIRS:
        safe_pair = pair.replace('/', '')
        csv_file = f"cortex_data_{safe_pair}.csv"
        model_file = f"cortex_model_{safe_pair}.joblib"
        
        print(f"\n⚙️ Entrenando Agente para: {pair}")
        
        if not os.path.exists(csv_file):
            print(f"❌ No encontré {csv_file}, saltando...")
            continue

        df = pd.read_csv(csv_file)
        
        # Limpieza rápida de NaNs por si acaso
        df = df.replace([np.inf, -np.inf], np.nan).dropna()

        X = df[['feat_volatility', 'feat_vol_ratio', 'feat_rsi', 'feat_trend_dev']]
        y = df['TARGET']
        
        # Split
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        
        # Configuración ROBUSTA (Balanced)
        clf = RandomForestClassifier(
            n_estimators=100,
            max_depth=8, # Un poco menos profundo para evitar overfit en data más pequeña
            class_weight='balanced',
            n_jobs=-1,
            random_state=42
        )
        
        clf.fit(X_train, y_train)
        
        # Reporte rápido
        y_pred = clf.predict(X_test)
        report = classification_report(y_test, y_pred, output_dict=True)
        # Solo imprimimos accuracy para no ensuciar, el detalle está en el backtest
        print(f"   🎯 Accuracy: {report['accuracy']:.2f} | Weighted F1: {report['weighted avg']['f1-score']:.2f}")
        
        joblib.dump(clf, model_file, compress=3)
        print(f"   ✅ Guardado: {model_file}")

if __name__ == "__main__":
    train_specialists()