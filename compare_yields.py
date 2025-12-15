import pandas as pd
import numpy as np
import os

# ======================================================
#  🏦 CONFIG V4 - INSTITUTIONAL RISK OFFICER
# ======================================================
FILES = {
    "BTC":  {"path": "data/funding_BTCUSDT.csv",      "threshold": 0.0000, "dd_limit": -0.10}, # Siempre activo (Threshold 0), Stop laxo
    "ETH":  {"path": "data/funding_ETHUSDT.csv",      "threshold": 0.0000, "dd_limit": -0.10}, # Siempre activo
    "PEPE": {"path": "data/funding_1000PEPEUSDT.csv", "threshold": 0.0005, "dd_limit": -0.05}, # Solo si paga > 0.05%/8h, Stop estricto
    "SOL":  {"path": "data/funding_SOLUSDT.csv",      "threshold": 0.0002, "dd_limit": -0.05}  # Filtro medio
}

INITIAL_CAPITAL = 10000
NEGATIVE_PENALTY = 1.5   # Si pagamos funding, duele x1.5
ENTRY_EXIT_COST = 0.002  # 0.2% cada vez que entramos/salimos (Spot+Fut)

def analyze_asset_v4(symbol, config):
    filepath = config['path']
    activate_threshold = config['threshold']
    max_dd_limit = config['dd_limit']

    if not os.path.exists(filepath):
        return None

    df = pd.read_csv(filepath)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    df = df[df.index >= '2023-01-01']
    
    balance = INITIAL_CAPITAL
    equity_curve = []
    in_market = False
    stop_loss_triggered = False
    
    trades_count = 0
    
    # Simulación Vela a Vela (State Machine)
    for ts, row in df.iterrows():
        rate = row['fundingRate']
        
        # 1. Chequeo de Risk Officer (Hard Stop previo)
        if stop_loss_triggered:
            equity_curve.append(balance) # Nos quedamos en cash forever
            continue

        # 2. Lógica de Activación (Sniper)
        should_be_in = rate > activate_threshold
        
        # Transiciones
        if should_be_in and not in_market:
            # ENTRAR
            balance *= (1 - ENTRY_EXIT_COST)
            in_market = True
            trades_count += 1
        elif not should_be_in and in_market:
            # SALIR
            balance *= (1 - ENTRY_EXIT_COST)
            in_market = False
        
        # 3. Pago / Cobro
        if in_market:
            # Aplicar Penalty si es negativo
            actual_payout_pct = rate * NEGATIVE_PENALTY if rate < 0 else rate
            payout = balance * actual_payout_pct
            balance += payout
            
        equity_curve.append(balance)
        
        # 4. Chequeo de Drawdown Dinámico
        # Calculamos DD sobre la marcha
        current_peak = max(equity_curve)
        current_dd = (balance - current_peak) / current_peak
        
        if in_market and current_dd < max_dd_limit:
            # STOP LOSS DE EMERGENCIA
            balance *= (1 - ENTRY_EXIT_COST) # Salida forzosa
            in_market = False
            stop_loss_triggered = True
            # print(f"🚨 {symbol} STOPPED OUT at {ts} (DD: {current_dd*100:.2f}%)")

    # Resultados Finales
    final_balance = equity_curve[-1]
    total_ret = (final_balance - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    # Calcular métricas solo de los periodos activos
    active_rates = df[df['fundingRate'] > activate_threshold]['fundingRate']
    avg_apr = active_rates.mean() * 3 * 365 * 100 if not active_rates.empty else 0
    
    return {
        "Symbol": symbol,
        "Net Return %": total_ret,
        "Stopped Out?": "YES 💀" if stop_loss_triggered else "NO ✅",
        "Trades": trades_count,
        "Active Days": len(active_rates) / 3, # Aprox (8h periods)
        "Avg Active APR": avg_apr
    }

def main():
    print(f"🛡️ ANALISIS V4: INSTITUTIONAL RISK OFFICER")
    print(f"   Config: Filtros Dinámicos + Hard Stop por DD + Costos de Rotación")
    print("="*85)
    
    results = []
    for sym, conf in FILES.items():
        res = analyze_asset_v4(sym, conf)
        if res: results.append(res)
        
    df_res = pd.DataFrame(results)
    df_res = df_res.sort_values("Net Return %", ascending=False)
    
    pd.options.display.float_format = '{:.2f}'.format
    print(df_res.to_string(index=False))
    
    print("\n💡 LECCIÓN:")
    print("   Si PEPE tiene muchos 'Trades' (entra/sale), los fees se comen la ganancia.")
    print("   El objetivo es encontrar el equilibrio donde el filtro elimina el riesgo pero no genera overtrading.")

if __name__ == "__main__":
    main()