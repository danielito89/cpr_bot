import itertools
import pandas as pd
import asyncio
import logging
from backtester_v8 import BacktesterV8, MockBotController, SimulatorState

# Desactivar logs del backtester para que no ensucien la salida del optimizador
logging.getLogger().setLevel(logging.CRITICAL)

# --- PARÁMETROS A OPTIMIZAR ---
PARAM_GRID = {
    "VOLUME_FACTOR": [1.1, 1.2, 1.3],
    "STRICT_VOLUME_FACTOR": [1.5, 2.0],
    "TRAILING_STOP_TRIGGER_ATR": [1.25, 1.5, 2.0],
    "TRAILING_STOP_DISTANCE_ATR": [0.5, 1.0],
    "BREAKOUT_TP_MULT": [1.25, 10.0], # 1.25 = Sniper, 10.0 = Runner
    "RANGING_STRATEGY": [True]        # True = Habilitado
}

async def run_optimization():
    print("🚀 INICIANDO AUTO-TUNE V9...")
    print("=" * 60)
    
    # Generar todas las combinaciones
    keys, values = zip(*PARAM_GRID.items())
    combinations = [dict(zip(keys, v)) for v in itertools.product(*values)]
    
    results = []
    total_combos = len(combinations)
    
    print(f"Probando {total_combos} configuraciones en ETHUSDT (2022-2025)...")
    print(f"{'#':<4} | {'Vol':<4} {'Str':<4} | {'Trail':<9} | {'TP':<5} | {'PnL Final':<12} | {'PF':<5} | {'Trades':<6} | {'DD%':<6}")
    print("-" * 80)

    for i, params in enumerate(combinations):
        # Instanciar Backtester
        bt = BacktesterV8()
        
        # --- INYECCIÓN DE PARÁMETROS ---
        # Sobrescribimos las variables del controlador mock
        bt.controller.volume_factor = params["VOLUME_FACTOR"]
        bt.controller.strict_volume_factor = params["STRICT_VOLUME_FACTOR"]
        bt.controller.trailing_stop_trigger_atr = params["TRAILING_STOP_TRIGGER_ATR"]
        bt.controller.trailing_stop_distance_atr = params["TRAILING_STOP_DISTANCE_ATR"]
        bt.controller.breakout_tp_mult = params["BREAKOUT_TP_MULT"]
        # -------------------------------

        # Ejecutar sin imprimir (silencioso)
        # Nota: Necesitamos que run() no imprima prints normales, 
        # o los ignoramos en la consola.
        await bt.run()
        
        # Recolectar métricas
        df = pd.DataFrame(bt.state.trades_history)
        if df.empty:
            pnl, pf, trades, dd = 0, 0, 0, 0
        else:
            pnl = df['pnl'].sum()
            gross_win = df[df['pnl']>0]['pnl'].sum()
            gross_loss = abs(df[df['pnl']<0]['pnl'].sum())
            pf = gross_profit / gross_loss if gross_loss != 0 else 0
            trades = len(df)
            
            # Calc Drawdown rápido
            equity = [10000]
            peak = 10000
            max_dd = 0
            for r in df['pnl']:
                equity.append(equity[-1] + r)
                if equity[-1] > peak: peak = equity[-1]
                dd = (peak - equity[-1]) / peak
                if dd > max_dd: max_dd = dd
            dd = max_dd * 100

        # Guardar resultado
        res_row = params.copy()
        res_row.update({"PnL": pnl, "PF": pf, "Trades": trades, "DD": dd})
        results.append(res_row)
        
        # Imprimir línea de progreso
        print(f"{i+1:<4} | {params['VOLUME_FACTOR']:<4} {params['STRICT_VOLUME_FACTOR']:<4} | {params['TRAILING_STOP_TRIGGER_ATR']}/{params['TRAILING_STOP_DISTANCE_ATR']:<5} | {params['BREAKOUT_TP_MULT']:<5} | ${pnl:<11.0f} | {pf:<5.2f} | {trades:<6} | {dd:<6.1f}%")

    # --- ANÁLISIS FINAL ---
    df_res = pd.DataFrame(results)
    
    print("\n" + "="*60)
    print("🏆 TOP 5 CONFIGURACIONES (Por PnL)")
    print("="*60)
    print(df_res.sort_values(by="PnL", ascending=False).head(5).to_string(index=False))
    
    print("\n🏆 TOP 5 CONFIGURACIONES (Por Profit Factor - Min 100 trades)")
    df_stable = df_res[df_res["Trades"] > 100]
    print(df_stable.sort_values(by="PF", ascending=False).head(5).to_string(index=False))
    
    # Guardar todo
    df_res.to_csv("optimization_results.csv", index=False)
    print("\nResultados guardados en 'optimization_results.csv'")

if __name__ == "__main__":
    # Pequeño hack para silenciar los prints del backtester v8 si no los comentaste
    import sys, os
    sys.stdout = open(os.devnull, 'w') # Silenciar stdout
    # Restaurar stdout para nuestros prints
    original_stdout = sys.__stdout__
    
    # Función wrapper para imprimir solo lo nuestro
    def print_safe(*args, **kwargs):
        original_stdout.write(" ".join(map(str, args)) + "\n")
    
    # Reemplazar print global (un poco sucio pero efectivo para scripts rápidos)
    import builtins
    builtins.print = print_safe
    
    asyncio.run(run_optimization())