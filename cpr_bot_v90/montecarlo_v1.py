#!/usr/bin/env python3
"""
montecarlo_v1.py
Motor de Stress Test Vectorizado para CPR Bot
"""

import os
import argparse
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# Configuración visual
plt.style.use('dark_background')

def load_trades(path):
    df = pd.read_csv(path)
    if 'return_pct' not in df.columns:
        # Si no existe, intentamos calcularla si están los datos necesarios
        if 'pnl_usd' in df.columns and 'balance' in df.columns:
            print("⚠️ Calculando retornos % basados en PnL/Balance...")
            df['prev_balance'] = df['balance'] - df['pnl_usd']
            df['return_pct'] = df['pnl_usd'] / df['prev_balance']
        else:
            raise ValueError("El CSV debe tener columna 'return_pct' o 'pnl_usd' + 'balance'")
    return df['return_pct'].values

def run_vectorized_mc(returns, n_sims, n_steps, initial_balance, block_size=1):
    """
    Ejecuta simulaciones usando álgebra lineal (Mucho más rápido que bucles).
    """
    print(f"⚡ Ejecutando {n_sims} simulaciones de {n_steps} trades...")
    
    # 1. Crear matriz de índices aleatorios
    # Shape: (n_sims, n_steps)
    if block_size == 1:
        # Muestreo simple con reemplazo
        random_indices = np.random.randint(0, len(returns), size=(n_sims, n_steps))
        sim_returns = returns[random_indices]
    else:
        # Block Bootstrap (Para preservar rachas/correlación serial)
        # Es complejo vectorizar block puro, hacemos semi-vectorizado rápido
        sim_returns = np.zeros((n_sims, n_steps))
        n_blocks = int(np.ceil(n_steps / block_size))
        
        for i in range(n_sims):
            # Elegimos índices de inicio de bloque aleatorios
            start_indices = np.random.randint(0, len(returns) - block_size, size=n_blocks)
            # Construimos el path copiando bloques
            path = []
            for start in start_indices:
                path.extend(returns[start : start + block_size])
            sim_returns[i, :] = path[:n_steps]

    # 2. Matriz de Factores de Crecimiento (1 + r)
    growth_factors = 1 + sim_returns
    
    # 3. Cumulative Product (Interés Compuesto)
    # axis=1 calcula el acumulado a lo largo de los trades para cada simulación
    equity_curves = initial_balance * np.cumprod(growth_factors, axis=1)
    
    # Insertar el balance inicial al principio de cada curva (Columna 0)
    start_col = np.full((n_sims, 1), initial_balance)
    equity_curves = np.hstack([start_col, equity_curves])
    
    return equity_curves

def calculate_drawdowns(equity_curves):
    """Calcula Max Drawdown para cada simulación vectorizadamente"""
    # Máximo acumulado hasta el momento
    running_max = np.maximum.accumulate(equity_curves, axis=1)
    # Drawdown actual
    drawdowns = (equity_curves - running_max) / running_max
    # Max Drawdown (mínimo valor negativo)
    max_dds = np.min(drawdowns, axis=1) * 100 # En porcentaje
    return max_dds

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', required=True, help='CSV generado por backtester_v18')
    parser.add_argument('--sims', type=int, default=5000, help='Número de simulaciones')
    parser.add_argument('--balance', type=float, default=1000.0, help='Balance Inicial')
    parser.add_argument('--mode', choices=['simple', 'block'], default='simple', help='Modo de muestreo')
    parser.add_argument('--block', type=int, default=10, help='Tamaño de bloque para conservar rachas')
    
    args = parser.parse_args()
    
    # 1. Cargar Datos
    returns = load_trades(args.csv)
    n_steps = len(returns)
    
    # 2. Motor Monte Carlo
    block_size = args.block if args.mode == 'block' else 1
    equity_curves = run_vectorized_mc(returns, args.sims, n_steps, args.balance, block_size)
    
    # 3. Estadísticas
    final_balances = equity_curves[:, -1]
    max_drawdowns = calculate_drawdowns(equity_curves)
    
    # Probabilidad de Ruina (Perder >90% del capital)
    ruin_threshold = args.balance * 0.10
    ruin_count = np.sum(np.min(equity_curves, axis=1) < ruin_threshold)
    ruin_prob = (ruin_count / args.sims) * 100
    
    # Métricas Clave
    median_bal = np.median(final_balances)
    worst_case_bal = np.percentile(final_balances, 1) # 1% worst case
    worst_dd = np.min(max_drawdowns) # El peor DD de todas las sims
    avg_dd = np.mean(max_drawdowns)
    
    print("\n" + "="*50)
    print(f"🎲 RESULTADOS MONTE CARLO ({args.sims} Sims)")
    print("="*50)
    print(f"💰 Balance Inicial:   ${args.balance:,.2f}")
    print(f"📊 Balance Mediano:   ${median_bal:,.2f}")
    print(f"💀 Worst Case (1%):   ${worst_case_bal:,.2f}")
    print("-" * 50)
    print(f"📉 Drawdown Promedio: {avg_dd:.2f}%")
    print(f"💣 Worst Drawdown:    {worst_dd:.2f}%")
    print(f"🔥 Prob. Ruina Total: {ruin_prob:.2f}% (< ${ruin_threshold:.0f})")
    print("="*50)

    # 4. Gráficos
    plt.figure(figsize=(14, 8))
    
    # Subplot 1: Spaghetti Plot (Primeras 100 sims)
    plt.subplot(2, 2, 1)
    # Ploteamos solo las primeras 100 para no matar la RAM gráfica
    plt.plot(equity_curves[:100].T, color='cyan', alpha=0.1, linewidth=1)
    plt.plot(np.median(equity_curves, axis=0), color='white', linewidth=2, label='Mediana')
    plt.title('Trayectorias Posibles (Primeras 100)')
    plt.yscale('log')
    plt.grid(True, alpha=0.3)
    
    # Subplot 2: Histograma Balances Finales
    plt.subplot(2, 2, 2)
    plt.hist(final_balances, bins=50, color='lime', alpha=0.7, log=True)
    plt.axvline(args.balance, color='red', linestyle='--')
    plt.title('Distribución de Balance Final (Log Scale)')
    plt.xlabel('Balance USD')
    
    # Subplot 3: Histograma Drawdowns
    plt.subplot(2, 2, 3)
    plt.hist(max_drawdowns, bins=50, color='orange', alpha=0.7)
    plt.title('Distribución de Max Drawdowns')
    plt.xlabel('% Caída')
    
    # Subplot 4: Cono de Incertidumbre
    plt.subplot(2, 2, 4)
    p05 = np.percentile(equity_curves, 5, axis=0)
    p50 = np.percentile(equity_curves, 50, axis=0)
    p95 = np.percentile(equity_curves, 95, axis=0)
    x = np.arange(len(p50))
    plt.plot(x, p50, color='white', label='Mediana')
    plt.fill_between(x, p05, p95, color='cyan', alpha=0.2, label='90% Confianza')
    plt.title('Cono de Confianza 90%')
    plt.yscale('log')
    plt.legend()
    
    plt.tight_layout()
    output_img = "montecarlo_results.png"
    plt.savefig(output_img)
    print(f"📸 Gráfico guardado: {output_img}")
    plt.show()

if __name__ == "__main__":
    main()