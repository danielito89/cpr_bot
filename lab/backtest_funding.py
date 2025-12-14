import pandas as pd
import matplotlib.pyplot as plt

# CONFIG
FUNDING_FILE = "../data/funding_ETHUSDT.csv"
INITIAL_CAPITAL = 10000 
LEVERAGE = 1 # Delta Neutral estricto (1x Short)
COMPOUND = True # ¿Reinvertimos las ganancias?

def run_funding_backtest():
    try:
        df = pd.read_csv(FUNDING_FILE)
    except:
        print("❌ Primero corre fetch_funding.py")
        return

    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    
    print(f"🏦 BACKTEST CASH & CARRY (ETH Funding)")
    print(f"   Desde: {df.index[0]} | Hasta: {df.index[-1]}")
    print("="*60)
    
    # Simulación
    balance = INITIAL_CAPITAL
    equity_curve = []
    
    # Costo de entrada/salida (Spot + Futuros)
    # Taker fee x 2 (Spot Buy + Future Sell) + Taker fee x 2 (Close)
    # Aprox 0.1% total roundtrip si usas limit orders, 0.2% si eres taker.
    # Pongamos un "Penalty" inicial de entrada
    ENTRY_COST_PCT = 0.002 
    balance = balance * (1 - ENTRY_COST_PCT)
    
    yearly_stats = {}

    for ts, row in df.iterrows():
        rate = row['fundingRate']
        
        # El pago es: Posición * Tasa
        # Posición = Balance * Leverage
        payout = (balance * LEVERAGE) * rate
        
        if COMPOUND:
            balance += payout
        else:
            # Si no componemos, el balance base se mantiene, acumulamos en "payouts"
            # (Simplificado: asumimos compuesto para ver potencial de crecimiento)
            balance += payout
            
        equity_curve.append({'ts': ts, 'equity': balance, 'rate': rate})
    
    # Resultados
    df_eq = pd.DataFrame(equity_curve)
    
    total_ret = (balance - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    apr_avg = df['fundingRate'].mean() * 3 * 365 * 100 # 3 pagos al dia
    
    print(f"💰 Balance Final:   ${balance:.2f}")
    print(f"🚀 Retorno Total:   {total_ret:.2f}%")
    print(f"📊 APR Promedio:    {apr_avg:.2f}% (Anualizado)")
    
    # Análisis Anual
    df_eq['year'] = df_eq['ts'].dt.year
    print("\n📅 RENDIMIENTO POR AÑO (%):")
    # Calcular retorno porcentual por año
    years = df_eq['year'].unique()
    for y in years:
        d = df_eq[df_eq['year'] == y]
        start = d.iloc[0]['equity']
        end = d.iloc[-1]['equity']
        ret = (end - start) / start * 100
        print(f"   {y}: {ret:.2f}%")

if __name__ == "__main__":
    run_funding_backtest()