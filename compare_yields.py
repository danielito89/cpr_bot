import pandas as pd
import numpy as np
import os

# ======================================================
#  🏦 CONFIG V2 - INSTITUTIONAL YIELD ANALYSIS
# ======================================================
# Archivos generados previamente
FILES = {
    "BTC": "data/funding_BTCUSDT.csv",
    "ETH": "data/funding_ETHUSDT.csv",
    "SOL": "data/funding_SOLUSDT.csv",
    "PEPE": "data/funding_1000PEPEUSDT.csv"
}

INITIAL_CAPITAL = 10000
NEGATIVE_PENALTY = 2.0  # Castigo severo: Si pagas funding, pagas doble (simula basis risk)
ENTRY_EXIT_COST = 0.004 # 0.4% Roundtrip (0.1% spot + 0.1% futs entry + salida)

def analyze_asset(symbol, filepath):
    if not os.path.exists(filepath):
        print(f"⚠️ Falta archivo para {symbol}")
        return None

    df = pd.read_csv(filepath)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    
    # Filtrar desde 2023 para igualdad de condiciones
    df = df[df.index >= '2023-01-01']
    
    # 1. Ajuste de Tasas (Lógica de Castigo)
    # Si rate > 0: Cobramos normal
    # Si rate < 0: Pagamos con castigo (simulando que duele más salir o cubrir)
    df['adjusted_payout'] = np.where(
        df['fundingRate'] < 0,
        df['fundingRate'] * NEGATIVE_PENALTY, 
        df['fundingRate']
    )
    
    # 2. Simulación de Balance
    balance = INITIAL_CAPITAL * (1 - ENTRY_EXIT_COST) # Costo entrada
    equity = []
    
    for payout_pct in df['adjusted_payout']:
        cash_flow = balance * payout_pct
        balance += cash_flow
        equity.append(balance)
        
    balance = balance * (1 - ENTRY_EXIT_COST) # Costo salida
    equity[-1] = balance # Actualizar final
    
    total_ret = (balance - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    # 3. Métricas de Calidad (Efficiency)
    # Convertimos a APR para que los números sean legibles
    annualized_rates = df['fundingRate'] * 3 * 365 * 100
    mean_apr = annualized_rates.mean()
    std_apr = annualized_rates.std()
    
    # Funding Efficiency Ratio (Similar a Sharpe)
    # Mean / Std. Cuanto más alto, más "estable" es el pago.
    efficiency = mean_apr / std_apr if std_apr != 0 else 0
    
    # Métricas de Dolor
    negative_events = (df['fundingRate'] < 0).sum()
    worst_event = df['fundingRate'].min() * 100 # En porcentaje nominal
    
    return {
        "Symbol": symbol,
        "Total Ret %": total_ret,
        "Avg APR %": mean_apr,
        "Vol APR %": std_apr,
        "Efficiency": efficiency,
        "Neg Events": negative_events,
        "Max Pain %": worst_event
    }

def main():
    print(f"📊 ANALISIS DE CALIDAD DE FUNDING (V2 PROFESSIONAL)")
    print(f"   Penalty Negativo: x{NEGATIVE_PENALTY} | Costo Roundtrip: {ENTRY_EXIT_COST*100}%")
    print("="*85)
    
    results = []
    for sym, path in FILES.items():
        res = analyze_asset(sym, path)
        if res: results.append(res)
        
    # Crear DataFrame para ranking bonito
    df_res = pd.DataFrame(results)
    
    # Ordenar por Eficiencia (La métrica reina)
    df_res = df_res.sort_values("Efficiency", ascending=False)
    
    # Formateo
    pd.options.display.float_format = '{:.2f}'.format
    
    # Reordenar columnas
    cols = ["Symbol", "Efficiency", "Total Ret %", "Avg APR %", "Vol APR %", "Neg Events", "Max Pain %"]
    print(df_res[cols].to_string(index=False))
    
    print("\n🧐 CONCLUSIÓN RÁPIDA:")
    winner = df_res.iloc[0]
    print(f"   El activo más eficiente es {winner['Symbol']} (Score: {winner['Efficiency']:.2f}).")
    print(f"   Paga menos volatilidad por cada punto de retorno.")

if __name__ == "__main__":
    main()