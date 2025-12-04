import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# ==========================================
# 1. CONFIGURACIÓN GANADORA (A PROBAR EN 1M)
# ==========================================
PARAMS = {
    'strict_volume_factor': 5.0,       # Valor obtenido del optimizer de 1H
    'breakout_tp_mult': 11.0,
    'trailing_stop_trigger_atr': 1.94,
    'volume_factor': 1.18,              
    'atr_length': 14                    
}

# RUTA A TU DATA DE 1 MINUTO (Asegúrate de que esta ruta sea correcta)
FILE_PATH = '/home/orangepi/bot_cpr/cpr_bot_v90/data/mainnet_data_1m_ETHUSDT.csv' 

# ==========================================
# 2. FUNCIONES DE CÁLCULO MANUAL (SIN PANDAS_TA)
# ==========================================
def calculate_atr(df, length=14):
    """Calcula ATR manualmente usando Pandas"""
    high = df['high']
    low = df['low']
    close = df['close']
    
    # 1. Calcular True Range (TR)
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # 2. Calcular ATR (usando media móvil exponencial suavizada estilo Wilder)
    # alpha = 1/length simula el suavizado de Wilder
    atr = tr.ewm(alpha=1/length, adjust=False).mean()
    return atr

def calculate_sma(series, length):
    """Calcula Media Móvil Simple manualmente"""
    return series.rolling(window=length).mean()

# ==========================================
# 3. LÓGICA DE SIMULACIÓN
# ==========================================
def run_visual_backtest(file_path, params):
    print(f"📂 Cargando datos de: {file_path}...")
    try:
        df = pd.read_csv(file_path)
    except FileNotFoundError:
        print(f"❌ Error: No se encuentra el archivo en: {file_path}")
        return

    # Limpieza de nombres de columnas
    df.columns = [x.lower().strip() for x in df.columns]
    
    # Asegurar tipos numéricos
    cols_to_float = ['open', 'high', 'low', 'close', 'volume']
    for col in cols_to_float:
        if col in df.columns:
            df[col] = df[col].astype(float)

    # ------------------------------------------------
    # CÁLCULO DE INDICADORES (Nativo, sin librerías extra)
    # ------------------------------------------------
    print("🧮 Calculando indicadores (modo nativo)...")
    
    # ATR
    df['atr'] = calculate_atr(df, length=params['atr_length'])
    
    # Volume SMA (Usamos 20 periodos como estándar si no se especifica otro)
    df['vol_ma'] = calculate_sma(df['volume'], length=20) 
    
    # Lógica de Entrada
    # Condición: Volumen actual > Promedio * Strict Factor
    df['entry_signal'] = df['volume'] > (df['vol_ma'] * params['strict_volume_factor'])

    # Simulación de Trades
    balance = 1000  # Capital inicial
    equity_curve = [balance]
    position = None # None, 'long'
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    highest_price = 0 
    
    trades = []

    print(f"🚀 Ejecutando simulación vela a vela sobre {len(df)} velas...")
    for i in range(1, len(df)):
        row = df.iloc[i]
        
        # Saltar si no hay datos suficientes para ATR o MA
        if pd.isna(row['atr']) or pd.isna(row['vol_ma']): 
            equity_curve.append(balance)
            continue

        # --- GESTIÓN DE POSICIÓN ABIERTA ---
        if position == 'long':
            # Actualizar Trailing Stop
            if row['high'] > highest_price:
                highest_price = row['high']
                new_sl = highest_price - (row['atr'] * params['trailing_stop_trigger_atr'])
                if new_sl > stop_loss:
                    stop_loss = new_sl
            
            # Chequeo de Salida
            if row['low'] <= stop_loss:
                pnl = (stop_loss - entry_price) / entry_price * 100
                balance = balance * (1 + pnl/100)
                trades.append({'type': 'SL/Trail', 'pnl': pnl, 'bar': i})
                position = None
            elif row['high'] >= take_profit:
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
                atr_val = row['atr']
                take_profit = entry_price + (atr_val * params['breakout_tp_mult'])
                stop_loss = entry_price - (atr_val * params['trailing_stop_trigger_atr'])

        equity_curve.append(balance)

    # ==========================================
    # 4. RESULTADOS
    # ==========================================
    total_trades = len(trades)
    print("\n" + "="*40)
    print("📊 RESULTADOS DEL BACKTEST RAPIDO")
    print("="*40)
    
    if total_trades > 0:
        winners = len([t for t in trades if t['pnl'] > 0])
        wr = (winners / total_trades) * 100
        pnl_net = balance - 1000
        
        print(f"✅ Trades Totales: {total_trades}")
        print(f"✅ Win Rate: {wr:.2f}%")
        print(f"✅ Balance Final: ${balance:.2f}")
        print(f"✅ PnL Neto: ${pnl_net:.2f}")
        
        # Graficar (Si tienes entorno gráfico, sino guardará imagen)
        try:
            plt.figure(figsize=(12, 6))
            plt.plot(equity_curve, label='Capital (Equity)')
            plt.title(f'Backtest (1m) - Vol Factor: {params["strict_volume_factor"]}')
            plt.xlabel('Velas')
            plt.ylabel('Capital USDT')
            plt.legend()
            plt.grid(True, alpha=0.3)
            # Guardar en archivo por si no hay pantalla
            plt.savefig('backtest_result.png') 
            print("\n📈 Gráfico guardado como 'backtest_result.png'")
        except Exception as e:
            print(f"No se pudo graficar: {e}")
            
    else:
        print("⚠️ 0 Trades realizados.")
        print(f"🔍 Diagnóstico: El 'strict_volume_factor' ({params['strict_volume_factor']}) es demasiado alto para los datos de 1m.")
        print("💡 Sugerencia: El volumen en 1m es mucho más ruidoso y menor que en 1h.")

if __name__ == "__main__":
    run_visual_backtest(FILE_PATH, PARAMS)