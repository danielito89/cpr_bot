import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
import joblib

def train_cortex_v8():
    print("🧠 ENTRENANDO CORTEX V8 (UNIVERSAL)...")
    
    csv_file = "cortex_training_data_v8.csv"
    try:
        df = pd.read_csv(csv_file)
    except:
        print(f"❌ No se encontró {csv_file}")
        return

    # Definir X e y
    # Nota: feat_vol_norm aparece dos veces en el miner por error tipográfico en la lista de guardado?
    # Revisemos el miner: clean_df usa 'feat_vol_norm' dos veces si no cambiamos nombres.
    # CORRECCION: En el miner, el volumen se llama 'feat_vol_norm' igual que la volatilidad.
    # EL MINER DE ARRIBA TIENE UN BUG DE NOMBRE. CORREGIR EN EL MINER O AQUI.
    # ASUMIENDO QUE EL MINER SE GUARDA CORRECTO, LAS COLUMNAS SON:
    # 'feat_vol_norm' (Volatilidad) y 'feat_vol_norm.1' (Volumen) si pandas duplica.
    # PARA EVITAR ESTO, CAMBIARÉ EL MINER DE ARRIBA EN TU COPIA:
    # En el miner, cambia df['feat_vol_norm'] = df['volume']... por df['feat_volume_norm']
    
    # ASUMIENDO NOMBRES CORRECTOS (Ver abajo nota importante)
    X = df.iloc[:, :-1] # Todas menos Target
    y = df['TARGET']
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    print(f"⚙️ Features de entrada: {list(X.columns)}")
    
    # Configuración Balanceada
    clf = RandomForestClassifier(
        n_estimators=100,
        max_depth=10, 
        class_weight='balanced',
        n_jobs=-1,
        random_state=42
    )
    
    clf.fit(X_train, y_train)
    
    print("\n📊 REPORTE DE RENDIMIENTO:")
    y_pred = clf.predict(X_test)
    print(classification_report(y_test, y_pred, target_names=['SNIPER', 'FLOW', 'WAIT']))
    
    # Guardar
    joblib.dump(clf, "cortex_model_v8.joblib", compress=3)
    print("✅ CEREBRO V8 GUARDADO.")

if __name__ == "__main__":
    train_cortex_v8()