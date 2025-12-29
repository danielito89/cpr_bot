import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
import joblib

def train_cortex():
    print("🧠 ENTRENANDO CORTEX (RANDOM FOREST)...")
    
    # 1. Cargar Datos
    try:
        df = pd.read_csv("cortex_training_data.csv")
    except:
        print("❌ No se encontró cortex_training_data.csv. Corre el miner primero.")
        return

    # 2. Separar Features (X) y Target (y)
    X = df[['feat_volatility', 'feat_vol_ratio', 'feat_rsi', 'feat_trend_dev']]
    y = df['TARGET']
    
    # 3. Split Test/Train
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    # 4. Configurar Modelo (OPTIMIZADO PARA LIGHTSAIL 1GB RAM)
    # n_estimators=100 (Suficiente)
    # max_depth=10 (Clave: Limita el tamaño en memoria)
    # n_jobs=-1 (Usa todos los núcleos de la Orange Pi)
    clf = RandomForestClassifier(n_estimators=100, max_depth=10, n_jobs=-1, random_state=42)
    
    # 5. Entrenar
    clf.fit(X_train, y_train)
    
    # 6. Evaluar
    print("\n📊 REPORTE DE RENDIMIENTO:")
    y_pred = clf.predict(X_test)
    print(classification_report(y_test, y_pred, target_names=['SNIPER', 'FLOW', 'WAIT']))
    
    # 7. Guardar Cerebro
    model_filename = "cortex_model_v1.joblib"
    joblib.dump(clf, model_filename, compress=3) # Compresión alta para transferencia
    print(f"✅ Modelo guardado: {model_filename}")
    print("🚀 Listo para enviar a Lightsail.")

if __name__ == "__main__":
    train_cortex()