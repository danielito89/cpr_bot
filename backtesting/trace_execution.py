import sys
import os
import pandas as pd
import numpy as np

# --- 1. CONFIGURACIÓN ---
PROJECT_ROOT = "/home/orangepi/bot_cpr"
if PROJECT_ROOT not in sys.path: sys.path.append(PROJECT_ROOT)

from bots.breakout.strategy import BreakoutBotStrategy

# Configuramos PEPE con el parámetro 1.8 YA INCRUSTADO
SYMBOL = "1000PEPE_USDT"
TF = "1h"
PARAMS = {'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.8}

print(f"🕵️ INICIANDO RASTREO PARA {SYMBOL}...")
print(f"⚙️ Parámetro Vol Multiplier: {PARAMS['vol_multiplier']} (Debe ser 1.8)")

# --- 2. CARGA DE DATOS ---
csv_path = os.path.join(PROJECT_ROOT, "backtesting", "data", f"{SYMBOL}_{TF}_FULL.csv")
if not os.path.exists(csv_path):
    print("❌ No encuentro el CSV.")
    sys.exit()

df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
df.columns = [c.strip().capitalize() for c in df.columns] # High, Low, Close...

# --- 3. PREPARACIÓN IDÉNTICA A LA SIMULACIÓN ---
# Calculamos indicadores GLOBALES
strat = BreakoutBotStrategy()
strat.vol_multiplier = PARAMS['vol_multiplier']
strat.sl_atr = PARAMS['sl_atr']

print("🛠️ Calculando indicadores globales...")
df = strat.calculate_indicators(df)

# Sincronización (Lo mismo que hace el debug_sim)
df = df.resample('1h').ffill()

# --- 4. ZONA CERO: 16 DE MAYO 2023 ---
target_date = "2023-05-16 00:00:00"
print(f"\n🔬 ENFOCANDO MICROSCOPIO EN: {target_date}")

if target_date not in df.index:
    print("❌ La fecha objetivo no existe en el índice después del resample.")
    sys.exit()

# Extraemos la ventana exacta que vería el bot
idx_loc = df.index.get_loc(target_date)
window = df.iloc[idx_loc-50 : idx_loc+1] # 51 velas

# Datos de la vela actual
curr = window.iloc[-1]
resistance = curr.get('Resistance', np.nan)
vol_sma = curr.get('Vol_SMA', np.nan)

print("\n📊 DATOS QUE VE EL BOT:")
print(f"   Precio Cierre:    {curr['Close']}")
print(f"   Resistencia:      {resistance}")
print(f"   Volumen Actual:   {curr['Volume']:,.0f}")
print(f"   Volumen Promedio: {vol_sma:,.0f}")
print(f"   Volumen Requerido: {vol_sma * strat.vol_multiplier:,.0f} (SMA x {strat.vol_multiplier})")

# Chequeo manual de lógica
breakout_price = curr['Close'] > resistance
breakout_vol = curr['Volume'] > (vol_sma * strat.vol_multiplier)

print(f"\n🧠 LÓGICA INTERNA:")
print(f"   ¿Rompió Precio?   {'✅ SI' if breakout_price else '❌ NO'}")
print(f"   ¿Rompió Volumen?  {'✅ SI' if breakout_vol else '❌ NO'}")

# --- 5. CONSULTA FINAL A LA ESTRATEGIA ---
print("\n🤖 LLAMANDO A strategy.get_signal()...")
state = {'status': 'WAITING_BREAKOUT'}

try:
    signal = strat.get_signal(window, state)
    print(f"\n📢 RESULTADO FINAL: {signal['action']}")
    
    if signal['action'] == 'ENTER_LONG':
        print("🎉 ¡EUREKA! El sistema funciona con 1.8.")
        print("👉 Si debug_sim dio 0 trades, es porque no tenía el '1.8' actualizado en su código.")
    else:
        print("💀 SIGUE DANDO HOLD. Hay algo más bloqueando (quizás ATR o Tendencia).")
        # Si sigue fallando, imprimimos todas las columnas para ver si hay un NaN raro
        print("\n🔍 DUMP DE LA ÚLTIMA VELA:")
        print(curr)

except Exception as e:
    print(f"💥 LA ESTRATEGIA CRASHEÓ: {e}")
    import traceback
    traceback.print_exc()