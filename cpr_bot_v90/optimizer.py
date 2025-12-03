import itertools
import pandas as pd
import asyncio
import logging
import sys
import os

# Importar tu backtester (Asegúrate que el nombre del archivo sea backtester_v8.py)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from backtester_v8 import BacktesterV8

# Silenciar logs para que corra rápido y limpio
logging.getLogger().setLevel(logging.CRITICAL)

# --- GRILLA DE PARÁMETROS A PROBAR ---
# Aquí defines qué quieres probar. El script probará TODAS las combinaciones.
PARAM_GRID = {
    "VOLUME_FACTOR": [1.1, 1.2],             # Base (A favor)
    "STRICT_VOLUME_FACTOR": [1.5, 2.0, 3.0], # Estricto (En contra)
    "TRAILING_STOP_TRIGGER_ATR": [1.25, 2.0, 5.0], # 5.0 = Sniper (TP Fijo)
    "BREAKOUT_TP_MULT": [1.25, 10.0]         # 1.25 = Sniper, 10.0 = Runner
}

async def run_optimization():
    print("🚀 INICIANDO AUTO-TUNE V9 (Buscando la configuración robusta)...")
    print("=" * 80)
    print(f"{'#':<3} | {'Vol':<3}/{'Str':<3} | {'Trail':<4} | {'TP':<5} | {'PnL Final':<12} | {'PF':<5} | {'Trades':<6} | {'Win%':<5}")
    print("-" * 80)

    # Generar combinaciones
    keys, values = zip(*PARAM_GRID.items())
    combinations = [dict(zip(keys, v)) for v in itertools.product(*values)]
    
    results = []

    for i, params in enumerate(combinations):
        # Instanciar nuevo backtester
        bt = BacktesterV8()
        
        # --- INYECCIÓN DE PARÁMETROS ---
        # Forzamos los valores en el controlador simulado
        bt.controller.volume_factor = params["VOLUME_FACTOR"]
        bt.controller.strict_volume_factor = params["STRICT_VOLUME_FACTOR"]
        bt.controller.trailing_stop_trigger_atr = params["TRAILING_STOP_TRIGGER_ATR"]
        bt.controller.breakout_tp_mult = params["BREAKOUT_TP_MULT"]
        # -------------------------------

        # Ejecutar (Silencioso)
        await bt.run()
        
        # Recolectar Datos
        df = pd.DataFrame(bt.state.trades_history)
        if df.empty:
            pnl, pf, trades, win_rate = 0, 0, 0, 0
        else:
            pnl = df['pnl'].sum()
            
            # --- FIX: Nombre de variable corregido ---
            gross_profit = df[df['pnl'] > 0]['pnl'].sum()  # Antes era gross_win
            gross_loss = abs(df[df['pnl'] < 0]['pnl'].sum())
            
            pf = gross_profit / gross_loss if gross_loss != 0 else 0
            trades = len(df)
            win_rate = (len(df[df['pnl'] > 0]) / trades) * 100

        # Imprimir fila
        print(f"{i+1:<3} | {params['VOLUME_FACTOR']:<3}/{params['STRICT_VOLUME_FACTOR']:<3} | {params['TRAILING_STOP_TRIGGER_ATR']:<4} | {params['BREAKOUT_TP_MULT']:<5} | ${pnl:<11.2f} | {pf:<5.2f} | {trades:<6} | {win_rate:.1f}%")
        
        # Guardar
        res = params.copy()
        res.update({"PnL": pnl, "PF": pf, "Trades": trades, "WinRate": win_rate})
        results.append(res)

    # --- ANÁLISIS FINAL ---
    df_res = pd.DataFrame(results)
    print("\n" + "="*80)
    print("🏆 TOP 5 CONFIGURACIONES (Por Profit Factor - Min 100 trades)")
    print("-" * 80)
    
    # Filtramos setups con pocos trades (ruido)
    valid_setups = df_res[df_res["Trades"] > 100]
    
    if not valid_setups.empty:
        top = valid_setups.sort_values(by="PF", ascending=False).head(5)
        print(top.to_string(index=False))
    else:
        print("⚠️ Ninguna configuración superó los 100 trades. Revisa los datos o filtros.")

    # Guardar CSV
    df_res.to_csv("optimization_results.csv", index=False)
    print("\nResultados guardados en 'optimization_results.csv'")

if __name__ == "__main__":
    # Hack para suprimir prints del backtester
    original_stdout = sys.stdout
    sys.stdout = open(os.devnull, 'w')
    
    try:
        asyncio.run(run_optimization())
    finally:
        sys.stdout.close()
        sys.stdout = original_stdout