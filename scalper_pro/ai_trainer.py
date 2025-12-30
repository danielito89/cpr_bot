import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
import joblib
import lightgbm as lgb # <--- EL NUEVO MOTOR

def train_cortex_v9():
    print("🧠 ENTRENANDO CORTEX V9 (LightGBM)...")
    
    try:
        df = pd.read_csv("cortex_training_data_v9.csv")
    except:
        print("❌ Falta el dataset V9.")
        return

    X = df.iloc[:, :-1] # Features
    y = df['TARGET']    # Labels
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    print(f"⚙️ Features: {list(X.columns)}")
    
    # CONFIGURACIÓN LightGBM
    # boosting_type='gbdt': Gradient Boosting Decision Tree
    # class_weight='balanced': Crucial para el desbalance
    
    clf = lgb.LGBMClassifier(
        n_estimators=200,      # Más iteraciones, LGBM es rápido
        learning_rate=0.1,     # Velocidad de aprendizaje estándar
        max_depth=10,          # Control de overfit
        num_leaves=31,         # Estándar LGBM
        class_weight='balanced',
        n_jobs=-1,
        random_state=42,
        verbosity=-1           # Silencioso
    )
    
    clf.fit(X_train, y_train)
    
    print("\n📊 REPORTE DE RENDIMIENTO:")
    y_pred = clf.predict(X_test)
    print(classification_report(y_test, y_pred, target_names=['SNIPER', 'FLOW', 'WAIT']))
    
    # Guardar
    joblib.dump(clf, "cortex_model_v9.joblib", compress=3)
    print("✅ CEREBRO V9 (LightGBM) GUARDADO.")

if __name__ == "__main__":
    train_cortex_v9()