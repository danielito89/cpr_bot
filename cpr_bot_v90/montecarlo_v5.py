#!/usr/bin/env python3
"""
montecarlo_v5_final_patched.py
NIVEL: FINAL QUANT AUDIT (INSTITUTIONAL GRADE)
----------------------------------------------
CORRECCIONES APLICADAS:
1. Fix Syntax Error en Crash Prob.
2. Robust Pool Handling (len > 10 & std > epsilon).
3. Student-t Clipping Relativo (Max(Local, Global)).
4. Pre-clipping Físico (-0.99 a +1.0).
5. Crash Event Override (Salto real de precio).
6. Orden de ejecución corregido (Crash -> Continue).
"""

import os
import argparse
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy import stats

plt.style.use('dark_background')

# --- CONFIGURACIÓN DE MERCADO ---
PROB_SWITCH_REGIME = 0.05 

# Cisnes Negros (Flash Crash)
BASE_CRASH_PROB = 0.001       # 0.1% Base
BASE_CRASH_SEVERITY = -0.35   # -35% Drop instantáneo

# Límites de Liquidez (Tier Limits)
TIER_1_LIMIT = 50000     
TIER_2_LIMIT = 250000    
TIER_3_LIMIT = 1000000   

def load_returns(path):
    df = pd.read_csv(path)
    if 'return_pct' not in df.columns:
        if 'pnl_usd' in df.columns and 'balance' in df.columns:
            print("⚠️ Calculando retornos % históricos...")
            df['prev_balance'] = df['balance'] - df['pnl_usd']
            df['prev_balance'] = df['prev_balance'].replace(0, 1)
            df['return_pct'] = df['pnl_usd'] / df['prev_balance']
        else:
            raise ValueError("CSV inválido. Requiere 'return_pct'.")
    return df['return_pct'].fillna(0).values

def detect_regimes(returns, window=20):
    series = pd.Series(returns)
    rolling_vol = series.rolling(window=window).std().fillna(0)
    
    low_thresh = rolling_vol.quantile(0.33)
    high_thresh = rolling_vol.quantile(0.66)
    
    regime_low = returns[rolling_vol <= low_thresh]
    regime_med = returns[(rolling_vol > low_thresh) & (rolling_vol <= high_thresh)]
    regime_high = returns[rolling_vol > high_thresh]
    
    print(f"📊 Regímenes de Volatilidad:")
    print(f"   🟢 Calm:  {len(regime_low)} muestras (Std: {np.std(regime_low):.4f})")
    print(f"   🟡 Norm:  {len(regime_med)} muestras (Std: {np.std(regime_med):.4f})")
    print(f"   🔴 Panic: {len(regime_high)} muestras (Std: {np.std(regime_high):.4f})")
    
    return [regime_low, regime_med, regime_high]

def apply_multiplicative_slippage(raw_return):
    """
    Slippage multiplicativo robusto.
    """
    # 1. Pre-Clamp Físico (FIX USER #2): 
    # Nadie gana más del 100% (1.0) ni pierde más del 99% (-0.99) en un tick
    raw_return = np.clip(raw_return, -0.99, 1.0) 
    
    base_impact = 0.0006 
    variable_impact = abs(raw_return) * 0.10
    total_impact = base_impact + variable_impact
    
    # Fórmula multiplicativa
    effective_return = (1 + raw_return) * (1 - total_impact) - 1
    return effective_return

def get_adaptive_scaler_linear(current_balance, peak_balance):
    """
    Smooth Linear Adaptive Sizing
    """
    drawdown = 0.0
    if peak_balance > 0:
        drawdown = (peak_balance - current_balance) / peak_balance
    
    scaler = 1.0
    
    if drawdown <= 0.10:
        scaler = 1.0
    elif drawdown <= 0.20:
        scaler = 1.0 - 3.0 * (drawdown - 0.10)
    elif drawdown <= 0.40:
        scaler = 0.7 - 1.0 * (drawdown - 0.20)
    else:
        scaler = 0.5 
        
    # Liquidity Drag
    if current_balance > TIER_3_LIMIT: scaler *= 0.60
    elif current_balance > TIER_2_LIMIT: scaler *= 0.80
    elif current_balance > TIER_1_LIMIT: scaler *= 0.95
        
    return scaler

