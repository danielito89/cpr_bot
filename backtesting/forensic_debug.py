import sys
import os
import pandas as pd
import numpy as np

# --- 1. CONFIGURACIÓN ---
PROJECT_ROOT = "/home/orangepi/bot_cpr"
if PROJECT_ROOT not in sys.path: sys.path.append(PROJECT_ROOT)

from bots.breakout.strategy import BreakoutBotStrategy

SYMBOL = "1000PEPE_USDT"
TF = "1h"
CSV_PATH = os.path.join(PROJECT_ROOT, "backtesting", "data", f"{SYMBOL}_{TF}_FULL.csv")

print(f"📂 Cargando {CSV_PATH}...")
df = pd.read_csv(CSV_PATH, index_col=0, parse_dates=True)
df.columns = [c.strip().capitalize() for c in df.columns]

# --- 2. PREPARAR ESTRATEGIA ---
strategy = BreakoutBotStrategy()
# Usamos tus params actuales
strategy.vol_multiplier = 1.9 
# (Los demas no importan para la entrada)

print("🛠️ Calculando indicadores...")
df = strategy.calculate_indicators(df)

# --- 3. BÚSQUEDA DEL RECHAZO ---
print("\n🔎 ANALIZANDO POR QUÉ RECHAZA...")

for i in range(200, len(df)):
    curr = df.iloc[i]
    res = curr.get('Resistance')
    
    if curr['Close'] > res:
        # ¡Es un Breakout de Precio!
        vol = curr['Volume']
        vol_ma = curr['Vol_SMA'] # <--- AQUI ESTABA LA CLAVE
        req_vol = vol_ma * strategy.vol_multiplier
        
        print(f"\n📅 FECHA: {df.index[i]}")
        print(f"   Precio: {curr['Close']} > Resistencia {res} (✅ Breakout Precio)")
        
        print(f"   📊 ANÁLISIS DE VOLUMEN:")
        print(f"      Volumen Real:      {vol:,.0f}")
        print(f"      Promedio (SMA20):  {vol_ma:,.0f}")
        print(f"      Multiplicador:     x{strategy.vol_multiplier}")
        print(f"      Volumen Necesario: {req_vol:,.0f}")
        
        if vol > req_vol:
            print("   ✅ CONCLUSIÓN: DEBERÍA ENTRAR.")
            # Si aquí dice entrar, entonces debug_sim fallaba por otra cosa.
        else:
            diff = (req_vol - vol) / req_vol * 100
            print(f"   ❌ CONCLUSIÓN: HOLD. Falta un {diff:.2f}% de volumen.")
        
        # Consultamos a la estrategia oficial para confirmar
        window = df.iloc[i-50 : i+1]
        state = {'status': 'WAITING_BREAKOUT'}
        sig = strategy.get_signal(window, state)
        print(f"   🤖 ESTRATEGIA FINAL: {sig['action']}")
        
        break # Solo vemos el primero