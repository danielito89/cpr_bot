import pandas as pd
import numpy as np
import os
from datetime import timedelta

# ======================================================
#  🏦 CONFIG V4.1 - ANTI-FRAGILE INSTITUTIONAL
# ======================================================
FILES = {
    "BNB":  {"path": "data/funding_BNBUSDT.csv",      "type": "CORE", "dd_limit": -0.15},
    "BTC":  {"path": "data/funding_BTCUSDT.csv",      "type": "CORE", "dd_limit": -0.15}, 
    "ETH":  {"path": "data/funding_ETHUSDT.csv",      "type": "CORE", "dd_limit": -0.15}  
}

INITIAL_CAPITAL = 10000
NEGATIVE_PENALTY = 1.5   
ENTRY_EXIT_COST = 0.002  # 0.2% fee entrada + 0.2% fee salida
COOLDOWN_DAYS = 7        # Tiempo de castigo tras Stop Loss

def analyze_asset_v4_1(symbol, config):
    filepath = config['path']
    max_dd_limit = config['dd_limit']
    asset_type = config['type']

    if not os.path.exists(filepath): return None

    df = pd.read_csv(filepath)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    df = df[df.index >= '2023-01-01']
    
    # --- LOGICA DE THRESHOLD DINÁMICO ---
    # Si es CORE (BTC/ETH), threshold es casi 0 (siempre dentro salvo catástrofe)
    # Si es SATELLITE (PEPE), threshold es el cuantil 70 rolling (solo oportunidades de oro)
    if asset_type == "SATELLITE":
        # Rolling de 30 días (3 periodos por día * 30 = 90)
        df['dynamic_thresh'] = df['fundingRate'].rolling(90).quantile(0.70)
        # Limpieza inicial
        df['dynamic_thresh'] = df['dynamic_thresh'].fillna(0.0002) 
    else:
        df['dynamic_thresh'] = -0.0001 # Hysteresis leve para no salir en 0 exacto

    balance = INITIAL_CAPITAL
    peak_balance = INITIAL_CAPITAL
    
    in_market = False
    cooldown_until = None
    trades_count = 0
    stops_triggered = 0
    
    equity_curve = []
    
    for ts, row in df.iterrows():
        rate = row['fundingRate']
        threshold = row['dynamic_thresh']
        
        # 1. Chequeo de Cooldown
        if cooldown_until:
            if ts < cooldown_until:
                equity_curve.append(balance)
                continue
            else:
                cooldown_until = None # Reset cooldown
                # Resetear Peak para que el DD no nos saque inmediatamente al volver
                peak_balance = balance 

        # 2. Lógica de Entrada/Salida
        should_be_in = rate > threshold
        
        # Transiciones
        if should_be_in and not in_market:
            balance *= (1 - ENTRY_EXIT_COST)
            in_market = True
            trades_count += 1
            # Al entrar, el peak es el balance actual (reset de DD psicológico)
            peak_balance = balance
            
        elif not should_be_in and in_market:
            balance *= (1 - ENTRY_EXIT_COST)
            in_market = False
        
        # 3. Cash Flow
        if in_market:
            actual_payout = rate * NEGATIVE_PENALTY if rate < 0 else rate
            balance += (balance * actual_payout)
            
            # 4. Drawdown Tracking (FIX V4.1)
            # Solo trackeamos nuevos maximos si estamos IN MARKET
            if balance > peak_balance:
                peak_balance = balance
            
            current_dd = (balance - peak_balance) / peak_balance
            
            # 5. Stop Loss Institucional
            if current_dd < max_dd_limit:
                balance *= (1 - ENTRY_EXIT_COST) # Venta forzosa
                in_market = False
                stops_triggered += 1
                cooldown_until = ts + timedelta(days=COOLDOWN_DAYS)
                # print(f"🚨 {symbol} STOPPED OUT ({current_dd*100:.2f}%) -> Cooling down until {cooldown_until}")

        equity_curve.append(balance)

    # Resultados
    final_balance = equity_curve[-1]
    total_ret = (final_balance - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    # Efficiency sobre Equity Curve (Sharpe simplificado del PnL)
    series = pd.Series(equity_curve)
    pct_changes = series.pct_change().dropna()
    sharpe = pct_changes.mean() / pct_changes.std() * np.sqrt(365*3) if pct_changes.std() != 0 else 0
    
    return {
        "Symbol": symbol,
        "Type": asset_type,
        "Net Return %": total_ret,
        "Trades": trades_count,
        "Stops Triggered": stops_triggered,
        "Efficiency (Sharpe)": sharpe
    }

def main():
    print(f"🛡️ ANALISIS V4.1: ANTI-FRAGILE SYSTEM")
    print(f"   Cooldown: {COOLDOWN_DAYS} días | Dynamic Thresholds para Satellites")
    print("="*85)
    
    results = []
    for sym, conf in FILES.items():
        res = analyze_asset_v4_1(sym, conf)
        if res: results.append(res)
        
    df_res = pd.DataFrame(results)
    df_res = df_res.sort_values("Net Return %", ascending=False)
    
    pd.options.display.float_format = '{:.2f}'.format
    print(df_res.to_string(index=False))

if __name__ == "__main__":
    main()