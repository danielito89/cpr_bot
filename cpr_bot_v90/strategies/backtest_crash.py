#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os

# ======================================================
#  🌪️ CONFIG V1 – CRASH BOT (BLACK SWAN HUNTER)
# ======================================================

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "1000PEPEUSDT"]
TIMEFRAME_STR = "1h"

# ---- TRIGGER: PÁNICO PURO ----
# Caída acumulada en la ventana de tiempo
CRASH_WINDOW_HOURS = 24   
CRASH_DROP_THRESHOLD = 0.10  # 10% de caída (Pánico Real)

# ---- GESTIÓN DE SALIDA ----
# Usamos un Trailing Stop dinámico
TRAILING_ACTIVATION = 0.05   # Activar trailing después de 5% de ganancia
TRAILING_DIST = 0.02         # Seguir el precio a un 2% de distancia

# Stop Loss Fijo de Emergencia (Si entra y rebota fuerte)
FIXED_SL_PCT = 0.03          # 3% desde la entrada

# ---- MICRO RIESGO ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.005       # 0.5% Riesgo por trade (Apuesta de lotería)
LEVERAGE = 1                 # Sin apalancamiento agresivo (el movimiento ya es fuerte)

# Costos
COMMISSION = 0.0004
SLIPPAGE_PCT = 0.001         # Slippage alto (1%) porque entramos en volatilidad extrema

# ======================================================
#  MOTOR DE CARGA Y SEÑAL
# ======================================================
def process_symbol(symbol):
    candidates = [
        f"data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"data/{symbol}_{TIMEFRAME_STR}.csv",
        f"cpr_bot_v90/data/mainnet_data_{TIMEFRAME_STR}_{symbol}.csv"
    ]
    df = None
    for path in candidates:
        if os.path.exists(path):
            df = pd.read_csv(path)
            break
    if df is None: return None

    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- SEÑAL DE CRASH (Rolling Window) ---
    # Precio hace 24h
    df['price_24h_ago'] = df['close'].shift(CRASH_WINDOW_HOURS)
    
    # Calcular Drop %
    df['drop_pct'] = (df['close'] - df['price_24h_ago']) / df['price_24h_ago']
    
    # Trigger: Drop < -10%
    df['signal_crash'] = np.where(df['drop_pct'] < -CRASH_DROP_THRESHOLD, 1, 0)
    
    # Shift 1H (Entramos al cierre de la vela que confirma el pánico)
    df['signal_crash'] = df['signal_crash'].shift(1)
    
    return df.dropna()

