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
        print("❌ Faltan datos. Corre el miner primero.")
        return

    X = df[['feat_volatility', 'feat_vol_ratio', 'feat_rsi', 'feat_trend_dev']]
    y = df['TARGET']
    
    # 2. Split
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    # 3. Configuración del Modelo (CON CORRECCIÓN DE BALANCE)
    print("⚙️ Configurando RandomForest con class_weight='balanced'...")
    
    clf = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,         # Control de Overfitting y Memoria
        class_weight='balanced', # <--- LA CLAVE PARA QUE NO APRENDA SOLO A ESPERAR
        n_jobs=-1,
        random_state=42
    )
    
    # 4. Entrenar
    clf.fit(X_train, y_train)
    
    # 5. Evaluar
    print("\n📊 REPORTE DE RENDIMIENTO (Test Set):")
    y_pred = clf.predict(X_test)
    print(classification_report(y_test, y_pred, target_names=['SNIPER', 'FLOW', 'WAIT']))
    
    # Feature Importance (Para que veas qué está mirando la IA)
    print("\n👀 Importancia de Features:")
    importances = clf.feature_importances_
    features = X.columns
    for feat, imp in zip(features, importances):
        print(f"   {feat}: {imp:.4f}")

    # 6. Guardar
    joblib.dump(clf, "cortex_model_v1.joblib", compress=3)
    print(f"\n✅ CEREBRO GUARDADO: cortex_model_v1.joblib")

if __name__ == "__main__":
    train_cortex()