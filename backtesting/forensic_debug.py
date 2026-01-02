import sys
import os
import pandas as pd
import numpy as np

print("🟢 INICIANDO ANÁLISIS FORENSE...")

# --- 1. CONFIGURACIÓN ---
PROJECT_ROOT = "/home/orangepi/bot_cpr"
if PROJECT_ROOT not in sys.path: sys.path.append(PROJECT_ROOT)

from bots.breakout.strategy import BreakoutBotStrategy

# Usaremos PEPE porque sabemos que tiene muchos trades
SYMBOL = "1000PEPE_USDT"
TF = "1h"
CSV_PATH = os.path.join(PROJECT_ROOT, "backtesting", "data", f"{SYMBOL}_{TF}_FULL.csv")

# --- 2. CARGA DE DATOS ---
if not os.path.exists(CSV_PATH):
    print(f"❌ NO SE ENCUENTRA EL ARCHIVO: {CSV_PATH}")
    sys.exit()

print(f"📂 Cargando {CSV_PATH}...")
df = pd.read_csv(CSV_PATH, index_col=0, parse_dates=True)

# Limpieza de columnas (Igual que en la simulación)
df.columns = [c.strip().capitalize() for c in df.columns]
print(f"📊 Columnas detectadas: {list(df.columns)}")

# --- 3. PREPARAR ESTRATEGIA ---
strategy = BreakoutBotStrategy()
# Parámetros 'Gold' para PEPE
strategy.sl_atr = 2.5
strategy.tp_partial_atr = 6.0
strategy.trailing_dist_atr = 3.5
strategy.vol_multiplier = 1.9

print("🛠️ Calculando indicadores...")
try:
    df = strategy.calculate_indicators(df)
    # Verificamos si se crearon las columnas críticas
    required_cols = ['Resistance', 'Atr'] # Ajustar según tu strategy.py
    print(f"✅ Indicadores calculados. Columnas actuales: {list(df.columns)}")
except Exception as e:
    print(f"❌ ERROR CRÍTICO calculando indicadores: {e}")
    sys.exit()

# --- 4. BÚSQUEDA DEL "PACIENTE CERO" ---
print("\n🔎 BUSCANDO EL PRIMER BREAKOUT POTENCIAL...")

found_candidate = False

# Iteramos hasta encontrar una vela donde el Cierre > Resistencia
# Empezamos en 200 para tener datos previos
for i in range(200, len(df)):
    curr = df.iloc[i]
    prev = df.iloc[i-1]
    
    # Buscamos la columna de resistencia. 
    # NOTA: Si tu strategy.py la llama 'resistance' (minúscula), aquí fallará y sabremos por qué.
    try:
        res_val = curr.get('Resistance', curr.get('resistance', None))
        
        if res_val is None:
            print("❌ ERROR: No encuentro la columna 'Resistance' o 'resistance' en el DF.")
            break
            
        close_val = curr['Close']
        
        # ¿Rompió resistencia?
        if close_val > res_val:
            print(f"\n💡 ¡CANDIDATO ENCONTRADO! Fecha: {df.index[i]}")
            print(f"   Precio Close: {close_val}")
            print(f"   Resistencia:  {res_val}")
            print("-" * 30)
            
            # AHORA PREGUNTAMOS A LA ESTRATEGIA QUÉ OPINA
            # Simulamos el entorno de ejecución
            window = df.iloc[i-50 : i+1]
            state = {'status': 'WAITING_BREAKOUT'}
            
            print("🤔 Consultando estrategia.get_signal()...")
            try:
                signal = strategy.get_signal(window, state)
                action = signal['action']
                
                print(f"🤖 LA ESTRATEGIA DIJO: {action}")
                
                if action == 'HOLD':
                    print("❌ RECHAZADO. Analizando por qué:")
                    # Análisis manual de condiciones
                    vol = curr['Volume']
                    # Intentamos adivinar cómo se llama la media de volumen en tu estrategia
                    vol_ma = curr.get('Volume_MA', curr.get('volume_ma', curr.get('Vol_MA', None)))
                    
                    if vol_ma:
                        req_vol = vol_ma * strategy.vol_multiplier
                        print(f"   Volumen Actual: {vol:.2f}")
                        print(f"   Volumen Requerido: {req_vol:.2f} (MA * {strategy.vol_multiplier})")
                        if vol < req_vol:
                            print("   👉 CAUSA: VOLUMEN INSUFICIENTE")
                        else:
                            print("   👉 CAUSA: MISTERIOSA (Tal vez filtro de tendencia o ATR)")
                    else:
                        print("   ⚠️ No encuentro columna de Volume_MA para diagnosticar.")
                
                else:
                    print("✅ ¡SEÑAL VÁLIDA! El sistema funciona, el problema estaba en el bucle del simulador.")

            except Exception as e:
                print(f"❌ La estrategia CRASHEÓ al pedir señal: {e}")
                import traceback
                traceback.print_exc()
            
            found_candidate = True
            break # Solo queremos ver el primero
            
    except Exception as e:
        print(f"❌ Error iterando fila: {e}")
        break

if not found_candidate:
    print("\n⚠️ ALERTA: Recorrí todo el archivo y NO ENCONTRÉ ningún cierre > resistencia.")
    print("Posibles causas:")
    print("1. La columna 'Resistance' está llena de NaN.")
    print("2. La lógica de cálculo de resistencia está mal.")
    # Imprimimos muestra de resistencia
    print(f"Muestra Resistance: {df['Resistance'].dropna().head()}")