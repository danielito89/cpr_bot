#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib
from datetime import timedelta

# ======================================================
#  🐻 CONFIG V72 – DEAD CAT SNIPER (PORTFOLIO LEVEL)
# ======================================================

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "1000PEPEUSDT"]
TIMEFRAME_STR = "1h"

# ---- CAPA 1: RÉGIMEN BASE (BEAR MARKET) ----
EMA_DAILY_FAST = 50
EMA_DAILY_SLOW = 200

# ---- CAPA 2: TRIGGER NORMAL (DEAD CAT BOUNCE) ----
# 1. Impulso: Caída > 5% en 12h (3 velas 4H)
IMPULSE_DROP_PCT = 0.05  
IMPULSE_BARS = 3         
# 2. Rebote: Subida intra-vela > 3% vs Cierre anterior
BOUNCE_PCT = 0.03        

# ---- CAPA 3: TRIGGER PÁNICO (CRASH MODE) ----
# Caída > 8% en 24h (6 velas 4H) -> Ignora Régimen Diario
CRASH_DROP_PCT = 0.08    
CRASH_BARS = 6           

# ---- CAPA 4: GESTIÓN DE RIESGO (PORTFOLIO) ----
MAX_ACTIVE_SHORTS = 1           # Solo 1 bala a la vez (Evita correlación)
KILL_SWITCH_LOOKBACK = 20       # Mirar últimos 20 trades
KILL_SWITCH_PAUSE_DAYS = 14     # Si PnL < 0, vacaciones 2 semanas

# ---- CAPA 5: TRADE MANAGEMENT ----
RISK_PER_TRADE = 0.015          # 1.5% Riesgo
RISK_REWARD = 1.5               # TP = 1.5R
SL_BUFFER_ATR = 0.5             # SL = High + 0.5 ATR

# Costos
INITIAL_BALANCE = 10000
COMMISSION = 0.0004
SPREAD = 0.0004
SLIPPAGE = 0.0006

# ======================================================
#  1. PREPARACIÓN DE DATOS (Por Símbolo)
# ======================================================
def prepare_symbol_data(symbol):
    # Carga robusta
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

    # Formato
    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- DATOS DIARIOS (RÉGIMEN) ---
    ohlc_1d = {'open':'first', 'high':'max', 'low':'min', 'close':'last'}
    df_1d = df.resample('1D').apply(ohlc_1d).dropna()
    df_1d['ema50'] = talib.EMA(df_1d['close'], 50)
    df_1d['ema200'] = talib.EMA(df_1d['close'], 200)
    df_1d['bear_regime'] = np.where(df_1d['ema50'] < df_1d['ema200'], 1, 0)
    
    # --- DATOS 4H (TACTICAL) ---
    ohlc_4h = {'open':'first', 'high':'max', 'low':'min', 'close':'last'}
    df_4h = df.resample('4h').apply(ohlc_4h).dropna()
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], 14)
    
    # 1. Señal "Dead Cat Bounce" (Normal)
    # Impulso Bajista: Close actual < Close hace 3 velas * (1 - 5%)
    impulse = df_4h['close'] < (df_4h['close'].shift(IMPULSE_BARS) * (1 - IMPULSE_DROP_PCT))
    # Rebote: High actual > Close previo * (1 + 3%)
    bounce = df_4h['high'] > (df_4h['close'].shift(1) * (1 + BOUNCE_PCT))
    # Rechazo: Cierre < Apertura (Vela Roja)
    rejection = df_4h['close'] < df_4h['open']
    
    df_4h['sig_dead_cat'] = np.where(impulse & bounce & rejection, 1, 0)
    
    # 2. Señal "Crash Mode" (Pánico)
    # Caída > 8% en 24h
    crash = df_4h['close'] < (df_4h['close'].shift(CRASH_BARS) * (1 - CRASH_DROP_PCT))
    df_4h['sig_crash'] = np.where(crash, 1, 0)

    # --- MERGE ---
    # Traemos el régimen diario (Shift 1 para no ver futuro)
    df_merged = df_4h.join(df_1d.shift(1)[['bear_regime']], rsuffix='_d')
    df_merged['symbol'] = symbol
    
    return df_merged.dropna()