def run_final_audit(returns, n_sims, n_steps, initial_balance, seed=42):
    np.random.seed(seed)
    print(f"☢️  Ejecutando Audit V5 Patched (Seed {seed})...")
    
    regime_pools = detect_regimes(returns)
    equity_curves = np.zeros((n_sims, n_steps + 1))
    equity_curves[:, 0] = initial_balance
    
    df_student = 3
    global_mean = np.mean(returns)
    global_std = np.std(returns)

    for i in range(n_sims):
        current_balance = initial_balance
        peak_balance = initial_balance
        current_regime = 0 
        
        for t in range(n_steps):
            
            # --- A. CRASH EVENT OVERRIDE (FIX USER #4) ---
            # Probabilidad aumenta en régimen de pánico
            regime_mult = 1.0 + (current_regime * 2.0) 
            current_crash_prob = BASE_CRASH_PROB * regime_mult # FIX USER #1: Sintaxis arreglada
            
            if np.random.random() < current_crash_prob:
                # Severity aleatoria
                severity = BASE_CRASH_SEVERITY * (1.0 + np.random.random() * 0.5)
                # Salto directo de precio (ignora slippage/sizing)
                current_balance *= (1 + severity)
                
                if current_balance > peak_balance: peak_balance = current_balance
                equity_curves[i, t+1] = current_balance
                
                if current_balance < 10: 
                    equity_curves[i, t+1:] = 0
                    break
                continue # FIX: Salta el resto del loop para este step
            
            # --- B. TRANSICIÓN RÉGIMEN ---
            if np.random.random() < PROB_SWITCH_REGIME:
                current_regime = np.random.choice([0, 1, 2])
            
            # --- C. GENERACIÓN STUDENT-T (FIX USER #1 & #3) ---
            pool = regime_pools[current_regime]
            
            # Chequeo robusto para pools vacíos o sin varianza
            if len(pool) > 10 and np.std(pool) > 1e-9:
                sample_mean = np.mean(pool)
                sample_std = np.std(pool, ddof=1)
                
                # Scale Student-t
                scale = sample_std * np.sqrt((df_student - 2) / df_student)
                r = stats.t.rvs(df_student, loc=sample_mean, scale=scale)
                
                # Clipping Relativo Robusto: Max(5*Local, 3*Global)
                limit = max(5 * sample_std, 3 * global_std)
                r = np.clip(r, sample_mean - limit, sample_mean + limit)
            else:
                # Fallback Robusto
                r = np.random.normal(loc=global_mean, scale=max(global_std, 1e-6))
            
            # --- D. SLIPPAGE & SIZING ---
            r_effective = apply_multiplicative_slippage(r)
            pos_scaler = get_adaptive_scaler_linear(current_balance, peak_balance)
            
            r_final = r_effective * pos_scaler
            
            # Actualizar Balance
            current_balance *= (1 + r_final)
            
            if current_balance > peak_balance: peak_balance = current_balance
            equity_curves[i, t+1] = current_balance
            
            if current_balance < 10: 
                equity_curves[i, t+1:] = 0
                break
                
    return equity_curves

