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

plt.style.use('dark_background')

def load_trades(path):
    df = pd.read_csv(path)
    if 'return_pct' not in df.columns:
        if 'pnl_usd' in df.columns and 'balance' in df.columns:
            print("⚠️ Calculando retornos % basados en PnL/Balance...")
            df['prev_balance'] = df['balance'] - df['pnl_usd']
            df['return_pct'] = df['pnl_usd'] / df['prev_balance']
        else:
            raise ValueError("CSV incompleto. Requier 'return_pct'")
    return df['return_pct'].values

def run_vectorized_mc(returns, n_sims, n_steps, initial_balance, block_size=1):
    print(f"⚡ Ejecutando {n_sims} simulaciones de {n_steps} trades...")
    
    if block_size == 1:
        random_indices = np.random.randint(0, len(returns), size=(n_sims, n_steps))
        sim_returns = returns[random_indices]
    else:
        sim_returns = np.zeros((n_sims, n_steps))
        n_blocks = int(np.ceil(n_steps / block_size))
        
        for i in range(n_sims):
            start_indices = np.random.randint(0, len(returns) - block_size, size=n_blocks)
            path = []
            for start in start_indices:
                path.extend(returns[start : start + block_size])
            sim_returns[i, :] = path[:n_steps]

    growth_factors = 1 + sim_returns
    equity_curves = initial_balance * np.cumprod(growth_factors, axis=1)
    
    start_col = np.full((n_sims, 1), initial_balance)
    equity_curves = np.hstack([start_col, equity_curves])
    
    return equity_curves

def calculate_drawdowns(equity_curves):
    running_max = np.maximum.accumulate(equity_curves, axis=1)
    drawdowns = (equity_curves - running_max) / running_max
    max_dds = np.min(drawdowns, axis=1) * 100 
    return max_dds

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', required=True, help='CSV generado por backtester_v18')
    parser.add_argument('--sims', type=int, default=5000)
    parser.add_argument('--balance', type=float, default=1000.0)
    parser.add_argument('--mode', choices=['simple', 'block'], default='block')
    parser.add_argument('--block', type=int, default=10)
    
    args = parser.parse_args()
    
    returns = load_trades(args.csv)
    n_steps = len(returns)
    
    block_size = args.block if args.mode == 'block' else 1
    equity_curves = run_vectorized_mc(returns, args.sims, n_steps, args.balance, block_size)
    
    final_balances = equity_curves[:, -1]
    max_drawdowns = calculate_drawdowns(equity_curves)
    
    ruin_threshold = args.balance * 0.10
    ruin_count = np.sum(np.min(equity_curves, axis=1) < ruin_threshold)
    ruin_prob = (ruin_count / args.sims) * 100
    
    median_bal = np.median(final_balances)
    worst_case_bal = np.percentile(final_balances, 1) 
    worst_dd = np.min(max_drawdowns) 
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

    plt.figure(figsize=(14, 8))
    
    plt.subplot(2, 2, 1)
    plt.plot(equity_curves[:100].T, color='cyan', alpha=0.1, linewidth=1)
    plt.plot(np.median(equity_curves, axis=0), color='white', linewidth=2, label='Mediana')
    plt.title('Trayectorias Posibles (Primeras 100)')
    plt.yscale('log')
    plt.grid(True, alpha=0.3)
    
    plt.subplot(2, 2, 2)
    plt.hist(final_balances, bins=50, color='lime', alpha=0.7, log=True)
    plt.axvline(args.balance, color='red', linestyle='--')
    plt.title('Distribución Final (Log)')
    
    plt.subplot(2, 2, 3)
    plt.hist(max_drawdowns, bins=50, color='orange', alpha=0.7)
    plt.title('Distribución Max Drawdowns')
    
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

if __name__ == "__main__":
    main()