# ======================================================
#  2. MOTOR DE BACKTEST (EVENT DRIVEN)
# ======================================================
def run_portfolio_backtest():
    print(f"\n🧪 INICIANDO BACKTEST V72 (PORTFOLIO SINCRONIZADO)")
    print(f"   Max Shorts: {MAX_ACTIVE_SHORTS} | Kill Switch: {KILL_SWITCH_PAUSE_DAYS} dias")
    print("="*60)

    # 1. Cargar y unificar todo en una línea de tiempo
    all_data = []
    for s in SYMBOLS:
        d = prepare_symbol_data(s)
        if d is not None: all_data.append(d)
    
    if not all_data: return

    # Crear un DataFrame gigante ordenado por tiempo
    # Índice: Timestamp, Columnas: Datos + Symbol
    master_df = pd.concat(all_data).sort_index()
    
    # Agrupar por timestamp para simular "la hora actual"
    timeline = master_df.groupby(level=0)

    # Estado del Portafolio
    balance = INITIAL_BALANCE
    peak_balance = balance
    active_positions = [] # Lista de dicts: {'symbol', 'entry', 'qty', 'sl', 'tp'}
    closed_trades = []    # Lista de dicts: {'pnl', 'close_time', ...}
    
    kill_switch_until = None
    equity_curve = []

    friction = COMMISSION + SPREAD + SLIPPAGE

    for ts, group in timeline:
        # group es un DataFrame con las filas de todos los símbolos en este timestamp (4H)
        
        # --- A. GESTIÓN DE POSICIONES ABIERTAS ---
        # (Verificamos si tocan SL o TP con los datos de ESTA vela)
        # Nota: En backtest 4H, asumimos que SL/TP ocurren dentro de la vela.
        # Si ambos se tocan, asumimos SL (pesimista).
        
        still_open = []
        for pos in active_positions:
            sym = pos['symbol']
            # Buscar datos del símbolo en este timestamp
            if sym not in group['symbol'].values:
                still_open.append(pos)
                continue
            
            row = group[group['symbol'] == sym].iloc[0]
            h, l, c = row['high'], row['low'], row['close']
            
            # Check SL (Short: High >= SL)
            if h >= pos['sl']:
                exit_p = pos['sl'] * (1 + SLIPPAGE)
                pnl = (pos['entry'] - exit_p) * pos['qty']
                fee = exit_p * pos['qty'] * COMMISSION
                net = pnl - fee
                
                balance += (net + (pos['entry'] * pos['qty'] * COMMISSION)) # Devolver margen ajustado
                closed_trades.append({'ts': ts, 'pnl': net, 'type': 'SL'})
                # No agregamos a still_open (cerrada)
            
            # Check TP (Short: Low <= TP)
            elif l <= pos['tp']:
                exit_p = pos['tp'] * (1 - SLIPPAGE)
                pnl = (pos['entry'] - exit_p) * pos['qty']
                fee = exit_p * pos['qty'] * COMMISSION
                net = pnl - fee
                
                balance += (net + (pos['entry'] * pos['qty'] * COMMISSION))
                closed_trades.append({'ts': ts, 'pnl': net, 'type': 'TP'})
            
            else:
                # Mark to Market para equity curve
                mtm_pnl = (pos['entry'] - c) * pos['qty']
                still_open.append(pos)
        
        active_positions = still_open
        
        # Actualizar Peak y Equity
        # Equity = Balance Realizado + PnL Latente
        unrealized = 0
        for pos in active_positions:
            sym = pos['symbol']
            if sym in group['symbol'].values:
                row = group[group['symbol'] == sym].iloc[0]
                unrealized += (pos['entry'] - row['close']) * pos['qty']
        
        current_equity = balance + unrealized
        if current_equity > peak_balance: peak_balance = current_equity
        equity_curve.append({'ts': ts, 'equity': current_equity})

        # --- B. KILL SWITCH LOGIC ---
        # Revisar los últimos 20 trades CERRADOS
        if kill_switch_until and ts < kill_switch_until:
            continue # Vacaciones forzadas
        
        if len(closed_trades) >= KILL_SWITCH_LOOKBACK:
            recent_pnl = sum([t['pnl'] for t in closed_trades[-KILL_SWITCH_LOOKBACK:]])
            if recent_pnl < 0:
                kill_switch_until = ts + timedelta(days=KILL_SWITCH_PAUSE_DAYS)
                # print(f"🚫 [{ts}] KILL SWITCH ACTIVADO (PnL Reciente: {recent_pnl:.2f})")
                continue

        # --- C. BÚSQUEDA DE NUEVAS ENTRADAS ---
        # Solo si tenemos espacio en el portafolio
        if len(active_positions) >= MAX_ACTIVE_SHORTS:
            continue

        # Iterar posibles candidatos en este timestamp
        for idx, row in group.iterrows():
            sym = row['symbol']
            
            # Filtro básico: No operar si ya tengo posición en este símbolo
            if any(p['symbol'] == sym for p in active_positions): continue
            
            # Lógica de Señales
            is_dead_cat = (row['bear_regime'] == 1) and (row['sig_dead_cat'] == 1)
            is_crash = (row['sig_crash'] == 1) # Ignora bear_regime
            
            if is_dead_cat or is_crash:
                # Entrada (Cierre de vela 4H -> Ejecución Open siguiente teórica, 
                # aquí usamos Close actual con slippage como aproximación inmediata o Open next)
                # Usaremos Close de esta vela como trigger, entrada teórica al precio de cierre 
                # (Slippage penaliza para simular market order)
                
                entry_price = row['close'] * (1 - friction)
                atr = row['atr']
                
                # SL: High de la vela + Buffer
                sl_price = row['high'] + (atr * SL_BUFFER_ATR)
                risk_dist = sl_price - entry_price
                
                if risk_dist <= 0: continue # Dato sucio
                
                tp_dist = risk_dist * RISK_REWARD
                tp_price = entry_price - tp_dist
                
                # Sizing
                risk_amt = balance * RISK_PER_TRADE
                qty = risk_amt / risk_dist
                
                # Max Lever check
                notional = qty * entry_price
                if notional > balance * 5: qty = (balance * 5) / entry_price
                
                # Ejecutar
                cost = notional * COMMISSION
                balance -= cost
                
                active_positions.append({
                    'symbol': sym, 'entry': entry_price, 'qty': qty, 
                    'sl': sl_price, 'tp': tp_price
                })
                
                # Solo 1 entrada por turno (Prioridad al primero que aparezca en la lista)
                # Si quieres priorizar por "Crash" vs "Dead Cat", ordena el grupo antes.
                if len(active_positions) >= MAX_ACTIVE_SHORTS: break

    # --- REPORTE FINAL ---
    print("\n" + "="*60)
    print(f"💰 Balance Final:   ${balance:.2f}")
    
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    print(f"🚀 Retorno Total:   {total_ret:.2f}%")
    
    df_eq = pd.DataFrame(equity_curve)
    if not df_eq.empty:
        peak = df_eq['equity'].cummax()
        dd = (df_eq['equity'] - peak) / peak
        print(f"📉 Max Drawdown:    {dd.min()*100:.2f}%")
    
    print(f"🧮 Trades Cerrados: {len(closed_trades)}")
    
    if closed_trades:
        df_t = pd.DataFrame(closed_trades)
        df_t['year'] = df_t['ts'].dt.year
        print("\n📅 RENDIMIENTO ANUAL (SHORT ONLY):")
        print(df_t.groupby('year')['pnl'].agg(['sum', 'count']))

if __name__ == "__main__":
    run_portfolio_backtest()