def analyze_and_export(equity_curves, initial_balance, args):
    final_balances = equity_curves[:, -1]
    
    running_max = np.maximum.accumulate(equity_curves, axis=1)
    running_max[running_max == 0] = 1 
    drawdowns = (equity_curves - running_max) / running_max
    max_dds = np.min(drawdowns, axis=1) * 100
    
    ruin_prob = (np.sum(final_balances < (initial_balance * 0.1)) / args.sims) * 100
    median_bal = np.median(final_balances)
    
    var_95_val = np.percentile(final_balances, 5)
    cvar_95_val = final_balances[final_balances <= var_95_val].mean()
    
    out_file_npz = f"mc_v5_final_{args.seed}.npz"
    np.savez_compressed(
        out_file_npz, 
        equity=equity_curves, 
        max_dds=max_dds, 
        seed=args.seed,
        params=dict(
            df_student=3,
            crash_prob=BASE_CRASH_PROB,
            crash_severity=BASE_CRASH_SEVERITY,
            switch_prob=PROB_SWITCH_REGIME
        )
    )
    
    return {
        'median': median_bal,
        'worst_1': np.percentile(final_balances, 1),
        'avg_dd': np.mean(max_dds),
        'worst_dd': np.min(max_dds),
        'ruin_prob': ruin_prob,
        'VaR_95': var_95_val,
        'CVaR_95': cvar_95_val,
        'file': out_file_npz
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', required=True)
    parser.add_argument('--sims', type=int, default=2000)
    parser.add_argument('--balance', type=float, default=1000.0)
    parser.add_argument('--seed', type=int, default=777)
    args = parser.parse_args()
    
    returns = load_returns(args.csv)
    
    equity_curves = run_final_audit(returns, args.sims, len(returns), args.balance, args.seed)
    stats = analyze_and_export(equity_curves, args.balance, args)
    
    print("\n" + "="*60)
    print(f"💎 MONTE CARLO V5: FINAL PATCHED (Seed {args.seed})")
    print("="*60)
    print(f"💰 Balance Inicial:    ${args.balance:,.2f}")
    print(f"📊 Balance Mediano:    ${stats['median']:,.2f}")
    print(f"💀 Worst Case (1%):    ${stats['worst_1']:,.2f}")
    print("-" * 60)
    print(f"🛡️  VaR 95% (Risk):     ${stats['VaR_95']:,.2f}")
    print(f"☠️  CVaR 95% (Doom):    ${stats['CVaR_95']:,.2f}")
    print("-" * 60)
    print(f"📉 Drawdown Promedio:  {stats['avg_dd']:.2f}%")
    print(f"💣 Worst Drawdown:     {stats['worst_dd']:.2f}%")
    print(f"🔥 Prob. Ruina Total:  {stats['ruin_prob']:.2f}%")
    print("-" * 60)
    print(f"💾 Audit Data: {stats['file']}")
    print("="*60)

    # Plot
    plt.figure(figsize=(12, 10))
    
    # 1. Cono de Confianza
    plt.subplot(2, 2, 1)
    p05 = np.percentile(equity_curves, 5, axis=0)
    p50 = np.percentile(equity_curves, 50, axis=0)
    p95 = np.percentile(equity_curves, 95, axis=0)
    x = np.arange(len(p50))
    plt.plot(x, p50, color='white', label='Mediana')
    plt.fill_between(x, p05, p95, color='cyan', alpha=0.2, label='90% Confianza')
    plt.title('Equity Projection (Adaptive + Crashes)')
    plt.yscale('log')
    plt.legend()
    
    # 2. Histograma Final Balances (FIX USER #1: No Dummy)
    plt.subplot(2, 2, 2)
    finals = equity_curves[:, -1]
    finals = finals[finals > 10] 
    plt.hist(finals, bins=50, color='lime', alpha=0.7, log=True)
    plt.axvline(args.balance, color='red', linestyle='--', label='Initial')
    plt.axvline(stats['VaR_95'], color='orange', linestyle=':', label='VaR 95%')
    plt.title('Final Balance Distribution')
    plt.legend()

    # 3. Histograma Drawdowns
    plt.subplot(2, 2, 3)
    running_max = np.maximum.accumulate(equity_curves, axis=1)
    running_max[running_max == 0] = 1
    dds = (equity_curves - running_max) / running_max
    min_dds = np.min(dds, axis=1) * 100
    plt.hist(min_dds, bins=50, color='red', alpha=0.7)
    plt.title('Max Drawdown Distribution')

    # 4. Trayectorias
    plt.subplot(2, 2, 4)
    plt.plot(equity_curves[:30].T, alpha=0.5)
    plt.title('30 Trayectorias (Flash Crashes Visibles)')
    plt.yscale('log')

    plt.tight_layout()
    plt.savefig(f'mc_v5_patched_{args.seed}.png')
    print(f"📸 Gráfico: mc_v5_patched_{args.seed}.png")

if __name__ == "__main__":
    main()