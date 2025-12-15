import pandas as pd
import numpy as np
import os

import pandas as pd
import numpy as np
import os

# ======================================================
#  🏦 CONFIG V3 - SORTINO & DRAWDOWN ANALYSIS
# ======================================================
FILES = {
    "BTC": "data/funding_BTCUSDT.csv",
    "ETH": "data/funding_ETHUSDT.csv",
    "SOL": "data/funding_SOLUSDT.csv",
    "PEPE": "data/funding_1000PEPEUSDT.csv"
}

INITIAL_CAPITAL = 10000
NEGATIVE_PENALTY = 2.0  
ENTRY_EXIT_COST = 0.004 # 0.4% Roundtrip

def analyze_asset(symbol, filepath):
    if not os.path.exists(filepath):
        print(f"⚠️ Falta archivo para {symbol}")
        return None

    df = pd.read_csv(filepath)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    df = df[df.index >= '2023-01-01']
    
    # 1. Ajuste de Tasas (Penalización)
    df['adjusted_payout'] = np.where(
        df['fundingRate'] < 0,
        df['fundingRate'] * NEGATIVE_PENALTY, 
        df['fundingRate']
    )
    
    # 2. Equity Curve & Drawdown
    balance = INITIAL_CAPITAL * (1 - ENTRY_EXIT_COST)
    equity = []
    
    for payout_pct in df['adjusted_payout']:
        balance += (balance * payout_pct)
        equity.append(balance)
        
    # Costo de salida final
    final_balance = balance * (1 - ENTRY_EXIT_COST)
    equity[-1] = final_balance
    
    # Cálculo de Drawdown sobre la curva de equity
    equity_series = pd.Series(equity)
    rolling_max = equity_series.cummax()
    drawdown = (equity_series - rolling_max) / rolling_max
    max_dd_pct = drawdown.min() * 100  # Será un número negativo
    
    total_ret = (final_balance - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    # 3. Métricas Avanzadas (Sortino Style)
    # Convertimos a tasas anualizadas para el cálculo estadístico
    annualized_rates = df['fundingRate'] * 3 * 365 * 100
    mean_apr = annualized_rates.mean()
    
    # Downside Deviation: Solo nos importa la volatilidad de los días malos (o neutrales)
    # Definimos "Malo" como cualquier funding < 0 (pagar)
    # Si todo es positivo, usamos un epsilon pequeño para no dividir por cero
    negative_rates = annualized_rates[annualized_rates < 0]
    
    if len(negative_rates) > 0:
        downside_std = negative_rates.std()
    else:
        # Si nunca fue negativo, usamos la std general pero muy baja
        downside_std = annualized_rates.std() * 0.1 
        
    if pd.isna(downside_std) or downside_std == 0: downside_std = 1.0 # Safety
        
    # Efficiency Score V3 (Sortino Proxy)
    efficiency = mean_apr / downside_std
    
    return {
        "Symbol": symbol,
        "Efficiency": efficiency,
        "Total Ret %": total_ret,
        "Avg APR %": mean_apr,
        "Downside Vol": downside_std,
        "Max DD %": max_dd_pct,
        "Neg Days": (df['fundingRate'] < 0).sum()
    }

def main():
    print(f"📊 ANALISIS DE YIELD V3 (SORTINO & DRAWDOWN)")
    print("="*95)
    
    results = []
    for sym, path in FILES.items():
        res = analyze_asset(sym, path)
        if res: results.append(res)
        
    df_res = pd.DataFrame(results)
    df_res = df_res.sort_values("Efficiency", ascending=False)
    
    pd.options.display.float_format = '{:.2f}'.format
    cols = ["Symbol", "Efficiency", "Total Ret %", "Max DD %", "Avg APR %", "Downside Vol", "Neg Days"]
    
    print(df_res[cols].to_string(index=False))
    
    print("\n💡 INTERPRETACIÓN:")
    print("   Efficiency = Retorno por unidad de Riesgo de Tasa Negativa.")
    print("   Max DD = La peor caída acumulada en tu cuenta de 'Renta Fija'.")

    # Allocator Sugerido (Muy simple basado en eficiencia relativa)
    print("\n⚖️ ALLOCATION SUGERIDO (Basado en Efficiency Score):")
    total_score = df_res['Efficiency'].sum()
    df_res['Alloc %'] = (df_res['Efficiency'] / total_score) * 100
    print(df_res[["Symbol", "Alloc %"]].to_string(index=False))

if __name__ == "__main__":
    main()