# ======================================================
#  MOTOR DE BACKTEST (EVENT DRIVEN)
# ======================================================
def run_backtest():
    print(f"\n🌪️ INICIANDO BACKTEST: CRASH BOT V1")
    print(f"   Trigger: Caída > {CRASH_DROP_THRESHOLD*100}% en {CRASH_WINDOW_HOURS}h")
    print(f"   Risk: {FIXED_RISK_PCT*100}% | Slippage: {SLIPPAGE_PCT*100}%")
    print("="*60)

    # Unificar línea de tiempo
    all_data = []
    for s in SYMBOLS:
        d = process_symbol(s)
        if d is not None: 
            d['symbol'] = s
            all_data.append(d)
    
    if not all_data: return

    master_df = pd.concat(all_data).sort_index()
    timeline = master_df.groupby(level=0)

    balance = INITIAL_BALANCE
    positions = [] # [{'symbol', 'entry', 'qty', 'sl', 'ts_act', 'ts_price'}]
    trades_log = []
    
    # Control de Cool-off (No entrar mil veces en el mismo crash)
    last_crash_entry = {s: pd.Timestamp.min for s in SYMBOLS}
    CRASH_COOLDOWN = pd.Timedelta(hours=48) # Esperar 2 días tras un trade para no sobre-operar el mismo evento

    for ts, group in timeline:
        
        # 1. GESTIÓN DE POSICIONES
        active_pos = []
        for pos in positions:
            sym = pos['symbol']
            if sym not in group['symbol'].values:
                active_pos.append(pos)
                continue
            
            row = group[group['symbol'] == sym].iloc[0]
            h, l, c = row['high'], row['low'], row['close']
            
            exit_price = None
            reason = None
            
            # A) Stop Loss Fijo (El rebote nos mató)
            if h >= pos['sl']:
                exit_price = pos['sl'] * (1 + SLIPPAGE_PCT) # Peor precio
                reason = "SL Fijo"
            
            # B) Trailing Stop
            else:
                # Calcular ganancia actual (Short)
                pnl_pct = (pos['entry'] - l) / pos['entry']
                
                # Activar Trailing si pasamos el umbral
                if pnl_pct >= TRAILING_ACTIVATION:
                    pos['trailing_active'] = True
                
                if pos['trailing_active']:
                    # El stop baja persiguiendo al precio
                    # Nuevo SL = Low actual + Distancia
                    potential_stop = l * (1 + TRAILING_DIST)
                    
                    # En short, el SL solo baja (se hace más pequeño el precio)
                    if pos['trailing_stop'] is None or potential_stop < pos['trailing_stop']:
                        pos['trailing_stop'] = potential_stop
                    
                    # Chequear si nos sacó (High tocó el trailing)
                    if h >= pos['trailing_stop']:
                        exit_price = pos['trailing_stop']
                        reason = "Trailing TP"
            
            if exit_price:
                pnl = (pos['entry'] - exit_price) * pos['qty']
                cost = (pos['entry'] + exit_price) * pos['qty'] * COMMISSION
                net = pnl - cost
                balance += (net + (pos['entry'] * pos['qty'] * COMMISSION)) # Devolver margen
                trades_log.append({'ts': ts, 'pnl': net, 'symbol': sym, 'type': reason})
            else:
                active_pos.append(pos)
        
        positions = active_pos

        # 2. ENTRADAS (Solo si no hay posición en ese símbolo)
        for idx, row in group.iterrows():
            sym = row['symbol']
            
            # Si ya estoy short en este símbolo, ignorar
            if any(p['symbol'] == sym for p in positions): continue
            
            # Cooldown check
            if ts < last_crash_entry[sym] + CRASH_COOLDOWN: continue

            if row['signal_crash'] == 1:
                # Entramos Short
                price = row['open'] * (1 - SLIPPAGE_PCT) # Venta a mercado
                
                # Stop Loss Fijo
                sl = price * (1 + FIXED_SL_PCT)
                dist = sl - price
                
                if dist <= 0: continue

                # Sizing (Micro Risk)
                risk_amt = balance * FIXED_RISK_PCT
                qty = risk_amt / dist
                
                # Cap tamaño (no apostar la casa)
                notional = qty * price
                if notional > balance * 0.5: qty = (balance * 0.5) / price
                
                cost = notional * COMMISSION
                balance -= cost
                
                positions.append({
                    'symbol': sym, 'entry': price, 'qty': qty, 
                    'sl': sl, 'trailing_active': False, 'trailing_stop': None
                })
                last_crash_entry[sym] = ts
                # print(f"📉 [{ts}] CRASH ENTRY {sym} @ {price:.2f}")

    # --- REPORTE ---
    print("\n" + "="*60)
    print(f"💰 Balance Final:   ${balance:.2f}")
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    print(f"🚀 Retorno Total:   {total_ret:.2f}%")
    print(f"🧮 Trades Totales:  {len(trades_log)}")
    
    if trades_log:
        df_t = pd.DataFrame(trades_log)
        df_t['year'] = df_t['ts'].dt.year
        print("\n📅 RENDIMIENTO ANUAL (CRASH ONLY):")
        print(df_t.groupby('year')['pnl'].agg(['sum', 'count']))
        
        print("\n🏆 Top 3 Wins:")
        print(df_t.sort_values('pnl', ascending=False).head(3)[['ts','symbol','pnl']])

if __name__ == "__main__":
    run_backtest()