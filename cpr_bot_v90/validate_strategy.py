import pandas as pd
import pandas_ta as ta
import numpy as np
import matplotlib.pyplot as plt

# ==========================================
# 1. CONFIGURACIÓN GANADORA (A PROBAR EN 1M)
# ==========================================
PARAMS = {
    'strict_volume_factor': 22.0,       # Ojo: Este valor podría ser altísimo para 1m
    'breakout_tp_mult': 11.0,
    'trailing_stop_trigger_atr': 1.94,
    'volume_factor': 1.18,              # Factor base
    'atr_length': 14                    # Estándar, ajusta si usas otro
}

# RUTA A TU DATA DE 1 MINUTO (Ajusta esto)
FILE_PATH = 'data/mainnet_data_1m_ETHUSDT.csv' 

# ==========================================
# 2. LÓGICA DE SIMULACIÓN RÁPIDA
# ==========================================
def run_visual_backtest(file_path, params):
    print(f"📂 Cargando datos de: {file_path}...")
    try:
        df = pd.read_csv(file_path)
    except FileNotFoundError:
        print("❌ Error: No se encuentra el archivo CSV de 1 minuto.")
        return

    # Limpieza básica
    df.columns = [x.lower() for x in df.columns]
    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    
    # Indicadores Necesarios
    print("🧮 Calculando indicadores...")
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=params['atr_length'])
    df['vol_ma'] = ta.sma(df['volume'], length=20) # Asumiendo SMA 20 para el volumen promedio
    
    # Lógica de Entrada (Simplificada basada en tus params)
    # Condición: Volumen actual > Promedio * Strict Factor
    # NOTA: Aquí asumo que 'strict_volume_factor' es el filtro principal de entrada
    df['entry_signal'] = df['volume'] > (df['vol_ma'] * params['strict_volume_factor'])

    # Simulación de Trades
    balance = 1000  # Capital inicial
    equity_curve = [balance]
    position = None # None, 'long'
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    highest_price = 0 # Para el Trailing
    
    trades = []

    print("🚀 Ejecutando simulación vela a vela...")
    for i in range(1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i-1]
        
        if pd.isna(row['atr']): continue

        # --- GESTIÓN DE POSICIÓN ABIERTA ---
        if position == 'long':
            # Actualizar Trailing Stop
            if row['high'] > highest_price:
                highest_price = row['high']
                # Mueve el SL si el precio sube (Trailing)
                new_sl = highest_price - (row['atr'] * params['trailing_stop_trigger_atr'])
                if new_sl > stop_loss:
                    stop_loss = new_sl
            
            # Chequeo de Salida (SL o TP)
            if row['low'] <= stop_loss:
                # Salida por Stop Loss (o Trailing Stop)
                pnl = (stop_loss - entry_price) / entry_price * 100
                balance = balance * (1 + pnl/100)
                trades.append({'type': 'SL/Trail', 'pnl': pnl, 'bar': i})
                position = None
            elif row['high'] >= take_profit:
                # Salida por TP
                pnl = (take_profit - entry_price) / entry_price * 100
                balance = balance * (1 + pnl/100)
                trades.append({'type': 'TP', 'pnl': pnl, 'bar': i})
                position = None
        
        # --- GESTIÓN DE ENTRADA ---
        elif position is None:
            if row['entry_signal']:
                position = 'long'
                entry_price = row['close']
                highest_price = entry_price
                # Definir TP y SL inicial
                atr_val = row['atr']
                take_profit = entry_price + (atr_val * params['breakout_tp_mult'])
                stop_loss = entry_price - (atr_val * params['trailing_stop_trigger_atr']) # SL inicial = distancia del trailing

        equity_curve.append(balance)

    # ==========================================
    # 3. RESULTADOS Y GRÁFICO
    # ==========================================
    total_trades = len(trades)
    if total_trades > 0:
        winners = len([t for t in trades if t['pnl'] > 0])
        wr = (winners / total_trades) * 100
        pnl_net = balance - 1000
        print(f"\n📊 RESULTADOS EN 1M:")
        print(f"   Trades: {total_trades}")
        print(f"   Win Rate: {wr:.2f}%")
        print(f"   Balance Final: ${balance:.2f} (PnL: ${pnl_net:.2f})")
        
        # Graficar
        plt.figure(figsize=(12, 6))
        plt.plot(equity_curve, label='Capital (Equity)')
        plt.title(f'Backtest Visual (1m) - Vol Factor: {params["strict_volume_factor"]}')
        plt.xlabel('Velas')
        plt.ylabel('Capital USDT')
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.show()
    else:
        print("\n⚠️ 0 Trades realizados.")
        print("   Diagnóstico: El 'strict_volume_factor' de 22 es demasiado alto para velas de 1m.")
        print("   Sugerencia: Prueba bajarlo a valores entre 2.0 y 5.0 para 1m.")

if __name__ == "__main__":
    # Asegúrate de tener instalado: pip install pandas pandas_ta matplotlib
    run_visual_backtest(FILE_PATH, PARAMS)